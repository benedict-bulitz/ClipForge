"""Channel performance overview: aggregation over the existing analytics store only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from clipforge.config import get_settings
from clipforge.database import Base, get_db
from clipforge.main import app
from clipforge.models import (
    ProductionFingerprint,
    YouTubeAnalyticsSnapshot,
    YouTubeConnection,
    YouTubeMetricValue,
    YouTubeUpload,
)
from clipforge.youtube import performance
from clipforge.youtube.performance import VideoRow

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CHANNEL = "UC_perf_channel"


def row(views=None, *, duration=30.0, stayed=None, **values) -> VideoRow:
    data = {"views": views, **values}
    return VideoRow(f"v{id(data)}", NOW, duration, data, stayed)


# ---------------------------------------------------------------------------
# Aggregation semantics (pure)
# ---------------------------------------------------------------------------


def test_count_averages_exclude_missing_values_instead_of_counting_zero():
    rows = [row(100, likes=10), row(300, likes=None), row(None, likes=30)]
    views = performance.mean_of(rows, "views")
    likes = performance.mean_of(rows, "likes")
    assert views == {"value": 200.0, "n": 2, "missing": 1, "sources": {"youtube_analytics_api": 2}}
    assert likes == {"value": 20.0, "n": 2, "missing": 1, "sources": {"youtube_analytics_api": 2}}
    assert performance.mean_of([row(None)], "views") == {"value": None, "n": 0, "missing": 1, "sources": {}}


def test_view_percentage_is_watch_time_weighted_not_a_blind_mean():
    small = row(50, duration=20.0, averageViewPercentage=90.0, averageViewDuration=18.0)
    big = row(50_000, duration=40.0, averageViewPercentage=40.0, averageViewDuration=16.0)
    result = performance.weighted_view_percentage([small, big])
    # (90*50*20 + 40*50000*40) / (50*20 + 50000*40)
    assert result["value"] == pytest.approx((90 * 50 * 20 + 40 * 50_000 * 40) / (50 * 20 + 50_000 * 40), abs=1e-3)
    assert result["method"] == "watch_time_weighted" and result["n"] == 2
    assert abs(result["value"] - 65.0) > 20  # the naive mean would be 65
    # without a known duration for every video: views-weighted, and it says so
    no_duration = row(50_000, duration=None, averageViewPercentage=40.0)
    mixed = performance.weighted_view_percentage([small, no_duration])
    assert mixed["method"] == "views_weighted"
    assert mixed["value"] == pytest.approx((90 * 50 + 40 * 50_000) / 50_050, abs=1e-3)


def test_view_duration_is_total_watch_time_over_total_views():
    result = performance.weighted_view_duration([row(100, averageViewDuration=30.0), row(900, averageViewDuration=10.0)])
    assert result["value"] == pytest.approx((30 * 100 + 10 * 900) / 1000)
    assert result["method"] == "views_weighted"


def test_rates_are_ratios_of_sums_and_per_1k_is_normalised():
    rows = [row(1000, likes=50, comments=5, shares=2, subscribersGained=4, engagedViews=600),
            row(9000, likes=90, comments=9, shares=18, subscribersGained=9, engagedViews=3000),
            row(500, likes=None, engagedViews=None)]
    assert performance.ratio_of_sums(rows, "likes", scale=1000)["value"] == pytest.approx(140 / 10_000 * 1000)
    assert performance.ratio_of_sums(rows, "likes", scale=1000)["missing"] == 1
    assert performance.ratio_of_sums(rows, "comments", scale=1000)["value"] == pytest.approx(1.4)
    assert performance.ratio_of_sums(rows, "shares", scale=1000)["value"] == pytest.approx(2.0)
    assert performance.ratio_of_sums(rows, "subscribersGained", scale=1000)["value"] == pytest.approx(1.3)
    engaged = performance.ratio_of_sums(rows, "engagedViews")
    assert engaged["value"] == pytest.approx(3600 / 10_000) and engaged["n"] == 2


def test_net_subscribers_is_gained_minus_lost():
    rows = [row(100, subscribersGained=10, subscribersLost=3), row(100, subscribersGained=4, subscribersLost=6), row(100, subscribersGained=5)]
    result = performance.net_subscribers(rows)
    assert result == {"value": 2.5, "n": 2, "missing": 1}  # (7 + -2) / 2


def test_average_length_uses_the_real_rendered_duration():
    result = performance.average_length([row(1, duration=31.5), row(1, duration=58.5), row(1, duration=None)])
    assert result["value"] == 45.0 and result["n"] == 2 and result["missing"] == 1
    assert result["source"] == "rendered_video_duration"


def test_trend_needs_enough_videos_and_a_nonzero_previous_value():
    current = {"value": 112.0, "n": 5, "missing": 0}
    assert performance.trend(current, {"value": 100.0, "n": 5, "missing": 0})["direction"] == "up"
    assert performance.trend({"value": 92.0, "n": 5}, {"value": 100.0, "n": 5})["change"] == pytest.approx(-0.08)
    assert performance.trend({"value": 101.0, "n": 5}, {"value": 100.0, "n": 5})["direction"] == "flat"
    assert performance.trend(current, {"value": 100.0, "n": 2}) is None  # previous cohort too small
    assert performance.trend({"value": 100.0, "n": 2}, {"value": 90.0, "n": 5}) is None
    assert performance.trend(current, {"value": 0.0, "n": 5}) is None
    assert performance.trend(current, {"value": None, "n": 0}) is None


def test_engaged_view_rate_is_never_labelled_swipe_away_and_swipe_away_is_never_derived():
    assert "swipe" not in " ".join(performance.METRICS).casefold()
    assert "engaged_view_rate" in performance.PRIMARY
    stayed = performance.stayed_to_watch([row(100, engagedViews=40)])
    assert stayed["value"] is None and stayed["available"] is False  # engaged views never stand in for it


def test_stayed_to_watch_uses_only_real_imports_views_weighted():
    result = performance.stayed_to_watch([row(100, stayed=80.0), row(300, stayed=60.0), row(1000)])
    assert result["value"] == pytest.approx((80 * 100 + 60 * 300) / 400) and result["n"] == 2 and result["available"]


def _baseline(values: list[float], key: str = "averageViewPercentage") -> list[VideoRow]:
    return [row(1000, **{key: value}) for value in values]


def test_retention_diagnosis_uses_the_channels_own_quartiles_not_global_thresholds():
    # A channel whose videos usually keep ~40%: a 30% cohort is weak *for this channel*.
    baseline = _baseline([38, 40, 41, 42, 44, 45])
    weak = _baseline([30, 31, 29])
    result = performance.diagnose(weak, baseline, min_sample=5)
    assert result["retention"]["status"] == "weaker" and result["retention"]["baseline_n"] == 6
    # The very same 30% is "stronger" on a channel that usually keeps ~20%.
    low_channel = _baseline([18, 19, 20, 21, 22, 20])
    assert performance.diagnose(weak, low_channel, min_sample=5)["retention"]["status"] == "stronger"
    normal = _baseline([40, 42, 41])
    assert performance.diagnose(normal, baseline, min_sample=5)["retention"]["status"] == "normal"


def test_diagnosis_reports_insufficient_data_instead_of_guessing():
    baseline = _baseline([38, 40, 41])  # below min_sample
    result = performance.diagnose(_baseline([30, 31, 29]), baseline, min_sample=5)
    assert {entry["status"] for entry in result.values()} == {"insufficient_data"}
    assert performance.main_signal(result) is None


def test_hook_engagement_and_conversion_signals_and_one_main_signal():
    def video(views, engaged, likes, comments, shares, subs, avp=45.0):
        return row(views, engagedViews=engaged, likes=likes, comments=comments, shares=shares, subscribersGained=subs, averageViewPercentage=avp)

    baseline = [video(1000, 600 + i * 10, 40, 4, 3, 3) for i in range(6)]
    # weak opening (engaged views), normal retention -> hook is the bottleneck
    cohort = [video(1000, 300, 40, 4, 3, 3) for _ in range(3)]
    result = performance.diagnose(cohort, baseline, min_sample=5)
    assert result["hook"]["evidence"] == "engaged_view_rate" and result["hook"]["status"] == "weaker"
    assert result["retention"]["status"] == "normal"
    assert performance.main_signal(result) == {"code": "hook_bottleneck", "message": "Opening/Hook may be the bottleneck"}
    # good retention, weak interactions -> engagement
    cohort = [video(1000, 650, 5, 0, 0, 3) for _ in range(3)]
    result = performance.diagnose(cohort, baseline, min_sample=5)
    assert result["engagement"]["status"] == "weaker"
    assert performance.main_signal(result)["code"] == "engagement_weaker"
    # good retention and engagement, weak subscribers -> conversion
    cohort = [video(1000, 650, 40, 4, 3, 0) for _ in range(3)]
    result = performance.diagnose(cohort, baseline, min_sample=5)
    assert result["conversion"]["status"] == "weaker"
    assert performance.main_signal(result)["code"] == "conversion_weaker"


def test_hook_prefers_real_stayed_to_watch_when_imported():
    baseline = [row(1000, stayed=70.0 + i, engagedViews=600) for i in range(6)]
    cohort = [row(1000, stayed=50.0, engagedViews=600) for _ in range(3)]
    result = performance.diagnose(cohort, baseline, min_sample=5)
    assert result["hook"]["evidence"] == "stayed_to_watch" and result["hook"]["status"] == "weaker"


# ---------------------------------------------------------------------------
# Loading from the store (cohorts, same-age trend, API)
# ---------------------------------------------------------------------------


def add_video(
    db, index: int, *, days_ago: float, duration: float | None = 30.0, snapshots: list[tuple[float, dict[str, float | None]]] | None = None,
    channel: str = CHANNEL, stayed: float | None = None, live: tuple[int, int | None, int | None] | None = None,
    scheduled: bool = False, status: str = "ok",
) -> YouTubeUpload:
    """``live``: videos.list statistics (views, likes, comments) as the status sync stores them."""
    published = NOW - timedelta(days=days_ago)
    fingerprint = ProductionFingerprint(project_id=f"p{index}", render_revision=1, render_sha256=f"{index:064d}", fingerprint={"content": {"duration_seconds": duration}})
    db.add(fingerprint)
    db.flush()
    upload = YouTubeUpload(
        project_id=f"p{index}", project_revision=1, render_revision=1, render_sha256=f"{index:064d}", render_file_size=10,
        channel_id=channel, youtube_video_id=f"vid{index:05d}", state="ready", upload_status="processed",
        remote_privacy_status="private" if scheduled else "public", published_at=None if scheduled else published,
        publish_at=NOW + timedelta(days=2) if scheduled else None, remote_publish_at=NOW + timedelta(days=2) if scheduled else None,
        fingerprint_id=fingerprint.id, title=f"Video {index}",
    )
    if live is not None:
        upload.remote_view_count, upload.remote_like_count, upload.remote_comment_count = live
        upload.remote_status_checked_at = NOW - timedelta(minutes=5)
    db.add(upload)
    db.flush()
    for age, metrics in snapshots or []:
        snapshot = YouTubeAnalyticsSnapshot(
            upload_id=upload.id, youtube_video_id=upload.youtube_video_id, channel_id=channel, project_id=upload.project_id,
            project_revision=1, render_revision=1, source="youtube_analytics_api", age_bucket="manual",
            published_age_hours=age, status=status, fetched_at=published + timedelta(hours=age),
        )
        for name, value in metrics.items():
            snapshot.metrics.append(YouTubeMetricValue(
                youtube_video_id=upload.youtube_video_id, name=name, value=value,
                availability="available" if value is not None else "unavailable", source="youtube_analytics_api",
                fetched_at=snapshot.fetched_at,
            ))
        db.add(snapshot)
    if stayed is not None:
        manual = YouTubeAnalyticsSnapshot(
            upload_id=upload.id, youtube_video_id=upload.youtube_video_id, channel_id=channel, project_id=upload.project_id,
            project_revision=1, render_revision=1, source="manual_studio_import", age_bucket="manual", status="ok", fetched_at=NOW,
        )
        manual.metrics.append(YouTubeMetricValue(youtube_video_id=upload.youtube_video_id, name="stayed_to_watch", value=stayed, availability="available", source="manual_studio_import", fetched_at=NOW))
        db.add(manual)
    db.commit()
    return upload


def add_snapshot(db, upload: YouTubeUpload, age: float, values: dict[str, float | None]) -> None:
    snapshot = YouTubeAnalyticsSnapshot(
        upload_id=upload.id, youtube_video_id=upload.youtube_video_id, channel_id=upload.channel_id, project_id=upload.project_id,
        project_revision=1, render_revision=1, source="youtube_analytics_api", age_bucket="72h",
        published_age_hours=age, status="ok", fetched_at=NOW - timedelta(minutes=1),
    )
    for name, value in values.items():
        snapshot.metrics.append(YouTubeMetricValue(
            youtube_video_id=upload.youtube_video_id, name=name, value=value, availability="available",
            source="youtube_analytics_api", fetched_at=snapshot.fetched_at,
        ))
    db.add(snapshot)
    db.commit()


def metrics(views, **extra):
    base = {"views": views, "engagedViews": views * 0.6, "averageViewPercentage": 50.0, "averageViewDuration": 15.0,
            "likes": views * 0.04, "comments": views * 0.004, "shares": views * 0.003,
            "subscribersGained": views * 0.002, "subscribersLost": views * 0.0005, "estimatedMinutesWatched": views * 0.25}
    base.update(extra)
    return base


@pytest.fixture()
def connected(db):
    db.add(YouTubeConnection(slot="primary", channel_id=CHANNEL, channel_title="Knowledge Lab"))
    db.commit()
    return db


def test_scope_selects_one_cohort_for_every_metric(connected):
    db = connected
    for index in range(25):
        add_video(db, index, days_ago=index * 5 + 1, duration=20.0 + index, snapshots=[(200.0, metrics(1000 + index * 100))])
    last10 = performance.performance_overview(db, scope="last10", now=NOW)
    assert last10["video_count"] == 10 and last10["previous_count"] == 10 and last10["eligible_total"] == 25
    assert last10["metrics"]["avg_views"]["value"] == pytest.approx(sum(1000 + i * 100 for i in range(10)) / 10)
    assert last10["metrics"]["avg_video_length"]["value"] == pytest.approx(sum(20.0 + i for i in range(10)) / 10)
    assert all(entry["n"] + entry["missing"] == 10 for name, entry in last10["metrics"].items() if name != "stayed_to_watch")
    days28 = performance.performance_overview(db, scope="28d", now=NOW)
    in_28 = [i for i in range(25) if i * 5 + 1 <= 28]
    assert days28["video_count"] == len(in_28)
    assert days28["metrics"]["avg_views"]["value"] == pytest.approx(sum(1000 + i * 100 for i in in_28) / len(in_28))
    everything = performance.performance_overview(db, scope="all", now=NOW)
    assert everything["video_count"] == 25 and everything["trends"] == {} and everything["previous_count"] == 0
    assert performance.performance_overview(db, scope="bogus", now=NOW)["scope"] == "last10"


def test_videos_without_analytics_and_other_channels_are_not_counted(connected):
    db = connected
    add_video(db, 1, days_ago=1, snapshots=[(24.0, metrics(1000))])
    add_video(db, 2, days_ago=2, snapshots=[])  # published, no analytics yet
    add_video(db, 3, days_ago=3, snapshots=[(24.0, metrics(5000))], channel="UC_other")
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["video_count"] == 1 and result["metrics"]["avg_views"]["value"] == 1000


def test_trend_compares_the_previous_cohort_at_the_same_video_age(connected):
    db = connected
    # previous 10: old videos; their 72h snapshot had 1000 views, lifetime now 9000
    for index in range(10, 20):
        add_video(db, index, days_ago=40 + index, snapshots=[(72.0, metrics(1000)), (900.0, metrics(9000))])
    # latest 10: only 3 days old, 1200 views at 72h
    for index in range(10):
        add_video(db, index, days_ago=3.5 + index * 0.01, snapshots=[(72.0, metrics(1200))])
    result = performance.performance_overview(db, scope="last10", now=NOW)
    # lifetime comparison would say -87%; same-age comparison says +20%
    assert result["trend_age_hours"] == 72.0
    views = result["trends"]["avg_views"]
    assert views["direction"] == "up" and views["change"] == pytest.approx(0.2) and views["n_previous"] == 10
    assert "avg_video_length" not in result["trends"]


def test_no_trend_when_the_preceding_cohort_is_too_small(connected):
    db = connected
    for index in range(12):
        add_video(db, index, days_ago=index + 1, snapshots=[(24.0, metrics(1000 + index))])
    result = performance.performance_overview(db, scope="last10", now=NOW)
    assert result["previous_count"] == 2 and result["trends"] == {}


def test_overview_diagnosis_uses_the_rest_of_the_channel_as_baseline(connected):
    db = connected
    for index in range(10):
        add_video(db, index, days_ago=index + 1, snapshots=[(200.0, metrics(1000, averageViewPercentage=30.0 + index * 0.1))])
    for index in range(10, 20):
        add_video(db, index, days_ago=index + 30, snapshots=[(200.0, metrics(1000, averageViewPercentage=48.0 + (index % 5)))])
    result = performance.performance_overview(db, scope="last10", now=NOW)
    assert result["diagnosis_baseline"]["videos"] == 10
    assert result["diagnosis"]["retention"]["status"] == "weaker"
    assert result["main_signal"]["code"] == "retention_weaker"
    everything = performance.performance_overview(db, scope="all", now=NOW)
    assert {entry["status"] for entry in everything["diagnosis"].values()} == {"insufficient_data"}


def test_swipe_away_is_reported_unavailable_and_stayed_only_from_studio_imports(connected):
    db = connected
    add_video(db, 1, days_ago=1, snapshots=[(24.0, metrics(1000))])
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["swipe_away"]["available"] is False
    assert result["metrics"]["stayed_to_watch"]["value"] is None
    assert result["metrics"]["engaged_view_rate"]["value"] == pytest.approx(0.6)
    add_video(db, 2, days_ago=2, snapshots=[(24.0, metrics(3000))], stayed=72.0)
    stayed = performance.performance_overview(db, scope="all", now=NOW)["metrics"]["stayed_to_watch"]
    assert stayed["value"] == 72.0 and stayed["n"] == 1 and stayed["source"] == "manual_studio_import"


def test_route_reads_the_store_only_and_writes_nothing(connected):
    db = connected
    for index in range(4):
        add_video(db, index, days_ago=index + 1, snapshots=[(24.0, metrics(1000))])
    counts = lambda: tuple(int(db.scalar(select(func.count()).select_from(model)) or 0) for model in (YouTubeAnalyticsSnapshot, YouTubeMetricValue, YouTubeUpload))
    before = counts()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: get_settings()
    try:
        response = TestClient(app).get("/api/videos/performance", params={"scope": "28d"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "28d" and body["video_count"] == 4 and body["primary"][0] == "avg_views"
    assert counts() == before


def test_no_new_analytics_tables():
    assert not [name for name in Base.metadata.tables if "performance" in name or "overview" in name or "aggregate" in name]


# ---------------------------------------------------------------------------
# Authority per metric: live videos.list counters, Analytics-only metrics from
# the latest snapshot (the Mac: live 971 views while Analytics had 339)
# ---------------------------------------------------------------------------

LIVE = "youtube_data_api_videos_list"
API = "youtube_analytics_api"
PROVEN = {  # d9xX7tiZBBI, exact production Analytics query on the real Mac
    "views": 339, "engagedViews": 163, "estimatedMinutesWatched": 44, "averageViewDuration": 14, "averageViewPercentage": 33.33,
    "likes": 9, "comments": 0, "shares": 0, "subscribersGained": 0, "subscribersLost": 0,
}


def test_proven_case_live_counters_and_analytics_metrics_side_by_side(connected):
    db = connected
    add_video(db, 1, days_ago=3, snapshots=[(72.0, PROVEN)], live=(971, 18, 0))
    result = performance.performance_overview(db, scope="last10", now=NOW)
    m = result["metrics"]
    assert result["video_count"] == 1 and result["sources"] == {LIVE: 1, API: 1}
    # Current counters: videos.list, never the lagging Analytics count.
    assert m["avg_views"]["value"] == 971.0 and m["avg_views"]["sources"] == {LIVE: 1}
    assert m["avg_likes"]["value"] == 18.0 and m["avg_likes"]["sources"] == {LIVE: 1}
    assert m["avg_comments"]["value"] == 0.0  # a real 0 stays 0
    # Analytics-only metrics: the snapshot.
    assert m["avg_view_duration"]["value"] == 14.0 and m["avg_view_percentage"]["value"] == 33.33
    assert m["avg_watch_time_minutes"]["value"] == 44.0 and m["avg_watch_time_minutes"]["sources"] == {API: 1}
    assert m["avg_shares"]["value"] == 0.0 and m["avg_net_subscribers"]["value"] == 0.0
    # engagedViews / views from the SAME snapshot: 163/339, not 163/971.
    assert m["engaged_view_rate"]["value"] == pytest.approx(163 / 339, abs=1e-4) and m["engaged_view_rate"]["sources"] == {API: 1}
    assert m["engaged_view_rate"]["value"] != pytest.approx(163 / 971, abs=1e-3)
    # likes per 1k from one source (live 18/971); shares per 1k from the snapshot's own views.
    assert m["likes_per_1k_views"]["value"] == pytest.approx(18 / 971 * 1000, abs=1e-3) and m["likes_per_1k_views"]["sources"] == {LIVE: 1}
    assert m["shares_per_1k_views"]["value"] == 0.0 and m["shares_per_1k_views"]["sources"] == {API: 1}
    # Unchanged semantics: stayed to watch only from Studio imports, no swipe-away.
    assert m["stayed_to_watch"]["available"] is False and result["swipe_away"]["available"] is False


def test_analytics_arriving_never_pulls_current_views_back(connected):
    db = connected
    upload = add_video(db, 1, days_ago=3, live=(971, 18, 0))
    before = performance.performance_overview(db, scope="all", now=NOW)["metrics"]
    assert before["avg_views"]["value"] == 971.0 and before["avg_view_duration"]["value"] is None
    add_snapshot(db, upload, 72.0, PROVEN)  # YouTube processed part of it
    after = performance.performance_overview(db, scope="all", now=NOW)["metrics"]
    assert after["avg_views"]["value"] == 971.0 and after["avg_likes"]["value"] == 18.0
    assert after["avg_view_duration"]["value"] == 14.0


def test_without_live_statistics_the_snapshot_count_is_the_fallback(connected):
    db = connected
    add_video(db, 1, days_ago=3, snapshots=[(72.0, PROVEN)])  # never synced by videos.list
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["sources"] == {LIVE: 0, API: 1}
    assert result["metrics"]["avg_views"]["value"] == 339.0 and result["metrics"]["avg_views"]["sources"] == {API: 1}
    assert result["metrics"]["likes_per_1k_views"]["value"] == pytest.approx(9 / 339 * 1000, abs=1e-3)


def test_hidden_live_like_count_does_not_mix_sources_in_a_ratio(connected):
    db = connected
    add_video(db, 1, days_ago=3, snapshots=[(72.0, PROVEN)], live=(971, None, 0))  # likes hidden on YouTube
    m = performance.performance_overview(db, scope="all", now=NOW)["metrics"]
    assert m["avg_likes"]["value"] == 9.0 and m["avg_likes"]["sources"] == {API: 1}
    # 9 likes / 339 views (one snapshot), never 9 / 971
    assert m["likes_per_1k_views"]["value"] == pytest.approx(9 / 339 * 1000, abs=1e-3) and m["likes_per_1k_views"]["sources"] == {API: 1}


def test_published_videos_with_only_youtube_statistics_form_a_cohort(connected):
    db = connected
    add_video(db, 1, days_ago=1, live=(956, 15, 2))  # "Analytics: Processing" in the library
    add_video(db, 2, days_ago=2, live=(400, 5, None))
    result = performance.performance_overview(db, scope="last10", now=NOW)
    assert result["video_count"] == 2 and result["eligible_total"] == 2
    assert result["sources"] == {LIVE: 2, API: 0}
    assert result["metrics"]["avg_views"] == {"value": 678.0, "n": 2, "missing": 0, "sources": {LIVE: 2}}
    assert result["metrics"]["avg_likes"]["value"] == 10.0
    assert result["metrics"]["likes_per_1k_views"]["value"] == pytest.approx(20 / 1356 * 1000, abs=1e-3)
    assert result["metrics"]["avg_video_length"]["value"] == 30.0
    # Analytics-only metrics are missing (rendered "—"), never zero, and never derived.
    for name in ("avg_view_duration", "avg_view_percentage", "engaged_view_rate", "avg_shares", "avg_watch_time_minutes", "avg_subscribers_gained"):
        assert result["metrics"][name]["value"] is None and result["metrics"][name]["n"] == 0, name
    assert result["metrics"]["avg_comments"]["value"] == 2.0 and result["metrics"]["avg_comments"]["missing"] == 1  # unknown is not 0
    assert result["metrics"]["stayed_to_watch"]["available"] is False and result["swipe_away"]["available"] is False
    assert result["updated_at"] is not None


def test_partial_analytics_and_statistics_only_videos_share_one_cohort(connected):
    db = connected
    add_video(db, 1, days_ago=1, live=(956, 15, 2))  # analytics still processing
    retention_missing = {**metrics(2000), "averageViewPercentage": None, "averageViewDuration": None, "engagedViews": None}
    add_video(db, 2, days_ago=3, snapshots=[(48.0, retention_missing)], live=(2600, 99, 9), status="partial")
    add_video(db, 3, days_ago=5, snapshots=[(96.0, metrics(1000))], live=(1300, 50, 4))
    result = performance.performance_overview(db, scope="last10", now=NOW)
    assert result["video_count"] == 3 and result["sources"] == {LIVE: 3, API: 2}
    assert result["metrics"]["avg_views"]["value"] == pytest.approx((956 + 2600 + 1300) / 3)  # all live
    # Retention: only the one video that has it; missing values do not empty the panel or count as zero.
    assert result["metrics"]["avg_view_percentage"] == {"value": 50.0, "n": 1, "missing": 2, "method": "watch_time_weighted", "sources": {API: 1}}
    assert result["metrics"]["avg_view_duration"]["value"] == 15.0 and result["metrics"]["avg_view_duration"]["n"] == 1
    # engagedViews / views of that one snapshot (600/1000), not over any live views.
    assert result["metrics"]["engaged_view_rate"]["value"] == pytest.approx(0.6) and result["metrics"]["engaged_view_rate"]["n"] == 1


def test_latest_snapshot_with_data_supplies_the_analytics_metrics(connected):
    db = connected
    add_video(db, 1, days_ago=20, snapshots=[(24.0, metrics(300, averageViewPercentage=70.0)), (72.0, metrics(800, averageViewPercentage=60.0)), (168.0, metrics(1500, averageViewPercentage=55.0))], live=(9999, 1, 1))
    m = performance.performance_overview(db, scope="all", now=NOW)["metrics"]
    assert m["avg_view_percentage"]["value"] == 55.0 and m["avg_watch_time_minutes"]["value"] == 1500 * 0.25
    assert m["avg_views"]["value"] == 9999.0  # the current counter, not the 7d capture


def test_an_empty_analytics_capture_falls_back_to_statistics(connected):
    db = connected
    empty = dict.fromkeys(performance.PERFORMANCE_METRICS)
    add_video(db, 1, days_ago=2, snapshots=[(24.0, empty)], live=(500, 20, 1), status="partial")
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["metrics"]["avg_views"]["value"] == 500.0 and result["sources"] == {LIVE: 1, API: 0}


def test_scheduled_and_never_reported_videos_do_not_dilute_the_averages(connected):
    db = connected
    add_video(db, 1, days_ago=1, live=(1000, 10, 1))
    add_video(db, 2, days_ago=0, scheduled=True, live=(0, 0, 0))  # private placeholder counters
    add_video(db, 3, days_ago=0, scheduled=True)
    add_video(db, 4, days_ago=1)  # published, YouTube reported nothing yet
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["video_count"] == 1 and result["metrics"]["avg_views"]["value"] == 1000.0 and result["metrics"]["avg_views"]["n"] == 1


def test_statistics_only_videos_follow_the_connected_channel(connected):
    db = connected
    add_video(db, 1, days_ago=1, live=(1000, 10, 1))
    add_video(db, 2, days_ago=1, live=(90_000, 900, 90), channel="UC_other")
    result = performance.performance_overview(db, scope="all", now=NOW)
    assert result["channel"]["id"] == CHANNEL and result["video_count"] == 1
    assert result["metrics"]["avg_views"]["value"] == 1000.0


def test_same_age_trend_uses_snapshots_only_never_live_counters(connected):
    db = connected
    for index in range(10, 20):
        add_video(db, index, days_ago=40 + index, snapshots=[(72.0, metrics(1000))], live=(90_000, 1, 1))
    for index in range(10):
        add_video(db, index, days_ago=3.5 + index * 0.01, snapshots=[(72.0, metrics(1200))] if index < 5 else None, live=(5000, 1, 1))
    result = performance.performance_overview(db, scope="last10", now=NOW)
    assert result["video_count"] == 10 and result["sources"] == {LIVE: 10, API: 5}
    # 72h snapshots: +20%; live lifetime counters (5000 vs 90000) would say -94%
    assert result["trends"]["avg_views"]["n_current"] == 5 and result["trends"]["avg_views"]["change"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# End to end through the real routes and the analytics sync (fake Google)
# ---------------------------------------------------------------------------


def test_mac_report_end_to_end_overview_refresh_and_newest_sort(db, tmp_path, monkeypatch):
    from test_youtube_learning_loop import METRICS, connect
    from test_youtube_publishing_v2 import api_client, build_project, generated_thumbnail, upload
    from test_youtube_video_library import OTHER
    from youtube_support import FakeYouTube, analytics_payload, publish_options, youtube_settings

    from clipforge.security.secrets import SecretStore
    from clipforge.youtube import analytics, connection, publishing, uploads

    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    settings, store, fake = youtube_settings(tmp_path), SecretStore(), FakeYouTube()
    connect(db, settings, store, fake)
    published = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    fake.publish(published.youtube_video_id, "2026-09-10T11:00:00Z")
    fake.videos[published.youtube_video_id]["statistics"] = {"viewCount": "956", "likeCount": "15", "commentCount": "2"}
    scheduled = upload(db, build_project(db, settings, project_id=OTHER, content=b"S" * 20_000), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    fake.videos[scheduled.youtube_video_id]["status"].update(publishAt="2026-09-30T18:00:00Z", uploadStatus="processed")
    fake.videos[scheduled.youtube_video_id]["statistics"] = {"viewCount": "0", "likeCount": "0", "commentCount": "0"}
    start = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    clock = {"now": start}
    monkeypatch.setattr(analytics, "_now", lambda: clock["now"])
    client = api_client(db, settings, store, fake)
    try:
        def step(hours: float, data: dict | None) -> dict:
            clock["now"] = start + timedelta(hours=hours)
            fake.analytics_handler = analytics_payload(data)
            return next(item for item in client.post("/api/videos/refresh-recent").json()["results"] if item["upload_id"] == published.id)

        assert step(80, None)["status"] == "no_data_yet"  # 72h capture: YouTube has not processed the data yet
        rows = {item["id"]: item for item in client.get("/api/videos").json()["items"]}
        assert rows[published.id]["live_stats"]["views"] == 956 and rows[published.id]["analytics"]["state"] == "processing"
        newest = [item["id"] for item in client.get("/api/videos", params={"sort": "newest"}).json()["items"]]

        # Opening the overview: one read, no YouTube call, no write; real numbers instead of "no data".
        calls, snapshots = len(fake.calls), db.scalar(select(func.count()).select_from(YouTubeAnalyticsSnapshot))
        overview = client.get("/api/videos/performance").json()
        assert len(fake.calls) == calls and db.scalar(select(func.count()).select_from(YouTubeAnalyticsSnapshot)) == snapshots
        assert overview["video_count"] == 1 and overview["metrics"]["avg_views"]["value"] == 956.0
        assert overview["metrics"]["avg_likes"]["value"] == 15.0 and overview["metrics"]["avg_view_duration"]["value"] is None

        # Before the fix the 72h bucket was used up by the empty capture: "not_due" until 7d, then forever.
        assert step(82, METRICS)["status"] == "not_due"  # retried only after NO_DATA_RETRY_AFTER
        before = len(fake.calls)
        assert step(90, METRICS)["status"] == "ok"
        video_queries = [params for name, params in fake.calls[before:] if name == "analytics" and params["dimensions"] == "video"]
        assert len(video_queries) == 1 and video_queries[0]["metrics"] == ",".join(analytics.VIDEO_METRICS)  # one query, not one per metric
        assert step(95, METRICS)["status"] == "not_due"  # a bucket with data is never fetched twice

        overview = client.get("/api/videos/performance").json()
        assert overview["sources"] == {"youtube_analytics_api": 1, "youtube_data_api_videos_list": 1}
        assert overview["metrics"]["avg_views"]["value"] == 956.0  # live counter, not the Analytics count (5400)
        assert overview["metrics"]["avg_view_duration"]["value"] == METRICS["averageViewDuration"]
        assert overview["metrics"]["avg_view_percentage"]["value"] == METRICS["averageViewPercentage"]
        assert overview["metrics"]["engaged_view_rate"]["value"] == pytest.approx(METRICS["engagedViews"] / METRICS["views"], abs=1e-4)
        assert overview["metrics"]["avg_likes"]["value"] == 15.0
        assert [item["id"] for item in client.get("/api/videos", params={"sort": "newest"}).json()["items"]] == newest
        assert newest == [scheduled.id, published.id]  # effective date: the scheduled time is the newest
    finally:
        app.dependency_overrides.clear()
        connection.reset_youtube_auth_cache()


def test_empty_analytics_captures_are_retried_a_bounded_number_of_times():
    from types import SimpleNamespace

    from clipforge.youtube import analytics

    def snap(bucket, status, hours):
        return SimpleNamespace(source=analytics.SOURCE_API, age_bucket=bucket, status=status, fetched_at=NOW + timedelta(hours=hours))

    assert analytics.due_bucket([], 30.0, NOW) == "24h"
    assert analytics.due_bucket([snap("24h", "ok", 0)], 30.0, NOW + timedelta(hours=30)) is None
    empty = [snap("24h", "no_data_yet", 0)]
    assert analytics.due_bucket(empty, 26.0, NOW + timedelta(hours=2)) is None
    assert analytics.due_bucket(empty, 30.0, NOW + timedelta(hours=6)) == "24h"
    exhausted = [snap("7d", "no_data_yet", hours) for hours in range(0, 6 * (analytics.NO_DATA_RETRIES + 1), 6)]
    assert analytics.due_bucket(exhausted, 400.0, NOW + timedelta(days=30)) is None
    # A new bucket is always due, whatever the earlier empty captures.
    assert analytics.due_bucket([snap("24h", "no_data_yet", 0)], 73.0, NOW + timedelta(hours=1)) == "72h"
