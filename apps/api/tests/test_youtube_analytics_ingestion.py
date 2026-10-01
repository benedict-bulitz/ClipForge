"""YouTube Analytics ingestion: date window, no-data vs error states, and the
read-only live diagnostic (fake Google only)."""
from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from test_youtube_learning_loop import METRICS, connect
from test_youtube_publishing_v2 import build_project, generated_thumbnail, upload
from youtube_support import FakeYouTube, analytics_payload, publish_options, youtube_settings

from clipforge.models import YouTubeAnalyticsSnapshot, YouTubeConnection, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, publishing, uploads
from clipforge.youtube.provider import YouTubeApiError

PACIFIC = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Date window (Analytics API dates are plain calendar days)
# ---------------------------------------------------------------------------


def test_window_is_dates_with_start_never_after_end():
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert analytics.report_window(datetime(2026, 9, 28, 14, 0, tzinfo=UTC), now) == (date(2026, 9, 27), date(2026, 10, 1))
    # published "now" (or a clock a little ahead): still a valid range
    start, end = analytics.report_window(now + timedelta(minutes=5), now)
    assert start <= end


@pytest.mark.parametrize("published_pt", [
    datetime(2026, 9, 28, 23, 30, tzinfo=PACIFIC),  # late evening: already Sep 29 in UTC
    datetime(2026, 9, 28, 0, 15, tzinfo=PACIFIC),   # just after midnight
    datetime(2026, 9, 28, 16, 59, tzinfo=PACIFIC),  # 23:59 UTC
])
def test_window_contains_the_publication_day_in_utc_and_pacific(published_pt):
    published = published_pt.astimezone(UTC)
    for hours in (0.5, 30, 80, 400):
        now = published + timedelta(hours=hours)
        start, end = analytics.report_window(published, now)
        assert start <= published_pt.date() <= end and start <= published.date() <= end


def test_window_accepts_naive_utc_from_sqlite():
    naive = datetime(2026, 9, 28, 23, 30, tzinfo=UTC).replace(tzinfo=None)  # as SQLite returns it
    assert analytics.report_window(naive, datetime(2026, 10, 1, tzinfo=UTC)) == (date(2026, 9, 27), date(2026, 10, 1))


# ---------------------------------------------------------------------------
# "No rows yet" is never the same state as an error or a rejected metric
# ---------------------------------------------------------------------------


def _metrics(availability: str) -> dict:
    return {name: {"value": None, "availability": availability, "reason": None} for name in analytics.VIDEO_METRICS}


def test_snapshot_status_separates_no_data_partial_and_ok():
    assert analytics.snapshot_status(_metrics("no_data_yet"), "not_ready") == "no_data_yet"
    assert analytics.snapshot_status(_metrics("unavailable"), "not_ready") == "partial"
    assert analytics.snapshot_status(_metrics("available"), "ok") == "ok"
    assert analytics.snapshot_status(_metrics("available"), "not_ready") == "ok"
    assert analytics.snapshot_status(_metrics("available"), "unavailable") == "partial"


def test_empty_answer_is_no_data_yet_but_a_rejected_query_is_not():
    calls = []

    class Provider:
        def __init__(self, handler):
            self.handler = handler

        def analytics_report(self, token, params):
            calls.append(params)
            return self.handler(params)

    empty = {"columnHeaders": [{"name": "video"}, {"name": "views"}], "rows": []}
    results, _raw = analytics.fetch_video_metrics(Provider(lambda params: empty), "t", "vid", date(2026, 9, 27), date(2026, 10, 1))
    assert {item["availability"] for item in results.values()} == {"no_data_yet"} and len(calls) == 1

    def rejects_all(params):
        raise YouTubeApiError("bad_request", "The query is not supported.", status_code=400, reason="badRequest")

    results, raw = analytics.fetch_video_metrics(Provider(rejects_all), "t", "vid", date(2026, 9, 27), date(2026, 10, 1))
    assert {item["availability"] for item in results.values()} == {"unavailable"}
    assert {item["reason"] for item in results.values()} == {"rejected_by_api"} and raw["metric:views"]["error"]

    def other_errors(params):
        raise YouTubeApiError("forbidden", "Forbidden", status_code=403, reason="forbidden")

    with pytest.raises(YouTubeApiError):  # refresh_analytics records it as analytics_error_code, never "processing"
        analytics.fetch_video_metrics(Provider(other_errors), "t", "vid", date(2026, 9, 27), date(2026, 10, 1))


def test_fallback_row_without_the_column_is_not_reported_as_no_data_yet():
    def handler(params):
        if "," in params["metrics"]:
            raise YouTubeApiError("bad_request", "Unknown identifier", status_code=400)
        return {"columnHeaders": [{"name": "video"}, {"name": "views"}], "rows": [["vid", 12]]}

    class Provider:
        def analytics_report(self, token, params):
            return handler(params)

    results, _raw = analytics.fetch_video_metrics(Provider(), "t", "vid", date(2026, 9, 27), date(2026, 10, 1))
    assert results["views"] == {"value": 12.0, "availability": "available", "reason": None}
    assert results["likes"] == {"value": None, "availability": "unavailable", "reason": "not_returned_by_api"}


# ---------------------------------------------------------------------------
# --analytics-live: real provider path, read-only
# ---------------------------------------------------------------------------


def _diagnostics():
    path = Path(__file__).resolve().parents[1] / "scripts" / "youtube_diagnostics.py"
    spec = importlib.util.spec_from_file_location("youtube_diagnostics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handler(video_rows: bool, channel_rows: bool = True, reject_combined: bool = False):
    production = analytics_payload(METRICS if video_rows else None)

    def handler(params):
        dimension = params.get("dimensions")
        video = "filters" in params
        if reject_combined and dimension == "video" and "," in params["metrics"]:
            raise YouTubeApiError("bad_request", "The query is not supported.", status_code=400, reason="badRequest")
        if dimension in {"video", "creatorContentType", "elapsedVideoTimeRatio"}:
            return production(params)
        names = params["metrics"].split(",")
        headers = ([{"name": "day"}] if dimension == "day" else []) + [{"name": name} for name in names]
        has = video_rows if video else channel_rows
        row = ([params["startDate"]] if dimension == "day" else []) + [METRICS.get(name, 0) for name in names]
        return {"columnHeaders": headers, "rows": [row] if has else []}

    return handler


@pytest.fixture()
def published_video(db, tmp_path):
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    settings, store, fake = youtube_settings(tmp_path), SecretStore(), FakeYouTube()
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    fake.publish(row.youtube_video_id, "2026-09-28T14:00:00Z")
    uploads.sync_status(db, row, settings, store, fake)
    fake.analytics_handler = analytics_payload(None)
    analytics.refresh_analytics(db, row, settings, store, fake, now=datetime(2026, 9, 29, 15, 0, tzinfo=UTC))  # a 24h capture, empty
    yield db, settings, fake, row
    connection.reset_youtube_auth_cache()


def _run(published_video, monkeypatch, capsys, handler) -> str:
    db, settings, fake, row = published_video
    fake.analytics_handler = handler
    module = _diagnostics()
    monkeypatch.setattr(module, "GoogleYouTubeProvider", lambda: fake)
    counts = lambda: tuple(int(db.scalar(select(func.count()).select_from(model)) or 0) for model in (YouTubeAnalyticsSnapshot, YouTubeUpload, YouTubeConnection))
    type(db).commit(db)  # the fixture's own setup (bypasses a guard left by an earlier run)
    before, attempt = counts(), row.last_analytics_attempt_at
    module._read_only(db)
    module._analytics_live(db, settings, row.youtube_video_id)
    db.rollback()
    assert counts() == before and db.get(YouTubeUpload, row.id).last_analytics_attempt_at == attempt  # nothing written
    return capsys.readouterr().out


def test_live_diagnostic_prints_the_exact_production_query_and_writes_nothing(published_video, monkeypatch, capsys):
    out = _run(published_video, monkeypatch, capsys, _handler(video_rows=False))
    row = published_video[3]
    assert f"VIDEO: {row.youtube_video_id}" in out and "STORED CAPTURES" in out and "video_metrics: headers=" in out
    assert "startDate=" in out and f"filters=video=={row.youtube_video_id}" in out and f"metrics={','.join(analytics.VIDEO_METRICS)}" in out
    assert "dimensions=video" in out and "HTTP 200" in out
    assert "snapshot status: no_data_yet" in out and "channel==MINE resolves to: UC_fake_channel_01" in out
    assert "the channel has Analytics rows, this video has none yet" in out


def test_live_diagnostic_verdicts(published_video, monkeypatch, capsys):
    assert "production query returns data NOW" in _run(published_video, monkeypatch, capsys, _handler(video_rows=True))
    out = _run(published_video, monkeypatch, capsys, _handler(video_rows=False, channel_rows=False))
    assert "NO Analytics rows for the whole channel" in out
    out = _run(published_video, monkeypatch, capsys, _handler(video_rows=True, reject_combined=True))
    assert "production metrics query FAILED: bad_request (HTTP 400)" in out and "B6 production query without engagedViews" in out


def test_live_diagnostic_refuses_writes(published_video):
    db = published_video[0]
    module = _diagnostics()
    db.commit()
    module._read_only(db)
    assert db.scalar(select(func.count()).select_from(YouTubeUpload)) == 1  # reads still work (autoflush)
    db.add(YouTubeConnection(slot="secondary", channel_id="UC_x", channel_title="x"))
    with pytest.raises(RuntimeError, match="read-only"):
        db.scalar(select(func.count()).select_from(YouTubeUpload))  # autoflush of a pending write
    with pytest.raises(RuntimeError, match="read-only"):
        db.commit()
    db.rollback()


def test_performance_trace_names_the_source_per_metric(db, capsys):
    from test_youtube_performance_overview import CHANNEL, add_video

    db.add(YouTubeConnection(slot="primary", channel_id=CHANNEL, channel_title="Knowledge Lab"))
    db.commit()
    add_video(db, 1, days_ago=3, snapshots=[(72.0, {"views": 339, "engagedViews": 163, "averageViewDuration": 14, "averageViewPercentage": 33.33, "likes": 9})], live=(971, 18, 0))
    add_video(db, 2, days_ago=1, live=(500, 5, 1))
    record = db.get(YouTubeConnection, "primary")
    _diagnostics()._performance(db, record, "all", youtube_settings(Path("/tmp")))
    out = capsys.readouterr().out
    assert "'views': 'youtube_data_api_videos_list'" in out and "'averageViewDuration': 'youtube_analytics_api'" in out
    assert "'averageViewDuration': None" in out  # the video Analytics has not processed yet
    assert "video_count=2" in out
