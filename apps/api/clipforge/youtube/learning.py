from __future__ import annotations

"""Derived, read-only learning signals from stored raw analytics.

Nothing here writes to ClipForge's generation rules: hooks, Story Arc, Visual
Director thresholds, pacing and TTS are never modified from performance data.
Everything is derived deterministically from the raw snapshots and the
immutable production fingerprint, so it can be recomputed at any time.

Precision rules: retention is read at YouTube's own buckets (the nearest
``elapsedVideoTimeRatio`` point, 1/100 of the video) - never interpolated -
and every comparison states its sample size.  Language is associative only.
"""

import statistics
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ProductionFingerprint,
    YouTubeAnalyticsSnapshot,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from . import content_type as content_types
from . import status as status_authority
from .analytics import SOURCE_API, SOURCE_MANUAL
from .uploads import aware, lifecycle, serialize_upload

OPENING_SECONDS = (1.0, 2.0, 3.0)
NOTABLE_DROP = 0.10
RELATIVE_DROP_FACTOR = 2.0
MIN_RELATIVE_DROP = 0.05
MIN_GROUP_VIDEOS = 3
MIN_GROUP_SCENES = 8
MEDIUM_CONFIDENCE_VIEWS = 1000
HIGH_CONFIDENCE_VIDEOS = 20
ASSOCIATION_CAVEAT = (
    "Descriptive association across your own ClipForge uploads only; it does not show that a "
    "production choice caused the difference."
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Snapshot access
# ---------------------------------------------------------------------------


def metric_map(snapshot: YouTubeAnalyticsSnapshot | None) -> dict[str, dict[str, Any]]:
    if snapshot is None:
        return {}
    return {
        item.name: {"value": item.value, "availability": item.availability, "reason": item.reason, "source": item.source, "note": item.note}
        for item in snapshot.metrics
    }


def metric_value(snapshot: YouTubeAnalyticsSnapshot | None, name: str) -> float | None:
    item = metric_map(snapshot).get(name)
    return item["value"] if item and item["availability"] == "available" else None


def api_snapshots(db: Session, upload_id: str) -> list[YouTubeAnalyticsSnapshot]:
    return list(db.scalars(
        select(YouTubeAnalyticsSnapshot)
        .where(YouTubeAnalyticsSnapshot.upload_id == upload_id, YouTubeAnalyticsSnapshot.source == SOURCE_API)
        .order_by(YouTubeAnalyticsSnapshot.fetched_at)
    ).all())


def latest_with_data(snapshots: list[YouTubeAnalyticsSnapshot]) -> YouTubeAnalyticsSnapshot | None:
    return next((item for item in reversed(snapshots) if item.status in {"ok", "partial"}), None)


def latest_with_retention(snapshots: list[YouTubeAnalyticsSnapshot]) -> YouTubeAnalyticsSnapshot | None:
    return next((item for item in reversed(snapshots) if item.retention_status == "ok" and item.retention_points), None)


def fingerprint_for(db: Session, upload: YouTubeUpload) -> dict[str, Any]:
    record = db.get(ProductionFingerprint, upload.fingerprint_id) if upload.fingerprint_id else None
    return record.fingerprint if record is not None else {}


# ---------------------------------------------------------------------------
# Retention -> ClipForge timeline
# ---------------------------------------------------------------------------


def _points(points: Iterable[YouTubeRetentionPoint]) -> list[YouTubeRetentionPoint]:
    return sorted((item for item in points if item.audience_watch_ratio is not None), key=lambda item: item.elapsed_video_ratio)


def nearest_point(points: list[YouTubeRetentionPoint], second: float, duration: float) -> YouTubeRetentionPoint | None:
    if not points or duration <= 0:
        return None
    ratio = second / duration
    return min(points, key=lambda item: (abs(item.elapsed_video_ratio - ratio), item.elapsed_video_ratio))


def opening_retention(points: Iterable[YouTubeRetentionPoint], duration: float | None, hook: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audience retention at ~1/2/3 s, read at YouTube's own buckets.

    Not "Stayed to watch" (a Studio-only metric): these are audienceWatchRatio
    values of the retention curve near those seconds.
    """
    ordered = _points(points)
    result: dict[str, Any] = {
        "label": "Opening retention",
        "metric": "audienceWatchRatio",
        "not_stayed_to_watch": True,
        "bucket_seconds": _round(duration / 100, 3) if duration else None,
        "points": [],
        "hook": hook or {},
    }
    if not ordered or not duration:
        result["status"] = "no_data"
        return result
    first = ordered[0]
    result["status"] = "ok"
    result["first_bucket"] = {"second": _round(first.elapsed_video_ratio * duration, 2), "audience_watch_ratio": first.audience_watch_ratio}
    for second in OPENING_SECONDS:
        if second > duration:
            result["points"].append({"target_second": second, "status": "beyond_video"})
            continue
        point = nearest_point(ordered, second, duration)
        assert point is not None
        result["points"].append({
            "target_second": second,
            "bucket_ratio": point.elapsed_video_ratio,
            "bucket_second": _round(point.elapsed_video_ratio * duration, 2),
            "audience_watch_ratio": point.audience_watch_ratio,
            "relative_retention_performance": point.relative_retention_performance,
        })
    return result


def opening_value(opening: dict[str, Any], second: float = 3.0) -> float | None:
    for item in opening.get("points") or []:
        if item.get("target_second") == second:
            return item.get("audience_watch_ratio")
    return None


def map_scenes(points: Iterable[YouTubeRetentionPoint], fingerprint: dict[str, Any]) -> list[dict[str, Any]]:
    """Retention entering/leaving/through each rendered scene."""
    ordered = _points(points)
    duration = float((fingerprint.get("content") or {}).get("duration_seconds") or 0)
    scenes = fingerprint.get("scenes") or []
    if not ordered or duration <= 0 or not scenes:
        return []
    first, last = ordered[0].audience_watch_ratio or 0.0, ordered[-1].audience_watch_ratio or 0.0
    overall_rate = max(0.0, first - last) / duration
    total_stops = sum(item.stopped_watching or 0 for item in ordered)
    has_stops = any(item.stopped_watching is not None for item in ordered)
    rows = []
    for scene in scenes:
        start, end = float(scene.get("start") or 0), float(scene.get("end") or 0)
        entering, leaving = nearest_point(ordered, start, duration), nearest_point(ordered, end, duration)
        inside = [item for item in ordered if start / duration <= item.elapsed_video_ratio <= end / duration]
        row: dict[str, Any] = {
            "index": scene.get("index"),
            "scene_id": scene.get("scene_id"),
            "story_role": scene.get("story_role"),
            "start": start,
            "end": end,
            "duration": _round(end - start, 3),
            "points_in_scene": len(inside),
            "entering_bucket_second": _round(entering.elapsed_video_ratio * duration, 2) if entering else None,
            "leaving_bucket_second": _round(leaving.elapsed_video_ratio * duration, 2) if leaving else None,
        }
        if entering is None or leaving is None or entering is leaving:
            row.update(status="below_bucket_resolution", retention_entering=None, retention_leaving=None, retention_delta=None)
        else:
            delta = (leaving.audience_watch_ratio or 0) - (entering.audience_watch_ratio or 0)
            rate = -delta / max(1e-6, end - start)
            notable = delta <= -NOTABLE_DROP or (
                delta <= -MIN_RELATIVE_DROP and overall_rate > 0 and rate >= RELATIVE_DROP_FACTOR * overall_rate
            )
            row.update(
                status="ok",
                retention_entering=entering.audience_watch_ratio,
                retention_leaving=leaving.audience_watch_ratio,
                retention_delta=_round(delta),
                notable_drop=bool(notable),
            )
        values = [item.audience_watch_ratio for item in inside if item.audience_watch_ratio is not None]
        relative = [item.relative_retention_performance for item in inside if item.relative_retention_performance is not None]
        row["average_retention"] = _round(statistics.fmean(values)) if values else None
        row["average_relative_retention_performance"] = _round(statistics.fmean(relative)) if relative else None
        if has_stops and total_stops > 0 and inside:
            share = sum(item.stopped_watching or 0 for item in inside) / total_stops
            row["stopped_watching_share"] = _round(share)
            # 1.0 = stops spread evenly over the video's buckets.
            row["stopped_watching_index"] = _round(share / (len(inside) / len(ordered)))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Channel baseline and outlier classification
# ---------------------------------------------------------------------------

BASELINE_METRICS = (
    "averageViewPercentage",
    "view_duration_ratio",
    "opening_retention_3s",
    "views",
    "engagedViews",
    "likes_per_engaged_view",
    "comments_per_engaged_view",
    "shares_per_engaged_view",
)
CLASSIFICATION_METRICS = ("averageViewPercentage", "engagedViews")


def _snapshot_near_age(snapshots: list[YouTubeAnalyticsSnapshot], age: float | None) -> YouTubeAnalyticsSnapshot | None:
    """A snapshot of comparable age (within 2x, or both a week or older)."""
    candidates = [item for item in snapshots if item.status in {"ok", "partial"} and item.published_age_hours is not None]
    if not candidates:
        return None
    if age is None:
        return candidates[-1]
    def distance(item: YouTubeAnalyticsSnapshot) -> float:
        other = max(0.05, float(item.published_age_hours or 0))
        if other >= 168 and age >= 168:
            return 0.0
        return abs((other / max(0.05, age)) - 1) if other >= age else abs((age / other) - 1)
    best = min(candidates, key=distance)
    return best if distance(best) <= 1.0 else None


def video_outcomes(snapshot: YouTubeAnalyticsSnapshot | None, fingerprint: dict[str, Any], retention_snapshot: YouTubeAnalyticsSnapshot | None = None) -> dict[str, float | None]:
    duration = float((fingerprint.get("content") or {}).get("duration_seconds") or 0) or None
    engaged = metric_value(snapshot, "engagedViews")
    duration_value = metric_value(snapshot, "averageViewDuration")
    retention = retention_snapshot or snapshot
    opening = opening_retention(retention.retention_points if retention else [], duration)
    def per_engaged(name: str) -> float | None:
        value = metric_value(snapshot, name)
        return value / engaged if value is not None and engaged else None
    return {
        "averageViewPercentage": metric_value(snapshot, "averageViewPercentage"),
        "view_duration_ratio": duration_value / duration if duration_value is not None and duration else None,
        "opening_retention_3s": opening_value(opening),
        "views": metric_value(snapshot, "views"),
        "engagedViews": engaged,
        "likes_per_engaged_view": per_engaged("likes"),
        "comments_per_engaged_view": per_engaged("comments"),
        "shares_per_engaged_view": per_engaged("shares"),
    }


def _distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    if len(ordered) >= 2:
        quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
        p25, p75 = quartiles[0], quartiles[2]
    else:
        p25 = p75 = ordered[0]
    return {
        "n": len(ordered),
        "median": _round(statistics.median(ordered)),
        "p25": _round(p25),
        "p75": _round(p75),
        "min": _round(ordered[0]),
        "max": _round(ordered[-1]),
    }


def eligible_uploads(db: Session, channel_id: str) -> list[YouTubeUpload]:
    """Published, still existing, active ClipForge mappings on this channel."""
    return list(db.scalars(
        select(YouTubeUpload).where(
            YouTubeUpload.channel_id == channel_id,
            YouTubeUpload.youtube_video_id.is_not(None),
            YouTubeUpload.deleted_on_youtube.is_(False),
            YouTubeUpload.published_at.is_not(None),
        )
    ).all())


def channel_baseline(db: Session, channel_id: str, *, min_sample: int, exclude_upload_id: str | None = None, age_hours: float | None = None, content_type: str = content_types.SHORTS) -> dict[str, Any]:
    """Median/IQR of comparable Shorts (YouTube-confirmed content type only)."""
    values: dict[str, list[float]] = {name: [] for name in BASELINE_METRICS}
    included, unconfirmed = 0, 0
    for upload in eligible_uploads(db, channel_id):
        if upload.id == exclude_upload_id:
            continue
        kind = content_types.normalize(upload.content_type)
        if kind != content_types.normalize(content_type):
            unconfirmed += kind is None
            continue
        snapshots = api_snapshots(db, upload.id)
        snapshot = _snapshot_near_age(snapshots, age_hours)
        if snapshot is None:
            continue
        outcomes = video_outcomes(snapshot, fingerprint_for(db, upload), latest_with_retention([item for item in snapshots if item.fetched_at <= snapshot.fetched_at]))
        included += 1
        for name, value in outcomes.items():
            if value is not None:
                values[name].append(value)
    return {
        "content_type": content_types.normalize(content_type),
        "sample_size": included,
        "min_sample": min_sample,
        "sufficient": included >= min_sample,
        "excluded_unconfirmed_content_type": unconfirmed,
        "age_matched_hours": _round(age_hours, 1) if age_hours is not None else None,
        "metrics": {name: _distribution(items) for name, items in values.items()},
    }


def classify(outcomes: dict[str, float | None], baseline: dict[str, Any]) -> dict[str, Any]:
    """insufficient_data | below/near/above_channel_baseline | outlier_positive."""
    if not baseline.get("sufficient"):
        return {"label": "insufficient_data", "reason": f"needs at least {baseline.get('min_sample')} comparable videos, has {baseline.get('sample_size', 0)}", "per_metric": {}}
    per_metric: dict[str, str] = {}
    for name in CLASSIFICATION_METRICS:
        value = outcomes.get(name)
        dist = (baseline.get("metrics") or {}).get(name) or {}
        if value is None or dist.get("n", 0) < int(baseline.get("min_sample") or 0):
            per_metric[name] = "insufficient_data"
            continue
        iqr = (dist["p75"] or 0) - (dist["p25"] or 0)
        if iqr > 0 and value > dist["p75"] + 1.5 * iqr:
            per_metric[name] = "outlier_positive"
        elif value > dist["p75"]:
            per_metric[name] = "above_channel_baseline"
        elif value < dist["p25"]:
            per_metric[name] = "below_channel_baseline"
        else:
            per_metric[name] = "near_channel_baseline"
    labels = [label for label in per_metric.values() if label != "insufficient_data"]
    if not labels:
        label = "insufficient_data"
    elif "outlier_positive" in labels and "below_channel_baseline" not in labels:
        label = "outlier_positive"
    elif all(item in {"above_channel_baseline", "outlier_positive"} for item in labels):
        label = "above_channel_baseline"
    elif all(item == "below_channel_baseline" for item in labels):
        label = "below_channel_baseline"
    else:
        label = "near_channel_baseline"
    return {"label": label, "per_metric": per_metric, "method": "quartiles of your own comparable ClipForge Shorts; Tukey fence (Q3 + 1.5 IQR) for outliers"}


# ---------------------------------------------------------------------------
# Evidence records (observations for a future learner, never rule changes)
# ---------------------------------------------------------------------------


def _confidence(views: float | None, sample_size: int) -> str:
    """A single video is always low confidence as a learning signal."""
    if sample_size >= HIGH_CONFIDENCE_VIDEOS and (views or 0) >= MEDIUM_CONFIDENCE_VIEWS:
        return "high"
    if sample_size >= MIN_GROUP_VIDEOS * 2 and (views or 0) >= MEDIUM_CONFIDENCE_VIEWS:
        return "medium"
    return "low"


def evidence_records(fingerprint: dict[str, Any], scenes: list[dict[str, Any]], opening: dict[str, Any], outcomes: dict[str, float | None], baseline: dict[str, Any] | None, classification: dict[str, Any] | None) -> list[dict[str, Any]]:
    hook = fingerprint.get("hook") or {}
    by_id = {str(row.get("scene_id")): row for row in fingerprint.get("scenes") or []}
    views = outcomes.get("engagedViews") or outcomes.get("views")
    records = []
    for scene in scenes:
        if not scene.get("notable_drop"):
            continue
        production = by_id.get(str(scene.get("scene_id")), {})
        associated = {
            "story_role": production.get("story_role"),
            "scene_seconds": scene.get("duration"),
            "visual_strategy": production.get("visual_strategy"),
            "media_origin": production.get("media_origin"),
            "media_source": production.get("media_source"),
            "overlay_present": bool(production.get("overlay_kinds")),
            "critic_issue_count": production.get("critic_issue_count"),
        }
        if scene.get("index") == 1:
            associated.update(hook_strategy=hook.get("strategy"), on_screen_hook=bool(hook.get("on_screen_hook")))
        records.append({
            "kind": "scene_retention_drop",
            "observation": f"Large retention drop during Scene {scene.get('index')} ({scene.get('retention_entering'):.0%} -> {scene.get('retention_leaving'):.0%}).",
            "associated_production_data": associated,
            "confidence": _confidence(views, 1),
            "sample_size": 1,
            "views_basis": views,
        })
    three = opening_value(opening)
    dist = ((baseline or {}).get("metrics") or {}).get("opening_retention_3s") or {}
    if three is not None and (baseline or {}).get("sufficient") and dist.get("n", 0) >= int((baseline or {}).get("min_sample") or 0):
        position = "below" if three < dist["p25"] else "above" if three > dist["p75"] else "within"
        records.append({
            "kind": "opening_retention_vs_baseline",
            "observation": f"Opening retention at ~3 s ({three:.0%}) was {position} the interquartile range of your comparable Shorts (median {dist['median']:.0%}).",
            "associated_production_data": {
                "hook_strategy": hook.get("strategy"),
                "verbal_hook": hook.get("verbal_hook"),
                "on_screen_hook": hook.get("on_screen_hook"),
                "visual_hook": (hook.get("visual_hook") or {}).get("subject"),
                "first_scene_seconds": (fingerprint.get("scenes") or [{}])[0].get("duration"),
            },
            "confidence": _confidence(views, int(dist.get("n") or 0) + 1),
            "sample_size": int(dist.get("n") or 0) + 1,
        })
    if classification and classification.get("label") not in {None, "insufficient_data"}:
        records.append({
            "kind": "channel_baseline_position",
            "observation": f"Overall: {classification['label'].replace('_', ' ')} (averageViewPercentage and engagedViews vs. your own Shorts).",
            "associated_production_data": {
                "format": (fingerprint.get("content") or {}).get("format"),
                "duration_seconds": (fingerprint.get("content") or {}).get("duration_seconds"),
                "hook_strategy": hook.get("strategy"),
                "unresolved_critic_issues": (fingerprint.get("quality") or {}).get("unresolved_issue_count"),
            },
            "confidence": _confidence(views, int((baseline or {}).get("sample_size") or 0) + 1),
            "sample_size": int((baseline or {}).get("sample_size") or 0) + 1,
        })
    return records


# ---------------------------------------------------------------------------
# Per-video performance report (Results page)
# ---------------------------------------------------------------------------


def _snapshot_summary(snapshot: YouTubeAnalyticsSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "fetched_at": aware(snapshot.fetched_at),
        "age_bucket": snapshot.age_bucket,
        "published_age_hours": snapshot.published_age_hours,
        "status": snapshot.status,
        "retention_status": snapshot.retention_status,
        "views": metric_value(snapshot, "views"),
        "engagedViews": metric_value(snapshot, "engagedViews"),
        "averageViewPercentage": metric_value(snapshot, "averageViewPercentage"),
    }


def performance_report(db: Session, upload: YouTubeUpload, *, min_sample: int) -> dict[str, Any]:
    fingerprint = fingerprint_for(db, upload)
    snapshots = api_snapshots(db, upload.id)
    latest = latest_with_data(snapshots)
    retention = latest_with_retention(snapshots)
    state = lifecycle(upload)
    report: dict[str, Any] = {
        "upload": serialize_upload(upload),
        "fingerprint": {
            "render_revision": (fingerprint.get("render") or {}).get("render_revision"),
            "duration_seconds": (fingerprint.get("content") or {}).get("duration_seconds"),
            "hook": fingerprint.get("hook"),
            "format": (fingerprint.get("content") or {}).get("format"),
        },
        "snapshots": [_snapshot_summary(item) for item in snapshots],
        "last_analytics_sync_at": aware(upload.last_analytics_sync_at),
        "analytics_error": {"code": upload.analytics_error_code, "message": upload.analytics_error_message} if upload.analytics_error_code else None,
        "manual_metrics": [
            {"name": item.name, "value": item.value, "source": item.source, "note": item.note, "fetched_at": aware(item.fetched_at)}
            for snapshot in db.scalars(select(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == upload.id, YouTubeAnalyticsSnapshot.source == SOURCE_MANUAL).order_by(YouTubeAnalyticsSnapshot.fetched_at)).all()
            for item in snapshot.metrics
        ],
    }
    # Processed-analytics readiness, driven by YouTube's confirmed state.
    report["analytics_state"] = status_authority.analytics_state(
        upload, latest_status=latest.status if latest else None, retention_ok=retention is not None,
    )
    has_data = latest is not None or retention is not None
    if state in {"failed", "uploading"} or (state == "deleted" and not has_data):
        report["status"] = state
        return report
    if upload.published_at is None:
        report["status"] = "scheduled" if state == "scheduled" else "private"
        return report
    if not has_data:
        report["status"] = "waiting_for_data"
        return report
    report["status"] = "ready"
    duration = float((fingerprint.get("content") or {}).get("duration_seconds") or 0) or None
    report["latest_snapshot"] = {**_snapshot_summary(latest), "content_type": content_types.normalize(latest.content_type), "metrics": metric_map(latest)} if latest else None
    report["opening_retention"] = opening_retention(retention.retention_points if retention else [], duration, hook={
        key: (fingerprint.get("hook") or {}).get(key)
        for key in ("strategy", "verbal_hook", "on_screen_hook", "visual_hook", "score", "reason_codes", "source", "selected_candidate_id")
    })
    report["retention"] = {
        "snapshot_id": retention.id if retention else None,
        "fetched_at": aware(retention.fetched_at) if retention else None,
        "point_count": len(retention.retention_points) if retention else 0,
        "metrics": (retention.date_range or {}).get("retention_metrics") if retention else [],
    }
    scenes = map_scenes(retention.retention_points, fingerprint) if retention else []
    report["scene_retention"] = scenes
    age = latest.published_age_hours if latest else None
    baseline = channel_baseline(db, upload.channel_id, min_sample=min_sample, exclude_upload_id=upload.id, age_hours=age)
    outcomes = video_outcomes(latest, fingerprint, retention)
    kind = content_types.normalize(upload.content_type)
    classification = classify(outcomes, baseline) if kind == content_types.SHORTS else {
        "label": "insufficient_data",
        "reason": "YouTube has not confirmed this video as a Short yet" if kind is None else f"YouTube classifies this video as {kind}",
        "per_metric": {},
    }
    report["outcomes"] = outcomes
    report["baseline"] = baseline
    report["classification"] = classification
    report["evidence"] = evidence_records(fingerprint, scenes, report["opening_retention"], outcomes, baseline, classification)
    return report


# ---------------------------------------------------------------------------
# Cross-video learning table
# ---------------------------------------------------------------------------


def _scene_duration_bucket(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 2:
        return "<2s"
    if seconds < 3.5:
        return "2-3.5s"
    if seconds < 5:
        return "3.5-5s"
    return ">=5s"


def learning_rows(db: Session, channel_id: str, *, content_type: str = content_types.SHORTS) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    videos, scenes, excluded = [], [], 0
    for upload in eligible_uploads(db, channel_id):
        if content_types.normalize(upload.content_type) != content_types.normalize(content_type):
            excluded += 1
            continue
        snapshots = api_snapshots(db, upload.id)
        latest, retention = latest_with_data(snapshots), latest_with_retention(snapshots)
        if latest is None:
            continue
        fingerprint = fingerprint_for(db, upload)
        outcomes = video_outcomes(latest, fingerprint, retention)
        quality = fingerprint.get("quality") or {}
        videos.append({
            "upload_id": upload.id,
            "video_id": upload.youtube_video_id,
            "hook_strategy": (fingerprint.get("hook") or {}).get("strategy"),
            "format": (fingerprint.get("content") or {}).get("format"),
            "has_unresolved_critic_issues": bool(quality.get("unresolved_issue_count")),
            "published_age_hours": latest.published_age_hours,
            **outcomes,
        })
        for row in map_scenes(retention.retention_points, fingerprint) if retention else []:
            production = next((item for item in fingerprint.get("scenes") or [] if item.get("scene_id") == row.get("scene_id")), {})
            if row.get("retention_delta") is None:
                continue
            scenes.append({
                "upload_id": upload.id,
                "scene_id": row.get("scene_id"),
                "duration_bucket": _scene_duration_bucket(row.get("duration")),
                "media_origin": production.get("media_origin"),
                "text_heavy": bool(production.get("text_heavy")),
                "retention_delta": row["retention_delta"],
            })
    return videos, scenes, excluded


def _group(rows: list[dict[str, Any]], key: str, value: str, *, min_rows: int, min_videos: int) -> list[dict[str, Any]]:
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get(key) is None or row.get(value) is None:
            continue
        groups.setdefault(row[key], []).append(row)
    result = []
    for name, items in sorted(groups.items(), key=lambda pair: str(pair[0])):
        numbers = [float(item[value]) for item in items]
        video_count = len({item["upload_id"] for item in items})
        sufficient = len(items) >= min_rows and video_count >= min_videos
        result.append({
            "group": name,
            "n": len(items),
            "n_videos": video_count,
            "sufficient": sufficient,
            **({key_: val for key_, val in _distribution(numbers).items() if key_ != "n"} if sufficient else {}),
        })
    return result


def _statement(groups: list[dict[str, Any]], subject: Callable[[Any], str], outcome: str) -> list[str]:
    ready = [item for item in groups if item["sufficient"]]
    if len(ready) < 2:
        return []
    ranked = sorted(ready, key=lambda item: item["median"], reverse=True)
    best, worst = ranked[0], ranked[-1]
    return [(
        f"{subject(best['group'])} was associated with the highest median {outcome} ({best['median']:.3g}, n={best['n']}) "
        f"and {subject(worst['group'])} with the lowest ({worst['median']:.3g}, n={worst['n']})."
    )]


def learning_table(db: Session, channel_id: str) -> dict[str, Any]:
    videos, scenes, excluded = learning_rows(db, channel_id)
    questions = []

    def add(question_id: str, question: str, rows: list[dict[str, Any]], key: str, value: str, subject: Callable[[Any], str], outcome: str, *, scene_level: bool) -> None:
        min_rows = MIN_GROUP_SCENES if scene_level else MIN_GROUP_VIDEOS
        groups = _group(rows, key, value, min_rows=min_rows, min_videos=MIN_GROUP_VIDEOS)
        statements = _statement(groups, subject, outcome)
        questions.append({
            "id": question_id,
            "question": question,
            "outcome_metric": value,
            "unit": "scene" if scene_level else "video",
            "min_group_sample": {"rows": min_rows, "videos": MIN_GROUP_VIDEOS},
            "groups": groups,
            "status": "ok" if statements else "insufficient_data",
            "statements": statements,
        })

    add("hook_strategy_opening", "Which hook strategies have the strongest median opening retention?", videos, "hook_strategy", "opening_retention_3s", lambda value: f"The '{value}' hook", "opening retention at ~3 s", scene_level=False)
    add("scene_duration_retention", "Which scene durations are associated with better scene retention?", scenes, "duration_bucket", "retention_delta", lambda value: f"Scenes of {value}", "retention change", scene_level=True)
    add("media_origin_retention", "Do generated images and real media differ in scene retention?", scenes, "media_origin", "retention_delta", lambda value: f"{str(value).capitalize()} media", "retention change", scene_level=True)
    add("text_heavy_retention", "Do text-heavy scenes have larger drops?", scenes, "text_heavy", "retention_delta", lambda value: "Text-heavy scenes" if value else "Scenes without overlays/graphics", "retention change", scene_level=True)
    add("critic_issues_view_percentage", "Do videos with unresolved critic issues perform worse?", videos, "has_unresolved_critic_issues", "averageViewPercentage", lambda value: "Videos with unresolved critic issues" if value else "Videos without unresolved critic issues", "averageViewPercentage", scene_level=False)
    add("format_view_percentage", "Which formats have better averageViewPercentage?", videos, "format", "averageViewPercentage", lambda value: f"The '{value}' format", "averageViewPercentage", scene_level=False)
    return {
        "channel_id": channel_id,
        "content_type": content_types.SHORTS,
        "video_count": len(videos),
        "scene_count": len(scenes),
        "excluded_other_or_unconfirmed_content_type": excluded,
        "language": "association_only",
        "caveat": ASSOCIATION_CAVEAT,
        "questions": questions,
        "videos": videos,
    }
