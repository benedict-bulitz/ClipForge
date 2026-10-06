"""Removing one video from the Video Library (the Videos tab).

A library entry is not a file of its own: it is a publication record
(``youtube_uploads`` or ``social_publications``) shown by the read model in
``library``.  The final video it was made from is the *project's* canonical
master (``exporter.resolve_final_master``) and belongs to the project, which
is deleted only by the project-deletion lifecycle.  Removing an entry
therefore:

* deletes the local media that belongs to the entry alone - the small Video
  Library preview (``render_root / "video-library" / "<upload id>.webp|jpg"``),
  and nothing else;
* marks the record ``library_removed_at`` so the index and detail page no
  longer show it, while the record itself - remote video/post id, URL, event
  log, analytics snapshots, production fingerprint, idempotency key - stays,
  so ClipForge still knows the post exists and analytics/learning stay
  consistent;
* cancels an Instagram/TikTok post that ClipForge has not started uploading
  yet (scheduled, queued or missed), so a removed entry never publishes later;
* never calls YouTube, Instagram or TikTok and never deletes a remote post.

It is idempotent: a repeated request answers ``already_removed`` and retries
any media cleanup a previous attempt could not finish.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Project, SocialPublication, YouTubeUpload
from ..publishing import publications
from . import library

logger = logging.getLogger(__name__)

# Upload/publication ids are UUIDs, YouTube video ids 11 URL-safe characters.
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
PREVIEW_SUFFIXES = ("webp", "jpg")
# Social states that ClipForge can still stop before anything reaches the provider.
CANCEL_BEFORE_REMOVAL = ("pending", "scheduled", "missed")
# Bytes are on their way to (or being processed by) the provider right now.
IN_FLIGHT = ("uploading", "processing")


class LibraryRemovalRefused(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RemovalResult:
    id: str
    platform: str
    status: str  # removed | already_removed
    removed_media: list[dict[str, Any]] = field(default_factory=list)
    cleanup_complete: bool = True
    cancelled_schedule: bool = False
    remote_post: bool = False
    project_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "status": self.status,
            "removed_media": self.removed_media,
            "freed_bytes": sum(int(item["bytes"]) for item in self.removed_media),
            "cleanup_complete": self.cleanup_complete,
            "cancelled_schedule": self.cancelled_schedule,
            # What is intentionally kept.
            "retained": {
                "publication_record": True,
                "remote_post": self.remote_post,
                "project": self.project_available,
            },
        }


def _now() -> datetime:
    return datetime.now(UTC)


def owned_preview(upload: YouTubeUpload, settings: Settings) -> Path | None:
    """The entry's own library preview, or ``None``.

    Only a file ClipForge itself named (``<upload id>.webp|jpg``) directly in
    the library directory qualifies; any other stored value (a path, ``..``,
    another entry's file) is never touched.  The path is not resolved, so a
    symlink is removed as a link and its target is never deleted.
    """
    name = upload.library_thumbnail or ""
    if name not in {f"{upload.id}.{suffix}" for suffix in PREVIEW_SUFFIXES}:
        return None
    directory = library.library_directory(settings)
    path = directory / name
    if path.parent != directory or path.name != name:
        return None
    return path


def _remove_preview(upload: YouTubeUpload, settings: Settings) -> tuple[list[dict[str, Any]], bool]:
    """Delete the entry's preview; a file that is already gone counts as removed."""
    path = owned_preview(upload, settings)
    if path is None:
        return [], True
    try:
        if path.is_dir() and not path.is_symlink():
            logger.warning("Video Library preview of upload %s is a directory; left in place", upload.id)
            return [], False
        existed = path.is_symlink() or path.exists()
        size = path.lstat().st_size if existed else 0
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Video Library preview of upload %s could not be deleted", upload.id, exc_info=True)
        return [], False
    return ([{"kind": "library_preview", "name": path.name, "bytes": size}] if existed else []), True


def _remove_youtube(db: Session, upload: YouTubeUpload, settings: Settings, now: datetime) -> RemovalResult:
    already = upload.library_removed_at is not None
    media, complete = _remove_preview(upload, settings)
    if complete:
        upload.library_thumbnail = ""  # "no preview": never regenerated for a removed entry
    if not already:
        upload.library_removed_at = now
    db.commit()
    return RemovalResult(
        id=upload.id,
        platform="youtube",
        status="already_removed" if already else "removed",
        removed_media=media,
        cleanup_complete=complete,
        remote_post=bool(upload.youtube_video_id) and not upload.deleted_on_youtube,
        project_available=db.get(Project, upload.project_id) is not None,
    )


def _remove_social(db: Session, row: SocialPublication, now: datetime) -> RemovalResult:
    already = row.library_removed_at is not None
    cancelled = False
    if not already:
        if row.state in IN_FLIGHT:
            raise LibraryRemovalRefused(
                "publication_in_progress",
                "This video is being published right now. Remove it once publishing has finished or failed.",
            )
        if row.state in CANCEL_BEFORE_REMOVAL:
            if not publications.cancel_unstarted(db, row, now=now, note="cancelled: removed from the Video Library"):
                db.rollback()
                raise LibraryRemovalRefused(
                    "publication_in_progress",
                    "This video started publishing just now. Remove it once publishing has finished or failed.",
                )
            cancelled = True
        row.library_removed_at = now
        db.commit()
    return RemovalResult(
        id=row.id,
        platform=row.platform,
        status="already_removed" if already else "removed",
        # The Instagram/TikTok poster is the project's own cover, not a library copy.
        removed_media=[],
        cancelled_schedule=cancelled,
        remote_post=bool(row.remote_post_id or row.remote_url or row.state == "published"),
        project_available=db.get(Project, row.project_id) is not None,
    )


def remove_from_library(db: Session, identifier: str, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Remove one Videos-tab entry (YouTube upload id or YouTube video id, or
    an Instagram/TikTok publication id).  Raises ``LookupError`` for anything
    that is not (and never was) a library entry."""
    if not VIDEO_ID.fullmatch(identifier or ""):
        raise LookupError(identifier)
    now = now or _now()
    upload = library.find_video(db, identifier, include_removed=True)
    if upload is None:
        # Already removed stays "already removed", even if YouTube's status changed since.
        removed = db.get(YouTubeUpload, identifier) or db.scalar(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id == identifier))
        upload = removed if removed is not None and removed.library_removed_at is not None else None
    if upload is not None:
        return _remove_youtube(db, upload, settings, now).as_dict()
    row = db.scalar(select(SocialPublication).where(SocialPublication.id == identifier))
    # A cancelled post is not shown in the library, unless it was cancelled by its removal.
    if row is None or (row.state == "cancelled" and row.library_removed_at is None):
        raise LookupError(identifier)
    return _remove_social(db, row, now).as_dict()
