"""YouTube Learning Loop V1 - every Google call goes to an in-memory fake."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from youtube_support import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    FakeYouTube,
    analytics_payload,
    exported_project,
    publish_options,
    rendered_state,
    youtube_settings,
)

from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import (
    ProductionFingerprint,
    YouTubeAnalyticsSnapshot,
    YouTubeConnection,
    YouTubeMetricValue,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, learning, uploads
from clipforge.youtube.provider import (
    SCOPE_ANALYTICS,
    SCOPE_UPLOAD,
    ChannelIdentity,
    GoogleYouTubeProvider,
    OAuthClient,
    YouTubeApiError,
    read_chunks,
)
from clipforge.youtube.publishing import ScheduleChoice
from clipforge.youtube.routes import get_upload_dispatcher, get_youtube_provider

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_auth():
    connection.reset_youtube_auth_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()


@pytest.fixture()
def settings(tmp_path):
    return youtube_settings(tmp_path)


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def store():
    return SecretStore()


def connect(db, settings, store, fake) -> YouTubeConnection:
    url = connection.begin_authorization(settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    return connection.complete_authorization(db, settings, store, fake, code="auth-code", state=state)


def small_chunks(monkeypatch, size: int = 8192):
    monkeypatch.setattr(uploads, "read_chunks", lambda handle, offset: read_chunks(handle, offset, size))


def upload_now(db, project, settings, store, fake, **kwargs) -> YouTubeUpload:
    record = connection.active_connection(db)
    kwargs.setdefault("options", publish_options())
    upload, target, should_run = uploads.request_upload(db, project, settings, channel_id=record.channel_id, **kwargs)
    if should_run:
        uploads.run_upload(db, upload.id, target.path, settings, store, fake)
        if upload.youtube_video_id or db.get(YouTubeUpload, upload.id).youtube_video_id:
            uploads.after_upload(db, db.get(YouTubeUpload, upload.id), settings, store, fake)
    db.refresh(upload)
    return upload


def published(db, upload, fake, settings, store, when="2026-09-10T11:00:00Z"):
    fake.publish(upload.youtube_video_id, when)
    uploads.sync_status(db, upload, settings, store, fake)
    return upload


def all_database_text(db) -> str:
    dump = []
    for table in ("youtube_connections", "youtube_uploads", "youtube_analytics_snapshots", "youtube_metric_values", "production_fingerprints", "project_revisions"):
        dump.extend(json.dumps([str(value) for value in row]) for row in db.execute(text(f"SELECT * FROM {table}")))
    return "\n".join(dump)


# ---------------------------------------------------------------------------
# OAuth / connection
# ---------------------------------------------------------------------------


def test_authorization_url_uses_pkce_offline_access_and_minimum_scopes(settings):
    url = connection.begin_authorization(settings)
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "accounts.google.com"
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"] == ["http://localhost:8000/api/youtube/oauth/callback"]
    scopes = query["scope"][0].split()
    assert scopes == ["https://www.googleapis.com/auth/youtube", SCOPE_ANALYTICS]
    assert "client_secret" not in query


def test_oauth_callback_persists_channel_identity_and_keeps_refresh_token_in_keyring_only(db, settings, store, fake, test_keyring):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    try:
        client = TestClient(app)
        url = client.post("/api/youtube/connection/authorize").json()["authorization_url"]
        state = parse_qs(urlparse(url).query)["state"][0]
        response = client.get("/api/youtube/oauth/callback", params={"code": "one-time-code", "state": state}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "http://localhost:3000/settings/integrations?youtube=connected"
        body = client.get("/api/youtube/connection").json()
        assert body["status"] == "connected"
        assert body["channel_id"] == "UC_fake_channel_01"
        assert body["channel_title"] == "Knowledge Lab"
        assert body["capabilities"] == {"upload": True, "read": True, "schedule": True, "analytics": True}
        assert REFRESH_TOKEN not in json.dumps(body) and ACCESS_TOKEN not in json.dumps(body)
        # replayed state is refused (one-time CSRF state)
        replay = client.get("/api/youtube/oauth/callback", params={"code": "x", "state": state}, follow_redirects=False)
        assert "reason=invalid_state" in replay.headers["location"]
    finally:
        app.dependency_overrides.clear()
    assert test_keyring.secrets[("ClipForge", "YOUTUBE_REFRESH_TOKEN")] == REFRESH_TOKEN
    dump = all_database_text(db)
    assert REFRESH_TOKEN not in dump and ACCESS_TOKEN not in dump


def test_callback_error_and_denied_consent_do_not_connect(db, settings, store, fake):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    try:
        client = TestClient(app)
        denied = client.get("/api/youtube/oauth/callback", params={"error": "access_denied"}, follow_redirects=False)
        assert "youtube=error" in denied.headers["location"] and "reason=access_denied" in denied.headers["location"]
        assert client.get("/api/youtube/connection").json()["status"] == "not_connected"
    finally:
        app.dependency_overrides.clear()


def test_reconnect_and_disconnect(db, settings, store, fake, test_keyring):
    first = connect(db, settings, store, fake)
    assert first.channel_id == "UC_fake_channel_01"
    fake.channel = ChannelIdentity("UC_other_channel", "Other")
    second = connect(db, settings, store, fake)
    assert second.channel_id == "UC_other_channel"
    assert db.scalars(select(YouTubeConnection)).all() == [second]  # one connection authority
    connection.disconnect(db, store, fake)
    assert fake.revoked == [REFRESH_TOKEN]
    assert ("ClipForge", "YOUTUBE_REFRESH_TOKEN") not in test_keyring.secrets
    assert connection.active_connection(db) is None
    with pytest.raises(YouTubeApiError) as error:
        connection.access_token(db, settings, store, fake, capability="upload")
    assert error.value.code == "not_connected"


def test_expired_refresh_token_marks_connection_and_is_visible(db, settings, store, fake):
    connect(db, settings, store, fake)
    connection.reset_youtube_auth_cache()
    fake.refresh_error = YouTubeApiError("auth_expired", "YouTube access expired or was revoked. Reconnect YouTube.")
    with pytest.raises(YouTubeApiError) as error:
        connection.access_token(db, settings, store, fake, capability="upload")
    assert error.value.code == "auth_expired"
    record = connection.get_connection(db)
    assert record.status == "auth_expired"
    assert connection.serialize_connection(record, settings, store)["error"]["code"] == "auth_expired"


def test_insufficient_scope_is_reported_before_calling_google(db, settings, store, fake):
    fake.scopes = (SCOPE_UPLOAD,)
    connect(db, settings, store, fake)
    with pytest.raises(YouTubeApiError) as error:
        connection.access_token(db, settings, store, fake, capability="analytics")
    assert error.value.code == "insufficient_scope"


# ---------------------------------------------------------------------------
# Upload, idempotency, revisions
# ---------------------------------------------------------------------------


def test_private_upload_uses_the_final_video_and_the_chosen_metadata(db, settings, store, fake, monkeypatch):
    small_chunks(monkeypatch)
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload = upload_now(db, project, settings, store, fake)
    assert upload.state == "processing" and upload.upload_status == "uploaded"
    assert upload.youtube_video_id == "vid00000001"
    assert upload.privacy_status == "private"
    assert upload.channel_id == "UC_fake_channel_01"
    assert (upload.project_id, upload.project_revision, upload.render_revision) == (project.id, 2, 1)
    assert upload.upload_session_uri is None
    body = next(payload for name, payload in fake.calls if name == "start_upload")
    assert body["status"]["privacyStatus"] == "private" and "publishAt" not in body["status"]
    assert body["snippet"]["title"] == "Why are airplane windows round?"
    assert body["snippet"]["tags"] == ["aviation", "shorts", "engineering"]
    assert "#aviation" in body["snippet"]["description"]
    assert body["snippet"]["defaultLanguage"] == "en"
    session = next(iter(fake.sessions.values()))
    exported = settings.resolved_downloads_root.joinpath(project.revisions[-1].state["export"]["filename"]).read_bytes()
    assert bytes(session["data"]) == exported
    assert "upload_session_uri" not in uploads.serialize_upload(upload)


def test_same_render_is_never_uploaded_twice(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    first = upload_now(db, project, settings, store, fake)
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, options=publish_options(), channel_id=first.channel_id)
    assert refused.value.code == "already_uploaded"
    assert refused.value.message == f"Already uploaded as {first.youtube_video_id}."
    # even an explicit force is refused while the video exists on YouTube
    with pytest.raises(uploads.UploadRefused):
        uploads.request_upload(db, project, settings, options=publish_options(), channel_id=first.channel_id, force_new=True)
    assert len(fake.sessions) == 1
    assert len(db.scalars(select(YouTubeUpload)).all()) == 1


def test_upload_api_reports_already_uploaded(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)

    def dispatch(upload_id, path):
        uploads.run_upload(db, upload_id, path, settings, store, fake)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    app.dependency_overrides[get_upload_dispatcher] = lambda: dispatch
    try:
        client = TestClient(app)
        started = client.post(f"/api/youtube/projects/{project.id}/uploads", json={"base_revision": 2, "options": publish_options().model_dump()})
        assert started.status_code == 202 and started.json()["started"] is True
        again = client.post(f"/api/youtube/projects/{project.id}/uploads", json={"base_revision": 2, "options": publish_options().model_dump()})
        assert again.status_code == 409
        assert again.json()["detail"]["status"] == "already_uploaded"
        assert again.json()["detail"]["message"].startswith("Already uploaded as vid")
        panel = client.get(f"/api/youtube/projects/{project.id}").json()
        assert panel["current_render"]["code"] == "already_uploaded"
        assert panel["current_render"]["uploadable"] is False
        assert panel["uploads"][0]["lifecycle"] == "private"
        assert panel["performance"]["status"] == "private"
        stale = client.post(f"/api/youtube/projects/{project.id}/uploads", json={"base_revision": 1, "options": publish_options().model_dump()})
        assert stale.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_new_revision_gets_its_own_mapping_and_never_overwrites_the_old(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings, content=b"A" * 30_000)
    old = upload_now(db, project, settings, store, fake)
    old_snapshot = (old.id, old.youtube_video_id, old.render_revision, old.render_sha256)
    # An edit + re-render is uploadable directly from ClipForge storage - no export.
    from clipforge.models import ProjectRevision
    render_file = settings.render_root / project.id / "renders" / "v3" / "clipforge.mp4"
    render_file.parent.mkdir(parents=True, exist_ok=True)
    render_file.write_bytes(b"B" * 30_000)
    state = {**rendered_state(12.0), "export": project.revisions[-1].state["export"]}  # renders keep the old export
    state["render"] = {**state["render"], "revision": 3, "url": f"/media/{project.id}/renders/v3/clipforge.mp4"}
    state["music"] = {"enabled": False}
    project.revisions.append(ProjectRevision(number=3, parent_revision=2, instruction="shorter hook", kind="user", state=state, changed_components=["script"]))
    project.current_revision = project.active_tip_revision = 3
    db.commit()
    new = upload_now(db, project, settings, store, fake)
    assert new.id != old.id and new.youtube_video_id != old.youtube_video_id
    assert (new.render_revision, new.project_revision, new.source_kind) == (3, 3, "render")
    assert bytes(fake.sessions[list(fake.sessions)[-1]]["data"]) == b"B" * 30_000
    db.refresh(old)
    assert (old.id, old.youtube_video_id, old.render_revision, old.render_sha256) == old_snapshot
    fingerprints = db.scalars(select(ProductionFingerprint)).all()
    assert sorted(item.render_revision for item in fingerprints) == [1, 3]


def test_interrupted_upload_resumes_the_same_session_without_duplicates(db, settings, store, fake, monkeypatch):
    small_chunks(monkeypatch)
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    fake.chunk_errors[2] = YouTubeApiError("network_timeout", "The YouTube upload request timed out.", retryable=True)
    upload = upload_now(db, project, settings, store, fake)
    assert upload.state == "failed"
    assert upload.last_error_code == "network_timeout"
    assert upload.youtube_video_id is None
    assert upload.upload_session_uri and upload.bytes_uploaded == 8192
    resumed, target, should_run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id=upload.channel_id)
    assert resumed.id == upload.id and should_run
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)
    db.refresh(upload)
    assert upload.state == "uploaded" and upload.youtube_video_id
    assert len(fake.sessions) == 1 and len(fake.videos) == 1


def test_timeout_on_final_chunk_never_allows_a_silent_second_video(db, settings, store, fake, monkeypatch):
    small_chunks(monkeypatch, size=1024 * 1024)  # the whole file is one (final) chunk
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    fake.chunk_errors[1] = YouTubeApiError("network_timeout", "The YouTube upload request timed out.", retryable=True)
    upload = upload_now(db, project, settings, store, fake)
    assert upload.state == "failed" and upload.bytes_uploaded == upload.render_file_size
    fake.expire_sessions = True
    _again, target, should_run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id=upload.channel_id)
    assert should_run
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)
    db.refresh(upload)
    assert upload.last_error_code == "session_expired_unknown_outcome"
    assert len(fake.sessions) == 1  # no second session was started


def test_restart_marks_uploading_rows_interrupted(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload, _target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    assert uploads.claim_upload(db, upload.id)
    assert not uploads.claim_upload(db, upload.id)  # a double click cannot start a second run
    assert uploads.mark_interrupted_uploads(db) == 1
    db.refresh(upload)
    assert upload.state == "failed" and upload.last_error_code == "interrupted"


def test_expired_session_after_final_bytes_requires_explicit_confirmation(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload, target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    upload.upload_session_uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=lost"
    upload.bytes_uploaded = upload.render_file_size
    upload.state = "failed"
    db.commit()
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)
    db.refresh(upload)
    assert upload.last_error_code == "session_expired_unknown_outcome"
    assert fake.sessions == {}
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    assert refused.value.code == "unknown_outcome"
    fresh, target, should_run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01", force_new=True)
    assert should_run and fresh.id != upload.id


@pytest.mark.parametrize(("error", "code"), [
    (YouTubeApiError("quota_exceeded", "The request cannot be completed because you have exceeded your quota.", retryable=True), "quota_exceeded"),
    (YouTubeApiError("bad_request", "The request metadata specifies an invalid video title."), "bad_request"),
])
def test_upload_failure_is_visible_and_project_state_untouched(db, settings, store, fake, error, code):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    before = [(item.number, json.dumps(item.state, sort_keys=True)) for item in project.revisions]
    fake.start_error = error
    upload = upload_now(db, project, settings, store, fake)
    assert upload.state == "failed"
    assert uploads.serialize_upload(upload)["error"] == {"code": code, "message": error.message}
    db.refresh(project)
    assert [(item.number, json.dumps(item.state, sort_keys=True)) for item in project.revisions] == before


def test_wrong_channel_is_refused(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload, target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    fake.channel = ChannelIdentity("UC_intruder", "Someone else")
    connect(db, settings, store, fake)
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)
    db.refresh(upload)
    assert upload.last_error_code == "wrong_channel" and fake.sessions == {}


def test_changed_export_file_is_not_uploaded_under_the_old_identity(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload, target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    target.path.write_bytes(b"different" * 5000)
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)
    db.refresh(upload)
    assert upload.last_error_code == "export_changed" and fake.sessions == {}


# ---------------------------------------------------------------------------
# Status + scheduling
# ---------------------------------------------------------------------------


def test_status_sync_processing_ready_rejected_and_deleted(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload = upload_now(db, project, settings, store, fake)
    uploads.sync_status(db, upload, settings, store, fake)
    assert upload.state == "processing"
    fake.videos[upload.youtube_video_id]["status"]["uploadStatus"] = "processed"
    uploads.sync_status(db, upload, settings, store, fake)
    assert upload.state == "ready"
    fake.videos[upload.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    uploads.sync_status(db, upload, settings, store, fake)
    assert upload.state == "failed" and upload.rejection_reason == "duplicate"
    assert "duplicate" in uploads.serialize_upload(upload)["error"]["message"]
    del fake.videos[upload.youtube_video_id]
    uploads.sync_status(db, upload, settings, store, fake)
    assert upload.deleted_on_youtube and uploads.lifecycle(upload) == "deleted"
    # a deleted video may be re-uploaded, but only explicitly
    with pytest.raises(uploads.UploadRefused):
        uploads.request_upload(db, project, settings, options=publish_options(), channel_id=upload.channel_id)
    again, _target, should_run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id=upload.channel_id, force_new=True)
    assert should_run and again.id != upload.id
    db.refresh(upload)
    assert upload.youtube_video_id and upload.idempotency_key is None  # history kept


def test_schedule_uses_private_publish_at_and_preserves_status_fields(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload = upload_now(db, project, settings, store, fake)
    choice = ScheduleChoice(date="2026-09-12", time="17:00", timezone="Europe/Berlin")
    uploads.schedule_publication(db, upload, choice, settings, store, fake, now=NOW)
    body = next(payload for name, payload in fake.calls if name == "update_video")
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2026-09-12T15:00:00.000Z"  # CEST = UTC+2
    assert body["status"]["selfDeclaredMadeForKids"] is False and body["status"]["license"] == "youtube"
    when = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    assert upload.schedule_status == "scheduled" and uploads.aware(upload.publish_at) == when
    assert (upload.schedule_local_time, upload.schedule_timezone) == ("2026-09-12T17:00", "Europe/Berlin")
    assert uploads.serialize_upload(upload)["publish_at"] == when
    assert upload.privacy_status == "private" and upload.published_at is None  # nothing published
    assert uploads.lifecycle(upload) == "scheduled"


def test_schedule_rejects_invalid_times_and_failures_stay_retryable(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = exported_project(db, settings)
    upload = upload_now(db, project, settings, store, fake)
    with pytest.raises(uploads.UploadRefused) as past:
        uploads.schedule_publication(db, upload, ScheduleChoice(date="2026-09-10", time="11:00", timezone="UTC"), settings, store, fake, now=NOW)
    assert past.value.code == "invalid_time"
    with pytest.raises(uploads.UploadRefused):
        uploads.schedule_publication(db, upload, ScheduleChoice(date="2026-09-12", time="15:00", timezone="Mars/Olympus"), settings, store, fake, now=NOW)
    fake.update_error = YouTubeApiError("bad_request", "The video's publish time is invalid.")
    tomorrow = ScheduleChoice(date="2026-09-11", time="12:00", timezone="UTC")
    with pytest.raises(YouTubeApiError):
        uploads.schedule_publication(db, upload, tomorrow, settings, store, fake, now=NOW)
    assert upload.schedule_status == "schedule_failed" and "publish time" in upload.schedule_error
    fake.update_error = None
    uploads.schedule_publication(db, upload, tomorrow, settings, store, fake, now=NOW)
    assert upload.schedule_status == "scheduled" and upload.schedule_error is None


def test_published_video_cannot_be_rescheduled(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)
    assert uploads.aware(upload.published_at) == datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.schedule_publication(db, upload, ScheduleChoice(date="2026-09-11", time="12:00", timezone="UTC"), settings, store, fake, now=NOW)
    assert refused.value.code == "already_published"


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

METRICS = {
    "views": 5400, "engagedViews": 2100, "estimatedMinutesWatched": 310, "averageViewDuration": 7.4,
    "averageViewPercentage": 74.0, "likes": 120, "comments": 9, "shares": 14, "subscribersGained": 6, "subscribersLost": 1,
}
CURVE = [1.0] * 10 + [0.9 - 0.004 * i for i in range(90)]


def test_private_video_gets_no_analytics_snapshot(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    assert result == {"status": "not_published"}
    assert db.scalars(select(YouTubeAnalyticsSnapshot)).all() == []
    assert learning.performance_report(db, upload, min_sample=5)["status"] == "private"


def test_published_video_without_data_is_waiting_not_error(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)
    fake.analytics_handler = analytics_payload(None)
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    assert result["status"] == "no_data_yet" and result["retention_status"] == "not_ready"
    report = learning.performance_report(db, upload, min_sample=5)
    assert report["status"] == "waiting_for_data" and report["analytics_error"] is None


def test_analytics_success_keeps_views_and_engaged_views_separate_and_never_fabricates_stayed_to_watch(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    assert result["status"] == "ok" and result["age_bucket"] == "1h"
    snapshot = db.get(YouTubeAnalyticsSnapshot, result["snapshot_id"])
    values = {item.name: item for item in snapshot.metrics}
    assert values["views"].value == 5400 and values["engagedViews"].value == 2100
    assert values["views"].source == values["engagedViews"].source == "youtube_analytics_api"
    stayed = values["stayed_to_watch"]
    assert stayed.value is None and stayed.availability == "unavailable" and stayed.reason == "not_available_via_api"
    assert snapshot.content_type == "SHORTS" and upload.content_type == "SHORTS"
    assert snapshot.raw_responses["video_metrics"]["rows"][0][1] == 5400  # raw API response kept
    assert (snapshot.youtube_video_id, snapshot.project_revision, snapshot.render_revision) == (upload.youtube_video_id, 2, 1)
    report = learning.performance_report(db, upload, min_sample=5)
    metrics = report["latest_snapshot"]["metrics"]
    assert metrics["stayed_to_watch"]["value"] is None
    assert metrics["engagedViews"]["value"] != metrics["views"]["value"]
    queries = [params for name, params in fake.calls if name == "analytics"]
    assert all(params["ids"] == "channel==MINE" and params["filters"] == f"video=={upload.youtube_video_id}" for params in queries)


def test_rejected_metric_is_recorded_unavailable_while_others_still_arrive(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE, rejected={"engagedViews"})
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    assert result["status"] == "partial"
    values = {item.name: item for item in db.get(YouTubeAnalyticsSnapshot, result["snapshot_id"]).metrics}
    assert values["engagedViews"].availability == "unavailable" and values["engagedViews"].reason == "rejected_by_api"
    assert values["views"].value == 5400  # never substituted for engagedViews
    assert values["engagedViews"].value is None


@pytest.mark.parametrize("error", [
    YouTubeApiError("api_disabled", "YouTube Analytics API has not been used in project 1 before or it is disabled."),
    YouTubeApiError("quota_exceeded", "Quota exceeded.", retryable=True),
    YouTubeApiError("network_timeout", "The YouTube analytics request timed out.", retryable=True),
])
def test_analytics_failures_are_visible_and_create_no_fake_snapshot(db, settings, store, fake, error):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)

    def failing(_params):
        raise error

    fake.analytics_handler = failing
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    assert result["status"] == "error" and result["error"]["code"] == error.code
    assert db.scalars(select(YouTubeAnalyticsSnapshot)).all() == []
    assert learning.performance_report(db, upload, min_sample=5)["analytics_error"]["code"] == error.code


def test_retention_points_are_stored_raw_with_video_seconds(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings, duration=10.0), settings, store, fake)
    published(db, upload, fake, settings, store)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    points = db.scalars(select(YouTubeRetentionPoint).order_by(YouTubeRetentionPoint.elapsed_video_ratio)).all()
    assert len(points) == 100
    assert points[0].elapsed_video_ratio == 0.01 and points[0].video_second == 0.1
    assert points[-1].elapsed_video_ratio == 1.0 and points[-1].video_second == 10.0
    assert [point.audience_watch_ratio for point in points] == CURVE  # not smoothed
    assert points[20].stopped_watching is not None and points[20].total_segment_impressions is not None


def test_retention_falls_back_to_supported_metric_set(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store)
    base = analytics_payload(METRICS, curve=CURVE)

    def handler(params):
        if params["dimensions"] == "elapsedVideoTimeRatio" and "startedWatching" in params["metrics"]:
            raise YouTubeApiError("bad_request", "The query is not supported.")
        return base(params)

    fake.analytics_handler = handler
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    snapshot = db.get(YouTubeAnalyticsSnapshot, result["snapshot_id"])
    assert snapshot.retention_status == "ok"
    assert snapshot.date_range["retention_metrics"] == ["audienceWatchRatio", "relativeRetentionPerformance"]
    assert snapshot.retention_points[0].stopped_watching is None


def test_scene_mapping_and_opening_retention_follow_the_rendered_timeline(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings, duration=10.0), settings, store, fake)
    published(db, upload, fake, settings, store)
    # scene 1 (0-2.5 s) loses 30 points, then a slow decline
    curve = [1.0 - 0.3 * min(1, i / 25) for i in range(25)] + [0.7 - 0.001 * i for i in range(75)]
    fake.analytics_handler = analytics_payload(METRICS, curve=curve)
    analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
    report = learning.performance_report(db, upload, min_sample=5)
    scenes = report["scene_retention"]
    assert [row["start"] for row in scenes] == [0.0, 2.5, 5.0, 7.5]
    first = scenes[0]
    assert first["retention_entering"] == 1.0
    assert first["retention_leaving"] == pytest.approx(curve[24])
    assert first["retention_delta"] == pytest.approx(curve[24] - 1.0, abs=1e-4)
    assert first["notable_drop"] is True and scenes[2]["notable_drop"] is False
    assert first["entering_bucket_second"] == 0.1 and first["points_in_scene"] == 25
    assert first["stopped_watching_index"] > 1.5  # stops concentrated in scene 1
    opening = report["opening_retention"]
    assert opening["label"] == "Opening retention" and opening["not_stayed_to_watch"] is True
    assert [item["target_second"] for item in opening["points"]] == [1.0, 2.0, 3.0]
    assert opening["points"][0]["bucket_second"] == 1.0
    assert opening["points"][0]["audience_watch_ratio"] == pytest.approx(curve[9])
    assert opening["hook"]["strategy"] == "counterintuitive_insight"
    assert opening["hook"]["verbal_hook"] == "Square windows once tore planes apart."
    evidence = [item for item in report["evidence"] if item["kind"] == "scene_retention_drop"]
    assert evidence and evidence[0]["associated_production_data"]["hook_strategy"] == "counterintuitive_insight"
    assert evidence[0]["associated_production_data"]["visual_strategy"] == "stock_photo"
    assert evidence[0]["confidence"] == "low" and evidence[0]["sample_size"] == 1
    assert all("bad" not in json.dumps(item).casefold() for item in report["evidence"])


def test_short_scene_below_bucket_resolution_is_not_given_fake_precision():
    from types import SimpleNamespace

    points = [SimpleNamespace(elapsed_video_ratio=i / 100, audience_watch_ratio=1 - i / 200, relative_retention_performance=None, stopped_watching=None) for i in range(1, 101)]
    fingerprint = {"content": {"duration_seconds": 60.0}, "scenes": [{"index": 1, "scene_id": "s1", "start": 0.0, "end": 0.2}, {"index": 2, "scene_id": "s2", "start": 0.2, "end": 60.0}]}
    rows = learning.map_scenes(points, fingerprint)
    assert rows[0]["status"] == "below_bucket_resolution" and rows[0]["retention_delta"] is None
    assert rows[1]["status"] == "ok"


def test_snapshots_accumulate_history_at_age_buckets(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store, when="2026-09-10T10:30:00Z")
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    assert analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)["age_bucket"] == "1h"
    assert analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW + timedelta(minutes=5), due_only=True)["status"] == "not_due"
    fake.analytics_handler = analytics_payload({**METRICS, "views": 9000}, curve=CURVE)
    result = analytics.sync_due(db, settings, store, fake, now=NOW + timedelta(hours=23))
    assert result["results"][0]["age_bucket"] == "24h"
    history = db.scalars(select(YouTubeAnalyticsSnapshot).order_by(YouTubeAnalyticsSnapshot.fetched_at)).all()
    assert [item.age_bucket for item in history] == ["1h", "24h"]
    assert [learning.metric_value(item, "views") for item in history] == [5400, 9000]
    assert history[1].published_age_hours == pytest.approx(24.5)
    manual = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW + timedelta(hours=24))
    assert manual["age_bucket"] == "manual"
    assert len(learning.performance_report(db, upload, min_sample=5)["snapshots"]) == 3


def test_manual_studio_metric_extension_point(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    snapshot = analytics.record_manual_metric(db, upload, "stayed_to_watch", 68.5, note="from Studio", now=NOW)
    metric = db.scalar(select(YouTubeMetricValue).where(YouTubeMetricValue.snapshot_id == snapshot.id))
    assert (metric.name, metric.value, metric.source) == ("stayed_to_watch", 68.5, "manual_studio_import")
    with pytest.raises(uploads.UploadRefused):
        analytics.record_manual_metric(db, upload, "engagedViews", 1.0)
    with pytest.raises(uploads.UploadRefused):
        analytics.record_manual_metric(db, upload, "stayed_to_watch", 140)
    report = learning.performance_report(db, upload, min_sample=5)
    assert report["manual_metrics"][0]["source"] == "manual_studio_import"
    assert report["snapshots"] == []  # manual imports never masquerade as API data


# ---------------------------------------------------------------------------
# Baseline, outliers, learning table, fingerprints
# ---------------------------------------------------------------------------


def seed_channel(db, settings, store, fake, count: int, *, view_percentages=None, strategies=None):
    connect(db, settings, store, fake)
    rows = []
    for index in range(count):
        project_id = f"{index + 1:08d}-1111-4111-8111-111111111111"
        strategy = (strategies or ["counterintuitive_insight"])[index % len(strategies or ["x"])]
        project = exported_project(db, settings, content=bytes([index]) * 20_000, project_id=project_id, strategy=strategy)
        upload = upload_now(db, project, settings, store, fake)
        published(db, upload, fake, settings, store)
        percent = (view_percentages or [70.0] * count)[index]
        curve = [1.0] * 10 + [0.5 + 0.4 * percent / 100 - 0.003 * i for i in range(90)]
        fake.analytics_handler = analytics_payload({**METRICS, "averageViewPercentage": percent, "engagedViews": 1000 + 10 * percent}, curve=curve)
        analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)
        rows.append(upload)
    return rows


def test_minimum_sample_guard_prevents_baseline_claims(db, settings, store, fake):
    rows = seed_channel(db, settings, store, fake, 4, view_percentages=[60, 65, 70, 99])
    report = learning.performance_report(db, rows[-1], min_sample=5)
    assert report["baseline"]["sample_size"] == 3 and report["baseline"]["sufficient"] is False
    assert report["classification"]["label"] == "insufficient_data"
    assert all(item["kind"] != "channel_baseline_position" for item in report["evidence"])


def test_baseline_and_outlier_classification(db, settings, store, fake):
    rows = seed_channel(db, settings, store, fake, 7, view_percentages=[60, 62, 64, 66, 68, 70, 140])
    baseline = learning.channel_baseline(db, "UC_fake_channel_01", min_sample=5, exclude_upload_id=rows[-1].id, age_hours=1.0)
    assert baseline["sample_size"] == 6 and baseline["sufficient"]
    assert baseline["metrics"]["averageViewPercentage"]["median"] == 65.0
    assert baseline["metrics"]["likes_per_engaged_view"]["n"] == 6
    top = learning.performance_report(db, rows[-1], min_sample=5)
    assert top["classification"]["label"] == "outlier_positive"
    low = learning.performance_report(db, rows[0], min_sample=5)
    assert low["classification"]["label"] == "below_channel_baseline"
    middle = learning.performance_report(db, rows[3], min_sample=5)
    assert middle["classification"]["label"] == "near_channel_baseline"


def test_unconfirmed_content_type_is_excluded_from_the_shorts_baseline(db, settings, store, fake):
    rows = seed_channel(db, settings, store, fake, 6)
    rows[0].content_type = None
    rows[1].content_type = "VIDEO_ON_DEMAND"
    db.commit()
    baseline = learning.channel_baseline(db, "UC_fake_channel_01", min_sample=5)
    assert baseline["sample_size"] == 4 and baseline["excluded_unconfirmed_content_type"] == 1
    report = learning.performance_report(db, rows[1], min_sample=5)
    assert report["classification"]["label"] == "insufficient_data"


def test_learning_table_is_guarded_and_associative(db, settings, store, fake):
    seed_channel(db, settings, store, fake, 2, strategies=["curiosity_gap", "counterintuitive_insight"])
    thin = learning.learning_table(db, "UC_fake_channel_01")
    assert all(question["status"] == "insufficient_data" for question in thin["questions"])
    assert thin["language"] == "association_only" and "not show that" in thin["caveat"]


def test_learning_table_answers_with_enough_videos(db, settings, store, fake):
    seed_channel(db, settings, store, fake, 6, view_percentages=[60, 62, 64, 80, 82, 84], strategies=["curiosity_gap", "counterintuitive_insight"])
    table = learning.learning_table(db, "UC_fake_channel_01")
    hooks = next(item for item in table["questions"] if item["id"] == "hook_strategy_opening")
    assert hooks["status"] == "ok"
    assert {group["group"] for group in hooks["groups"]} == {"curiosity_gap", "counterintuitive_insight"}
    assert all(group["n"] == 3 and group["sufficient"] for group in hooks["groups"])
    assert "associated with" in hooks["statements"][0] and "caused" not in hooks["statements"][0]
    media = next(item for item in table["questions"] if item["id"] == "media_origin_retention")
    generated = next(group for group in media["groups"] if group["group"] == "generated")
    assert generated["n"] == 6 and generated["sufficient"] is False  # 6 scenes < 8 minimum
    for question in table["questions"]:
        for statement in question["statements"]:
            assert "caus" not in statement


def test_production_fingerprint_reflects_the_final_render_and_is_immutable(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    record = db.get(ProductionFingerprint, upload.fingerprint_id)
    fingerprint = record.fingerprint
    assert fingerprint["render"]["render_revision"] == 1 and fingerprint["render"]["render_sha256"] == upload.render_sha256
    assert fingerprint["content"]["duration_seconds"] == 10.0
    assert fingerprint["content"]["answer_reveal_seconds"] == 5.0 and fingerprint["content"]["payoff_seconds"] == 7.5
    assert fingerprint["hook"]["strategy"] == "counterintuitive_insight"
    assert fingerprint["hook"]["on_screen_hook"] == "Why round?"
    assert fingerprint["pacing"]["scene_durations"] == [2.5, 2.5, 2.5, 2.5]
    assert fingerprint["visual"]["generated_scene_count"] == 1 and fingerprint["visual"]["overlay_scene_count"] == 1
    assert [row["media_origin"] for row in fingerprint["scenes"]] == ["real", "real", "generated", "real"]
    assert fingerprint["quality"] == {"critic_status": "issues_remain", "critic_score": 71, "issue_count": 2, "unresolved_issue_count": 1, "repairs_attempted": 2, "repairs_successful": 1, "repair_pass_count": 1}
    assert fingerprint["audio"]["music_track_id"] == "track_7" and fingerprint["audio"]["voice_id"] == "marin"
    assert len(json.dumps(fingerprint)) < 12_000  # compact, no project blob
    record.fingerprint = {"tampered": True}
    with pytest.raises(ValueError, match="immutable"):
        db.commit()
    db.rollback()


def test_no_learning_code_mutates_generation_rules():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "clipforge" / "youtube"
    source = "\n".join(path.read_text() for path in root.glob("*.py"))
    for module in ("triple_hook", "visual_director", "story_arc", "pacing", "hook_library", "verbal_hook", "hooks", "voice", "script_writer"):
        assert f"from ..{module} import" not in source and f"from ..{module}\n" not in source


# ---------------------------------------------------------------------------
# Real provider error mapping (httpx transport, no network)
# ---------------------------------------------------------------------------


def _provider(handler):
    return GoogleYouTubeProvider(httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(("status_code", "payload", "code"), [
    (401, {"error": {"code": 401, "message": "Invalid Credentials", "errors": [{"reason": "authError"}]}}, "auth_expired"),
    (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, "auth_expired"),
    (403, {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}, "quota_exceeded"),
    (403, {"error": {"code": 403, "message": "YouTube Analytics API has not been used in project 12 before or it is disabled.", "errors": [{"reason": "accessNotConfigured"}]}}, "api_disabled"),
    (403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}}, "insufficient_scope"),
    (400, {"error": {"code": 400, "message": "Unknown identifier", "errors": [{"reason": "badRequest"}]}}, "bad_request"),
    (503, {"error": {"code": 503, "message": "backend"}}, "provider_error"),
])
def test_google_errors_map_to_stable_codes(status_code, payload, code):
    provider = _provider(lambda request: httpx.Response(status_code, json=payload))
    with pytest.raises(YouTubeApiError) as error:
        provider.analytics_report("token", {"ids": "channel==MINE"})
    assert error.value.code == code
    assert "token" not in error.value.message.casefold() or code == "auth_expired"


def test_network_timeout_maps_to_retryable_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(YouTubeApiError) as error:
        _provider(handler).list_videos("token", ["x"], "status")
    assert error.value.code == "network_timeout" and error.value.retryable


def test_real_provider_upload_protocol_and_revoke_body():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"})
        if request.headers["Content-Range"] == "bytes */20":
            return httpx.Response(308, headers={"Range": "bytes=0-9"})
        if request.headers["Content-Range"] == "bytes 10-19/20":
            return httpx.Response(201, json={"id": "vid123", "status": {"privacyStatus": "private", "uploadStatus": "uploaded"}})
        return httpx.Response(500)

    provider = _provider(handler)
    uri = provider.start_resumable_upload("token", {"snippet": {"title": "t"}, "status": {"privacyStatus": "private"}}, 20, "video/mp4")
    assert seen[0].url.params["uploadType"] == "resumable" and seen[0].url.params["part"] == "snippet,status"
    assert seen[0].headers["X-Upload-Content-Length"] == "20"
    assert json.loads(seen[0].content)["status"] == {"privacyStatus": "private"}
    progress = provider.query_upload("token", uri, 20)
    assert progress.complete is False and progress.offset == 10
    done = provider.upload_chunk("token", uri, b"x" * 10, 10, 20)
    assert done.complete and done.video["id"] == "vid123"
    provider.revoke("secret-refresh")
    revoke = seen[-1]
    assert "secret-refresh" not in str(revoke.url) and b"token=secret-refresh" in revoke.content


def test_token_exchange_sends_pkce_verifier():
    captured = {}

    def handler(request):
        captured.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "secret-access", "expires_in": 3599, "refresh_token": "r", "scope": f"{SCOPE_UPLOAD} {SCOPE_ANALYTICS}"})

    grant = _provider(handler).exchange_code(OAuthClient("cid", "csecret", "http://localhost:8000/cb"), "code1", "verifier1")
    assert captured["code_verifier"] == ["verifier1"] and captured["grant_type"] == ["authorization_code"]
    assert grant.scopes == (SCOPE_UPLOAD, SCOPE_ANALYTICS)
    assert grant.access_token == "secret-access" and "secret-access" not in repr(grant)
