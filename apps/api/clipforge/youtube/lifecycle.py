"""The one project-deletion lifecycle authority (upload-aware).

* Never successfully uploaded -> everything is deleted (project rows, local
  media, and the project's YouTube attempt records).
* Successfully uploaded -> all local media and project rows are deleted, but
  the compact learning record stays: upload mapping, immutable production
  fingerprint (timeline, hook, visuals, pacing, quality, audio), every
  analytics snapshot and raw retention point, plus a small archive entry.
  Scene mappings, opening retention, baselines and observations are derived
  deterministically from those records, so nothing large is duplicated.
* Unknown outcome (all bytes sent, no video ID, not verifiable) -> fail
  closed: nothing is deleted without an explicit destructive confirmation.

The YouTube video itself is never touched.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    GenerationJob,
    ProductionFingerprint,
    Project,
    YouTubeAnalyticsSnapshot,
    YouTubeLearningArchive,
    YouTubeMetricValue,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from ..security.secrets import SecretStore
from ..services import (
    ProjectDeletionBusy,
    ProjectDeletionError,
    delete_project,
    get_project,
    project_local_storage_bytes,
)
from .connection import access_token, active_connection
from .provider import YouTubeApiError, YouTubeProvider
from .uploads import ACTIVE_STATES, UploadRefused, _record_video, serialize_upload, sync_status

SUCCEEDED, NEVER, UNKNOWN, ACTIVE = "succeeded", "never_uploaded", "unknown", "active"


class ProjectDeletionUnverified(ProjectDeletionError):
    def __init__(self, plan: DeletionPlan) -> None:
        super().__init__("Upload status could not be verified.")
        self.plan = plan


@dataclass
class DeletionPlan:
    project_id: str
    title: str
    mode: str  # full | archive | unverified | busy
    reclaimable_bytes: int | None
    retained_bytes: int
    verification: str  # not_needed | live | offline
    uploads: list[dict[str, Any]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "mode": self.mode,
            "reclaimable_bytes": self.reclaimable_bytes,
            "retained_bytes": self.retained_bytes,
            "verification": self.verification,
            "uploads": self.uploads,
            "messages": self.messages,
        }


def _snapshot_count(db: Session, upload_id: str) -> int:
    return int(db.scalar(select(func.count()).where(YouTubeAnalyticsSnapshot.upload_id == upload_id)) or 0)


def classify_upload(db: Session, upload: YouTubeUpload) -> str:
    if upload.state in ACTIVE_STATES:
        return ACTIVE
    if upload.youtube_video_id:
        # A video ID means YouTube received the file.  Only a rejection or
        # processing failure without any analytics counts as never succeeding;
        # a later deletion on YouTube keeps its historic analytics.
        if upload.upload_status in {"rejected", "failed"} and _snapshot_count(db, upload.id) == 0:
            return NEVER
        return SUCCEEDED
    if upload.render_file_size and upload.bytes_uploaded >= upload.render_file_size:
        return UNKNOWN  # the final bytes were sent; the response never arrived
    return NEVER


def _verify(db: Session, uploads: list[YouTubeUpload], settings: Settings, store: SecretStore, provider: YouTubeProvider) -> str:
    """Live check where it can change the classification; failures change nothing."""
    needed = [item for item in uploads if item.youtube_video_id or classify_upload(db, item) == UNKNOWN]
    if not needed:
        return "not_needed"
    offline = False
    for upload in needed:
        try:
            if upload.youtube_video_id:
                sync_status(db, upload, settings, store, provider)
            elif upload.upload_session_uri:
                _connection, token = access_token(db, settings, store, provider, capability="upload")
                progress = provider.query_upload(token, upload.upload_session_uri, upload.render_file_size)
                if progress.complete and progress.video:
                    _record_video(db, upload, progress.video)
            else:
                offline = True
        except (YouTubeApiError, UploadRefused):
            db.rollback()
            offline = True
    return "offline" if offline else "live"


def _retained_bytes(db: Session, uploads: list[YouTubeUpload]) -> int:
    """Approximate size of the kept learning record (JSON of the kept rows)."""
    total = 0
    for upload in uploads:
        total += len(json.dumps(serialize_upload(upload), default=str))
        fingerprint = db.get(ProductionFingerprint, upload.fingerprint_id) if upload.fingerprint_id else None
        if fingerprint is not None:
            total += len(json.dumps(fingerprint.fingerprint, default=str))
        snapshots = db.scalars(select(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == upload.id)).all()
        for snapshot in snapshots:
            total += len(json.dumps(snapshot.raw_responses, default=str)) + 200 * (len(snapshot.metrics) + len(snapshot.retention_points))
    return total


def plan_deletion(
    db: Session,
    project_id: str,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    verify: bool = True,
) -> DeletionPlan:
    project = get_project(db, project_id)
    if project is None:
        raise ProjectDeletionError("Project not found.")
    uploads = list(db.scalars(select(YouTubeUpload).where(YouTubeUpload.project_id == project_id)).all())
    verification = _verify(db, uploads, settings, store, provider) if verify and uploads else "not_needed"
    classified = [(upload, classify_upload(db, upload)) for upload in uploads]
    try:
        reclaimable: int | None = project_local_storage_bytes(project_id, settings)
    except (ProjectDeletionError, OSError):
        reclaimable = None  # never block deletion on accounting
    running = db.scalar(select(GenerationJob.id).where(GenerationJob.project_id == project_id, GenerationJob.status == "running"))
    kinds = {kind for _upload, kind in classified}
    messages: list[str] = []
    if running is not None or ACTIVE in kinds:
        mode = "busy"
        messages.append("An upload or generation is still running. Try again when it finishes.")
    elif SUCCEEDED in kinds:
        mode = "archive"
    elif UNKNOWN in kinds:
        mode = "unverified"
        messages.append("Upload status could not be verified.")
    else:
        mode = "full"
    kept = [upload for upload, kind in classified if kind in {SUCCEEDED, UNKNOWN}] if mode == "archive" else []
    return DeletionPlan(
        project_id=project_id,
        title=project.title,
        mode=mode,
        reclaimable_bytes=reclaimable,
        retained_bytes=_retained_bytes(db, kept) if kept else 0,
        verification=verification,
        uploads=[{**serialize_upload(upload), "classification": kind} for upload, kind in classified],
        messages=messages,
    )


def _delete_youtube_rows(db: Session, uploads: list[YouTubeUpload], *, keep_fingerprints: set[str]) -> None:
    ids = [upload.id for upload in uploads]
    if not ids:
        return
    snapshot_ids = list(db.scalars(select(YouTubeAnalyticsSnapshot.id).where(YouTubeAnalyticsSnapshot.upload_id.in_(ids))).all())
    if snapshot_ids:
        db.execute(delete(YouTubeMetricValue).where(YouTubeMetricValue.snapshot_id.in_(snapshot_ids)))
        db.execute(delete(YouTubeRetentionPoint).where(YouTubeRetentionPoint.snapshot_id.in_(snapshot_ids)))
        db.execute(delete(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.id.in_(snapshot_ids)))
    fingerprints = {upload.fingerprint_id for upload in uploads if upload.fingerprint_id} - keep_fingerprints
    db.execute(delete(YouTubeUpload).where(YouTubeUpload.id.in_(ids)))
    if fingerprints:
        db.execute(delete(ProductionFingerprint).where(ProductionFingerprint.id.in_(fingerprints)))


def delete_project_lifecycle(
    db: Session,
    project_id: str,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    confirm_unverified: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    plan = plan_deletion(db, project_id, settings, store, provider, verify=verify)
    if plan.mode == "busy":
        raise ProjectDeletionBusy(plan.messages[0])
    if plan.mode == "unverified" and not confirm_unverified:
        raise ProjectDeletionUnverified(plan)
    project = get_project(db, project_id)
    assert project is not None
    title, prompt = project.title, project.original_prompt
    topic = None
    if project.revisions:
        topic = str(((project.revisions[-1].state or {}).get("intent") or {}).get("topic") or "") or None
    result = delete_project(db, project_id, settings)  # local files + project rows
    uploads = list(db.scalars(select(YouTubeUpload).where(YouTubeUpload.project_id == project_id)).all())
    archive_id = None
    if plan.mode == "archive":
        kept = [upload for upload in uploads if classify_upload(db, upload) in {SUCCEEDED, UNKNOWN}]
        dropped = [upload for upload in uploads if upload not in kept]
        _delete_youtube_rows(db, dropped, keep_fingerprints={upload.fingerprint_id for upload in kept if upload.fingerprint_id})
        archive = db.scalar(select(YouTubeLearningArchive).where(YouTubeLearningArchive.project_id == project_id))
        if archive is None:
            archive = YouTubeLearningArchive(project_id=project_id)
            db.add(archive)
        archive.title, archive.prompt, archive.topic = title[:200], prompt, (topic or None) and topic[:300]
        archive.upload_ids = [upload.id for upload in kept]
        archive.bytes_freed = result.reclaimed_bytes
        archive.retained_bytes = plan.retained_bytes
        db.commit()
        archive_id = archive.id
    else:
        _delete_youtube_rows(db, uploads, keep_fingerprints=set())
        db.commit()
    return {
        "mode": "archive" if plan.mode == "archive" else "full",
        "freed_bytes": result.reclaimed_bytes,
        "retained_bytes": plan.retained_bytes if plan.mode == "archive" else 0,
        "archive_id": archive_id,
    }


def delete_all_lifecycle(db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider) -> dict[str, Any]:
    from ..services import delete_all_projects

    archived: list[str] = []

    def one(session: Session, project_id: str, config: Settings):
        outcome = delete_project_lifecycle(session, project_id, config, store, provider)
        if outcome["mode"] == "archive":
            archived.append(project_id)
        from ..services import ProjectDeletionResult

        return ProjectDeletionResult(reclaimed_bytes=outcome["freed_bytes"])

    result = delete_all_projects(db, settings, delete_one=one)
    return {
        "deleted_projects": result.deleted_projects,
        "freed_bytes": result.freed_bytes,
        "failed_projects": result.failed_projects,
        "remaining_projects": result.remaining_projects,
        "archived_projects": len(archived),
    }


# ---------------------------------------------------------------------------
# Learning History (read-only)
# ---------------------------------------------------------------------------


def archived_uploads(db: Session) -> list[tuple[YouTubeUpload, YouTubeLearningArchive | None]]:
    """Successful uploads whose local project no longer exists."""
    project_ids = set(db.scalars(select(Project.id)).all())
    archives = {item.project_id: item for item in db.scalars(select(YouTubeLearningArchive)).all()}
    rows = db.scalars(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id.is_not(None)).order_by(YouTubeUpload.created_at.desc())).all()
    return [(upload, archives.get(upload.project_id)) for upload in rows if upload.project_id not in project_ids and classify_upload(db, upload) != NEVER]


def serialize_archive_entry(db: Session, upload: YouTubeUpload, archive: YouTubeLearningArchive | None, *, min_sample: int) -> dict[str, Any]:
    from .learning import api_snapshots, latest_with_data, metric_value

    latest = latest_with_data(api_snapshots(db, upload.id))
    connection = active_connection(db)
    return {
        "upload_id": upload.id,
        "project_id": upload.project_id,
        "title": (archive.title if archive else "") or upload.title,
        "topic": archive.topic if archive else None,
        "archived_at": archive.archived_at if archive else None,
        "editable": False,
        "upload": serialize_upload(upload),
        "summary": {
            "views": metric_value(latest, "views"),
            "engagedViews": metric_value(latest, "engagedViews"),
            "averageViewPercentage": metric_value(latest, "averageViewPercentage"),
            "fetched_at": latest.fetched_at if latest else None,
        },
        "on_connected_channel": bool(connection and connection.channel_id == upload.channel_id),
        "min_sample": min_sample,
    }
