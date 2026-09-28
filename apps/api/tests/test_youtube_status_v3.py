"""V3: remote YouTube state is the current authority (fake Google only)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_youtube_learning_loop import CURVE, METRICS, connect
from test_youtube_publishing_v2 import PID, api_client, build_project, upload
from youtube_support import FakeYouTube, analytics_payload, publish_options, youtube_settings

from clipforge.models import YouTubeAnalyticsSnapshot, YouTubeUpload
from clipforge.youtube import analytics, connection, learning, publishing, status, uploads
from clipforge.youtube.provider import YouTubeApiError
from clipforge.youtube.publishing import ScheduleChoice

BEFORE = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
REQUESTED_UTC = datetime(2026, 9, 28, 18, 30, tzinfo=UTC)  # 20:30 Europe/Berlin (CEST)
AT_2047 = datetime(2026, 9, 28, 18, 47, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()
    from clipforge.main import app

    app.dependency_overrides.clear()


@pytest.fixture()
def settings(tmp_path):
    return youtube_settings(tmp_path)


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def store():
    from clipforge.security.secrets import SecretStore

    return SecretStore()


def scheduled_upload(db, settings, store, fake) -> YouTubeUpload:
    """Uploaded with "Upload and schedule" for 28.09.2026 20:30 Europe/Berlin."""
    connect(db, settings, store, fake)
    options = publish_options(visibility="schedule", schedule={"date": "2026-09-28", "time": "20:30", "timezone": "Europe/Berlin"})
    row = upload(db, build_project(db, settings), settings, store, fake, options, now=BEFORE)
    row.uploaded_at = BEFORE - timedelta(hours=1)
    fake.videos[row.youtube_video_id]["status"]["uploadStatus"] = "processed"
    uploads.sync_status(db, row, settings, store, fake, now=BEFORE)
    assert status.current_state(row, BEFORE) == "scheduled"
    return row


def go_public(fake: FakeYouTube, video_id: str, *, views=12, likes=1, comments=0, published="2026-09-28T18:30:04Z", privacy="public") -> None:
    video = fake.videos[video_id]
    video["status"].update(privacyStatus=privacy, uploadStatus="processed")
    video["status"].pop("publishAt", None)
    video["snippet"]["publishedAt"] = published
    video["statistics"] = {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)}


def status_calls(fake: FakeYouTube) -> int:
    return sum(1 for name, _ in fake.calls if name == "list_videos")


# ---------------------------------------------------------------------------
# The exact real-world bug
# ---------------------------------------------------------------------------


def test_real_bug_scheduled_video_that_went_public_shows_published(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    assert uploads.aware(row.publish_at) == REQUESTED_UTC
    assert (row.schedule_local_time, row.schedule_timezone) == ("2026-09-28T20:30", "Europe/Berlin")
    go_public(fake, row.youtube_video_id)
    # The page opens at 20:47: the stale local "scheduled" row is reconciled.
    assert status.needs_reconcile(row, now=AT_2047)
    assert uploads.reconcile_if_due(db, row, settings, store, fake, now=AT_2047)
    current = status.current_status(row, now=AT_2047)
    assert current["state"] == "published" and current["label"] == "Published"
    assert current["scheduled_for"] is None  # the "Scheduled for 20:30" banner has nothing to show
    assert current["live_stats"] == {"views": 12, "likes": 1, "comments": 0, "checked_at": AT_2047, "source": "youtube_data_api_videos_list"}
    assert current["published_at"] == datetime(2026, 9, 28, 18, 30, 4, tzinfo=UTC)
    assert current["published_time_source"] == "youtube_snippet_published_at"
    assert current["first_observed_public_at"] == AT_2047
    # the request is preserved as history, not overwritten by YouTube's answer
    assert current["requested"] == {"visibility": "schedule", "publish_at": REQUESTED_UTC, "local_time": "2026-09-28T20:30", "timezone": "Europe/Berlin", "history": []}
    assert uploads.lifecycle(row, now=AT_2047) == "published"
    report = learning.performance_report(db, row, min_sample=5)
    assert report["analytics_state"] == "processing"  # published, detailed analytics not there yet
    assert report["status"] == "waiting_for_data"


def test_real_bug_through_the_api_and_reload_stays_published(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    go_public(fake, row.youtube_video_id)
    row.remote_status_checked_at = row.remote_status_attempted_at = datetime.now(UTC) - timedelta(hours=2)  # stale local row
    db.commit()
    client = api_client(db, settings, store, fake)
    first = client.get(f"/api/youtube/projects/{PID}").json()
    focus = next(item for item in first["uploads"] if item["id"] == first["focus_upload_id"])
    assert focus["current"]["state"] == "published" and focus["lifecycle"] == "published"
    assert focus["current"]["live_stats"]["views"] == 12
    assert focus["publish_at"] is not None  # historical request still there
    assert first["performance"]["analytics_state"] == "processing"
    calls = status_calls(fake)
    # Reload: freshness-gated (no second YouTube call) and still Published.
    second = client.get(f"/api/youtube/projects/{PID}").json()
    assert status_calls(fake) == calls
    assert next(item for item in second["uploads"] if item["id"] == second["focus_upload_id"])["current"]["state"] == "published"
    # Explicit "Refresh YouTube status" while YouTube is unreachable: stays Published, marked stale.
    fake.list_error = YouTubeApiError("network_timeout", "The YouTube video status request timed out.", retryable=True)
    refused = client.post(f"/api/youtube/uploads/{row.id}/sync")
    assert refused.status_code == 503
    third = client.get(f"/api/youtube/projects/{PID}").json()
    current = next(item for item in third["uploads"] if item["id"] == third["focus_upload_id"])["current"]
    assert current["state"] == "published" and current["stale"] is True and current["stale_reason"] == "refresh_failed"
    assert current["refresh_error"]["code"] == "network_timeout"


# ---------------------------------------------------------------------------
# A - I
# ---------------------------------------------------------------------------


def test_a_before_publish_time_remote_private_is_scheduled(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    current = status.current_status(row, now=BEFORE)
    assert current["state"] == "scheduled" and current["scheduled_for"] == REQUESTED_UTC


def test_b_after_publish_time_still_private_is_not_published(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    fake.videos[row.youtube_video_id]["status"].pop("publishAt", None)  # YouTube dropped the schedule, still private
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    current = status.current_status(row, now=AT_2047)
    assert current["state"] == "publish_pending" and current["label"] == "Still private after the scheduled time"
    assert row.published_at is None and uploads.lifecycle(row, now=AT_2047) == "private"


def test_b2_remote_schedule_still_listed_after_time_is_pending_not_published(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    assert status.current_state(row, AT_2047) == "publish_pending"


@pytest.mark.parametrize(("privacy", "state"), [("public", "published"), ("unlisted", "unlisted")])
def test_c_d_public_and_unlisted(db, settings, store, fake, privacy, state):
    row = scheduled_upload(db, settings, store, fake)
    go_public(fake, row.youtube_video_id, privacy=privacy, published="2026-09-27T12:00:01Z")
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    assert status.current_state(row, AT_2047) == state
    if privacy == "unlisted":
        # snippet.publishedAt is the upload time for unlisted videos: not claimed as publication
        assert uploads.aware(row.published_at) == AT_2047 and row.published_source == "first_observed_public"
    else:
        assert uploads.aware(row.published_at) == datetime(2026, 9, 27, 12, 0, 1, tzinfo=UTC)
        assert row.published_source == "youtube_snippet_published_at"


def test_e_processing(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    uploads.sync_status(db, row, settings, store, fake, now=BEFORE)
    current = status.current_status(row, now=BEFORE)
    assert current["processing"] is True and current["state"] == "private"


def test_f_rejected_with_reason(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    fake.videos[row.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    current = status.current_status(row, now=AT_2047)
    assert current["state"] == "rejected" and current["remote"]["rejection_reason"] == "duplicate"


def test_g_deleted(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    del fake.videos[row.youtube_video_id]
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    assert status.current_state(row, AT_2047) == "deleted"


def test_h_timeout_keeps_last_confirmed_state_and_marks_stale(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    fake.list_error = YouTubeApiError("network_timeout", "The YouTube video status request timed out.", retryable=True)
    with pytest.raises(YouTubeApiError):
        uploads.sync_status(db, row, settings, store, fake, now=AT_2047 - timedelta(minutes=30))
    current = status.current_status(row, now=AT_2047 - timedelta(minutes=30))
    assert current["state"] == "scheduled" and current["stale"] and current["last_checked_at"] == BEFORE


def test_i_confirmed_published_never_reverts_on_network_failure(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    go_public(fake, row.youtube_video_id)
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    fake.list_error = YouTubeApiError("network_error", "YouTube could not be reached for video status.", retryable=True)
    later = AT_2047 + timedelta(hours=1)
    assert uploads.reconcile_if_due(db, row, settings, store, fake, now=later) is False
    current = status.current_status(row, now=later)
    assert current["state"] == "published" and current["stale_reason"] == "refresh_failed"
    assert current["last_checked_at"] == AT_2047 and row.remote_privacy_status == "public"


# ---------------------------------------------------------------------------
# Bounded polling and freshness
# ---------------------------------------------------------------------------


def test_polling_is_bounded_around_the_publish_time(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    assert status.poll_interval(row, now=BEFORE) is None  # a day before: no polling
    assert status.poll_interval(row, now=REQUESTED_UTC - timedelta(minutes=10)) == 60
    assert status.poll_interval(row, now=REQUESTED_UTC + timedelta(minutes=1)) == 30
    assert status.poll_interval(row, now=REQUESTED_UTC + timedelta(minutes=5)) == 60
    assert status.poll_interval(row, now=REQUESTED_UTC + timedelta(minutes=30)) == 120
    assert status.poll_interval(row, now=REQUESTED_UTC + timedelta(minutes=61)) is None  # gives up
    go_public(fake, row.youtube_video_id)
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    assert status.poll_interval(row, now=AT_2047 + timedelta(seconds=5)) is None  # changed: stop


def test_freshness_gate_prevents_hammering(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    uploads.sync_status(db, row, settings, store, fake, now=REQUESTED_UTC + timedelta(minutes=1))
    assert not status.needs_reconcile(row, now=REQUESTED_UTC + timedelta(minutes=1, seconds=10))  # < 20 s
    assert status.needs_reconcile(row, now=REQUESTED_UTC + timedelta(minutes=1, seconds=31))  # 30 s cadence
    uploads.sync_status(db, row, settings, store, fake, now=BEFORE + timedelta(hours=1))
    assert not status.needs_reconcile(row, now=BEFORE + timedelta(hours=1, minutes=10))  # far from publish: 30 min
    fake.list_error = YouTubeApiError("network_error", "unreachable", retryable=True)
    before = status_calls(fake)
    for minute in range(5):
        uploads.reconcile_if_due(db, row, settings, store, fake, now=BEFORE + timedelta(hours=2, seconds=minute * 10))
    assert status_calls(fake) - before == 1  # one failed attempt, then backoff


# ---------------------------------------------------------------------------
# Analytics readiness and snapshot eligibility
# ---------------------------------------------------------------------------


def test_no_snapshot_while_youtube_still_reports_private_even_after_requested_time(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    result = analytics.refresh_analytics(db, row, settings, store, fake, now=AT_2047)
    assert result["status"] == "not_published" and result["current_state"] == "publish_pending"
    assert db.scalars(select(YouTubeAnalyticsSnapshot)).all() == []
    assert learning.performance_report(db, row, min_sample=5)["analytics_state"] == "not_published"


def test_snapshot_uses_confirmed_publication_even_if_local_row_said_scheduled(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    assert row.schedule_status == "scheduled"
    go_public(fake, row.youtube_video_id)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    result = analytics.refresh_analytics(db, row, settings, store, fake, now=AT_2047 + timedelta(hours=1))
    assert result["status"] == "ok"
    snapshot = db.scalar(select(YouTubeAnalyticsSnapshot))
    assert snapshot.age_bucket == "1h" and snapshot.published_age_hours == pytest.approx(1.28, abs=0.01)  # from snippet time, not the request
    assert learning.performance_report(db, row, min_sample=5)["analytics_state"] == "available"


def test_analytics_states(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    go_public(fake, row.youtube_video_id)
    uploads.sync_status(db, row, settings, store, fake, now=AT_2047)
    fake.analytics_handler = analytics_payload(METRICS, curve=None)  # metrics, no retention yet
    analytics.refresh_analytics(db, row, settings, store, fake, now=AT_2047 + timedelta(hours=2))
    assert learning.performance_report(db, row, min_sample=5)["analytics_state"] == "partial"
    row.analytics_error_code = "auth_expired"
    db.commit()
    assert learning.performance_report(db, row, min_sample=5)["analytics_state"] == "auth_error"


def test_sync_due_reconciles_before_deciding_eligibility(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    go_public(fake, row.youtube_video_id)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    result = analytics.sync_due(db, settings, store, fake, now=AT_2047 + timedelta(hours=1, minutes=5))
    assert result["results"][0]["status"] == "ok"
    db.refresh(row)
    assert status.current_state(row, AT_2047) == "published"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_changing_the_schedule_keeps_the_previous_request_in_history(db, settings, store, fake):
    row = scheduled_upload(db, settings, store, fake)
    uploads.schedule_publication(db, row, ScheduleChoice(date="2026-09-29", time="19:00", timezone="Europe/Berlin"), settings, store, fake, now=BEFORE)
    current = status.current_status(row, now=BEFORE)
    assert current["requested"]["local_time"] == "2026-09-29T19:00"
    assert current["requested"]["history"][0]["local_time"] == "2026-09-28T20:30"
    assert current["scheduled_for"] == datetime(2026, 9, 29, 17, 0, tzinfo=UTC)


def test_status_call_requests_statistics_and_status_parts(db, settings, store, fake):
    assert set(uploads.STATUS_PARTS.split(",")) >= {"status", "snippet", "statistics", "processingDetails"}


def test_never_checked_upload_is_stale_until_youtube_answers(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row, source, _run = uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options())
    uploads.run_upload(db, row.id, source.path, settings, store, fake)
    db.refresh(row)
    current = status.current_status(row, now=BEFORE)
    assert current["stale_reason"] == "never_checked" and status.needs_reconcile(row, now=BEFORE)
