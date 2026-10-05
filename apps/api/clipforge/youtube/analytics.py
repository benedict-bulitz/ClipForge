"""YouTube Analytics snapshots: raw metrics with provenance and raw retention.

The only analytics store.  Each fetch appends a snapshot; nothing is
overwritten.  Metrics the API rejects are stored as explicit ``unavailable``
rows, and Studio-only metrics (``stayed_to_watch``) are recorded as
unavailable via the API - never derived from ``engagedViews`` or anything
else.  ``views`` and ``engagedViews`` are separate metrics: since 31 March
2025 Shorts ``views`` count every start/replay, while ``engagedViews`` keeps
the previous, stricter counting.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    ProductionFingerprint,
    YouTubeAnalyticsSnapshot,
    YouTubeMetricValue,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from ..security.secrets import SecretStore
from . import content_type as content_types
from .connection import access_token, active_connection
from .provider import YouTubeApiError, YouTubeProvider
from .status import current_state, needs_reconcile
from .uploads import UploadRefused, sync_status

SOURCE_API = "youtube_analytics_api"
SOURCE_MANUAL = "manual_studio_import"
VIDEO_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
    "subscribersLost",
)
RETENTION_METRIC_SETS = (
    ("audienceWatchRatio", "relativeRetentionPerformance", "startedWatching", "stoppedWatching", "totalSegmentImpressions"),
    ("audienceWatchRatio", "relativeRetentionPerformance"),
    ("audienceWatchRatio",),
)
RETENTION_COLUMNS = {
    "audienceWatchRatio": "audience_watch_ratio",
    "relativeRetentionPerformance": "relative_retention_performance",
    "startedWatching": "started_watching",
    "stoppedWatching": "stopped_watching",
    "totalSegmentImpressions": "total_segment_impressions",
}
# Metrics that exist in YouTube Studio but not in the authenticated API.
API_UNAVAILABLE_METRICS = {
    "stayed_to_watch": (
        "not_available_via_api",
        (
            "YouTube Studio's 'Stayed to watch' is not exposed by the YouTube Analytics API. "
            "It is not engagedViews and is never derived from other metrics."
        ),
    ),
}
# Studio-only values a user may import by hand, with their valid range.
MANUAL_METRICS: dict[str, tuple[float, float]] = {"stayed_to_watch": (0.0, 100.0)}
AGE_BUCKETS: tuple[tuple[str, float], ...] = (("1h", 1), ("6h", 6), ("24h", 24), ("72h", 72), ("7d", 168))
BUCKET_HOURS: dict[str, float] = dict(AGE_BUCKETS)
# A bucket snapshot whose reporting window ends later than this after the
# bucket's own end day is not that bucket any more (e.g. a "7d" capture taken
# on day 40 with an open-ended window holds 40 days of data).  One day of slack
# keeps legacy captures that were simply taken a few hours late.
LATE_WINDOW_TOLERANCE = timedelta(days=1)
# YouTube Analytics lags behind views by a few days.  A capture that came back
# empty does not use up its bucket: it is due again after this interval, a
# bounded number of times (so a video that never gets data stops costing quota).
NO_DATA_RETRY_AFTER = timedelta(hours=6)
NO_DATA_RETRIES = 3
# At most this many due Analytics captures per explicit refresh (bounded quota;
# the rest are still due on the next click).
ANALYTICS_DUE_LIMIT = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rows_as_dicts(response: dict[str, Any]) -> list[dict[str, Any]]:
    headers = [str(item.get("name")) for item in response.get("columnHeaders") or [] if isinstance(item, dict)]
    return [dict(zip(headers, row, strict=False)) for row in response.get("rows") or [] if isinstance(row, list)]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def age_bucket(age_hours: float, taken: set[str]) -> str | None:
    """The latest target age reached (1h/6h/24h/72h/7d), if not captured yet.

    A later fetch never back-fills an earlier, missed bucket: its numbers
    describe the video at its real age, which ``published_age_hours`` keeps.
    """
    reached = [name for name, hours in AGE_BUCKETS if age_hours >= hours]
    return reached[-1] if reached and reached[-1] not in taken else None


def due_bucket(
    history: list[YouTubeAnalyticsSnapshot], age_hours: float, now: datetime, published_at: datetime | None = None,
) -> str | None:
    """The bucket a due-only sync should capture now, if any.

    Only a capture with data (``ok``/``partial``) takes a bucket.  An empty one
    (``no_data_yet``) is retried after ``NO_DATA_RETRY_AFTER``, at most
    ``NO_DATA_RETRIES`` times; otherwise the last bucket (7d) could end in
    "processing" for good although YouTube has the data a day later.  With
    ``published_at``, a capture whose window ran far past its bucket (taken
    before windows were bounded) does not take it either, so the bucket is
    captured again with a bounded window.
    """
    api = [item for item in history if item.source == SOURCE_API and item.status != "error"]
    taken = {
        item.age_bucket for item in api
        if item.status != "no_data_yet" and (published_at is None or within_bucket_window(item, published_at))
    }
    bucket = age_bucket(age_hours, taken)
    if bucket is None:
        return None
    empty = [item.fetched_at for item in api if item.age_bucket == bucket and item.status == "no_data_yet"]
    if not empty:
        return bucket
    if len(empty) > NO_DATA_RETRIES or now - _utc(max(empty)) < NO_DATA_RETRY_AFTER:  # type: ignore[operator]
        return None
    return bucket


def bucket_end_date(published_at: datetime | None, bucket: str | None) -> date | None:
    """The calendar day (UTC) on which the video reaches ``bucket``'s age; None for manual."""
    hours = BUCKET_HOURS.get(bucket or "")
    if hours is None or published_at is None:
        return None
    return (_utc(published_at) + timedelta(hours=hours)).date()  # type: ignore[operator]


def report_window(published_at: datetime, now: datetime, bucket: str | None = None) -> tuple[date, date]:
    """The Analytics API date range: plain DATEs (no time of day, no zone).

    YouTube Analytics reports by calendar day.  Starting the day before the
    UTC publication date keeps the publication day inside the range in every
    zone west of UTC (a video published late in the evening there is already
    "tomorrow" in UTC).  A day YouTube has not processed yet simply
    contributes no rows.

    For an age-bucket capture (1h ... 7d) the end is the day the video reaches
    that age (``bucket_end_date``), never later than today, so a capture taken
    late - or for an older video's first 7d capture - still describes the
    bucket's window, not the video's lifetime.  Calendar-day resolution only:
    the window covers at least the bucket's age and may include the rest of
    its final day; YouTube offers no hour-level cut-off.  A manual capture
    (no bucket) ends today: the video's numbers so far.
    """
    start, end = (_utc(published_at) - timedelta(days=1)).date(), _utc(now).date()  # type: ignore[operator,union-attr]
    limit = bucket_end_date(published_at, bucket)
    return start, min(end, limit) if limit is not None else end


def window_end(snapshot: YouTubeAnalyticsSnapshot, published_at: datetime | None) -> date | None:
    """The last day a snapshot's numbers can include.

    The stored request range when present; for rows without one, the day the
    capture was taken (its window always ended "today" before bounded windows).
    """
    end = (snapshot.date_range or {}).get("end")
    if end:
        try:
            return date.fromisoformat(str(end))
        except ValueError:
            pass
    if published_at is not None and snapshot.published_age_hours is not None:
        return (_utc(published_at) + timedelta(hours=float(snapshot.published_age_hours))).date()  # type: ignore[operator]
    return _utc(snapshot.fetched_at).date() if snapshot.fetched_at else None  # type: ignore[union-attr]


def within_bucket_window(snapshot: YouTubeAnalyticsSnapshot, published_at: datetime | None) -> bool:
    """Whether an age-bucket snapshot really describes its bucket (not a later/lifetime window).

    Non-bucket snapshots (``manual``) make no age claim and always pass.
    """
    limit = bucket_end_date(published_at, snapshot.age_bucket)
    if limit is None:
        return snapshot.age_bucket not in BUCKET_HOURS
    end = window_end(snapshot, published_at)
    return end is not None and end <= limit + LATE_WINDOW_TOLERANCE


def snapshot_status(metrics: dict[str, dict[str, Any]], retention_status: str) -> str:
    """ok | partial | no_data_yet for one capture (errors never get here: they raise)."""
    values = [item["availability"] for item in metrics.values()]
    if all(value == "no_data_yet" for value in values) and retention_status != "ok":
        return "no_data_yet"
    if any(value == "unavailable" for value in values) or retention_status == "unavailable":
        return "partial"
    return "ok"


def due_uploads(
    db: Session, channel_id: str, *, now: datetime | None = None, exclude: set[str] | frozenset[str] = frozenset(), limit: int | None = None,
) -> list[YouTubeUpload]:
    """Published videos of the channel whose Analytics capture is due (``due_bucket``).

    Chosen by due-ness, not by recency in the library, so scheduled or newer
    rows never crowd out an older published video whose data YouTube has now
    processed.  Videos without any captured data come first, then the newest;
    at most ``limit`` (default ``ANALYTICS_DUE_LIMIT``).  Store reads only.
    """
    now = now or _now()
    uploads = [
        upload for upload in db.scalars(
            select(YouTubeUpload).where(
                YouTubeUpload.channel_id == channel_id,
                YouTubeUpload.youtube_video_id.is_not(None),
                YouTubeUpload.deleted_on_youtube.is_(False),
                YouTubeUpload.published_at.is_not(None),
            )
        ).all()
        if upload.id not in exclude
    ]
    if not uploads:
        return []
    history: dict[str, list[YouTubeAnalyticsSnapshot]] = {upload.id: [] for upload in uploads}
    for snapshot in db.scalars(
        select(YouTubeAnalyticsSnapshot)
        .where(YouTubeAnalyticsSnapshot.upload_id.in_(list(history)), YouTubeAnalyticsSnapshot.source == SOURCE_API)
        .order_by(YouTubeAnalyticsSnapshot.fetched_at)
    ).all():
        history[snapshot.upload_id].append(snapshot)
    due = []
    for upload in uploads:
        published = _utc(upload.published_at)
        age_hours = max(0.0, (now - published).total_seconds() / 3600)  # type: ignore[operator]
        if due_bucket(history[upload.id], age_hours, now, upload.published_at) is not None:
            has_data = any(item.status in {"ok", "partial"} for item in history[upload.id])
            due.append((has_data, -published.timestamp(), upload.id, upload))  # type: ignore[union-attr]
    due.sort(key=lambda item: item[:3])
    return [item[3] for item in due][: ANALYTICS_DUE_LIMIT if limit is None else limit]


def _report_params(video_id: str, start: date, end: date, metrics: tuple[str, ...], dimensions: str) -> dict[str, str]:
    return {
        "ids": "channel==MINE",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "metrics": ",".join(metrics),
        "dimensions": dimensions,
        "filters": f"video=={video_id}",
    }


def fetch_video_metrics(
    provider: YouTubeProvider, token: str, video_id: str, start: date, end: date
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Each requested metric with its value or an explicit unavailability reason."""
    raw: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}
    try:
        response = provider.analytics_report(token, _report_params(video_id, start, end, VIDEO_METRICS, "video"))
        raw["video_metrics"] = response
        rows = rows_as_dicts(response)
        for name in VIDEO_METRICS:
            if not rows:
                results[name] = {"value": None, "availability": "no_data_yet", "reason": "no_rows_yet"}
            elif name not in rows[0]:
                results[name] = {"value": None, "availability": "unavailable", "reason": "not_returned_by_api"}
            else:
                results[name] = {"value": _number(rows[0][name]), "availability": "available", "reason": None}
        return results, raw
    except YouTubeApiError as exc:
        if exc.code != "bad_request":
            raise
    # One metric was not accepted in combination: ask individually, so the
    # supported ones still arrive and the rejected ones are recorded as such.
    for name in VIDEO_METRICS:
        try:
            response = provider.analytics_report(token, _report_params(video_id, start, end, (name,), "video"))
        except YouTubeApiError as exc:
            if exc.code != "bad_request":
                raise
            results[name] = {"value": None, "availability": "unavailable", "reason": "rejected_by_api"}
            raw[f"metric:{name}"] = {"error": exc.reason or exc.code}
            continue
        raw[f"metric:{name}"] = response
        rows = rows_as_dicts(response)
        if rows and name in rows[0]:
            results[name] = {"value": _number(rows[0][name]), "availability": "available", "reason": None}
        elif rows:
            results[name] = {"value": None, "availability": "unavailable", "reason": "not_returned_by_api"}
        else:
            results[name] = {"value": None, "availability": "no_data_yet", "reason": "no_rows_yet"}
    return results, raw


def fetch_content_type(
    provider: YouTubeProvider, token: str, video_id: str, start: date, end: date
) -> tuple[str | None, dict[str, Any]]:
    """YouTube's own classification, canonical (``SHORTS``, ``VIDEO_ON_DEMAND``, ...), never assumed.

    The API answers in camel case (``shorts``); ``content_type.normalize`` maps
    it to the one stored form and drops ``UNSPECIFIED`` in any casing.
    """
    try:
        response = provider.analytics_report(
            token, _report_params(video_id, start, end, ("views",), "creatorContentType")
        )
    except YouTubeApiError as exc:
        if exc.code != "bad_request":
            raise
        return None, {"error": exc.reason or exc.code}
    rows = rows_as_dicts(response)
    known = [value for value in (content_types.normalize(row.get("creatorContentType")) for row in rows) if value]
    return (known[0] if known else None), response


def fetch_retention(
    provider: YouTubeProvider, token: str, video_id: str, start: date, end: date
) -> tuple[str, list[dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    """(status, raw points, metrics used, raw response); "not_ready" is normal."""
    last_error: YouTubeApiError | None = None
    for metrics in RETENTION_METRIC_SETS:
        try:
            response = provider.analytics_report(
                token, _report_params(video_id, start, end, metrics, "elapsedVideoTimeRatio")
            )
        except YouTubeApiError as exc:
            if exc.code != "bad_request":
                raise
            last_error = exc
            continue
        rows = rows_as_dicts(response)
        if not rows:
            return "not_ready", [], metrics, response
        return "ok", rows, metrics, response
    return "unavailable", [], (), {"error": (last_error.reason or last_error.code) if last_error else "unknown"}


def _fingerprint_duration(db: Session, upload: YouTubeUpload) -> float | None:
    record = db.get(ProductionFingerprint, upload.fingerprint_id) if upload.fingerprint_id else None
    duration = ((record.fingerprint.get("content") or {}).get("duration_seconds")) if record else None
    return _number(duration)


def _snapshots(db: Session, upload_id: str) -> list[YouTubeAnalyticsSnapshot]:
    return list(
        db.scalars(
            select(YouTubeAnalyticsSnapshot)
            .where(YouTubeAnalyticsSnapshot.upload_id == upload_id)
            .order_by(YouTubeAnalyticsSnapshot.fetched_at)
        ).all()
    )


def _set_analytics_error(db: Session, upload: YouTubeUpload, error: YouTubeApiError | None) -> None:
    upload.analytics_error_code = error.code if error else None
    upload.analytics_error_message = error.message[:500] if error else None
    db.commit()


def refresh_analytics(
    db: Session,
    upload: YouTubeUpload,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    now: datetime | None = None,
    due_only: bool = False,
) -> dict[str, Any]:
    """Sync status, then append one analytics snapshot when the video is public."""
    now = now or _now()
    if not upload.youtube_video_id:
        raise UploadRefused("not_uploaded", "This upload has no YouTube video yet.", upload)
    try:
        # Eligibility comes from YouTube's confirmed state, never the requested schedule.
        sync_status(db, upload, settings, store, provider, now=now)
    except YouTubeApiError as exc:
        _set_analytics_error(db, upload, exc)
        return {"status": "error", "error": {"code": exc.code, "message": exc.message}}
    if upload.deleted_on_youtube:
        return {"status": "deleted"}
    if upload.published_at is None:
        return {"status": "not_published", "current_state": current_state(upload, now)}
    upload.last_analytics_attempt_at = now
    db.commit()
    age_hours = max(0.0, (now - _utc(upload.published_at)).total_seconds() / 3600)  # type: ignore[operator]
    bucket = due_bucket(_snapshots(db, upload.id), age_hours, now, upload.published_at)
    if due_only and bucket is None:
        return {"status": "not_due", "published_age_hours": round(age_hours, 2)}
    try:
        _connection, token = access_token(db, settings, store, provider, capability="analytics")
        start, end = report_window(upload.published_at, now, bucket)  # type: ignore[arg-type]
        metrics, raw = fetch_video_metrics(provider, token, upload.youtube_video_id, start, end)
        content_type, raw_type = fetch_content_type(provider, token, upload.youtube_video_id, start, end)
        retention_status, points, retention_metrics, raw_retention = fetch_retention(
            provider, token, upload.youtube_video_id, start, end
        )
    except YouTubeApiError as exc:
        _set_analytics_error(db, upload, exc)
        return {"status": "error", "error": {"code": exc.code, "message": exc.message}}
    raw.update(content_type=raw_type, retention=raw_retention)
    status = snapshot_status(metrics, retention_status)
    snapshot = YouTubeAnalyticsSnapshot(
        upload_id=upload.id,
        youtube_video_id=upload.youtube_video_id,
        channel_id=upload.channel_id,
        project_id=upload.project_id,
        project_revision=upload.project_revision,
        render_revision=upload.render_revision,
        source=SOURCE_API,
        age_bucket=bucket or "manual",
        published_age_hours=round(age_hours, 3),
        status=status,
        retention_status=retention_status,
        content_type=content_type,
        date_range={
            "start": start.isoformat(), "end": end.isoformat(), "retention_metrics": list(retention_metrics),
            # The window stops at the bucket's age (calendar days), not today.
            "bounded_to_bucket": bucket is not None,
        },
        raw_responses=raw,
        fetched_at=now,
    )
    for name, item in metrics.items():
        snapshot.metrics.append(YouTubeMetricValue(
            youtube_video_id=upload.youtube_video_id, name=name, value=item["value"],
            availability=item["availability"], reason=item["reason"], source=SOURCE_API, fetched_at=now,
        ))
    for name, (reason, note) in API_UNAVAILABLE_METRICS.items():
        snapshot.metrics.append(YouTubeMetricValue(
            youtube_video_id=upload.youtube_video_id, name=name, value=None,
            availability="unavailable", reason=reason, source=SOURCE_API, note=note, fetched_at=now,
        ))
    duration = _fingerprint_duration(db, upload)
    for row in points:
        ratio = _number(row.get("elapsedVideoTimeRatio"))
        if ratio is None:
            continue
        point = YouTubeRetentionPoint(
            youtube_video_id=upload.youtube_video_id,
            elapsed_video_ratio=ratio,
            video_second=round(ratio * duration, 3) if duration else 0.0,
            fetched_at=now,
        )
        for metric, column in RETENTION_COLUMNS.items():
            if metric in row:
                setattr(point, column, _number(row[metric]))
        snapshot.retention_points.append(point)
    db.add(snapshot)
    if content_type:
        upload.content_type = content_type
    upload.last_analytics_sync_at = now
    upload.analytics_error_code = None
    upload.analytics_error_message = None
    db.commit()
    db.refresh(snapshot)
    return {"status": status, "snapshot_id": snapshot.id, "age_bucket": snapshot.age_bucket, "retention_status": retention_status}


def sync_due(
    db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, *, now: datetime | None = None
) -> dict[str, Any]:
    """Take the snapshots that are due; safe to call from a manual button or cron."""
    now = now or _now()
    connection = active_connection(db)
    if connection is None:
        return {"status": "not_connected", "results": []}
    uploads = db.scalars(
        select(YouTubeUpload).where(
            YouTubeUpload.channel_id == connection.channel_id,
            YouTubeUpload.youtube_video_id.is_not(None),
            YouTubeUpload.deleted_on_youtube.is_(False),
        )
    ).all()
    results = []
    for upload in uploads:
        if upload.published_at is None and not needs_reconcile(upload, now=now):
            continue  # not published yet and YouTube was asked recently
        try:
            outcome = refresh_analytics(db, upload, settings, store, provider, now=now, due_only=True)
        except UploadRefused as exc:
            outcome = {"status": "skipped", "reason": exc.code}
        results.append({"upload_id": upload.id, "video_id": upload.youtube_video_id, **outcome})
        if outcome.get("status") == "error" and (outcome.get("error") or {}).get("code") in {"auth_expired", "quota_exceeded", "not_connected"}:
            break  # further calls would fail the same way
    return {"status": "ok", "results": results}


def record_manual_metric(
    db: Session, upload: YouTubeUpload, name: str, value: float, *, note: str | None = None, now: datetime | None = None
) -> YouTubeAnalyticsSnapshot:
    """Extension point for Studio-only metrics, stored with manual provenance."""
    if name not in MANUAL_METRICS:
        raise UploadRefused("unsupported_metric", f"Manual import is not supported for {name}.", upload)
    low, high = MANUAL_METRICS[name]
    if not math.isfinite(value) or not low <= value <= high:
        raise UploadRefused("invalid_value", f"{name} must be between {low:g} and {high:g}.", upload)
    if not upload.youtube_video_id:
        raise UploadRefused("not_uploaded", "This upload has no YouTube video yet.", upload)
    now = now or _now()
    published = _utc(upload.published_at)
    snapshot = YouTubeAnalyticsSnapshot(
        upload_id=upload.id,
        youtube_video_id=upload.youtube_video_id,
        channel_id=upload.channel_id,
        project_id=upload.project_id,
        project_revision=upload.project_revision,
        render_revision=upload.render_revision,
        source=SOURCE_MANUAL,
        age_bucket="manual",
        published_age_hours=round((now - published).total_seconds() / 3600, 3) if published else None,
        status="ok",
        retention_status="not_requested",
        fetched_at=now,
    )
    snapshot.metrics.append(YouTubeMetricValue(
        youtube_video_id=upload.youtube_video_id, name=name, value=float(value),
        availability="available", source=SOURCE_MANUAL, note=(note or "")[:500] or None, fetched_at=now,
    ))
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot
