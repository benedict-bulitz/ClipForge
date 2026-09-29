"""Revision-safe, resumable YouTube uploads (the one uploader).

Uploads read ClipForge's canonical final render directly - no export or
download step.  Identity is never inferred from titles: every row is keyed by
the connected ``channel_id``, ``project_id``, the render revision and the
SHA-256 of the exact uploaded MP4.  ``idempotency_key`` makes a second upload of the
same render impossible without an explicit, safe user action.
"""
from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings
from ..exporter import ExportUnavailable, resolve_final_master
from ..models import Project, YouTubeUpload
from ..security.secrets import SecretStore
from ..services import effective_revision_state
from . import schedule as schedule_authority
from . import status as status_authority
from .connection import access_token
from .fingerprint import get_or_create_fingerprint
from .provider import YouTubeApiError, YouTubeProvider, read_chunks
from .publishing import (
    PublishOptions,
    ScheduleChoice,
    ScheduleResolution,
    ThumbnailChoice,
    insert_body,
    load_defaults,
    prepare_thumbnail,
    record_last_used,
    resolve_schedule,
    thumbnail_choices,
    validate_options,
)

logger = logging.getLogger(__name__)

UPLOAD_STATES = ("pending", "uploading", "uploaded", "processing", "ready", "failed")
ACTIVE_STATES = ("pending", "uploading")
# Mutable status fields that videos.update(part=status) would otherwise reset.
MUTABLE_STATUS_FIELDS = (
    "privacyStatus",
    "publishAt",
    "license",
    "embeddable",
    "publicStatsViewable",
    "selfDeclaredMadeForKids",
    "containsSyntheticMedia",
)


class UploadRefused(RuntimeError):
    def __init__(self, code: str, message: str, upload: YouTubeUpload | None = None, *, issues: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.upload = upload
        self.issues = issues or []


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything ClipForge stores here is UTC."""
    return _utc(value)


def parse_google_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def google_time(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Source resolution: the canonical final render, no export step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadSource:
    path: Path
    kind: str
    project_revision: int
    render_revision: int
    current_state: dict[str, Any]
    render_state: dict[str, Any]
    warnings: tuple[str, ...] = ()


def resolve_upload_source(project: Project, settings: Settings) -> UploadSource:
    """The final video of the *current* revision, straight from ClipForge storage."""
    state = effective_revision_state(project)
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    if render.get("status") != "complete" or render.get("stale"):
        raise UploadRefused("not_rendered", "Render this revision before uploading it to YouTube.")
    try:
        master = resolve_final_master(project.id, project.title, state, settings)
    except ExportUnavailable as exc:
        raise UploadRefused("render_missing", str(exc)) from exc
    render_revision = master.render_revision or project.current_revision
    revisions = {revision.number: revision for revision in project.revisions}
    render_state = copy.deepcopy(revisions[render_revision].state) if render_revision in revisions else state
    return UploadSource(
        path=master.path,
        kind=master.kind,
        project_revision=project.current_revision,
        render_revision=render_revision,
        current_state=state,
        render_state=render_state,
        warnings=master.warnings,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def cached_sha256(path: Path) -> str:
    """SHA-256 keyed by path, size and mtime (final files are replaced atomically)."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _SHA_CACHE:
        if len(_SHA_CACHE) > 64:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = file_sha256(path)
    return _SHA_CACHE[key]


# ---------------------------------------------------------------------------
# Request / idempotency
# ---------------------------------------------------------------------------


def idempotency_key(channel_id: str, project_id: str, render_sha256: str) -> str:
    return f"{channel_id}:{project_id}:{render_sha256}"


def _reupload_allowed(upload: YouTubeUpload) -> bool:
    """A new upload of an already-mapped render is safe only when YouTube lost it."""
    return bool(
        upload.deleted_on_youtube
        or upload.upload_status in {"rejected", "failed", "deleted"}
        or upload.last_error_code == "session_expired_unknown_outcome"
    )


def _apply_options(upload: YouTubeUpload, options: PublishOptions, body: dict[str, Any], resolution: ScheduleResolution | None) -> None:
    upload.title = body["snippet"]["title"][:120]
    upload.description = body["snippet"]["description"]
    upload.tags = list(body["snippet"].get("tags") or [])
    upload.made_for_kids = bool(options.made_for_kids)
    upload.contains_synthetic_media = bool(options.contains_synthetic_media)
    upload.requested_visibility = options.visibility
    upload.notify_subscribers = options.notify_subscribers
    upload.upload_settings = {"body": body, "notify_subscribers": options.notify_subscribers, "options": options.model_dump()}
    if options.thumbnail is None or options.thumbnail.source == "youtube_auto":
        upload.thumbnail_source, upload.thumbnail_asset, upload.thumbnail_upload_status = "youtube_auto", None, "not_requested"
    else:
        upload.thumbnail_source, upload.thumbnail_asset, upload.thumbnail_upload_status = options.thumbnail.source, options.thumbnail.asset, "pending"
    upload.thumbnail_failure_reason = None
    if resolution is not None and resolution.status == "ok":
        upload.publish_at = resolution.publish_at
        upload.schedule_local_time = resolution.local_time
        upload.schedule_timezone = resolution.timezone
        upload.schedule_status = "scheduled"
        upload.schedule_source = "auto" if options.schedule_source == "auto" else "manual"
        upload.schedule_slot_time = options.schedule.time if options.schedule_source == "auto" and options.schedule else None
    else:
        upload.publish_at, upload.schedule_local_time, upload.schedule_timezone, upload.schedule_status = None, None, None, "none"
        upload.schedule_source, upload.schedule_slot_time = None, None


def preflight(
    db: Session,
    project: Project,
    settings: Settings,
    options: PublishOptions,
    *,
    categories: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, str]], ScheduleResolution | None, UploadSource | None]:
    issues: list[dict[str, str]] = []
    try:
        source = resolve_upload_source(project, settings)
    except UploadRefused as exc:
        issues.append({"field": "video", "message": exc.message})
        source = None
    defaults = load_defaults(db)
    state = source.current_state if source else effective_revision_state(project)
    option_issues, resolution = validate_options(
        options, defaults=defaults, thumbnails=thumbnail_choices(state, project.id, settings), categories=categories, now=now,
    )
    return issues + option_issues, resolution, source


def request_upload(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    channel_id: str,
    options: PublishOptions,
    categories: list[dict[str, Any]] | None = None,
    force_new: bool = False,
    now: datetime | None = None,
) -> tuple[YouTubeUpload, UploadSource, bool]:
    """Create (or reuse) the mapping for the current final render.

    Returns ``(upload, source, should_run)``.  Nothing is sent to YouTube if
    the preflight finds anything missing (``UploadRefused("preflight_failed")``).
    """
    issues, resolution, source = preflight(db, project, settings, options, categories=categories, now=now)
    if issues or source is None:
        raise UploadRefused("preflight_failed", f"{len(issues)} item{'s' if len(issues) != 1 else ''} need{'s' if len(issues) == 1 else ''} attention.", issues=issues)
    body = insert_body(options, resolution)
    sha = file_sha256(source.path)
    key = idempotency_key(channel_id, project.id, sha)
    existing = db.scalar(select(YouTubeUpload).where(YouTubeUpload.idempotency_key == key))
    if existing is not None:
        if existing.youtube_video_id or existing.last_error_code == "session_expired_unknown_outcome":
            if not force_new:
                code = "already_uploaded" if existing.youtube_video_id else "unknown_outcome"
                message = (
                    f"Already uploaded as {existing.youtube_video_id}."
                    if existing.youtube_video_id
                    else "An earlier upload may have completed on YouTube. Check YouTube Studio, then confirm a new upload."
                )
                raise UploadRefused(code, message, existing)
            if not _reupload_allowed(existing):
                raise UploadRefused("already_uploaded", f"Already uploaded as {existing.youtube_video_id}.", existing)
            existing.idempotency_key = None  # superseded, its history stays intact
            db.commit()
        elif existing.state in ACTIVE_STATES:
            return existing, source, False
        else:
            # A failed attempt that never produced a video: resume with the new settings.
            existing.state = "pending"
            existing.last_error_code = None
            existing.last_error_message = None
            existing.project_revision = source.project_revision
            if not existing.upload_session_uri:
                _apply_options(existing, options, body, resolution)
            db.commit()
            return existing, source, True
    fingerprint = get_or_create_fingerprint(
        db,
        source.render_state,
        upload_state=source.current_state,
        project_id=project.id,
        render_revision=source.render_revision,
        render_sha256=sha,
        file_size=source.path.stat().st_size,
    )
    upload = YouTubeUpload(
        project_id=project.id,
        project_revision=source.project_revision,
        render_revision=source.render_revision,
        render_sha256=sha,
        render_file_size=source.path.stat().st_size,
        render_content_hash=((source.render_state.get("content_hashes") or {}).get("render")),
        source_kind=source.kind,
        channel_id=channel_id,
        idempotency_key=key,
        fingerprint_id=fingerprint.id,
        state="pending",
        privacy_status="private",
    )
    _apply_options(upload, options, body, resolution)
    db.add(upload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(select(YouTubeUpload).where(YouTubeUpload.idempotency_key == key))
        if raced is None:
            raise
        return raced, source, False
    db.refresh(upload)
    return upload, source, True


def claim_upload(db: Session, upload_id: str) -> bool:
    """Atomically move a pending/failed row to uploading (prevents double runs)."""
    result = db.execute(
        update(YouTubeUpload)
        .where(
            YouTubeUpload.id == upload_id,
            YouTubeUpload.state.in_(("pending", "failed")),
            YouTubeUpload.youtube_video_id.is_(None),
        )
        .values(state="uploading", updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _fail(db: Session, upload: YouTubeUpload, code: str, message: str, *, keep_session: bool = True) -> None:
    upload.state = "failed"
    upload.last_error_code = code
    upload.last_error_message = message[:500]
    if not keep_session:
        upload.upload_session_uri = None
    db.commit()
    if not upload.youtube_video_id:
        # A failed upload holds no slot; a retry claims it again.
        _schedule_hook(db, upload, lambda: schedule_authority.release_for_upload(db, upload.id, f"upload_failed:{code}"))


def _schedule_hook(db: Session, upload: YouTubeUpload, action: Callable[[], Any]) -> None:
    """Slot bookkeeping must never fail an upload or a status sync."""
    try:
        action()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Could not update the slot reservation upload_id=%s", upload.id)


def _record_video(db: Session, upload: YouTubeUpload, video: dict[str, Any]) -> None:
    video_id = str(video.get("id") or "")
    if not video_id:
        raise YouTubeApiError("provider_error", "YouTube finished the upload without returning a video ID.")
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    upload.youtube_video_id = video_id
    upload.state = "uploaded"
    upload.uploaded_at = _now()
    upload.privacy_status = str(status.get("privacyStatus") or "private")
    upload.upload_status = status.get("uploadStatus")
    upload.upload_session_uri = None
    upload.bytes_uploaded = upload.render_file_size
    upload.last_error_code = None
    upload.last_error_message = None
    # Persist the video ID before anything else can fail.
    db.commit()
    _remember_successful_settings(db, upload)
    # YouTube's answer confirms (publishAt returned) or releases the slot claim.
    _schedule_hook(db, upload, lambda: schedule_authority.on_video_recorded(db, upload, video))


def _remember_successful_settings(db: Session, upload: YouTubeUpload) -> None:
    """YouTube accepted the upload: its reusable settings become the channel's preset."""
    options = (upload.upload_settings or {}).get("options") or {}
    if not options:
        return
    schedule = options.get("schedule") if options.get("visibility") == "schedule" else None
    try:
        record_last_used(db, upload.channel_id, options, schedule=schedule)
    except SQLAlchemyError:  # a preset is a convenience; never fail a finished upload
        db.rollback()
        logger.warning("Could not store the last-used upload preset upload_id=%s", upload.id)


def run_upload(
    db: Session,
    upload_id: str,
    path: Path,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
) -> YouTubeUpload | None:
    """Upload (or resume) one claimed row.  Never raises; failures are persisted."""
    if not claim_upload(db, upload_id):
        return db.get(YouTubeUpload, upload_id)
    upload = db.get(YouTubeUpload, upload_id)
    if upload is None:
        return None
    db.refresh(upload)
    upload.attempt_count += 1
    db.commit()
    try:
        connection, token = access_token(db, settings, store, provider, capability="upload")
        if connection.channel_id != upload.channel_id:
            _fail(db, upload, "wrong_channel", "YouTube is now connected to a different channel than this upload was prepared for.")
            return upload
        size = upload.render_file_size
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != upload.render_sha256:
                _fail(db, upload, "export_changed", "The exported MP4 changed since this upload was prepared. Upload the new export instead.", keep_session=False)
                return upload
            offset = 0
            if upload.upload_session_uri:
                try:
                    progress = provider.query_upload(token, upload.upload_session_uri, size)
                except YouTubeApiError as exc:
                    if exc.code != "upload_session_expired":
                        raise
                    if upload.bytes_uploaded >= size:
                        # Every byte may have reached YouTube; a new session could duplicate.
                        _fail(db, upload, "session_expired_unknown_outcome", "The upload session expired after the final bytes were sent. Check YouTube Studio before uploading again.", keep_session=False)
                        return upload
                    upload.upload_session_uri = None
                    upload.bytes_uploaded = 0
                    db.commit()
                    progress = None
                if progress is not None:
                    if progress.complete and progress.video:
                        _record_video(db, upload, progress.video)
                        return upload
                    offset = progress.offset
            if not upload.upload_session_uri:
                body = (upload.upload_settings or {}).get("body")
                if not body or body.get("status", {}).get("selfDeclaredMadeForKids") is None:
                    # Never send a video without an explicit audience answer.
                    _fail(db, upload, "settings_missing", "This upload has no publishing settings. Start it again from the publishing sheet.", keep_session=False)
                    return upload
                upload.upload_session_uri = provider.start_resumable_upload(
                    token, body, size, "video/mp4",
                    notify_subscribers=bool((upload.upload_settings or {}).get("notify_subscribers", True)),
                )
                upload.bytes_uploaded = 0
                db.commit()  # the session exists before any byte is sent
                offset = 0
            for chunk_offset, data in read_chunks(handle, offset):
                final = chunk_offset + len(data) >= size
                confirmed = chunk_offset
                if final:
                    upload.bytes_uploaded = size  # final bytes in flight
                    db.commit()
                try:
                    progress = provider.upload_chunk(token, upload.upload_session_uri, data, chunk_offset, size)
                except YouTubeApiError as exc:
                    # A lost response to the final chunk leaves the outcome
                    # unknown: keep "all bytes sent" so an expired session can
                    # never silently lead to a second video.
                    if not (final and exc.code in {"network_timeout", "network_error", "provider_error"}):
                        upload.bytes_uploaded = confirmed
                    db.commit()
                    raise
                if progress.complete:
                    if not progress.video:
                        raise YouTubeApiError("provider_error", "YouTube finished the upload without returning a video.")
                    _record_video(db, upload, progress.video)
                    return upload
                upload.bytes_uploaded = progress.offset
                db.commit()
            # All bytes sent but no completion: ask the session for the result.
            progress = provider.query_upload(token, upload.upload_session_uri, size)
            if progress.complete and progress.video:
                _record_video(db, upload, progress.video)
                return upload
            _fail(db, upload, "upload_incomplete", "YouTube did not confirm the upload. Retry to resume it.")
            return upload
    except YouTubeApiError as exc:
        _fail(db, upload, exc.code, exc.message, keep_session=exc.code != "upload_session_expired")
        return upload
    except OSError:
        _fail(db, upload, "export_missing", "The exported MP4 could not be read.")
        return upload
    except Exception:  # an upload failure must never corrupt project state
        logger.exception("YouTube upload failed unexpectedly upload_id=%s", upload_id)
        db.rollback()
        upload = db.get(YouTubeUpload, upload_id)
        if upload is not None:
            _fail(db, upload, "internal_error", "The upload failed unexpectedly. Retry to resume it.")
        return upload


def mark_interrupted_uploads(db: Session) -> int:
    """After a restart no upload thread is alive; keep the session for resuming."""
    result = db.execute(
        update(YouTubeUpload)
        .where(YouTubeUpload.state == "uploading")
        .values(state="failed", last_error_code="interrupted", last_error_message="The upload was interrupted. Retry to resume it.")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Status sync and scheduling
# ---------------------------------------------------------------------------


def apply_video_resource(upload: YouTubeUpload, item: dict[str, Any], *, now: datetime | None = None) -> None:
    """Kept for callers/tests: YouTube's answer is applied only by the status authority."""
    status_authority.apply_remote_video(upload, item, now=now or _now())


STATUS_PARTS = "status,snippet,processingDetails,statistics,paidProductPlacementDetails"


def sync_status(
    db: Session,
    upload: YouTubeUpload,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    now: datetime | None = None,
) -> YouTubeUpload:
    """The one reconciliation path: ask videos.list, persist YouTube's answer.

    A failure is recorded (stale) and re-raised; the last confirmed remote
    state is never changed by it.
    """
    if not upload.youtube_video_id:
        raise UploadRefused("not_uploaded", "This upload has no YouTube video yet.", upload)
    now = now or _now()
    try:
        connection, token = access_token(db, settings, store, provider, capability="read")
        if connection.channel_id != upload.channel_id:
            raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this video belongs to.")
        items = provider.list_videos(token, [upload.youtube_video_id], STATUS_PARTS)
    except YouTubeApiError as exc:
        db.rollback()
        status_authority.record_refresh_failure(upload, exc.code, exc.message, now=now)
        db.commit()
        raise
    upload.last_status_sync_at = now
    if not items:
        status_authority.apply_remote_missing(upload, now=now)
    else:
        if upload.last_error_code == "deleted_on_youtube":
            upload.last_error_code = None
            upload.last_error_message = None
        status_authority.apply_remote_video(upload, items[0], now=now)
    db.commit()
    if not items:
        _schedule_hook(db, upload, lambda: schedule_authority.release_for_upload(db, upload.id, "deleted_on_youtube"))
    else:
        _schedule_hook(db, upload, lambda: schedule_authority.on_video_recorded(db, upload, items[0], now=now))
    db.refresh(upload)
    return upload


def reconcile_if_due(
    db: Session, upload: YouTubeUpload, settings: Settings, store: SecretStore, provider: YouTubeProvider, *, now: datetime | None = None
) -> bool:
    """Page-open / poll reconciliation, gated by freshness. Never raises."""
    now = now or _now()
    if not status_authority.needs_reconcile(upload, now=now):
        return False
    try:
        sync_status(db, upload, settings, store, provider, now=now)
    except (YouTubeApiError, UploadRefused):
        return False
    return True


def _status_update(
    db: Session,
    upload: YouTubeUpload,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    changes: dict[str, Any],
    *,
    require_private: bool = False,
) -> dict[str, Any]:
    """videos.update(part=status) that preserves every other status field."""
    connection, token = access_token(db, settings, store, provider, capability="schedule")
    if connection.channel_id != upload.channel_id:
        raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this video belongs to.")
    items = provider.list_videos(token, [upload.youtube_video_id], "status")
    if not items:
        upload.deleted_on_youtube = True
        raise YouTubeApiError("deleted_on_youtube", "This video no longer exists on YouTube.")
    current = items[0].get("status") if isinstance(items[0].get("status"), dict) else {}
    if require_private and current.get("privacyStatus") != "private":
        upload.remote_privacy_status = str(current.get("privacyStatus") or upload.remote_privacy_status)
        raise YouTubeApiError("not_private", "YouTube schedules only private videos; this video is no longer private.")
    status = {name: current[name] for name in MUTABLE_STATUS_FIELDS if name in current}
    if "selfDeclaredMadeForKids" not in status and upload.made_for_kids is not None:
        status["selfDeclaredMadeForKids"] = upload.made_for_kids
    if "containsSyntheticMedia" not in status and upload.contains_synthetic_media is not None:
        status["containsSyntheticMedia"] = upload.contains_synthetic_media
    status.update(changes)
    result = provider.update_video(token, {"id": upload.youtube_video_id, "status": status}, "status")
    return (result.get("status") or {}) if isinstance(result, dict) else {}


def schedule_publication(
    db: Session,
    upload: YouTubeUpload,
    choice: ScheduleChoice,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    now: datetime | None = None,
) -> YouTubeUpload:
    """Private + publishAt at the wall time the user chose in their time zone."""
    if not upload.youtube_video_id or upload.deleted_on_youtube:
        raise UploadRefused("not_uploaded", "Only a video that exists on YouTube can be scheduled.", upload)
    if upload.published_at is not None or status_authority.current_state(upload, now or _now()) in status_authority.LIVE_STATES:
        raise UploadRefused("already_published", "This video was already published; YouTube only schedules private, never-published videos.", upload)
    resolution = resolve_schedule(choice, now=now)
    if resolution.status != "ok" or resolution.publish_at is None:
        raise UploadRefused("invalid_time", resolution.message or "Choose a valid publication time.", upload)
    try:
        status = _status_update(
            db, upload, settings, store, provider,
            {"privacyStatus": "private", "publishAt": google_time(resolution.publish_at)}, require_private=True,
        )
    except YouTubeApiError as exc:
        upload.schedule_status = "schedule_failed"
        upload.schedule_error = exc.message[:500]
        db.commit()
        _schedule_hook(db, upload, lambda: schedule_authority.release_for_upload(db, upload.id, "schedule_failed"))
        raise
    record = dict(upload.upload_settings or {})
    history = list(record.get("schedule_history") or [])
    if upload.publish_at is not None:
        history.append({"publish_at": google_time(upload.publish_at), "local_time": upload.schedule_local_time, "timezone": upload.schedule_timezone, "replaced_at": google_time(now or _now())})
    record["schedule_history"] = history
    upload.upload_settings = record
    # The request (provenance) and YouTube's confirmation are stored separately.
    upload.publish_at = resolution.publish_at
    upload.schedule_local_time = resolution.local_time
    upload.schedule_timezone = resolution.timezone
    upload.remote_publish_at = parse_google_time(status.get("publishAt")) or upload.remote_publish_at
    upload.schedule_status = "scheduled"
    upload.schedule_error = None
    upload.schedule_source, upload.schedule_slot_time = "manual", None
    db.commit()
    db.refresh(upload)
    _schedule_hook(db, upload, lambda: _record_manual_schedule(db, upload, resolution, choice, status, now=now))
    try:
        record_last_used(db, upload.channel_id, {"visibility": "schedule"}, schedule=choice.model_dump())
    except SQLAlchemyError:  # convenience only
        db.rollback()
    return upload


def _record_manual_schedule(db: Session, upload: YouTubeUpload, resolution: ScheduleResolution, choice: ScheduleChoice, status: dict[str, Any], *, now: datetime | None) -> None:
    """A (re)schedule YouTube accepted: its slot is confirmed; the old claim is gone."""
    schedule_authority.release_for_upload(db, upload.id, "rescheduled")
    confirmed = parse_google_time(status.get("publishAt")) or resolution.publish_at
    if confirmed is None:
        return
    reservation = schedule_authority.reserve(
        db, upload.channel_id, publish_at=confirmed, local_time=resolution.local_time or choice.time,
        timezone=choice.timezone, source="manual", project_id=upload.project_id, now=now,
    )
    schedule_authority.attach_upload(db, reservation, upload)
    schedule_authority.confirm_for_upload(db, upload, confirmed, now=now)
    schedule_authority.record_video(db, upload.channel_id, {"id": upload.youtube_video_id, "status": {**status, "privacyStatus": "private", "publishAt": google_time(confirmed)}, "snippet": {"title": upload.title}}, now=now)


def set_audience(
    db: Session, upload: YouTubeUpload, made_for_kids: bool, settings: Settings, store: SecretStore, provider: YouTubeProvider
) -> YouTubeUpload:
    """Answer (or correct) the audience question on a video that already exists."""
    if not upload.youtube_video_id or upload.deleted_on_youtube:
        raise UploadRefused("not_uploaded", "This video does not exist on YouTube.", upload)
    status = _status_update(db, upload, settings, store, provider, {"selfDeclaredMadeForKids": bool(made_for_kids)})
    upload.made_for_kids = bool(made_for_kids)
    declared = status.get("selfDeclaredMadeForKids", status.get("madeForKids"))
    upload.made_for_kids_confirmed = declared if isinstance(declared, bool) else None
    db.commit()
    db.refresh(upload)
    return upload


# ---------------------------------------------------------------------------
# After the video exists: thumbnail + read-back (never fails the upload)
# ---------------------------------------------------------------------------


def apply_thumbnail(
    db: Session, upload: YouTubeUpload, settings: Settings, store: SecretStore, provider: YouTubeProvider
) -> YouTubeUpload:
    if not upload.youtube_video_id:
        raise UploadRefused("not_uploaded", "The video must exist on YouTube before its thumbnail can be set.", upload)
    if upload.thumbnail_source in {None, "youtube_auto"}:
        upload.thumbnail_upload_status = "not_requested"
        db.commit()
        return upload
    project = db.get(Project, upload.project_id)
    if project is None:
        upload.thumbnail_upload_status = "failed"
        upload.thumbnail_failure_reason = "The ClipForge project and its cover files were deleted."
        db.commit()
        return upload
    upload.thumbnail_upload_status = "pending"
    db.commit()
    try:
        from ..services import effective_revision_state as current_state

        prepared = prepare_thumbnail(
            ThumbnailChoice(source=upload.thumbnail_source, asset=upload.thumbnail_asset),  # type: ignore[arg-type]
            current_state(project), project.id, settings,
        )
        if prepared is None:
            upload.thumbnail_upload_status = "not_requested"
            db.commit()
            return upload
        connection, token = access_token(db, settings, store, provider, capability="upload")
        if connection.channel_id != upload.channel_id:
            raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this video belongs to.")
        provider.set_thumbnail(token, upload.youtube_video_id, prepared.data, prepared.content_type)
    except (YouTubeApiError, ValueError, OSError) as exc:
        upload.thumbnail_upload_status = "failed"
        upload.thumbnail_failure_reason = (exc.message if isinstance(exc, YouTubeApiError) else str(exc))[:500]
        db.commit()
        return upload
    upload.thumbnail_upload_status = "applied"
    upload.thumbnail_sha256 = prepared.sha256
    upload.thumbnail_failure_reason = None
    upload.thumbnail_applied_at = _now()
    db.commit()
    return upload


def after_upload(
    db: Session, upload: YouTubeUpload, settings: Settings, store: SecretStore, provider: YouTubeProvider
) -> YouTubeUpload:
    """Thumbnail first, then read YouTube's stored settings back."""
    if not upload.youtube_video_id:
        return upload
    apply_thumbnail(db, upload, settings, store, provider)
    try:
        sync_status(db, upload, settings, store, provider)
    except (YouTubeApiError, UploadRefused):
        pass  # the video ID is saved; status can be refreshed later
    return upload


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def youtube_links(video_id: str | None) -> dict[str, str | None]:
    if not video_id:
        return {"watch_url": None, "shorts_url": None, "studio_url": None}
    return {
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "shorts_url": f"https://www.youtube.com/shorts/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }


_LIFECYCLE = {
    "uploading": "uploading",
    "upload_failed": "failed",
    "rejected": "failed",
    "processing_failed": "failed",
    "deleted": "deleted",
    "published": "published",
    "unlisted": "published",
    "scheduled": "scheduled",
    "publish_pending": "private",
    "private": "private",
}


def lifecycle(upload: YouTubeUpload, *, now: datetime | None = None) -> str:
    """Coarse bucket of the *current* (remote-driven) state; see status.current_state."""
    return _LIFECYCLE[status_authority.current_state(upload, now or _now())]


def video_status(upload: YouTubeUpload) -> str:
    """Uploading | Uploaded | Processing | Ready | Failed | Deleted - independent of visibility."""
    if upload.deleted_on_youtube:
        return "deleted"
    if upload.state in ACTIVE_STATES:
        return "uploading"
    if upload.state == "failed":
        return "failed"
    return {"uploaded": "uploaded", "processing": "processing", "ready": "ready"}.get(upload.state, upload.state)


def serialize_upload(upload: YouTubeUpload) -> dict[str, Any]:
    size = max(1, upload.render_file_size or 1)
    return {
        "id": upload.id,
        "project_id": upload.project_id,
        "project_revision": upload.project_revision,
        "render_revision": upload.render_revision,
        "render_sha256": upload.render_sha256,
        "channel_id": upload.channel_id,
        "youtube_video_id": upload.youtube_video_id,
        "state": upload.state,
        "lifecycle": lifecycle(upload),
        "current": status_authority.current_status(upload),
        "next_status_check_in_seconds": status_authority.poll_interval(upload),
        "privacy_status": upload.remote_privacy_status or upload.privacy_status,
        "publish_at": aware(upload.publish_at),  # requested (historical)
        "schedule_status": upload.schedule_status,
        "schedule_error": upload.schedule_error,
        "published_at": aware(upload.published_at),
        "published_source": upload.published_source,
        "upload_status": upload.upload_status,
        "processing_status": upload.processing_status,
        "failure_reason": upload.failure_reason,
        "rejection_reason": upload.rejection_reason,
        "content_type": upload.content_type,
        "deleted_on_youtube": upload.deleted_on_youtube,
        "title": upload.title,
        "video_status": video_status(upload),
        "thumbnail": {
            "status": upload.thumbnail_upload_status,
            "source": upload.thumbnail_source,
            "asset": upload.thumbnail_asset,
            "failure_reason": upload.thumbnail_failure_reason,
            "applied_at": aware(upload.thumbnail_applied_at),
        },
        "audience": {
            "made_for_kids": upload.made_for_kids,
            "confirmed_by_youtube": upload.made_for_kids_confirmed,
            "answered": upload.made_for_kids_confirmed is not None or upload.made_for_kids is not None,
        },
        "contains_synthetic_media": upload.contains_synthetic_media,
        "requested_visibility": upload.requested_visibility,
        "visibility_restricted": upload.visibility_restricted,
        "schedule": {
            "status": upload.schedule_status,
            "publish_at": aware(upload.publish_at),
            "local_time": upload.schedule_local_time,
            "timezone": upload.schedule_timezone,
            "error": upload.schedule_error,
        },
        "settings": (upload.upload_settings or {}).get("options"),
        "source_kind": upload.source_kind,
        "progress": round(min(1.0, (upload.bytes_uploaded or 0) / size), 3) if upload.state in ACTIVE_STATES else None,
        "error": {"code": upload.last_error_code, "message": upload.last_error_message} if upload.last_error_code else None,
        "is_active_mapping": upload.idempotency_key is not None,
        "can_reupload": _reupload_allowed(upload),
        "uploaded_at": aware(upload.uploaded_at),
        "last_status_sync_at": aware(upload.last_status_sync_at),
        "last_analytics_sync_at": aware(upload.last_analytics_sync_at),
        "created_at": aware(upload.created_at),
        **youtube_links(upload.youtube_video_id),
    }


UploadDispatcher = Callable[[str, Path], None]
