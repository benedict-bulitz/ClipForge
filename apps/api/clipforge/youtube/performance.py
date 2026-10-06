"""Channel performance overview: a read-only aggregate over the one analytics store.

Nothing is stored here.  Every number comes from the existing authorities:

* ``youtube_analytics_snapshots`` / ``youtube_metric_values`` (YouTube
  Analytics API, and manual Studio imports for ``stayed_to_watch``);
* ``youtube_uploads`` (the video, its channel and publication time, and the
  YouTube Data API ``videos.list`` statistics the Video Library shows);
* ``production_fingerprints`` (the real rendered duration).

Authority per metric, not per video:

* the current counters (views, likes, comments) come from ``videos.list``
  (``status.live_stats``, what the Video Library shows); YouTube Analytics
  lags behind them by days, so a processed snapshot must never pull a video's
  views back down (live 971 vs Analytics 339).  Without live statistics the
  snapshot's count is the fallback;
* everything only Analytics has (engaged views, watch time, average view
  duration/percentage, shares, subscribers) comes from the video's *latest*
  snapshot with data, and stays missing until YouTube has processed it;
* a ratio never mixes the two: engagedViews / views, view-weighted durations
  and shares/subscribers per 1k use the snapshot's own views; likes/comments
  per 1k use the live pair when both are live.
Trends compare cohorts at the *same video age* (``learning._snapshot_near_age``)
so a young cohort is not "down" just because it had less time to collect views.

Aggregation rules:

* counts (views, likes, ...): arithmetic mean over the videos that have the
  metric; missing values are excluded, never counted as zero;
* rates: ratio of sums (engaged views / views, likes per 1,000 views, ...);
* average view duration: views-weighted (total watch time / total views);
* average view percentage: watch-time weighted when every contributing video
  has a known duration, otherwise views-weighted (the method is reported);
* every metric reports ``n`` (videos used) and ``missing``.

Swipe-away is never derived: the API has no such metric.  ``stayed_to_watch``
only exists when imported by hand from YouTube Studio.  ``engagedViews / views``
is reported as the *Engaged View Rate*, never as a swipe-away or stayed metric.
The Hook / Retention / Engagement / Conversion diagnosis compares the cohort
with the channel's own other videos (quartiles, ``learning._distribution``);
there are no universal "good Shorts" thresholds.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    ProductionFingerprint,
    YouTubeAnalyticsSnapshot,
    YouTubeMetricValue,
    YouTubeUpload,
)
from .analytics import SOURCE_API, SOURCE_MANUAL
from .connection import get_connection
from .library import _analytics_summaries, library_condition
from .status import LIVE_STATS_SOURCE, live_stats
from .uploads import aware

SCOPES = ("last10", "28d", "90d", "all")
DEFAULT_SCOPE = "last10"
LAST_N = 10
PERIOD_DAYS = {"28d": 28, "90d": 90}
PERFORMANCE_METRICS = (
    "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
    "likes", "comments", "shares", "subscribersGained", "subscribersLost",
)
# What videos.list statistics report (the rest exists only in YouTube Analytics).
LIVE_STATS_METRICS = ("views", "likes", "comments")
MIN_TREND_VIDEOS = 3
TREND_FLAT = 0.03
MAX_TREND_AGE_HOURS = 168.0
MIN_TREND_AGE_HOURS = 1.0
MIN_DIAGNOSIS_VIDEOS = 3
SWIPE_AWAY_REASON = (
    "YouTube's API has no swipe-away or stayed-to-watch metric; ClipForge never derives one. "
    "Stayed to watch appears only when imported from YouTube Studio."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass
class VideoRow:
    """One eligible video: its values from one snapshot (latest, or age-matched)."""

    upload_id: str
    published_at: datetime
    duration: float | None
    values: dict[str, float | None] = field(default_factory=dict)  # one Analytics snapshot
    stayed_to_watch: float | None = None
    live: dict[str, float | None] | None = None  # videos.list counters (views/likes/comments)

    def current(self, name: str) -> float | None:
        """Current value: the live counter when YouTube reports it, else the snapshot's."""
        live = (self.live or {}).get(name)
        return live if live is not None else self.values.get(name)

    def source_of(self, name: str) -> str | None:
        if (self.live or {}).get(name) is not None:
            return LIVE_STATS_SOURCE
        return SOURCE_API if self.values.get(name) is not None else None

    def pair(self, numerator: str, denominator: str) -> tuple[float | None, float | None, str]:
        """(numerator, denominator) from one source: live when both are live, else the snapshot."""
        live = self.live or {}
        if live.get(numerator) is not None and live.get(denominator) is not None:
            return live[numerator], live[denominator], LIVE_STATS_SOURCE
        return self.values.get(numerator), self.values.get(denominator), SOURCE_API


# ---------------------------------------------------------------------------
# Aggregation (pure; unit-tested directly)
# ---------------------------------------------------------------------------


def _result(value: float | None, n: int, total: int, **extra: Any) -> dict[str, Any]:
    return {"value": None if value is None else round(value, 4), "n": n, "missing": total - n, **extra}


def _sources(used: list[str]) -> dict[str, int]:
    """How many videos contributed from each source (only sources actually used)."""
    return {source: used.count(source) for source in (LIVE_STATS_SOURCE, SOURCE_API) if source in used}


def mean_of(rows: list[VideoRow], name: str) -> dict[str, Any]:
    """Arithmetic mean per video of the current value; missing values are excluded (not zero)."""
    usable = [row for row in rows if row.current(name) is not None]
    values = [row.current(name) for row in usable]
    value = sum(values) / len(values) if values else None  # type: ignore[arg-type]
    return _result(value, len(values), len(rows), sources=_sources([row.source_of(name) for row in usable]))  # type: ignore[misc]


def net_subscribers(rows: list[VideoRow]) -> dict[str, Any]:
    values = [
        row.values["subscribersGained"] - row.values["subscribersLost"]  # type: ignore[operator]
        for row in rows
        if row.values.get("subscribersGained") is not None and row.values.get("subscribersLost") is not None
    ]
    return _result(sum(values) / len(values) if values else None, len(values), len(rows))


def ratio_of_sums(rows: list[VideoRow], numerator: str, denominator: str = "views", scale: float = 1.0) -> dict[str, Any]:
    """sum(numerator) / sum(denominator) over videos that have both (denominator > 0),
    each video's pair from one source (``VideoRow.pair``)."""
    pairs = [
        (num, den, source)
        for num, den, source in (row.pair(numerator, denominator) for row in rows)
        if num is not None and (den or 0) > 0
    ]
    total = sum(den for _num, den, _source in pairs)  # type: ignore[misc]
    value = sum(num for num, _den, _source in pairs) / total * scale if pairs and total else None  # type: ignore[misc]
    return _result(value, len(pairs), len(rows), sources=_sources([source for _num, _den, source in pairs]))


def weighted_view_duration(rows: list[VideoRow]) -> dict[str, Any]:
    """Channel average view duration = total watch time / total views (views-weighted)."""
    pairs = [
        (row.values["averageViewDuration"], row.values["views"])
        for row in rows
        if row.values.get("averageViewDuration") is not None and (row.values.get("views") or 0) > 0
    ]
    total = sum(views for _avd, views in pairs)
    value = sum(avd * views for avd, views in pairs) / total if pairs and total else None  # type: ignore[operator]
    return _result(value, len(pairs), len(rows), method="views_weighted", sources=_sources([SOURCE_API] * len(pairs)))


def weighted_view_percentage(rows: list[VideoRow]) -> dict[str, Any]:
    """Channel average view percentage = total watch time / (views x duration).

    That is the watch-time weighted mean; it needs every contributing video's
    duration.  Otherwise the views-weighted mean is used (and reported).
    """
    usable = [
        row for row in rows
        if row.values.get("averageViewPercentage") is not None and (row.values.get("views") or 0) > 0
    ]
    if not usable:
        return _result(None, 0, len(rows), method=None, sources={})
    with_duration = all(row.duration and row.duration > 0 for row in usable)
    weights = [(row.values["views"] or 0) * ((row.duration or 0) if with_duration else 1.0) for row in usable]
    value = sum(row.values["averageViewPercentage"] * weight for row, weight in zip(usable, weights, strict=True)) / sum(weights)  # type: ignore[operator]
    return _result(value, len(usable), len(rows), method="watch_time_weighted" if with_duration else "views_weighted", sources=_sources([SOURCE_API] * len(usable)))


def average_length(rows: list[VideoRow]) -> dict[str, Any]:
    values = [row.duration for row in rows if row.duration is not None and row.duration > 0]
    return _result(sum(values) / len(values) if values else None, len(values), len(rows), source="rendered_video_duration")


def stayed_to_watch(rows: list[VideoRow]) -> dict[str, Any]:
    """Only real Studio imports; views-weighted when views are known for all of them."""
    usable = [row for row in rows if row.stayed_to_watch is not None]
    if not usable:
        return _result(None, 0, len(rows), source="manual_studio_import", available=False)
    weights = [row.current("views") or 0 for row in usable]
    if all(weight > 0 for weight in weights):
        value = sum(row.stayed_to_watch * weight for row, weight in zip(usable, weights, strict=True)) / sum(weights)  # type: ignore[operator]
    else:
        value = sum(row.stayed_to_watch for row in usable) / len(usable)  # type: ignore[misc]
    return _result(value, len(usable), len(rows), source="manual_studio_import", available=True)


def _interactions(row: VideoRow) -> float | None:
    parts = [row.values.get(name) for name in ("likes", "comments", "shares")]
    return None if any(part is None for part in parts) else sum(parts)  # type: ignore[arg-type]


def interactions_per_1k_views(rows: list[VideoRow]) -> dict[str, Any]:
    """(likes + comments + shares) per 1,000 views, ratio of sums."""
    pairs = [(_interactions(row), row.values.get("views")) for row in rows]
    usable = [(top, views) for top, views in pairs if top is not None and (views or 0) > 0]
    total = sum(views for _top, views in usable)  # type: ignore[misc]
    value = sum(top for top, _views in usable) / total * 1000 if usable and total else None  # type: ignore[misc]
    return _result(value, len(usable), len(rows))


METRICS: dict[str, Callable[[list[VideoRow]], dict[str, Any]]] = {
    # primary
    "avg_views": lambda rows: mean_of(rows, "views"),
    "avg_view_duration": weighted_view_duration,
    "avg_view_percentage": weighted_view_percentage,
    "engaged_view_rate": lambda rows: ratio_of_sums(rows, "engagedViews"),
    "stayed_to_watch": stayed_to_watch,
    "avg_video_length": average_length,
    # secondary
    "avg_likes": lambda rows: mean_of(rows, "likes"),
    "avg_comments": lambda rows: mean_of(rows, "comments"),
    "avg_shares": lambda rows: mean_of(rows, "shares"),
    "avg_subscribers_gained": lambda rows: mean_of(rows, "subscribersGained"),
    "avg_subscribers_lost": lambda rows: mean_of(rows, "subscribersLost"),
    "avg_net_subscribers": net_subscribers,
    "avg_watch_time_minutes": lambda rows: mean_of(rows, "estimatedMinutesWatched"),
    "likes_per_1k_views": lambda rows: ratio_of_sums(rows, "likes", scale=1000),
    "comments_per_1k_views": lambda rows: ratio_of_sums(rows, "comments", scale=1000),
    "shares_per_1k_views": lambda rows: ratio_of_sums(rows, "shares", scale=1000),
    "subscribers_per_1k_views": lambda rows: ratio_of_sums(rows, "subscribersGained", scale=1000),
}
PRIMARY = ("avg_views", "avg_view_duration", "avg_view_percentage", "engaged_view_rate", "avg_video_length")
SECONDARY = (
    "avg_likes", "avg_comments", "avg_shares", "avg_subscribers_gained", "avg_subscribers_lost",
    "avg_net_subscribers", "avg_watch_time_minutes", "likes_per_1k_views", "comments_per_1k_views",
    "shares_per_1k_views", "subscribers_per_1k_views",
)


def aggregate(rows: list[VideoRow]) -> dict[str, dict[str, Any]]:
    return {name: compute(rows) for name, compute in METRICS.items()}


# ---------------------------------------------------------------------------
# Trend (same-age comparison with the immediately preceding cohort)
# ---------------------------------------------------------------------------


def trend(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any] | None:
    """Relative change, or None when either side is too small or zero."""
    if current["value"] is None or previous["value"] is None:
        return None
    if current["n"] < MIN_TREND_VIDEOS or previous["n"] < MIN_TREND_VIDEOS or previous["value"] == 0:
        return None
    change = (current["value"] - previous["value"]) / abs(previous["value"])
    direction = "flat" if abs(change) < TREND_FLAT else "up" if change > 0 else "down"
    return {"change": round(change, 4), "direction": direction, "n_current": current["n"], "n_previous": previous["n"]}


# ---------------------------------------------------------------------------
# Diagnosis against the channel's own baseline
# ---------------------------------------------------------------------------

# signal -> (cohort aggregate, per-video baseline value, label of the evidence)
def _per_video(numerator: Callable[[VideoRow], float | None], scale: float = 1.0) -> Callable[[VideoRow], float | None]:
    def value(row: VideoRow) -> float | None:
        top, views = numerator(row), row.values.get("views")
        return top / views * scale if top is not None and views else None
    return value


def _hook_signal(rows: list[VideoRow]) -> tuple[str, Callable[[list[VideoRow]], dict[str, Any]], Callable[[VideoRow], float | None]]:
    """Real Studio "stayed to watch" when enough videos have it; otherwise the
    explicitly labelled engaged-view signal."""
    if sum(1 for row in rows if row.stayed_to_watch is not None) >= MIN_DIAGNOSIS_VIDEOS:
        return "stayed_to_watch", stayed_to_watch, lambda row: row.stayed_to_watch
    return "engaged_view_rate", METRICS["engaged_view_rate"], _per_video(lambda row: row.values.get("engagedViews"))


SIGNALS: dict[str, tuple[str, Callable[[list[VideoRow]], dict[str, Any]], Callable[[VideoRow], float | None]]] = {
    "retention": ("avg_view_percentage", weighted_view_percentage, lambda row: row.values.get("averageViewPercentage")),
    "engagement": ("interactions_per_1k_views", interactions_per_1k_views, _per_video(_interactions, 1000)),
    "conversion": ("subscribers_per_1k_views", METRICS["subscribers_per_1k_views"], _per_video(lambda row: row.values.get("subscribersGained"), 1000)),
}


def diagnose(cohort: list[VideoRow], baseline: list[VideoRow], *, min_sample: int) -> dict[str, Any]:
    """weaker | normal | stronger vs the interquartile range of the channel's
    other videos; ``insufficient_data`` when either side is too small."""
    from .learning import _distribution

    result: dict[str, Any] = {}
    hook = _hook_signal([*cohort, *baseline])
    for name, (evidence, aggregate_fn, per_video) in {"hook": hook, **SIGNALS}.items():
        value = aggregate_fn(cohort)
        base_values = [item for item in (per_video(row) for row in baseline) if item is not None]
        dist = _distribution(base_values)
        entry: dict[str, Any] = {"evidence": evidence, "value": value["value"], "n": value["n"], "baseline_n": dist["n"]}
        if value["value"] is None or value["n"] < MIN_DIAGNOSIS_VIDEOS or dist["n"] < min_sample:
            entry["status"] = "insufficient_data"
        else:
            entry.update(baseline_median=dist["median"], baseline_p25=dist["p25"], baseline_p75=dist["p75"])
            entry["status"] = "weaker" if value["value"] < dist["p25"] else "stronger" if value["value"] > dist["p75"] else "normal"
        result[name] = entry
    return result


MAIN_SIGNALS = (
    ("hook_bottleneck", "Opening/Hook may be the bottleneck", lambda d: d["hook"]["status"] == "weaker" and d["retention"]["status"] in {"normal", "stronger"}),
    ("retention_weaker", "Retention weaker than channel baseline", lambda d: d["retention"]["status"] == "weaker"),
    ("engagement_weaker", "Engagement weaker than expected", lambda d: d["engagement"]["status"] == "weaker" and d["retention"]["status"] != "weaker"),
    ("conversion_weaker", "Subscriber conversion below baseline", lambda d: d["conversion"]["status"] == "weaker" and d["retention"]["status"] != "weaker"),
)


def main_signal(diagnosis: dict[str, Any]) -> dict[str, str] | None:
    """At most one short message; None when nothing stands out."""
    for code, message, applies in MAIN_SIGNALS:
        if applies(diagnosis):
            return {"code": code, "message": message}
    return None


# ---------------------------------------------------------------------------
# Loading from the existing store
# ---------------------------------------------------------------------------


def _eligible_uploads(db: Session, channel_id: str | None) -> list[YouTubeUpload]:
    statement = select(YouTubeUpload).where(library_condition(), YouTubeUpload.published_at.is_not(None))
    if channel_id:
        statement = statement.where(YouTubeUpload.channel_id == channel_id)
    return list(db.scalars(statement).all())


def _durations(db: Session, fingerprint_ids: Iterable[str]) -> dict[str, float | None]:
    ids = [item for item in fingerprint_ids if item]
    if not ids:
        return {}
    doc = ProductionFingerprint.fingerprint
    rows = db.execute(
        select(ProductionFingerprint.id, doc[("content", "duration_seconds")].as_float()).where(ProductionFingerprint.id.in_(ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def _stayed_to_watch(db: Session, upload_ids: list[str]) -> dict[str, float]:
    """Latest manual Studio import of stayed_to_watch per upload (real values only)."""
    if not upload_ids:
        return {}
    rows = db.execute(
        select(YouTubeAnalyticsSnapshot.upload_id, YouTubeMetricValue.value, YouTubeMetricValue.fetched_at)
        .join(YouTubeMetricValue, YouTubeMetricValue.snapshot_id == YouTubeAnalyticsSnapshot.id)
        .where(
            YouTubeAnalyticsSnapshot.upload_id.in_(upload_ids),
            YouTubeAnalyticsSnapshot.source == SOURCE_MANUAL,
            YouTubeMetricValue.name == "stayed_to_watch",
            YouTubeMetricValue.availability == "available",
            YouTubeMetricValue.value.is_not(None),
        )
        .order_by(YouTubeMetricValue.fetched_at)
    ).all()
    return {row.upload_id: float(row.value) for row in rows}  # later rows win


def _row(upload: YouTubeUpload, summary: Any, duration: float | None) -> tuple[VideoRow, datetime | None] | None:
    """The video's latest analytics snapshot with data and its live videos.list
    counters, side by side; None when YouTube reported neither."""
    metrics = (summary.metrics or {}) if summary is not None else {}
    has_analytics = any(value is not None for value in metrics.values())
    stats = live_stats(upload)
    if not has_analytics and stats is None:
        return None  # nothing reported yet: not part of any cohort (never a zero)
    values: dict[str, float | None] = {name: metrics.get(name) for name in PERFORMANCE_METRICS}
    live = {name: None if stats[name] is None else float(stats[name]) for name in LIVE_STATS_METRICS} if stats else None
    stamps = [stamp for stamp in (_utc(summary.fetched_at) if has_analytics else None, stats["checked_at"] if stats else None) if stamp]
    return VideoRow(upload.id, _utc(upload.published_at), duration, values, live=live), max(stamps) if stamps else None  # type: ignore[arg-type]


def _select(rows: list[VideoRow], scope: str, now: datetime) -> tuple[list[VideoRow], list[VideoRow]]:
    """(cohort, immediately preceding comparable cohort); newest first."""
    ordered = sorted(rows, key=lambda row: (row.published_at, row.upload_id), reverse=True)
    if scope == "last10":
        return ordered[:LAST_N], ordered[LAST_N:LAST_N * 2]
    if scope in PERIOD_DAYS:
        span = timedelta(days=PERIOD_DAYS[scope])
        current = [row for row in ordered if now - span <= row.published_at <= now]
        previous = [row for row in ordered if now - 2 * span <= row.published_at < now - span]
        return current, previous
    return ordered, []


def _age_matched(db: Session, current: list[VideoRow], previous: list[VideoRow]) -> tuple[list[VideoRow], list[VideoRow], float | None]:
    """Both cohorts at one common video age (<= 7 days), from the stored snapshot history."""
    from .learning import _snapshot_near_age

    ids = [row.upload_id for row in (*current, *previous)]
    if not current or not previous:
        return [], [], None
    snapshots: dict[str, list[YouTubeAnalyticsSnapshot]] = {upload_id: [] for upload_id in ids}
    for snapshot in db.scalars(
        select(YouTubeAnalyticsSnapshot)
        .options(selectinload(YouTubeAnalyticsSnapshot.metrics))
        .where(
            YouTubeAnalyticsSnapshot.upload_id.in_(ids),
            YouTubeAnalyticsSnapshot.source == SOURCE_API,
            YouTubeAnalyticsSnapshot.status.in_(("ok", "partial")),
            YouTubeAnalyticsSnapshot.published_age_hours.is_not(None),
        )
        .order_by(YouTubeAnalyticsSnapshot.fetched_at)
    ).all():
        snapshots[snapshot.upload_id].append(snapshot)
    oldest = [max((item.published_age_hours or 0) for item in items) for items in snapshots.values() if items]
    if not oldest:
        return [], [], None
    age = min(MAX_TREND_AGE_HOURS, min(oldest))
    if age < MIN_TREND_AGE_HOURS:
        return [], [], None

    def at_age(rows: list[VideoRow]) -> list[VideoRow]:
        result = []
        for row in rows:
            snapshot = _snapshot_near_age(snapshots.get(row.upload_id, []), age)
            if snapshot is None:
                continue
            values = {item.name: item.value for item in snapshot.metrics if item.availability == "available"}
            result.append(VideoRow(row.upload_id, row.published_at, row.duration, {name: values.get(name) for name in PERFORMANCE_METRICS}))
        return result

    return at_age(current), at_age(previous), age


def performance_overview(
    db: Session, *, scope: str = DEFAULT_SCOPE, min_sample: int = 5, now: datetime | None = None, account_id: str | None = None
) -> dict[str, Any]:
    now = now or _now()
    scope = scope if scope in SCOPES else DEFAULT_SCOPE
    # One channel's cohort: the chosen YouTube account, else the default channel.
    connection = get_connection(db, account_id)
    channel_id = connection.channel_id if connection else None
    uploads = _eligible_uploads(db, channel_id)
    summaries = _analytics_summaries(db, None, PERFORMANCE_METRICS)
    durations = _durations(db, (upload.fingerprint_id for upload in uploads if upload.fingerprint_id))
    rows: list[VideoRow] = []
    fetched: dict[str, datetime] = {}
    for upload in uploads:
        loaded = _row(upload, summaries.get(upload.id), durations.get(upload.fingerprint_id or ""))
        if loaded is None:
            continue
        rows.append(loaded[0])
        if loaded[1] is not None:
            fetched[upload.id] = loaded[1]
    stayed = _stayed_to_watch(db, [row.upload_id for row in rows])
    for row in rows:
        row.stayed_to_watch = stayed.get(row.upload_id)

    cohort, previous = _select(rows, scope, now)
    metrics = aggregate(cohort)
    trend_current, trend_previous, trend_age = _age_matched(db, cohort, previous)
    trends: dict[str, Any] = {}
    if trend_current and trend_previous:
        now_values, before_values = aggregate(trend_current), aggregate(trend_previous)
        for name in METRICS:
            if name == "avg_video_length":
                continue  # a production property, not a performance trend
            change = trend(now_values[name], before_values[name])
            if change is not None:
                trends[name] = {**change, "age_hours": round(trend_age or 0, 1)}
    cohort_ids = {row.upload_id for row in cohort}
    baseline = [row for row in rows if row.upload_id not in cohort_ids]
    diagnosis = diagnose(cohort, baseline, min_sample=min_sample) if cohort else {}
    updated = [fetched[row.upload_id] for row in cohort if row.upload_id in fetched]
    return {
        "scope": scope,
        "scopes": list(SCOPES),
        "channel": {"id": channel_id, "title": connection.channel_title if connection else None, "account_id": connection.id if connection else None},
        "video_count": len(cohort),
        "eligible_total": len(rows),
        "previous_count": len(previous),
        "updated_at": aware(max(updated)) if updated else None,
        "value_basis": "latest_snapshot_per_video",
        # Videos that have each source (they overlap): live counters, processed Analytics.
        "sources": {
            LIVE_STATS_SOURCE: sum(1 for row in cohort if row.live is not None),
            SOURCE_API: sum(1 for row in cohort if any(value is not None for value in row.values.values())),
        },
        "primary": list(PRIMARY),
        "secondary": list(SECONDARY),
        "metrics": metrics,
        "trends": trends,
        "trend_age_hours": round(trend_age, 1) if trend_age else None,
        "swipe_away": {"available": False, "reason": SWIPE_AWAY_REASON},
        "diagnosis": diagnosis,
        "diagnosis_baseline": {"videos": len(baseline), "min_sample": min_sample},
        "main_signal": main_signal(diagnosis) if diagnosis else None,
    }
