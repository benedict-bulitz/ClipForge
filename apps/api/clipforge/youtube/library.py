"""Video Library: the global, project-independent view of uploaded videos.

A read model only.  Everything shown here comes from the existing authorities:

* ``youtube_uploads`` - the one project<->video mapping (never FK-bound to a
  project, so it outlives project deletion); its current state is derived by
  ``status.current_state`` and live stats are its Data-API ``remote_*`` fields.
* ``youtube_analytics_snapshots`` / ``youtube_metric_values`` - the one
  analytics store.  The index reads only the latest snapshot's metric values
  in one aggregate query; raw responses and retention points are never read
  for the index.  The detail page reuses ``learning.performance_report``.
* ``production_fingerprints`` and ``youtube_learning_archives`` - the compact
  production record and the deleted project's title/prompt/topic.

The only thing this module stores is a small preview image per video (a few
KB, ``render_root / "video-library"``), kept so a video stays recognisable
after its project's heavy media was deleted.
"""
from __future__ import annotations

import io
import logging
import statistics
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, load_only

from ..config import Settings
from ..models import (
    ProductionFingerprint,
    Project,
    YouTubeAnalyticsSnapshot,
    YouTubeConnection,
    YouTubeLearningArchive,
    YouTubeMetricValue,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from . import status as status_authority
from .analytics import SOURCE_API
from .uploads import ACTIVE_STATES, aware, serialize_upload, youtube_links

logger = logging.getLogger(__name__)

LIBRARY_DIR = "video-library"
THUMBNAIL_BOX = (270, 480)  # fits a 9:16 cover at a quarter of 1080x1920
THUMBNAIL_QUALITY = 72
THUMBNAIL_MAX_BYTES = 60_000
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100
RECENT_REFRESH_LIMIT = 10

# Index summary metrics from the latest analytics snapshot (YouTube Analytics API).
SUMMARY_METRICS = ("views", "engagedViews", "averageViewDuration", "averageViewPercentage", "likes", "comments")
SORTS = ("newest", "oldest", "views", "average_view_percentage", "average_view_duration")
STATUS_FILTERS = ("all", "published", "unlisted", "scheduled", "private", "processing", "deleted", "rejected")
PROJECT_FILTERS = ("all", "available", "archived")
ANALYTICS_FILTERS = ("all", "available", "processing")
STATE_LABELS = {
    "published": "Published",
    "unlisted": "Unlisted",
    "scheduled": "Scheduled",
    "private": "Private",
    "processing": "Processing",
    "deleted": "Deleted",
    "rejected": "Rejected",
    "processing_failed": "Processing failed",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Which uploads are library videos
# ---------------------------------------------------------------------------


def _has_snapshot():
    return exists().where(YouTubeAnalyticsSnapshot.upload_id == YouTubeUpload.id)


def library_condition():
    """SQL form of ``lifecycle.classify_upload(...) == SUCCEEDED``.

    A video ID means YouTube received the file; only a rejection/processing
    failure without any analytics never counted as a success.  Kept in one
    place and pinned to ``classify_upload`` by a test.
    """
    return and_(
        YouTubeUpload.youtube_video_id.is_not(None),
        YouTubeUpload.state.not_in(ACTIVE_STATES),
        or_(
            YouTubeUpload.upload_status.is_(None),
            YouTubeUpload.upload_status.not_in(("rejected", "failed")),
            _has_snapshot(),
        ),
    )


def library_state(upload: YouTubeUpload, now: datetime) -> str:
    """The library's status bucket, derived from the one current-status authority."""
    state = status_authority.current_state(upload, now)
    if state in {"deleted", "rejected", "processing_failed", "published", "unlisted", "scheduled"}:
        return state
    if upload.upload_status == "uploaded":
        return "processing"
    return "private"  # private, or still private after the scheduled time


def has_been_public(upload: YouTubeUpload, now: datetime) -> bool:
    """Whether YouTube ever showed this video to an audience (public/unlisted).

    Before that, ``videos.list`` statistics are placeholder counters of a
    private video, not audience numbers, so the library does not show them.
    """
    return (
        upload.published_at is not None
        or upload.first_observed_public_at is not None
        or status_authority.current_state(upload, now) in status_authority.LIVE_STATES
    )


REMOTE_DELETED_REASON = "This video no longer exists on YouTube (deleted or removed), so its YouTube and Studio pages cannot open."


def _youtube_actions(upload: YouTubeUpload, state: str) -> dict[str, Any]:
    """Watch/Studio links only while the remote video exists (independent of the project)."""
    if state == "deleted":
        return {"available": False, "reason": REMOTE_DELETED_REASON, "watch_url": None, "shorts_url": None, "studio_url": None}
    return {"available": True, "reason": None, **youtube_links(upload.youtube_video_id)}


def _status_bucket(state: str) -> str:
    return "rejected" if state == "processing_failed" else state


def _state_label(upload: YouTubeUpload, state: str, now: datetime) -> str:
    current = status_authority.current_state(upload, now)
    if current == "publish_pending":
        return status_authority.LABELS[current]
    return STATE_LABELS[state]


# ---------------------------------------------------------------------------
# Index summary data (no raw responses, no retention points)
# ---------------------------------------------------------------------------


@dataclass
class _Summary:
    snapshot_status: str | None = None
    fetched_at: datetime | None = None
    retention_ok: bool = False
    metrics: dict[str, float | None] | None = None


def _library_ids():
    return select(YouTubeUpload.id).where(library_condition()).scalar_subquery()


def _analytics_summaries(
    db: Session, upload_ids: list[str] | None = None, metrics: tuple[str, ...] = SUMMARY_METRICS
) -> dict[str, _Summary]:
    """Latest API snapshot with data (``learning.latest_with_data``) per upload, plus
    its available ``metrics``, in two aggregate queries.

    ``upload_ids=None`` means every library video (a subquery, not a huge IN list).
    Also used by the channel performance overview (``performance.py``).
    """
    if upload_ids is not None and not upload_ids:
        return {}
    scope = YouTubeAnalyticsSnapshot.upload_id.in_(_library_ids() if upload_ids is None else upload_ids)
    ranked = (
        select(
            YouTubeAnalyticsSnapshot.id.label("snapshot_id"),
            YouTubeAnalyticsSnapshot.upload_id.label("upload_id"),
            YouTubeAnalyticsSnapshot.status.label("status"),
            YouTubeAnalyticsSnapshot.fetched_at.label("fetched_at"),
            func.row_number().over(
                partition_by=YouTubeAnalyticsSnapshot.upload_id,
                order_by=(YouTubeAnalyticsSnapshot.fetched_at.desc(), YouTubeAnalyticsSnapshot.id.desc()),
            ).label("rank"),
        )
        .where(
            scope,
            YouTubeAnalyticsSnapshot.source == SOURCE_API,
            YouTubeAnalyticsSnapshot.status.in_(("ok", "partial")),
        )
        .subquery()
    )
    latest = select(ranked).where(ranked.c.rank == 1).subquery()
    columns = [
        func.max(case(
            (and_(YouTubeMetricValue.name == name, YouTubeMetricValue.availability == "available"), YouTubeMetricValue.value),
            else_=None,
        )).label(name)
        for name in metrics
    ]
    rows = db.execute(
        select(latest.c.upload_id, latest.c.status, latest.c.fetched_at, *columns)
        .select_from(latest)
        .outerjoin(YouTubeMetricValue, YouTubeMetricValue.snapshot_id == latest.c.snapshot_id)
        .group_by(latest.c.upload_id, latest.c.status, latest.c.fetched_at)
    ).all()
    result = {
        row.upload_id: _Summary(
            snapshot_status=row.status,
            fetched_at=row.fetched_at,
            metrics={name: getattr(row, name) for name in metrics},
        )
        for row in rows
    }
    # "Has a retention curve" from the snapshot status only; points stay unread.
    with_retention = db.scalars(
        select(YouTubeAnalyticsSnapshot.upload_id)
        .where(
            scope,
            YouTubeAnalyticsSnapshot.source == SOURCE_API,
            YouTubeAnalyticsSnapshot.retention_status == "ok",
        )
        .distinct()
    ).all()
    for upload_id in with_retention:
        result.setdefault(upload_id, _Summary()).retention_ok = True
    return result


def _analytics_state(upload: YouTubeUpload, summary: _Summary | None, now: datetime) -> str:
    return status_authority.analytics_state(
        upload,
        latest_status=summary.snapshot_status if summary else None,
        retention_ok=bool(summary and summary.retention_ok),
        now=now,
    )


def _analytics_bucket(state: str) -> str | None:
    if state in {"available", "partial"}:
        return "available"
    if state == "processing":
        return "processing"
    return None


def _sort_date(upload: YouTubeUpload) -> datetime:
    """Publication time once published, otherwise when it was uploaded."""
    return _utc(upload.published_at) or _utc(upload.uploaded_at) or _utc(upload.created_at) or datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Small retained preview
# ---------------------------------------------------------------------------


def library_directory(settings: Settings) -> Path:
    return settings.render_root.resolve() / LIBRARY_DIR


def thumbnail_path(upload: YouTubeUpload, settings: Settings) -> Path | None:
    if not upload.library_thumbnail:
        return None
    path = (library_directory(settings) / upload.library_thumbnail).resolve()
    return path if path.parent == library_directory(settings) and path.is_file() else None


def thumbnail_url(upload: YouTubeUpload, settings: Settings) -> str | None:
    path = thumbnail_path(upload, settings)
    return f"/media/{LIBRARY_DIR}/{path.name}?v={int(path.stat().st_mtime)}" if path else None


def _encode_small(source: Path) -> tuple[bytes, str]:
    """A small WebP (JPEG where WebP is unavailable) fitted into THUMBNAIL_BOX."""
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail(THUMBNAIL_BOX, Image.Resampling.LANCZOS)
        for quality in (THUMBNAIL_QUALITY, 60, 48):
            buffer = io.BytesIO()
            try:
                image.save(buffer, format="WEBP", quality=quality, method=4)
                suffix = "webp"
            except (OSError, KeyError, ValueError):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                suffix = "jpg"
            if buffer.tell() <= THUMBNAIL_MAX_BYTES:
                break
        return buffer.getvalue(), suffix


def _render_state(project: Project, render_revision: int) -> dict[str, Any]:
    revision = next((item for item in project.revisions if item.number == render_revision), None)
    return revision.state if revision is not None and isinstance(revision.state, dict) else {}


def _preview_sources(db: Session, upload: YouTubeUpload, project: Project, settings: Settings, work: Path) -> list[Path]:
    """The cover actually sent to YouTube, then the project's YouTube cover,
    then any valid cover, then a frame of the uploaded render."""
    from ..services import effective_revision_state
    from .publishing import _safe_child, thumbnail_choices

    root = settings.render_root.resolve()
    sources: list[Path] = []
    try:
        choices = [item for item in thumbnail_choices(effective_revision_state(project), project.id, settings) if item["valid"]]
    except (OSError, ValueError, KeyError):
        choices = []
    chosen = [item for item in choices if item["source"] == upload.thumbnail_source and item["asset"] == upload.thumbnail_asset]
    youtube = [item for item in choices if item["platform"] == "youtube"]
    for item in [*chosen, *youtube, *choices]:
        path = _safe_child(root, str(item["url"]).removeprefix("/media/"))
        if path is not None and path not in sources:
            sources.append(path)
    render = _render_state(project, upload.render_revision).get("render") or {}
    url = str(render.get("url") or "") if isinstance(render, dict) else ""
    video = _safe_child(root, url.removeprefix("/media/")) if url.startswith(f"/media/{project.id}/") else None
    if video is not None:
        from ..renderer import ffmpeg_path
        from ..thumbnails import extract_video_frame

        ffmpeg = ffmpeg_path()
        frame = work / "frame.jpg"
        if ffmpeg and extract_video_frame(ffmpeg, video, 1.0, frame, width=540):
            sources.append(frame)
    return sources


def ensure_library_thumbnail(
    db: Session, upload: YouTubeUpload, settings: Settings, *, force: bool = False, commit: bool = True
) -> str | None:
    """Keep a small preview for a library video; never raises, never blocks.

    Made on demand (the first library page/detail that shows the video) and -
    with ``force`` - by the deletion lifecycle before any purge, so the upload
    path itself is untouched.  Once the project
    is gone nothing new can be made: the video keeps what it has (or the UI's
    placeholder).  ``""`` records "no usable image" so pages do not re-scan.
    """
    if not upload.youtube_video_id:
        return None
    if thumbnail_path(upload, settings) is not None:
        return upload.library_thumbnail
    if upload.library_thumbnail == "" and not force:
        return None
    project = db.get(Project, upload.project_id)
    if project is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="clipforge-library-") as scratch:
            for source in _preview_sources(db, upload, project, settings, Path(scratch)):
                try:
                    data, suffix = _encode_small(source)
                except (OSError, UnidentifiedImageError, ValueError):
                    continue
                directory = library_directory(settings)
                directory.mkdir(parents=True, exist_ok=True)
                name = f"{upload.id}.{suffix}"
                (directory / name).write_bytes(data)
                upload.library_thumbnail = name
                if commit:
                    db.commit()
                return name
        upload.library_thumbnail = ""
        if commit:
            db.commit()
    except Exception:
        logger.exception("Video Library preview could not be kept for upload %s", upload.id)
    return None


def remove_library_thumbnail(upload: YouTubeUpload, settings: Settings) -> int:
    """Delete the preview of an upload that is removed completely; returns bytes freed."""
    path = thumbnail_path(upload, settings)
    if path is None:
        return 0
    size = path.stat().st_size
    try:
        path.unlink()
    except OSError:
        return 0
    return size


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

_STATE_COLUMNS = (
    YouTubeUpload.id,
    YouTubeUpload.project_id,
    YouTubeUpload.channel_id,
    YouTubeUpload.youtube_video_id,
    YouTubeUpload.state,
    YouTubeUpload.upload_status,
    YouTubeUpload.deleted_on_youtube,
    YouTubeUpload.privacy_status,
    YouTubeUpload.remote_privacy_status,
    YouTubeUpload.remote_status_checked_at,
    YouTubeUpload.remote_publish_at,
    YouTubeUpload.publish_at,
    YouTubeUpload.schedule_status,
    YouTubeUpload.first_observed_public_at,
    YouTubeUpload.published_at,
    YouTubeUpload.uploaded_at,
    YouTubeUpload.created_at,
    YouTubeUpload.remote_view_count,
    YouTubeUpload.analytics_error_code,
)


def _search_condition(query: str):
    """Case-insensitive substring match; ``%``/``_`` typed by the user are literal."""
    text = query.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{text}%"
    columns = (
        YouTubeUpload.title,
        YouTubeUpload.youtube_video_id,
        Project.original_prompt,
        Project.title,
        YouTubeLearningArchive.prompt,
        YouTubeLearningArchive.topic,
        YouTubeLearningArchive.title,
    )
    return or_(*(func.lower(func.coalesce(column, "")).like(pattern, escape="\\") for column in columns))


def _metric(summary: _Summary | None, name: str) -> float | None:
    return (summary.metrics or {}).get(name) if summary else None


def _sort_value(sort: str, upload: YouTubeUpload, summary: _Summary | None, now: datetime) -> float | None:
    if sort == "views":
        # Live views count once the video has been public; placeholders before do not rank.
        return float(upload.remote_view_count) if has_been_public(upload, now) and upload.remote_view_count is not None else None
    if sort == "average_view_percentage":
        return _metric(summary, "averageViewPercentage")
    if sort == "average_view_duration":
        return _metric(summary, "averageViewDuration")
    return None


def _order(rows: list[tuple[YouTubeUpload, _Summary | None]], sort: str, now: datetime) -> list[tuple[YouTubeUpload, _Summary | None]]:
    newest = sorted(rows, key=lambda row: (_sort_date(row[0]), row[0].id), reverse=True)
    if sort == "oldest":
        return list(reversed(newest))
    if sort == "newest":
        return newest
    # Metric sorts: only videos that have the value are ranked; the rest follow, newest first.
    ranked = [row for row in newest if _sort_value(sort, *row, now) is not None]
    missing = [row for row in newest if _sort_value(sort, *row, now) is None]
    ranked.sort(key=lambda row: _sort_value(sort, *row, now) or 0.0, reverse=True)
    return ranked + missing


def _fingerprint_facts(db: Session, fingerprint_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Duration/format/scene count of the page's fingerprints via JSON paths (no scene rows)."""
    if not fingerprint_ids:
        return {}
    doc = ProductionFingerprint.fingerprint
    rows = db.execute(
        select(
            ProductionFingerprint.id,
            doc[("content", "duration_seconds")].as_float().label("duration"),
            doc[("content", "format")].as_string().label("format"),
            doc[("pacing", "scene_count")].as_integer().label("scene_count"),
        ).where(ProductionFingerprint.id.in_(fingerprint_ids))
    ).all()
    return {row.id: {"duration_seconds": row.duration, "format": row.format, "scene_count": row.scene_count} for row in rows}


def _channel_title(connection: YouTubeConnection | None, channel_id: str) -> str | None:
    return connection.channel_title if connection is not None and connection.channel_id == channel_id else None


def serialize_video(
    upload: YouTubeUpload,
    *,
    summary: _Summary | None,
    project: tuple[str, str] | None,
    archive: YouTubeLearningArchive | None,
    facts: dict[str, Any] | None,
    connection: YouTubeConnection | None,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """One library entry: identity, current state, live stats and analytics summary."""
    state = library_state(upload, now)
    current = status_authority.current_status(upload, now=now)
    analytics_state = _analytics_state(upload, summary, now)
    project_title, project_prompt = project if project else (None, None)
    metrics = (summary.metrics or {}) if summary else {}
    public = has_been_public(upload, now)
    live_stats = current["live_stats"] if public else None
    actions = _youtube_actions(upload, state)
    return {
        "id": upload.id,
        "youtube_video_id": upload.youtube_video_id,
        "title": upload.title or (archive.title if archive else "") or project_title or upload.youtube_video_id,
        "prompt": project_prompt if project else (archive.prompt if archive else None),
        "topic": archive.topic if archive else None,
        "channel": {"id": upload.channel_id, "title": _channel_title(connection, upload.channel_id)},
        "state": state,
        "state_label": _state_label(upload, state, now),
        "processing": current["processing"],
        "stale": current["stale"],
        "stale_reason": current["stale_reason"],
        "last_checked_at": current["last_checked_at"],
        "scheduled_for": current["scheduled_for"],
        "requested_publish_at": aware(upload.publish_at),
        # The zone the schedule was chosen in (None for times set outside ClipForge).
        "schedule_timezone": upload.schedule_timezone,
        "published_at": current["published_at"],
        "uploaded_at": aware(upload.uploaded_at) or aware(upload.created_at),
        "sort_date": _sort_date(upload),
        "content_type": upload.content_type,
        "duration_seconds": (facts or {}).get("duration_seconds"),
        "format": (facts or {}).get("format"),
        "scene_count": (facts or {}).get("scene_count"),
        "project": {
            "id": upload.project_id,
            "available": project is not None,
            "title": project_title if project else (archive.title if archive else None),
            "archived_at": aware(archive.archived_at) if archive else None,
        },
        "thumbnail_url": thumbnail_url(upload, settings),
        # YouTube Data API (videos.list statistics), persisted by the status authority.
        # not_published: never public, counters are not audience numbers yet;
        # not_reported: public but no statistics stored; available: YouTube's values
        # (a returned 0 is a real 0).  After a remote deletion the last values remain.
        "live_stats": live_stats,
        "live_stats_state": "not_published" if not public else "available" if live_stats else "not_reported",
        # YouTube Analytics API, latest snapshot with data.
        "analytics": {
            "state": analytics_state,
            "fetched_at": aware(summary.fetched_at) if summary and summary.fetched_at else None,
            **{name: metrics.get(name) for name in SUMMARY_METRICS},
        },
        "youtube_actions": {"available": actions["available"], "reason": actions["reason"]},
        "watch_url": actions["watch_url"],
        "shorts_url": actions["shorts_url"],
        "studio_url": actions["studio_url"],
    }


def list_videos(
    db: Session,
    settings: Settings,
    *,
    status: str = "all",
    project: str = "all",
    analytics: str = "all",
    query: str = "",
    sort: str = "newest",
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    limit = max(1, min(MAX_PAGE_SIZE, limit))
    offset = max(0, offset)
    # Pass 1: a column projection of every library video (state inputs only).
    base = list(db.scalars(select(YouTubeUpload).options(load_only(*_STATE_COLUMNS)).where(library_condition())).all())
    summaries = _analytics_summaries(db)
    counts: dict[str, int] = dict.fromkeys(STATUS_FILTERS[1:], 0)
    analytics_counts = {"available": 0, "processing": 0}
    view_percentages: list[float] = []
    project_ids = set(db.scalars(
        select(Project.id).where(Project.id.in_(select(YouTubeUpload.project_id).where(library_condition()).scalar_subquery()))
    ).all())
    # Library-wide counts, independent of the current filters.
    for upload in base:
        counts[_status_bucket(library_state(upload, now))] += 1
        bucket = _analytics_bucket(_analytics_state(upload, summaries.get(upload.id), now))
        if bucket:
            analytics_counts[bucket] += 1
        value = _metric(summaries.get(upload.id), "averageViewPercentage")
        if value is not None:
            view_percentages.append(value)

    selected = base
    if query.strip():
        matching = set(db.scalars(
            select(YouTubeUpload.id)
            .outerjoin(Project, Project.id == YouTubeUpload.project_id)
            .outerjoin(YouTubeLearningArchive, YouTubeLearningArchive.project_id == YouTubeUpload.project_id)
            .where(library_condition(), _search_condition(query[:200]))
        ).all())
        selected = [upload for upload in selected if upload.id in matching]
    if project in {"available", "archived"}:
        selected = [upload for upload in selected if (upload.project_id in project_ids) == (project == "available")]
    if status in STATUS_FILTERS[1:]:
        selected = [upload for upload in selected if _status_bucket(library_state(upload, now)) == status]
    if analytics in {"available", "processing"}:
        selected = [upload for upload in selected if _analytics_bucket(_analytics_state(upload, summaries.get(upload.id), now)) == analytics]
    ordered = _order([(upload, summaries.get(upload.id)) for upload in selected], sort if sort in SORTS else "newest", now)
    page_ids = [upload.id for upload, _summary in ordered[offset:offset + limit]]

    # The page only: full rows (current status needs the schedule history), preview
    # backfill for still-existing projects, fingerprint facts via JSON paths.
    full = {
        upload.id: upload
        for upload in db.scalars(
            select(YouTubeUpload).where(YouTubeUpload.id.in_(page_ids)).execution_options(populate_existing=True)
        ).all()
    } if page_ids else {}
    page = [full[upload_id] for upload_id in page_ids if upload_id in full]
    projects = {
        row.id: (row.title, row.original_prompt)
        for row in db.execute(select(Project.id, Project.title, Project.original_prompt).where(Project.id.in_({item.project_id for item in page}))).all()
    } if page else {}
    archives = {
        item.project_id: item
        for item in db.scalars(select(YouTubeLearningArchive).where(YouTubeLearningArchive.project_id.in_({item.project_id for item in page}))).all()
    } if page else {}
    # One-time preview for uploads made before the library (or before their first view).
    backfill = [upload for upload in page if upload.project_id in projects and upload.library_thumbnail is None]
    for upload in backfill:
        ensure_library_thumbnail(db, upload, settings, commit=False)
    if backfill:
        db.commit()
    facts = _fingerprint_facts(db, [item.fingerprint_id for item in page if item.fingerprint_id])
    connection = db.get(YouTubeConnection, "primary")
    items = [
        serialize_video(
            upload,
            summary=summaries.get(upload.id),
            project=projects.get(upload.project_id),
            archive=archives.get(upload.project_id),
            facts=facts.get(upload.fingerprint_id or ""),
            connection=connection,
            settings=settings,
            now=now,
        )
        for upload in page
    ]
    return {
        "items": items,
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(ordered) else None,
        "filters": {"status": status, "project": project, "analytics": analytics, "query": query, "sort": sort if sort in SORTS else "newest"},
        "summary": {
            "total": len(base),
            **counts,
            "analytics_available": analytics_counts["available"],
            "analytics_processing": analytics_counts["processing"],
            "projects_available": sum(1 for upload in base if upload.project_id in project_ids),
            "projects_archived": sum(1 for upload in base if upload.project_id not in project_ids),
            # The median of each video's latest averageViewPercentage (n stated).
            "median_average_view_percentage": round(statistics.median(view_percentages), 2) if view_percentages else None,
            "median_average_view_percentage_n": len(view_percentages),
        },
        "connection": {
            "channel_id": connection.channel_id if connection else None,
            "channel_title": connection.channel_title if connection else None,
            "status": connection.status if connection else "not_connected",
        },
    }


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def find_video(db: Session, identifier: str) -> YouTubeUpload | None:
    """By upload id (stable internal id), or by YouTube video id."""
    upload = db.get(YouTubeUpload, identifier)
    if upload is None:
        upload = db.scalar(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id == identifier))
    if upload is None:
        return None
    return upload if db.scalar(select(YouTubeUpload.id).where(YouTubeUpload.id == upload.id, library_condition())) else None


def retention_curve(db: Session, snapshot_id: str | None, duration: float | None) -> list[dict[str, Any]]:
    """The stored raw points of one snapshot, exactly as returned (never interpolated)."""
    if not snapshot_id:
        return []
    points = db.scalars(
        select(YouTubeRetentionPoint)
        .where(YouTubeRetentionPoint.snapshot_id == snapshot_id, YouTubeRetentionPoint.audience_watch_ratio.is_not(None))
        .order_by(YouTubeRetentionPoint.elapsed_video_ratio)
    ).all()
    return [
        {
            "elapsed_video_ratio": point.elapsed_video_ratio,
            "second": round(point.elapsed_video_ratio * duration, 3) if duration else None,
            "audience_watch_ratio": point.audience_watch_ratio,
            "relative_retention_performance": point.relative_retention_performance,
        }
        for point in points
    ]


def _live_topic(project: Project) -> str | None:
    latest = max(project.revisions, key=lambda item: item.number, default=None) if project.revisions else None
    state: dict[str, Any] = latest.state if latest is not None and isinstance(latest.state, dict) else {}
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    return str(intent.get("topic") or "") or None


def production_context(fingerprint: dict[str, Any], *, prompt: str | None, topic: str | None) -> dict[str, Any]:
    content = fingerprint.get("content") or {}
    hook = fingerprint.get("hook") or {}
    visual_hook = hook.get("visual_hook") or {}
    quality = fingerprint.get("quality") or {}
    pacing = fingerprint.get("pacing") or {}
    visual = fingerprint.get("visual") or {}
    return {
        "available": bool(fingerprint),
        "prompt": prompt,
        "topic": topic,
        "format": content.get("format"),
        "duration_seconds": content.get("duration_seconds"),
        "scene_count": pacing.get("scene_count"),
        "mean_scene_seconds": pacing.get("mean_scene_seconds"),
        "hook_strategy": hook.get("strategy"),
        "verbal_hook": hook.get("verbal_hook"),
        "visual_hook": {key: visual_hook.get(key) for key in ("subject", "visual_strategy", "visual_goal")} if visual_hook else None,
        "on_screen_hook": hook.get("on_screen_hook"),
        "answer_reveal_seconds": content.get("answer_reveal_seconds"),
        "payoff_seconds": content.get("payoff_seconds"),
        "critic": {
            "status": quality.get("critic_status"),
            "issue_count": quality.get("issue_count"),
            "repairs_attempted": quality.get("repairs_attempted"),
            "repairs_successful": quality.get("repairs_successful"),
            "unresolved_issue_count": quality.get("unresolved_issue_count"),
        },
        "media_origin_counts": visual.get("media_origin_counts"),
    }


def publishing_context(upload: YouTubeUpload) -> dict[str, Any]:
    source = upload.schedule_source
    return {
        "requested_visibility": upload.requested_visibility,
        "requested_publish_at": aware(upload.publish_at),
        # The zone the schedule was chosen in (None for times set outside ClipForge).
        "schedule_timezone": upload.schedule_timezone,
        "schedule_source": source,
        "provenance": "Smart Scheduler slot" if source == "auto" else "Chosen manually" if source == "manual" else "Not scheduled",
        "smart_scheduler_selected": source == "auto",
        "slot_time": upload.schedule_slot_time,
        "local_time": upload.schedule_local_time,
        "timezone": upload.schedule_timezone,
        "schedule_status": upload.schedule_status,
        "uploaded_at": aware(upload.uploaded_at),
    }


def video_detail(db: Session, upload: YouTubeUpload, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    from .learning import fingerprint_for, performance_report

    now = now or _now()
    project_row = db.get(Project, upload.project_id)
    archive = db.scalar(select(YouTubeLearningArchive).where(YouTubeLearningArchive.project_id == upload.project_id))
    if project_row is not None:
        ensure_library_thumbnail(db, upload, settings)
    summaries = _analytics_summaries(db, [upload.id])
    fingerprint = fingerprint_for(db, upload)
    content = fingerprint.get("content") or {}
    pacing = fingerprint.get("pacing") or {}
    facts = {"duration_seconds": content.get("duration_seconds"), "format": content.get("format"), "scene_count": pacing.get("scene_count")}
    project = (project_row.title, project_row.original_prompt) if project_row is not None else None
    video = serialize_video(
        upload, summary=summaries.get(upload.id), project=project, archive=archive, facts=facts,
        connection=db.get(YouTubeConnection, "primary"), settings=settings, now=now,
    )
    report = performance_report(db, upload, min_sample=settings.youtube_baseline_min_sample)
    duration = float(content.get("duration_seconds") or 0) or None
    curve = retention_curve(db, (report.get("retention") or {}).get("snapshot_id"), duration)
    scenes = report.get("scene_retention") or []
    topic = video["topic"] or (_live_topic(project_row) if project_row is not None else None)
    return {
        "video": {**video, "topic": topic},
        "upload": serialize_upload(upload),
        "performance": report,
        "retention_curve": curve,
        "major_drops": [scene for scene in scenes if scene.get("notable_drop")],
        "production": production_context(fingerprint, prompt=video["prompt"], topic=topic),
        "publishing": publishing_context(upload),
    }


def recent_videos(db: Session, channel_id: str, limit: int = RECENT_REFRESH_LIMIT) -> list[YouTubeUpload]:
    """The newest library videos on the connected channel that still exist on YouTube."""
    rows = db.scalars(
        select(YouTubeUpload)
        .where(library_condition(), YouTubeUpload.channel_id == channel_id, YouTubeUpload.deleted_on_youtube.is_(False))
        .order_by(func.coalesce(YouTubeUpload.published_at, YouTubeUpload.uploaded_at, YouTubeUpload.created_at).desc())
        .limit(max(1, min(limit, RECENT_REFRESH_LIMIT * 2)))
    ).all()
    return list(rows)
