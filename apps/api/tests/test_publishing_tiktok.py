"""TikTok Direct Post through ClipForge - every TikTok call goes to an in-memory fake."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from publishing_support import FakeTikTok, apis, connect_tiktok, publishing_settings, social_project

from clipforge.models import SocialPublication
from clipforge.publishing import accounts, connections, publications, tiktok
from clipforge.publishing.errors import PublishingApiError
from clipforge.publishing.publications import PublicationRefused, PublicationRequest
from clipforge.security.secrets import SecretStore
from clipforge.youtube import uploads


@pytest.fixture(autouse=True)
def _reset():
    connections.reset_cache()
    uploads._SHA_CACHE.clear()
    yield
    connections.reset_cache()


@pytest.fixture()
def settings(tmp_path):
    return publishing_settings(tmp_path)


@pytest.fixture()
def store():
    return SecretStore()


@pytest.fixture()
def fake():
    fake = FakeTikTok()
    fake.add_user("open-a", "alpha")
    fake.add_user("open-b", "beta")
    return fake


def request(account, project, **tiktok_options) -> PublicationRequest:
    options = {
        "caption": "The comet disaster explained in seconds.",
        "hashtags": ["#aviation", "#QuickLearn"],
        "privacy_level": "SELF_ONLY",
        "is_aigc": True,
        "music_usage_confirmed": True,
        **tiktok_options,
    }
    return PublicationRequest(account_id=account.id, base_revision=project.current_revision, tiktok=options)


def publish(db, settings, store, fake, account, project, **options) -> SocialPublication:
    row, run_now = publications.request_publication(db, project, settings, store, apis(fake), request(account, project, **options))
    assert run_now
    publications.run(db, row.id, settings, store, apis(fake))
    db.refresh(row)
    return row


def test_creator_info_is_queried_before_every_direct_post(db, settings, store, fake):
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    fake.calls.clear()
    row = publish(db, settings, store, fake, account, project)
    names = [name for name, _detail in fake.calls]
    assert names.index("creator_info") < names.index("init")
    # once for the preflight of the request, once right before the init
    assert names.count("creator_info") == 2 and names[names.index("init") - 1] in {"creator_info", "refresh"}
    assert row.state == "processing" and row.remote_container_id


def test_caption_hashtags_privacy_interactions_and_ai_label_map_to_post_info(db, settings, store, fake):
    accounts.save_platform_config(db, "tiktok", {"app_audited": True})
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    publish(
        db, settings, store, fake, account, project,
        privacy_level="FOLLOWER_OF_CREATOR", allow_comments=True, allow_duet=False, allow_stitch=True, cover_frame_ms=1500, is_aigc=True,
    )
    init = next(detail for name, detail in fake.calls if name == "init")
    post_info = init["post_info"]
    # TikTok's "title" is the caption; hashtags belong in it.
    assert post_info["title"] == "The comet disaster explained in seconds.\n\n#aviation #QuickLearn"
    assert post_info["privacy_level"] == "FOLLOWER_OF_CREATOR"
    assert (post_info["disable_comment"], post_info["disable_duet"], post_info["disable_stitch"]) == (False, True, False)
    assert post_info["video_cover_timestamp_ms"] == 1500
    assert post_info["is_aigc"] is True
    assert (post_info["brand_content_toggle"], post_info["brand_organic_toggle"]) == (False, False)
    assert init["source_info"]["source"] == "FILE_UPLOAD"


def test_file_upload_follows_the_chunk_contract_and_status_polling_publishes(db, settings, store, fake):
    account = connect_tiktok(db, settings, store, fake, "open-a")
    content = b"\x00mp4" * 6000
    project = social_project(db, settings, content)
    row = publish(db, settings, store, fake, account, project)
    init = next(detail for name, detail in fake.calls if name == "init")
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": len(content), "chunk_size": len(content), "total_chunk_count": 1}
    assert fake.chunks == [{"publish_id": row.remote_container_id, "first": 0, "last": len(content) - 1, "total": len(content)}]
    assert bytes(fake.posts[row.remote_container_id]["data"]) == content
    assert row.upload_complete
    fake.statuses = [{"status": "PROCESSING_UPLOAD", "uploaded_bytes": len(content)}]
    later = datetime.now(UTC) + timedelta(minutes=1)
    publications.poll(db, row.id, settings, store, apis(fake), now=later)
    db.refresh(row)
    assert row.state == "processing" and row.remote_status == "PROCESSING_UPLOAD"
    publications.poll(db, row.id, settings, store, apis(fake), now=later + timedelta(minutes=2))
    db.refresh(row)
    assert row.state == "published" and row.remote_post_id == "7300000000000000001"
    assert row.remote_url == "https://www.tiktok.com/@alpha/video/7300000000000000001"


def test_large_files_use_tiktok_chunk_rules():
    size = 70 * 1000 * 1000  # above 64 MB: several chunks, remainder in the last one
    chunk, total = tiktok.chunk_plan(size)
    assert (chunk, total) == (10 * 1000 * 1000, 7)
    assert tiktok.chunk_ranges(size, chunk, total)[-1] == (6 * chunk, size - 1)
    assert tiktok.chunk_plan(4 * 1024 * 1024) == (4 * 1024 * 1024, 1)


def test_privacy_values_come_from_creator_info(db, settings, store, fake):
    accounts.save_platform_config(db, "tiktok", {"app_audited": True})
    fake.users["open-a"].privacy_options = ("FOLLOWER_OF_CREATOR", "SELF_ONLY")
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    draft = publications.draft(db, project, account, settings, store, apis(fake))
    assert draft["creator_info"]["privacy_level_options"] == ["FOLLOWER_OF_CREATOR", "SELF_ONLY"]
    assert draft["options"]["privacy_level"] is None  # the creator must choose
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(fake), request(account, project, privacy_level="PUBLIC_TO_EVERYONE"))
    assert error.value.code == "preflight_failed"
    assert any(item["field"] == "privacy_level" for item in error.value.issues)


def test_unaudited_app_never_downgrades_a_public_request(db, settings, store, fake):
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    draft = publications.draft(db, project, account, settings, store, apis(fake))
    assert draft["capabilities"]["public_post"] is False
    assert "Public Direct Post requires TikTok app approval" in draft["capabilities"]["notes"][0]
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(fake), request(account, project, privacy_level="PUBLIC_TO_EVERYONE"))
    assert any("Public Direct Post requires TikTok app approval" in item["message"] for item in error.value.issues)
    assert not [call for call in fake.calls if call[0] == "init"]


def test_provider_rejection_fails_permanently(db, settings, store, fake):
    accounts.save_platform_config(db, "tiktok", {"app_audited": True})  # the user claims an audit TikTok disagrees with
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    fake.init_errors = [PublishingApiError("unaudited_client", "Public Direct Post requires TikTok app approval.", retryable=False)]
    row = publish(db, settings, store, fake, account, project, privacy_level="PUBLIC_TO_EVERYONE")
    assert row.state == "failed" and row.last_error_code == "unaudited_client"
    assert row.idempotency_key is None and row.next_attempt_at is None  # never retried on its own


def test_status_failed_is_reported(db, settings, store, fake):
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    row = publish(db, settings, store, fake, account, project)
    fake.statuses = [{"status": "FAILED", "fail_reason": "frame_rate_check_failed"}]
    publications.poll(db, row.id, settings, store, apis(fake), now=datetime.now(UTC) + timedelta(minutes=1))
    db.refresh(row)
    assert row.state == "failed" and "frame rate" in row.last_error_message


def test_account_a_token_is_never_used_for_account_b(db, settings, store, fake):
    connect_tiktok(db, settings, store, fake, "open-a")
    b = connect_tiktok(db, settings, store, fake, "open-b")
    connections.reset_cache()
    project = social_project(db, settings)
    fake.calls.clear()
    row = publish(db, settings, store, fake, b, project)
    used = {detail if isinstance(detail, str) else detail.get("token") for name, detail in fake.calls if name in {"creator_info", "init", "status"}}
    assert used == {"at-open-b"}
    assert fake.posts[row.remote_container_id]["token"] == "at-open-b"
    publications.poll(db, row.id, settings, store, apis(fake), now=datetime.now(UTC) + timedelta(minutes=1))
    db.refresh(row)
    assert row.state == "published" and row.external_account_id == "open-b"


def test_creator_disabled_interactions_are_refused(db, settings, store, fake):
    fake.users["open-a"].duet_disabled = True
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(fake), request(account, project, allow_duet=True))
    assert [item["field"] for item in error.value.issues] == ["allow_duet"]


def test_ai_label_and_music_confirmation_must_be_answered(db, settings, store, fake):
    account = connect_tiktok(db, settings, store, fake, "open-a")
    project = social_project(db, settings)
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(fake), request(account, project, is_aigc=None, music_usage_confirmed=False))
    assert {item["field"] for item in error.value.issues} == {"is_aigc", "music_usage_confirmed"}


def test_http_errors_are_classified_without_secrets():
    def response(status: int, payload: dict) -> httpx.Response:
        return httpx.Response(status, json=payload, request=httpx.Request("POST", tiktok.VIDEO_INIT_URL))

    assert tiktok._error(response(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}}), context="init").code == "unaudited_client"
    assert tiktok._error(response(401, {"error": {"code": "access_token_invalid"}}), context="init").code == "auth_expired"
    rate = tiktok._error(response(429, {"error": {"code": "rate_limit_exceeded"}}), context="init")
    assert rate.code == "rate_limited" and rate.retryable
    server = tiktok._error(response(503, {}), context="init")
    assert server.code == "provider_error" and server.retryable
    assert tiktok._error(response(400, {"error": "invalid_grant", "error_description": "refresh_token=abc expired"}), context="refresh").code == "auth_expired"
    policy = tiktok._error(response(403, {"error": {"code": "spam_risk_user_banned_from_posting"}}), context="init")
    assert policy.code == "provider_policy" and not policy.retryable


def test_real_client_sends_the_documented_requests(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"creator_username": "alpha", "privacy_level_options": ["SELF_ONLY"], "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 300}, "error": {"code": "ok"}})
        if request.url.path.endswith("/video/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "v_pub_1", "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=1"}, "error": {"code": "ok"}})
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE"}, "error": {"code": "ok"}})

    api = tiktok.HttpTikTokApi(httpx.Client(transport=httpx.MockTransport(handler)))
    creator = api.creator_info("at-x")
    assert creator.privacy_level_options == ("SELF_ONLY",) and creator.duet_disabled and creator.max_video_post_duration_sec == 300
    publish_id, url = api.init_direct_post("at-x", {"title": "t", "privacy_level": "SELF_ONLY"}, {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10, "total_chunk_count": 1})
    assert publish_id == "v_pub_1"
    assert api.upload_chunk(url, b"0123456789", 0, 9, 10) == 201
    assert api.fetch_status("at-x", publish_id)["status"] == "PUBLISH_COMPLETE"
    assert seen[0].headers["Authorization"] == "Bearer at-x"
    assert seen[2].headers["Content-Range"] == "bytes 0-9/10" and seen[2].headers["Content-Type"] == "video/mp4"
