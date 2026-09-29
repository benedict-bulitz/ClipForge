"""Posting-time learning: derived, read-only, never applied automatically.

Everything is recomputed from what already exists - the upload mapping
(``published_at``, ``schedule_source``, ``schedule_slot_time``), the stored
analytics snapshots and their age buckets, and the production fingerprint -
so no analytics are duplicated.  Videos are only compared at the same
snapshot age bucket (never a 7-day-old video against a 2-hour-old one), with
medians and quartiles, and every window states its sample size.

A learned schedule is only *proposed*: it needs at least
``LEARNED_MIN_ELIGIBLE`` eligible published ClipForge Shorts and at least
``videos_per_day`` time windows with ``MIN_WINDOW_SAMPLES`` Shorts each.  The
user applies it explicitly; nothing here changes the channel's schedule.
Statements are associations, not causal claims.
"""
from __future__ import annotations

import statistics
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..models import YouTubePublishingSchedule
from . import learning
from . import slots as planner
from .schedule import slot_map

LEARNED_MIN_ELIGIBLE = 30
MIN_WINDOW_SAMPLES = 5
WINDOW_MINUTES = 120  # two-hour windows: coarse on purpose, small samples must not fit exact minutes
REFERENCE_BUCKETS = ("24h", "72h", "7d")  # the existing age-bucket snapshots
PRIMARY_METRIC = "engagedViews"
CAVEAT = (
    "Association across your own published ClipForge Shorts at the same age; it does not show that the "
    "publication time caused the difference (topics, hooks and trends vary too)."
)


def _minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:5])


def _clock(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _window(minute_of_day: int) -> int:
    return minute_of_day // WINDOW_MINUTES


def _label(window: int) -> str:
    return f"{_clock(window * WINDOW_MINUTES)}–{_clock((window + 1) * WINDOW_MINUTES)}"


def publication_rows(db: Session, channel_id: str, timezone: str) -> list[dict[str, Any]]:
    """Per published ClipForge Short: local publication time + outcomes per age bucket."""
    zone = ZoneInfo(timezone)
    rows = []
    for upload in learning.eligible_uploads(db, channel_id):
        if upload.content_type != "SHORTS" or upload.published_at is None:
            continue
        published = upload.published_at.replace(tzinfo=UTC) if upload.published_at.tzinfo is None else upload.published_at
        local = published.astimezone(zone)
        snapshots = learning.api_snapshots(db, upload.id)
        fingerprint = learning.fingerprint_for(db, upload)
        buckets: dict[str, dict[str, Any]] = {}
        for bucket in REFERENCE_BUCKETS:
            snapshot = next((item for item in reversed(snapshots) if item.age_bucket == bucket and item.status in {"ok", "partial"}), None)
            if snapshot is None:
                continue
            retention = learning.latest_with_retention([item for item in snapshots if item.fetched_at <= snapshot.fetched_at])
            outcomes = learning.video_outcomes(snapshot, fingerprint, retention)
            outcomes["averageViewDuration"] = learning.metric_value(snapshot, "averageViewDuration")
            buckets[bucket] = outcomes
        minute = local.hour * 60 + local.minute
        rows.append({
            "upload_id": upload.id,
            "video_id": upload.youtube_video_id,
            "local_date": local.date().isoformat(),
            "local_time": _clock(minute),
            "minute_of_day": minute,
            "weekday": local.weekday(),
            "timezone": timezone,
            "slot_time": upload.schedule_slot_time,
            "schedule_source": upload.schedule_source or "unknown",
            "buckets": buckets,
        })
    return rows


def analyze(db: Session, schedule: YouTubePublishingSchedule) -> dict[str, Any]:
    rows = publication_rows(db, schedule.channel_id, schedule.timezone)
    current = list(slot_map(db, schedule.channel_id).get(None, ()))
    coverage = {bucket: sum(1 for row in rows if bucket in row["buckets"]) for bucket in REFERENCE_BUCKETS}
    bucket = max(REFERENCE_BUCKETS, key=lambda name: (coverage[name], -REFERENCE_BUCKETS.index(name)))
    sample = [row for row in rows if bucket in row["buckets"] and row["buckets"][bucket].get(PRIMARY_METRIC) is not None]
    result: dict[str, Any] = {
        "available": False,
        "eligible_count": len(sample),
        "published_count": len(rows),
        "min_eligible": LEARNED_MIN_ELIGIBLE,
        "min_window_samples": MIN_WINDOW_SAMPLES,
        "window_minutes": WINDOW_MINUTES,
        "reference_age_bucket": bucket,
        "metric": f"{PRIMARY_METRIC} relative to your channel median at the same age ({bucket})",
        "timezone": schedule.timezone,
        "current": current,
        "suggested": None,
        "windows": [],
        "language": "association_only",
        "caveat": CAVEAT,
        "auto_applied": False,
    }
    values = [float(row["buckets"][bucket][PRIMARY_METRIC]) for row in sample]
    baseline = statistics.median(values) if values else 0.0
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in sample:
        groups.setdefault(_window(row["minute_of_day"]), []).append(row)
    windows = []
    for window, items in sorted(groups.items()):
        normalized = [float(item["buckets"][bucket][PRIMARY_METRIC]) / baseline for item in items] if baseline > 0 else []
        view_pct = [item["buckets"][bucket]["averageViewPercentage"] for item in items if item["buckets"][bucket].get("averageViewPercentage") is not None]
        opening = [item["buckets"][bucket]["opening_retention_3s"] for item in items if item["buckets"][bucket].get("opening_retention_3s") is not None]
        distribution = learning._distribution(normalized)
        windows.append({
            "window": _label(window),
            "start": _clock(window * WINDOW_MINUTES),
            "n": len(items),
            "sufficient": len(items) >= MIN_WINDOW_SAMPLES and bool(normalized),
            "median_normalized": distribution.get("median"),
            "p25_normalized": distribution.get("p25"),
            "p75_normalized": distribution.get("p75"),
            "median_average_view_percentage": round(statistics.median(view_pct), 2) if view_pct else None,
            "median_opening_retention_3s": round(statistics.median(opening), 4) if opening else None,
            "auto_scheduled": sum(1 for item in items if item["schedule_source"] == "auto"),
            "median_minute": int(statistics.median(item["minute_of_day"] for item in items)),
        })
    result["windows"] = windows
    if len(sample) < LEARNED_MIN_ELIGIBLE:
        result["reason"] = f"Needs at least {LEARNED_MIN_ELIGIBLE} published ClipForge Shorts with {bucket} analytics; has {len(sample)}."
        return result
    if baseline <= 0:
        result["reason"] = "Your Shorts have no engaged views yet to compare."
        return result
    ready = [item for item in windows if item["sufficient"]]
    if len(ready) < schedule.videos_per_day:
        result["reason"] = (
            f"Needs {schedule.videos_per_day} time window{'s' if schedule.videos_per_day != 1 else ''} with at least "
            f"{MIN_WINDOW_SAMPLES} Shorts each; has {len(ready)}."
        )
        return result
    chosen = sorted(ready, key=lambda item: (item["median_normalized"], item["n"]), reverse=True)[: schedule.videos_per_day]
    suggested = []
    for item in sorted(chosen, key=lambda value: value["start"]):
        start = _minutes(item["start"])
        keep = next((slot for slot in current if start <= _minutes(slot) < start + WINDOW_MINUTES), None)
        if keep is not None:
            suggested.append(keep)
            continue
        # The window's median publication time, on a 30-minute grid, inside the window.
        rounded = int(round(item["median_minute"] / 30) * 30)
        suggested.append(_clock(min(max(rounded, start), start + WINDOW_MINUTES - 30)))
    errors, _warnings = planner.validate_slots(suggested, schedule.videos_per_day)
    if errors:
        result["reason"] = "The strongest windows do not form a valid schedule yet."
        return result
    result.update(
        available=True,
        suggested=sorted(suggested),
        based_on=len(sample),
        reason=None,
        differs=sorted(suggested) != sorted(current),
    )
    return result
