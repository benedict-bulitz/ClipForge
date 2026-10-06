"""TikTok error diagnostics: TikTok's own reason and log_id survive, secrets never do."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from publishing_support import FakeTikTok, apis, connect_tiktok, publishing_settings, social_project
from sqlalchemy import text

from clipforge.publishing import connections, publications, tiktok
from clipforge.publishing.errors import PublishingApiError, safe_log_id, safe_provider_text
from clipforge.publishing.publications import PublicationRequest
from clipforge.security.secrets import SecretStore
from clipforge.youtube import uploads

LOG_ID = "202610061234567890ABCDEF0123456789"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=7300000&upload_token=Xy9-secret-upload-token"


def response(status: int, payload: dict, url: str = tiktok.VIDEO_INIT_URL) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


def init_error(code: str, message: str | None, *, log_id: str | None = LOG_ID, status: int = 400) -> PublishingApiError:
    error: dict = {"code": code}
    if message is not None:
        error["message"] = message
    if log_id is not None:
        error["log_id"] = log_id
    return tiktok._error(response(status, {"error": error}), context="post initialization")


@pytest.fixture(autouse=True)
def _reset():
    connections.reset_cache()
    uploads._SHA_CACHE.clear()
    yield
    connections.reset_cache()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_invalid_param_keeps_tiktoks_field_level_reason_and_log_id():
    error = init_error("invalid_param", "The value of post_info.video_cover_timestamp_ms is out of range.")
    assert error.code == "invalid_request" and error.retryable is False
    assert error.message == "TikTok rejected the post settings: The value of post_info.video_cover_timestamp_ms is out of range."
    assert error.provider_code == "invalid_param"
    assert error.provider_message == "The value of post_info.video_cover_timestamp_ms is out of range."
    assert error.log_id == LOG_ID
    assert error.status_code == 400 and error.context == "post initialization"
    assert error.diagnostics() == {
        "code": "invalid_request",
        "provider_code": "invalid_param",
        "provider_message": "The value of post_info.video_cover_timestamp_ms is out of range.",
        "log_id": LOG_ID,
        "http_status": 400,
        "context": "post initialization",
        "retryable": False,
    }


def test_invalid_params_spelling_is_still_understood():
    error = init_error("invalid_params", "post_info.title is too long")
    assert (error.code, error.provider_code, error.log_id) == ("invalid_request", "invalid_params", LOG_ID)
    assert error.message == "TikTok rejected the post settings: post_info.title is too long"


def test_invalid_param_without_a_message_falls_back_to_the_generic_text():
    error = init_error("invalid_param", None)
    assert error.message == "TikTok rejected the post settings." and error.provider_message is None
    assert error.log_id == LOG_ID


def test_top_level_log_id_is_kept_too():
    oauth = tiktok._error(response(400, {"error": "invalid_grant", "error_description": "Refresh token is expired.", "log_id": "OAUTH_LOG_1"}, tiktok.TOKEN_URL), context="token refresh")
    assert (oauth.code, oauth.provider_code, oauth.log_id) == ("auth_expired", "invalid_grant", "OAUTH_LOG_1")
    assert oauth.provider_message == "Refresh token is expired."


@pytest.mark.parametrize(("provider_code", "code", "retryable"), [
    ("access_token_invalid", "auth_expired", False),
    ("scope_not_authorized", "insufficient_scope", False),
    ("rate_limit_exceeded", "rate_limited", True),
    ("spam_risk_too_many_posts", "provider_limit", False),
    ("spam_risk_user_banned_from_posting", "provider_policy", False),
    ("reached_active_user_cap", "provider_limit", False),
    ("unaudited_client_can_only_post_to_private_accounts", "unaudited_client", False),
    ("privacy_level_option_mismatch", "invalid_option", False),
    ("invalid_file_upload", "invalid_media", False),
    ("internal_error", "provider_error", True),
])
def test_known_tiktok_codes_keep_their_mapping_and_now_carry_diagnostics(provider_code, code, retryable):
    error = init_error(provider_code, f"details for {provider_code}")
    assert (error.code, error.retryable, error.provider_code) == (code, retryable, provider_code)
    assert error.provider_message == f"details for {provider_code}" and error.log_id == LOG_ID
    # The curated ClipForge text stays the user-facing message for these codes.
    assert f"details for {provider_code}" not in error.message


def test_unknown_codes_and_http_failures_keep_the_provider_reason():
    unknown = init_error("brand_new_code", "Something specific went wrong.")
    assert unknown.code == "provider_rejected" and unknown.message == "TikTok refused the post initialization request: Something specific went wrong."
    server = init_error("", "Upstream failure", status=503)
    assert server.code == "provider_error" and server.retryable and server.provider_message == "Upstream failure" and server.log_id == LOG_ID
    not_json = tiktok._error(httpx.Response(502, text="<html>gateway</html>", request=httpx.Request("POST", tiktok.VIDEO_INIT_URL)), context="post initialization")
    assert not_json.code == "provider_error" and not_json.provider_message is None and "<html>" not in not_json.message


def test_secrets_are_removed_from_provider_messages():
    nasty = (
        f"Invalid upload {UPLOAD_URL} with Authorization: Bearer act.ABCDEFGHijklmnop1234567890 "
        "refresh_token=rft.QRSTUVWXyz0987654321abcd client_secret=supersecret code=one-time-code "
        "token act.SECONDtokenABCDEFGH12345"
    )
    error = init_error("invalid_param", nasty, log_id="bad log id; drop table")
    dump = json.dumps(error.diagnostics()) + error.message + repr(error)
    for secret in ("Xy9-secret-upload-token", "open-upload.tiktokapis.com", "act.ABCDEFGHijklmnop", "rft.QRSTUVWX", "supersecret", "one-time-code", "act.SECONDtoken"):
        assert secret not in dump, secret
    assert error.provider_message.startswith("Invalid upload [url] with Authorization: [redacted]")
    assert error.log_id is None  # not id-shaped -> not kept
    assert safe_provider_text("x" * 1000) == "x" * 300
    assert safe_provider_text("   ") is None and safe_provider_text(None) is None
    assert safe_log_id(1234567) == "1234567" and safe_log_id("abc def") is None


def test_raw_response_bodies_are_never_attached():
    body = {"error": {"code": "invalid_param", "message": "bad title", "log_id": LOG_ID}, "data": {"publish_id": "secret-ish", "upload_url": UPLOAD_URL}}
    error = tiktok._error(response(400, body), context="post initialization")
    assert UPLOAD_URL not in json.dumps(error.diagnostics()) and "publish_id" not in json.dumps(error.diagnostics())


def test_real_client_surfaces_init_rejections_with_diagnostics():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "invalid_param", "message": "post_info.privacy_level is required", "log_id": LOG_ID}})

    api = tiktok.HttpTikTokApi(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PublishingApiError) as raised:
        api.init_direct_post("act.secret-access-token", {"title": "t"}, {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10, "total_chunk_count": 1})
    assert raised.value.message == "TikTok rejected the post settings: post_info.privacy_level is required"
    assert raised.value.log_id == LOG_ID and raised.value.context == "post initialization"
    assert "act.secret-access-token" not in json.dumps(raised.value.diagnostics())


# ---------------------------------------------------------------------------
# Persistence: a failed attempt keeps what is needed to troubleshoot it
# ---------------------------------------------------------------------------


class RejectingTikTok(FakeTikTok):
    """TikTok answers the Direct Post init with a documented invalid_param error."""

    def init_direct_post(self, access_token, post_info, source_info):
        self.calls.append(("init", {"token": access_token, "post_info": post_info, "source_info": source_info}))
        raise tiktok._error(response(400, {"error": {
            "code": "invalid_param",
            "message": f"The video_cover_timestamp_ms is invalid (token {access_token}, upload {UPLOAD_URL})",
            "log_id": LOG_ID,
        }}), context="post initialization")


def test_failed_direct_post_retains_code_provider_code_message_and_log_id(db, tmp_path, test_keyring, caplog):
    settings, store, fake = publishing_settings(tmp_path), SecretStore(), RejectingTikTok()
    fake.add_user("open-a", "alpha")
    account = connect_tiktok(db, settings, store, fake, "open-a")
    connections.reset_cache()
    project = social_project(db, settings)
    request = PublicationRequest(
        account_id=account.id, base_revision=project.current_revision,
        tiktok={"caption": "Round windows.", "hashtags": ["#aviation"], "privacy_level": "SELF_ONLY", "is_aigc": False, "music_usage_confirmed": True},
    )
    row, _run_now = publications.request_publication(db, project, settings, store, apis(fake), request)
    with caplog.at_level(logging.WARNING, logger="clipforge.publishing.publications"):
        publications.run(db, row.id, settings, store, apis(fake))
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code == "invalid_request" and row.attempt_count == 1
    assert row.last_error_message.startswith("TikTok rejected the post settings: The video_cover_timestamp_ms is invalid")
    serialized = publications.serialize(row)
    diagnostics = serialized["error"]["diagnostics"]
    assert diagnostics["provider_code"] == "invalid_param"
    assert diagnostics["log_id"] == LOG_ID
    assert diagnostics["http_status"] == 400 and diagnostics["context"] == "post initialization"
    assert diagnostics["provider_message"].startswith("The video_cover_timestamp_ms is invalid")
    assert row.events[-1]["state"] == "failed" and row.events[-1]["error"]["log_id"] == LOG_ID
    # The request payload is unchanged by this diagnostics work.
    init = next(detail for name, detail in fake.calls if name == "init")
    assert set(init["post_info"]) == {"title", "privacy_level", "disable_comment", "disable_duet", "disable_stitch", "brand_content_toggle", "brand_organic_toggle", "is_aigc"}
    # Nothing secret reaches the database, the API shape or the log.
    secrets = [*test_keyring.secrets.values(), "at-open-a", "rt-open-a", "upload_token", "open-upload.tiktokapis.com", "tiktok-client-secret-never-logged"]
    stored = "\n".join(str(tuple(item)) for item in db.execute(text("SELECT * FROM social_publications")).all())
    logged = caplog.text
    for secret in secrets:
        assert secret not in stored, secret
        assert secret not in json.dumps(serialized, default=str), secret
        assert secret not in logged, secret
    assert LOG_ID in logged and "provider_code=invalid_param" in logged


def test_transient_retries_record_their_diagnostics_too(db, tmp_path):
    settings, store, fake = publishing_settings(tmp_path), SecretStore(), FakeTikTok()
    fake.add_user("open-a", "alpha")
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    fake.init_errors = [init_error("internal_error", "Please retry later.", status=500)]
    request = PublicationRequest(
        account_id=account.id, base_revision=project.current_revision,
        tiktok={"caption": "x", "privacy_level": "SELF_ONLY", "is_aigc": False, "music_usage_confirmed": True},
    )
    row, _run_now = publications.request_publication(db, project, settings, store, apis(fake), request)
    publications.run(db, row.id, settings, store, apis(fake))
    db.refresh(row)
    assert row.state == "pending" and row.last_error_code == "provider_error"
    assert publications.serialize(row)["error"]["diagnostics"]["log_id"] == LOG_ID
    assert row.events[-1]["error"]["provider_code"] == "internal_error"
