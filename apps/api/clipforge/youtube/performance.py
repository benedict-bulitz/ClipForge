"""Channel performance overview: a read-only aggregate over the one analytics store.

Nothing is stored here.  Every number comes from the existing authorities:

* ``youtube_analytics_snapshots`` / ``youtube_metric_values`` (YouTube
  Analytics API, and manual Studio imports for ``stayed_to_watch``);
* ``youtube_uploads`` (the video, its channel and publication time);
* ``production_fingerprints`` (the real rendered duration).

Per video, the *latest* snapshot with data is used (lifetime values so far).
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
    YouTubeConnection,
    YouTubeMetricValue,
    YouTubeUpload,
)
from .analytics import SOURCE_API, SOURCE_MANUAL
from .library import _analytics_summaries, library_condition
from .uploads import aware

SCOPES = ("last10", "28d", "90d", "all")
DEFAULT_SCOPE = "last10"
LAST_N = 10
PERIOD_DAYS = {"28d": 28, "90d": 90}
PERFORMANCE_METRICS = (
    "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
    "likes", "comments", "shares", "subscribersGained", "subscribersLost",
)
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
    values: dict[str, float | None] = field(default_factory=dict)
    stayed_to_watch: float | None = None


# ---------------------------------------------------------------------------
# Aggregation (pure; unit-tested directly)
# ---------------------------------------------------------------------------


def _result(value: float | None, n: int, total: int, **extra: Any) -> dict[str, Any]:
    return {"value": None if value is None else round(value, 4), "n": n, "missing": total - n, **extra}


def mean_of(rows: list[VideoRow], name: str) -> dict[str, Any]:
    """Arithmetic mean per video; videos without the metric are excluded (not zero)."""
    values = [row.values[name] for row in rows if row.values.get(name) is not None]
    return _result(sum(values) / len(values) if values else None, len(values), len(rows))  # type: ignore[arg-type]


def net_subscribers(rows: list[VideoRow]) -> dict[str, Any]:
    values = [
        row.values["subscribersGained"] - row.values["subscribersLost"]  # type: ignore[operator]
        for row in rows
        if row.values.get("subscribersGained") is not None and row.values.get("subscribersLost") is not None
    ]
    return _result(sum(values) / len(values) if values else None, len(values), len(rows))


def ratio_of_sums(rows: list[VideoRow], numerator: str, denominator: str = "views", scale: float = 1.0) -> dict[str, Any]:
    """sum(numerator) / sum(denominator) over videos that have both (denominator > 0)."""
    pairs = [
        (row.values[numerator], row.values[denominator])
        for row in rows
        if row.values.get(numerator) is not None and (row.values.get(denominator) or 0) > 0
    ]
    total = sum(den for _num, den in pairs)
    value = sum(num for num, _den in pairs) / total * scale if pairs and total else None  # type: ignore[misc]
    return _result(value, len(pairs), len(rows))


def weighted_view_duration(rows: list[VideoRow]) -> dict[str, Any]:
    """Channel average view duration = total watch time / total views (views-weighted)."""
    pairs = [
        (row.values["averageViewDuration"], row.values["views"])
        for row in rows
        if row.values.get("averageViewDuration") is not None and (row.values.get("views") or 0) > 0
    ]
    total = sum(views for _avd, views in pairs)
    value = sum(avd * views for avd, views in pairs) / total if pairs and total else None  # type: ignore[operator]
    return _result(value, len(pairs), len(rows), method="views_weighted")


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
        return _result(None, 0, len(rows), method=None)
    with_duration = all(row.duration and row.duration > 0 for row in usable)
    weights = [(row.values["views"] or 0) * ((row.duration or 0) if with_duration else 1.0) for row in usable]
    value = sum(row.values["averageViewPercentage"] * weight for row, weight in zip(usable, weights, strict=True)) / sum(weights)  # type: ignore[operator]
    return _result(value, len(usable), len(rows), method="watch_time_weighted" if with_duration else "views_weighted")


def average_length(rows: list[VideoRow]) -> dict[str, Any]:
    values = [row.duration for row in rows if row.duration is not None and row.duration > 0]
    return _result(sum(values) / len(values) if values else None, len(values), len(rows), source="rendered_video_duration")


def stayed_to_watch(rows: list[VideoRow]) -> dict[str, Any]:
    """Only real Studio imports; views-weighted when views are known for all of them."""
    usable = [row for row in rows if row.stayed_to_watch is not None]
    if not usable:
        return _result(None, 0, len(rows), source="manual_studio_import", available=False)
    weights = [row.values.get("views") or 0 for row in usable]
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


def performance_overview(db: Session, *, scope: str = DEFAULT_SCOPE, min_sample: int = 5, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    scope = scope if scope in SCOPES else DEFAULT_SCOPE
    connection = db.get(YouTubeConnection, "primary")
    channel_id = connection.channel_id if connection else None
    uploads = _eligible_uploads(db, channel_id)
    summaries = _analytics_summaries(db, None, PERFORMANCE_METRICS)
    durations = _durations(db, (upload.fingerprint_id for upload in uploads if upload.fingerprint_id))
    rows: list[VideoRow] = []
    fetched: dict[str, datetime] = {}
    for upload in uploads:
        summary = summaries.get(upload.id)
        if summary is None or not summary.metrics:
            continue  # only videos with analytics data
        rows.append(VideoRow(
            upload.id, _utc(upload.published_at),  # type: ignore[arg-type]
            durations.get(upload.fingerprint_id or ""), dict(summary.metrics),
        ))
        if summary.fetched_at is not None:
            fetched[upload.id] = _utc(summary.fetched_at)  # type: ignore[assignment]
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
        "channel": {"id": channel_id, "title": connection.channel_title if connection else None},
        "video_count": len(cohort),
        "eligible_total": len(rows),
        "previous_count": len(previous),
        "updated_at": aware(max(updated)) if updated else None,
        "value_basis": "latest_snapshot_per_video",
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
