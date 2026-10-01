""""Refresh recent videos": bounded status refresh + Analytics chosen by due-ness.

Mac state: 13 library rows (3 scheduled, 9 published, 1 deleted); the oldest
published Short (d9xX7tiZBBI) had an empty 24h capture, YouTube Analytics now
has its data, but it sat outside the 10 most recent rows and was never asked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_youtube_learning_loop import METRICS, connect
from test_youtube_publishing_v2 import api_client
from youtube_support import FakeYouTube, analytics_payload, youtube_settings

from clipforge.main import app
from clipforge.models import YouTubeAnalyticsSnapshot, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, library, publishing, uploads

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CHANNEL = "UC_fake_channel_01"
OLD = "d9xX7tiZBBI"
PROVEN = {"views": 339, "engagedViews": 163, "estimatedMinutesWatched": 44, "averageViewDuration": 14, "averageViewPercentage": 33.33,
          "likes": 9, "comments": 0, "shares": 0, "subscribersGained": 0, "subscribersLost": 0}


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(db, fake, video_id: str, index: int, *, published: datetime | None = None, scheduled_for: datetime | None = None,
         uploaded: datetime | None = None, deleted: bool = False, views: int = 100) -> YouTubeUpload:
    row = YouTubeUpload(
        project_id=f"{index:08d}-0000-4000-8000-000000000000", project_revision=1, render_revision=1, render_sha256=f"{index:064d}",
        render_file_size=10, channel_id=CHANNEL, youtube_video_id=video_id, state="ready", upload_status="processed",
        remote_privacy_status="public" if published else "private", published_at=published,
        published_source="youtube_snippet_published_at" if published else None,
        publish_at=scheduled_for, remote_publish_at=scheduled_for, uploaded_at=uploaded or published or NOW, title=video_id,
        deleted_on_youtube=deleted, remote_status_checked_at=NOW - timedelta(hours=1),
        remote_view_count=None if deleted else views if published else 0, remote_like_count=5 if published else 0, remote_comment_count=0,
    )
    db.add(row)
    if not deleted:
        status = {"privacyStatus": "public" if published else "private", "uploadStatus": "processed"}
        if scheduled_for:
            status["publishAt"] = _iso(scheduled_for)
        fake.videos[video_id] = {"id": video_id, "snippet": {"title": video_id, "publishedAt": _iso(published or uploaded or NOW)},
                                 "status": status, "statistics": {"viewCount": str(views), "likeCount": "5", "commentCount": "0"}}
    return row


def _capture(db, row: YouTubeUpload, bucket: str, status: str, fetched: datetime, values: dict | None = None) -> None:
    snapshot = YouTubeAnalyticsSnapshot(
        upload_id=row.id, youtube_video_id=row.youtube_video_id, channel_id=CHANNEL, project_id=row.project_id, project_revision=1,
        render_revision=1, source=analytics.SOURCE_API, age_bucket=bucket, status=status, fetched_at=fetched,
        published_age_hours=(fetched - row.published_at).total_seconds() / 3600,
    )
    from clipforge.models import YouTubeMetricValue

    for name, value in (values or {}).items():
        snapshot.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name=name, value=value, availability="available", source=analytics.SOURCE_API, fetched_at=fetched))
    db.add(snapshot)


@pytest.fixture()
def mac(db, tmp_path, monkeypatch):
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    settings, store, fake = youtube_settings(tmp_path), SecretStore(), FakeYouTube()
    connect(db, settings, store, fake)
    rows: dict[str, YouTubeUpload] = {}
    # 3 scheduled (uploaded recently, publish in the future): no published_at, rank by upload time
    for index in range(3):
        rows[f"sched{index}"] = _row(db, fake, f"sched{index}", index, uploaded=NOW - timedelta(hours=1 + index), scheduled_for=NOW + timedelta(days=1 + index))
    # 7 recent published, each already captured in its current bucket (not due)
    for index in range(7):
        published = NOW - timedelta(hours=5 + index * 6)
        rows[f"pub{index}"] = _row(db, fake, f"pub{index}", 10 + index, published=published, views=500 + index)
    # 2 older published outside the 10 newest rows: one not due, one (d9x) due for a retry
    rows["older"] = _row(db, fake, "older_not_due", 30, published=NOW - timedelta(hours=58), views=800)
    rows[OLD] = _row(db, fake, OLD, 31, published=NOW - timedelta(hours=62), views=971)
    rows["deleted"] = _row(db, fake, "gone0000001", 40, published=NOW - timedelta(hours=20), deleted=True)
    db.flush()
    for key in [f"pub{index}" for index in range(7)] + ["older"]:
        row = rows[key]
        age = (NOW - row.published_at).total_seconds() / 3600
        _capture(db, row, analytics.age_bucket(age, set()), "ok", NOW - timedelta(minutes=10), METRICS)
    _capture(db, rows[OLD], "24h", "no_data_yet", rows[OLD].published_at + timedelta(hours=30))
    db.commit()
    fake.analytics_handler = analytics_payload(PROVEN)
    monkeypatch.setattr(analytics, "_now", lambda: NOW)
    client = api_client(db, settings, store, fake)
    yield db, fake, client, rows
    app.dependency_overrides.clear()
    connection.reset_youtube_auth_cache()


def _analytics_calls(fake, start: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, params in fake.calls[start:]:
        if name == "analytics" and params.get("dimensions") == "video":
            video = params["filters"].split("==")[1]
            counts[video] = counts.get(video, 0) + 1
    return counts


def test_mac_state_the_old_due_video_sits_outside_the_ten_recent_rows(mac):
    db, _fake, _client, _rows = mac
    assert len(db.scalars(select(YouTubeUpload).where(library.library_condition())).all()) == 13
    recent = [row.youtube_video_id for row in library.recent_videos(db, CHANNEL)]
    assert len(recent) == 10 and OLD not in recent and "older_not_due" not in recent and "gone0000001" not in recent
    assert sum(1 for vid in recent if vid.startswith("sched")) == 3


def test_refresh_recent_asks_analytics_for_the_due_video_outside_the_recent_rows(mac):
    db, fake, client, rows = mac
    start = len(fake.calls)
    result = client.post("/api/videos/refresh-recent").json()
    calls = _analytics_calls(fake, start)
    # Status refresh stays bounded: the 10 recent rows (plus nothing for the other old one).
    status_checked = {item["video_id"] for item in result["results"]}
    assert {f"sched{i}" for i in range(3)} | {f"pub{i}" for i in range(7)} <= status_checked
    # The due video is asked exactly once; nothing not due is asked.
    assert calls == {OLD: 1}
    assert "older_not_due" not in status_checked
    entry = next(item for item in result["results"] if item["video_id"] == OLD)
    assert entry["status"] == "ok" and entry["age_bucket"] == "24h"
    snapshot = db.scalars(select(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == rows[OLD].id).order_by(YouTubeAnalyticsSnapshot.fetched_at)).all()[-1]
    assert snapshot.status in {"ok", "partial"}
    # The overview picks it up: Analytics metrics from the new snapshot, Avg Views still the live counters.
    overview = client.get("/api/videos/performance", params={"scope": "all"}).json()
    m = overview["metrics"]
    assert overview["video_count"] == 9 and overview["sources"] == {"youtube_data_api_videos_list": 9, "youtube_analytics_api": 9}
    assert m["avg_views"]["sources"] == {"youtube_data_api_videos_list": 9}
    assert m["avg_view_duration"]["n"] == 9 and m["avg_view_percentage"]["n"] == 9 and m["engaged_view_rate"]["n"] == 9


def test_before_the_snapshot_the_old_video_had_no_analytics_metrics(mac):
    _db, _fake, client, _rows = mac
    m = client.get("/api/videos/performance", params={"scope": "all"}).json()["metrics"]
    assert m["avg_view_duration"]["n"] == 8 and m["avg_views"]["n"] == 9  # d9x: live views only


def test_retry_limit_still_applies(mac):
    db, fake, client, rows = mac
    old = rows[OLD]
    for hours in (36, 42, 48):  # three more empty retries in the same bucket: exhausted
        _capture(db, old, "24h", "no_data_yet", old.published_at + timedelta(hours=hours))
    db.commit()
    start = len(fake.calls)
    result = client.post("/api/videos/refresh-recent").json()
    assert _analytics_calls(fake, start) == {}
    assert OLD not in {item["video_id"] for item in result["results"]}


def test_a_second_click_does_not_ask_again(mac):
    _db, fake, client, _rows = mac
    client.post("/api/videos/refresh-recent")
    start = len(fake.calls)
    client.post("/api/videos/refresh-recent")
    assert _analytics_calls(fake, start) == {}  # captured now: not due, no polling


def test_due_videos_beyond_the_bound_wait_for_the_next_click(mac, monkeypatch):
    db, fake, client, rows = mac
    monkeypatch.setattr(analytics, "ANALYTICS_DUE_LIMIT", 1)
    old = rows["older"]
    db.query(YouTubeAnalyticsSnapshot).filter(YouTubeAnalyticsSnapshot.upload_id == old.id).delete()
    db.commit()  # "older" is now due too (never captured)
    start = len(fake.calls)
    client.post("/api/videos/refresh-recent")
    first = _analytics_calls(fake, start)
    assert len(first) == 1 and set(first) <= {OLD, "older_not_due"}
    start = len(fake.calls)
    client.post("/api/videos/refresh-recent")
    second = _analytics_calls(fake, start)
    assert len(second) == 1 and set(first) | set(second) == {OLD, "older_not_due"}


def test_newest_sort_is_unchanged(mac):
    _db, _fake, client, _rows = mac
    before = [item["youtube_video_id"] for item in client.get("/api/videos", params={"sort": "newest", "limit": 50}).json()["items"]]
    client.post("/api/videos/refresh-recent")
    after = [item["youtube_video_id"] for item in client.get("/api/videos", params={"sort": "newest", "limit": 50}).json()["items"]]
    assert before == after and before[:3] == ["sched2", "sched1", "sched0"]  # scheduled times are the newest effective dates
