"""Private-first, revision-safe, resumable YouTube uploads (the one uploader).

Identity is never inferred from titles: every row is keyed by the connected
``channel_id``, ``project_id``, the exported render revision and the SHA-256
of the exact exported MP4.  ``idempotency_key`` makes a second upload of the
same render impossible without an explicit, safe user action.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..exporter import ExportUnavailable, exported_video_path
from ..models import ProductionFingerprint, Project, YouTubeUpload
from ..security.secrets import SecretStore
from ..services import effective_revision_state
from .connection import access_token
from .fingerprint import get_or_create_fingerprint
from .provider import YouTubeApiError, YouTubeProvider, read_chunks

logger = logging.getLogger(__name__)

UPLOAD_STATES = ("pending", "uploading", "uploaded", "processing", "ready", "failed")
ACTIVE_STATES = ("pending", "uploading")
TITLE_LIMIT = 100
DESCRIPTION_LIMIT_BYTES = 5000
TAGS_LIMIT_CHARS = 500
MAX_DESCRIPTION_HASHTAGS = 15
MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_SCHEDULE_AHEAD = timedelta(days=365)
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
    def __init__(self, code: str, message: str, upload: YouTubeUpload | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.upload = upload


@dataclass(frozen=True)
class ExportTarget:
    path: Path
    project_revision: int
    render_revision: int
    current_state: dict[str, Any]
    render_state: dict[str, Any]


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
# Export resolution and metadata
# ---------------------------------------------------------------------------


def resolve_export(project: Project, settings: Settings) -> ExportTarget:
    """The canonical exported MP4 of the *current* revision, or a clear refusal."""
    state = effective_revision_state(project)
    export = state.get("export") if isinstance(state.get("export"), dict) else {}
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    if export.get("status") != "exported":
        raise UploadRefused("not_exported", "Export the MP4 first; ClipForge uploads the final exported video.")
    if (
        render.get("status") != "complete"
        or render.get("stale")
        or not render.get("exported")
        or render.get("url") != export.get("media_url")
    ):
        raise UploadRefused("export_outdated", "This revision changed after the last export. Export the MP4 again before uploading.")
    try:
        path = exported_video_path(project.id, project.title, export, settings)
    except ExportUnavailable as exc:
        raise UploadRefused("export_missing", str(exc)) from exc
    render_revision = int(render.get("revision") or export.get("source_revision") or project.current_revision)
    revisions = {revision.number: revision for revision in project.revisions}
    render_state = copy.deepcopy(revisions[render_revision].state) if render_revision in revisions else state
    return ExportTarget(
        path=path,
        project_revision=project.current_revision,
        render_revision=render_revision,
        current_state=state,
        render_state=render_state,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def cached_sha256(path: Path) -> str:
    """SHA-256 keyed by path, size and mtime (the canonical export is replaced atomically)."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _SHA_CACHE:
        if len(_SHA_CACHE) > 64:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = file_sha256(path)
    return _SHA_CACHE[key]


def _clean_text(value: Any) -> str:
    # YouTube rejects "<" and ">" in titles and descriptions.
    return re.sub(r"[<>]", "", str(value or "")).replace("\r\n", "\n").strip()


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore").rstrip()


def youtube_metadata(state: dict[str, Any], project: Project) -> dict[str, Any]:
    """Title/description/tags from the saved YouTube social metadata, within API limits."""
    platforms = ((state.get("social_metadata") or {}).get("platforms") or {})
    youtube = platforms.get("youtube") if isinstance(platforms.get("youtube"), dict) else {}
    title = _clean_text(youtube.get("title")) or _clean_text(project.title) or "ClipForge video"
    title = " ".join(title.split())
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT].rstrip()
    hashtags = []
    for tag in youtube.get("hashtags") or []:
        clean = re.sub(r"[^\w]", "", str(tag).lstrip("#"), flags=re.UNICODE)
        if clean and clean.casefold() not in {item.casefold() for item in hashtags}:
            hashtags.append(clean)
    description = _clean_text(youtube.get("description"))
    missing = [tag for tag in hashtags[:MAX_DESCRIPTION_HASHTAGS] if f"#{tag}".casefold() not in description.casefold()]
    if missing:
        description = f"{description}\n\n{' '.join(f'#{tag}' for tag in missing)}".strip()
    description = _truncate_bytes(description, DESCRIPTION_LIMIT_BYTES)
    tags: list[str] = []
    used = 0
    for tag in hashtags:
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)
        if used + cost > TAGS_LIMIT_CHARS:
            break
        tags.append(tag)
        used += cost
    language = str((state.get("intent") or {}).get("language") or "").strip() or None
    return {"title": title, "description": description, "tags": tags, "language": language}


def upload_body(metadata: dict[str, Any], settings: Settings) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": metadata["title"],
        "description": metadata["description"],
        "categoryId": settings.youtube_upload_category_id,
    }
    if metadata.get("tags"):
        snippet["tags"] = metadata["tags"]
    if metadata.get("language"):
        snippet["defaultLanguage"] = metadata["language"]
        snippet["defaultAudioLanguage"] = metadata["language"]
    # Private first, always; publication is a separate explicit action.
    return {"snippet": snippet, "status": {"privacyStatus": "private"}}


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


def request_upload(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    channel_id: str,
    force_new: bool = False,
) -> tuple[YouTubeUpload, ExportTarget, bool]:
    """Create (or reuse) the mapping for the current exported render.

    Returns ``(upload, target, should_run)``.  Raises ``UploadRefused`` with
    code ``already_uploaded`` when this exact render already has a video.
    """
    target = resolve_export(project, settings)
    sha = file_sha256(target.path)
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
            return existing, target, False
        else:
            existing.state = "pending"
            existing.last_error_code = None
            existing.last_error_message = None
            existing.project_revision = target.project_revision
            db.commit()
            return existing, target, True
    fingerprint = get_or_create_fingerprint(
        db,
        target.render_state,
        upload_state=target.current_state,
        project_id=project.id,
        render_revision=target.render_revision,
        render_sha256=sha,
        file_size=target.path.stat().st_size,
    )
    metadata = youtube_metadata(target.current_state, project)
    upload = YouTubeUpload(
        project_id=project.id,
        project_revision=target.project_revision,
        render_revision=target.render_revision,
        render_sha256=sha,
        render_file_size=target.path.stat().st_size,
        render_content_hash=((target.render_state.get("content_hashes") or {}).get("render")),
        channel_id=channel_id,
        idempotency_key=key,
        fingerprint_id=fingerprint.id,
        state="pending",
        privacy_status="private",
        title=metadata["title"][:120],
        description=metadata["description"],
        tags=metadata["tags"],
    )
    db.add(upload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(select(YouTubeUpload).where(YouTubeUpload.idempotency_key == key))
        if raced is None:
            raise
        return raced, target, False
    db.refresh(upload)
    return upload, target, True


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
                fingerprint = db.get(ProductionFingerprint, upload.fingerprint_id) if upload.fingerprint_id else None
                content = (fingerprint.fingerprint.get("content") or {}) if fingerprint is not None else {}
                body = upload_body(
                    {"title": upload.title, "description": upload.description, "tags": list(upload.tags or []), "language": content.get("language")},
                    settings,
                )
                upload.upload_session_uri = provider.start_resumable_upload(token, body, size, "video/mp4")
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
    now = now or _now()
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    processing = item.get("processingDetails") if isinstance(item.get("processingDetails"), dict) else {}
    upload.deleted_on_youtube = False
    upload.upload_status = status.get("uploadStatus") or upload.upload_status
    upload.processing_status = processing.get("processingStatus") or upload.processing_status
    upload.privacy_status = str(status.get("privacyStatus") or upload.privacy_status)
    publish_at = parse_google_time(status.get("publishAt"))
    if publish_at is not None:
        upload.publish_at = publish_at
    upload_status = upload.upload_status
    if upload_status == "processed":
        upload.state = "ready"
        upload.failure_reason = None
    elif upload_status == "uploaded":
        upload.state = "processing"
    elif upload_status == "failed":
        upload.state = "failed"
        upload.failure_reason = status.get("failureReason")
        upload.last_error_code = "processing_failed"
        upload.last_error_message = f"YouTube could not process the video ({upload.failure_reason or 'unknown reason'})."
    elif upload_status == "rejected":
        upload.state = "failed"
        upload.rejection_reason = status.get("rejectionReason")
        upload.last_error_code = "rejected"
        upload.last_error_message = f"YouTube rejected the video ({upload.rejection_reason or 'no reason given'})."
    elif upload_status == "deleted":
        upload.deleted_on_youtube = True
    if upload.privacy_status in {"public", "unlisted"}:
        if upload.published_at is None:
            scheduled = _utc(upload.publish_at)
            if scheduled is not None and scheduled <= now:
                upload.published_at, upload.published_source = scheduled, "scheduled_publish_at"
            else:
                upload.published_at = parse_google_time(snippet.get("publishedAt")) or now
                upload.published_source = "youtube_snippet_published_at" if snippet.get("publishedAt") else "observed_at_sync"
        if upload.schedule_status == "scheduled":
            upload.schedule_status = "published"


def sync_status(
    db: Session, upload: YouTubeUpload, settings: Settings, store: SecretStore, provider: YouTubeProvider
) -> YouTubeUpload:
    if not upload.youtube_video_id:
        raise UploadRefused("not_uploaded", "This upload has no YouTube video yet.", upload)
    connection, token = access_token(db, settings, store, provider, capability="read")
    if connection.channel_id != upload.channel_id:
        raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this video belongs to.")
    items = provider.list_videos(token, [upload.youtube_video_id], "status,snippet,processingDetails")
    upload.last_status_sync_at = _now()
    if not items:
        upload.deleted_on_youtube = True
        upload.last_error_code = "deleted_on_youtube"
        upload.last_error_message = "This video no longer exists on YouTube (deleted or removed)."
    else:
        if upload.last_error_code == "deleted_on_youtube":
            upload.last_error_code = None
            upload.last_error_message = None
        apply_video_resource(upload, items[0])
    db.commit()
    db.refresh(upload)
    return upload


def schedule_publication(
    db: Session,
    upload: YouTubeUpload,
    publish_at: datetime,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    now: datetime | None = None,
) -> YouTubeUpload:
    """Private + publishAt, at the exact time the user chose."""
    now = now or _now()
    if publish_at.tzinfo is None:
        raise UploadRefused("invalid_time", "Choose a publication time with a time zone.", upload)
    publish_at = _utc(publish_at)  # type: ignore[assignment]
    if not upload.youtube_video_id or upload.deleted_on_youtube:
        raise UploadRefused("not_uploaded", "Only a video that exists on YouTube can be scheduled.", upload)
    if upload.published_at is not None or upload.privacy_status in {"public", "unlisted"}:
        raise UploadRefused("already_published", "This video was already published; YouTube only schedules private, never-published videos.", upload)
    if publish_at < now + MIN_SCHEDULE_LEAD:
        raise UploadRefused("invalid_time", "Choose a publication time at least 15 minutes in the future.", upload)
    if publish_at > now + MAX_SCHEDULE_AHEAD:
        raise UploadRefused("invalid_time", "Choose a publication time within the next year.", upload)
    try:
        connection, token = access_token(db, settings, store, provider, capability="schedule")
        if connection.channel_id != upload.channel_id:
            raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this video belongs to.")
        items = provider.list_videos(token, [upload.youtube_video_id], "status")
        if not items:
            upload.deleted_on_youtube = True
            raise YouTubeApiError("deleted_on_youtube", "This video no longer exists on YouTube.")
        current = items[0].get("status") if isinstance(items[0].get("status"), dict) else {}
        if current.get("privacyStatus") != "private":
            upload.privacy_status = str(current.get("privacyStatus") or upload.privacy_status)
            raise YouTubeApiError("not_private", "YouTube schedules only private videos; this video is no longer private.")
        status = {name: current[name] for name in MUTABLE_STATUS_FIELDS if name in current}
        status.update(privacyStatus="private", publishAt=google_time(publish_at))
        result = provider.update_video(token, {"id": upload.youtube_video_id, "status": status}, "status")
    except YouTubeApiError as exc:
        upload.schedule_status = "schedule_failed"
        upload.schedule_error = exc.message[:500]
        db.commit()
        raise
    confirmed = parse_google_time(((result.get("status") or {}) if isinstance(result, dict) else {}).get("publishAt"))
    upload.publish_at = confirmed or publish_at
    upload.privacy_status = "private"
    upload.schedule_status = "scheduled"
    upload.schedule_error = None
    db.commit()
    db.refresh(upload)
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


def lifecycle(upload: YouTubeUpload) -> str:
    """not a DB state: what the user should read about this video right now."""
    if upload.deleted_on_youtube:
        return "deleted"
    if upload.state == "failed":
        return "failed"
    if upload.state in ACTIVE_STATES:
        return "uploading"
    if upload.published_at is not None or upload.privacy_status in {"public", "unlisted"}:
        return "published"
    if upload.schedule_status == "scheduled":
        return "scheduled"
    return "private"


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
        "privacy_status": upload.privacy_status,
        "publish_at": aware(upload.publish_at),
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
