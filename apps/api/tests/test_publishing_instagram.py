"""Instagram Reels through ClipForge (Facebook Login for Business, resumable upload) - fakes only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from publishing_support import (
    FakeInstagram,
    apis,
    connect_instagram,
    publishing_settings,
    social_project,
)

from clipforge.publishing import connections, instagram, publications
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
    fake = FakeInstagram()
    fake.add_account("17841400000000001", "brand_one")
    fake.add_account("17841400000000002", "brand_two")
    fake.add_account("17841400000000009", "other_owner", fb_user="fb-user-2")
    return fake


def request(account, project, **options) -> PublicationRequest:
    values = {"caption": "Why airplane windows are round.", "hashtags": ["#aviation", "#LearnSomething"], "share_to_feed": True, **options}
    return PublicationRequest(account_id=account.id, base_revision=project.current_revision, instagram=values)


def start(db, settings, store, fake, account, project, **options):
    row, run_now = publications.request_publication(db, project, settings, store, apis(instagram=fake), request(account, project, **options))
    assert run_now
    publications.run(db, row.id, settings, store, apis(instagram=fake))
    db.refresh(row)
    return row


def test_account_identity_and_professional_capability(db, settings, store, fake):
    one, two = connect_instagram(db, settings, store, fake)
    assert (one.handle, two.handle) == ("brand_one", "brand_two")
    assert one.external_account_id == "17841400000000001"
    assert one.details["account_type"] == "professional" and one.details["page_id"] == "page-17841400000000001"
    assert one.details["token_expires_at"]
    assert one.restrictions == []


def test_reel_resumable_upload_and_publish_only_after_finished(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    content = b"\x00mp4" * 6000
    project = social_project(db, settings, content)
    row = start(db, settings, store, fake, one, project, cover_frame_ms=2000)
    container = fake.calls[[name for name, _ in fake.calls].index("container")][1]
    assert container["ig_user_id"] == "17841400000000001"
    assert container["params"] == {"caption": "Why airplane windows are round.\n\n#aviation #LearnSomething", "share_to_feed": True, "thumb_offset": 2000}
    upload = next(detail for name, detail in fake.calls if name == "upload")
    assert upload["uri"].startswith("https://rupload.facebook.com/ig-api-upload/v25.0/") and upload["size"] == len(content)
    assert row.state == "processing" and row.upload_complete and not fake.published
    fake.status_sequence = ["IN_PROGRESS", "IN_PROGRESS"]
    now = datetime.now(UTC)
    for minutes in (1, 2):
        publications.poll(db, row.id, settings, store, apis(instagram=fake), now=now + timedelta(minutes=minutes))
        db.refresh(row)
        assert row.state == "processing" and not fake.published  # never published before FINISHED
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=now + timedelta(minutes=5))
    db.refresh(row)
    assert row.state == "published" and len(fake.published) == 1
    assert row.remote_post_id == fake.published[0]["media_id"] and row.remote_url.startswith("https://www.instagram.com/reel/")
    # Polling a published row again does nothing.
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=now + timedelta(minutes=9))
    assert len(fake.published) == 1


def test_lost_publish_answer_is_recovered_without_a_second_post(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    project = social_project(db, settings)
    row = start(db, settings, store, fake, one, project)
    fake.publish_errors = [PublishingApiError("network_timeout", "timed out")]
    original = fake.publish_container

    def publish_then_lose_answer(app, token, ig_user_id, container_id):
        fake.publish_errors.clear()
        original(app, token, ig_user_id, container_id)
        raise PublishingApiError("network_timeout", "The Instagram publishing request timed out.")

    fake.publish_container = publish_then_lose_answer  # type: ignore[method-assign]
    now = datetime.now(UTC)
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=now + timedelta(minutes=1))
    db.refresh(row)
    assert row.state == "processing" and row.publish_requested_at is not None
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=now + timedelta(minutes=3))
    db.refresh(row)
    assert row.state == "published" and len(fake.published) == 1


def test_container_error_fails_with_the_provider_reason(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    project = social_project(db, settings)
    row = start(db, settings, store, fake, one, project)
    fake.status_sequence = ["ERROR"]
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=datetime.now(UTC) + timedelta(minutes=1))
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code == "instagram_error" and not fake.published


def test_caption_and_hashtag_limits(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    project = social_project(db, settings)
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(instagram=fake), request(one, project, caption="x" * 2300))
    assert error.value.issues[0]["field"] == "caption"
    many = [f"#tag{index}" for index in range(31)]
    with pytest.raises(PublicationRefused) as error:
        publications.request_publication(db, project, settings, store, apis(instagram=fake), request(one, project, hashtags=many))
    assert [item["field"] for item in error.value.issues] == ["hashtags"]


def test_draft_uses_instagram_metadata_not_youtube(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    project = social_project(db, settings)
    draft = publications.draft(db, project, one, settings, store, apis(instagram=fake))
    assert draft["options"]["caption"] == "Why airplane windows are round. Save it for later."
    assert draft["options"]["hashtags"] == ["#aviation", "#LearnSomething"]
    assert "title" not in draft["options"]
    assert draft["capabilities"]["title"] is False and draft["capabilities"]["thumbnail"] is False
    assert any("public" in note.casefold() for note in draft["capabilities"]["notes"])  # the cover-image limitation is explained
    assert "ClipForge backend is running" in draft["scheduling"]["notice"]


def test_account_isolation_tokens(db, settings, store, fake):
    connect_instagram(db, settings, store, fake)
    (other,) = connect_instagram(db, settings, store, fake, fb_user="fb-user-2")
    connections.reset_cache()
    project = social_project(db, settings)
    row = start(db, settings, store, fake, other, project)
    tokens = {detail["token"] for name, detail in fake.calls if name in {"container", "upload"}}
    assert tokens == {"ll-fb-user-2"}
    publications.poll(db, row.id, settings, store, apis(instagram=fake), now=datetime.now(UTC) + timedelta(minutes=1))
    db.refresh(row)
    assert row.state == "published" and fake.published[0]["ig_user_id"] == "17841400000000009"


def test_expired_token_requires_reconnect(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    details = dict(one.details)
    details["token_expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    one.details = details
    db.commit()
    connections.reset_cache()
    project = social_project(db, settings)
    with pytest.raises(PublishingApiError) as error:
        connections.access_token(db, settings, store, account=one)
    assert error.value.code == "auth_expired"
    db.refresh(one)
    assert one.status == "auth_expired"
    with pytest.raises(PublicationRefused) as refused:
        publications.request_publication(db, project, settings, store, apis(instagram=fake), request(one, project))
    assert refused.value.code == "preflight_failed"


def test_transient_upload_error_retries_with_a_fresh_container(db, settings, store, fake):
    one, _two = connect_instagram(db, settings, store, fake)
    project = social_project(db, settings)
    fake.upload_errors = [PublishingApiError("network_timeout", "timed out")]
    row = start(db, settings, store, fake, one, project)
    assert row.state == "pending" and row.remote_container_id is None and row.next_attempt_at is not None
    publications.run(db, row.id, settings, store, apis(instagram=fake), now=publications.aware(row.next_attempt_at) + timedelta(seconds=1))
    db.refresh(row)
    assert row.state == "processing" and row.attempt_count == 2
    assert len(fake.containers) == 2  # the unpublished first container simply expires at Meta


def test_graph_errors_are_classified():
    def response(status: int, error: dict) -> httpx.Response:
        return httpx.Response(status, json={"error": error}, request=httpx.Request("POST", "https://graph.facebook.com/v25.0/1/media"))

    assert instagram._error(response(400, {"code": 190, "message": "Error validating access token"}), context="x").code == "auth_expired"
    assert instagram._error(response(403, {"code": 10, "message": "Application does not have permission"}), context="x").code == "insufficient_scope"
    limit = instagram._error(response(400, {"code": 9, "error_subcode": 2207042, "message": "limit"}), context="x")
    assert limit.code == "provider_limit" and not limit.retryable
    not_ready = instagram._error(response(400, {"code": 9007, "error_subcode": 2207027, "message": "not ready"}), context="x")
    assert not_ready.code == "media_not_ready" and not_ready.retryable
    transient = instagram._error(response(500, {"code": 2, "is_transient": True, "message": "Service temporarily unavailable"}), context="x")
    assert transient.code == "provider_error" and transient.retryable
    media = instagram._error(response(400, {"code": 352, "error_subcode": 2207026, "message": "unsupported format"}), context="x")
    assert media.code == "invalid_media" and not media.retryable
    redacted = instagram._error(response(400, {"code": 100, "message": "bad access_token=EAAB123secret"}), context="x")
    assert "EAAB123secret" not in redacted.message


def test_real_client_uses_resumable_upload_headers(tmp_path):
    seen: list[httpx.Request] = []
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "rupload.facebook.com":
            request.read()
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "17900000001", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/17900000001"})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "18000000001"})
        return httpx.Response(200, json={"status_code": "FINISHED", "id": "17900000001"})

    api = instagram.GraphInstagramApi(httpx.Client(transport=httpx.MockTransport(handler)))
    app = instagram.MetaApp("1", "secret", "http://localhost:8000/cb", "v25.0")
    container, uri = api.create_reel_container(app, "EAA-token", "1784", {"caption": "hi", "share_to_feed": True, "thumb_offset": 0})
    assert container == "17900000001"
    form = seen[0].content.decode()
    assert "media_type=REELS" in form and "upload_type=resumable" in form and "share_to_feed=true" in form
    api.upload_video(app, "EAA-token", uri, video, 1000)
    rupload = seen[1]
    assert rupload.headers["Authorization"] == "OAuth EAA-token" and rupload.headers["offset"] == "0" and rupload.headers["file_size"] == "1000"
    assert api.container_status(app, "EAA-token", container) == ("FINISHED", None)
    assert api.publish_container(app, "EAA-token", "1784", container) == "18000000001"
    assert all("EAA-token" not in str(request.url) for request in seen)  # never in a URL
    with pytest.raises(PublishingApiError):
        api.upload_video(app, "EAA-token", "https://evil.example.com/upload", video, 1000)
