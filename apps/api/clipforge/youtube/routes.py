"""HTTP API for the YouTube Learning Loop (connection, uploads, analytics)."""
from __future__ import annotations

import logging
from datetime import datetime
from threading import Thread
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from keyring.errors import KeyringError
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings, refresh_settings
from ..database import SessionLocal, get_db
from ..integrations import get_secret_store
from ..models import YouTubeUpload
from ..security.secrets import SecretStore
from ..services import RevisionConflict, get_project
from . import analytics, connection, learning, uploads
from .provider import GoogleYouTubeProvider, YouTubeApiError, YouTubeProvider

logger = logging.getLogger(__name__)
CALLBACK_PATH = "/api/youtube/oauth/callback"


class _AccessLogRedactor(logging.Filter):
    """uvicorn logs full request paths; the OAuth callback carries a one-time code."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and CALLBACK_PATH in str(args[2]):
            record.args = (*args[:2], CALLBACK_PATH + "?[redacted]", *args[3:])
        return True


_ACCESS_REDACTOR = _AccessLogRedactor()
if _ACCESS_REDACTOR not in logging.getLogger("uvicorn.access").filters:
    logging.getLogger("uvicorn.access").addFilter(_ACCESS_REDACTOR)


def get_youtube_provider() -> YouTubeProvider:
    return GoogleYouTubeProvider()


def start_upload_thread(upload_id: str, path: Any) -> None:
    def work() -> None:
        settings, store, provider = get_settings(), SecretStore(), GoogleYouTubeProvider()
        with SessionLocal() as db:
            upload = uploads.run_upload(db, upload_id, path, settings, store, provider)
            if upload is not None and upload.youtube_video_id:
                try:
                    uploads.sync_status(db, upload, settings, store, provider)
                except (YouTubeApiError, uploads.UploadRefused):
                    pass  # status can be refreshed later; the video ID is saved

    Thread(target=work, daemon=True, name=f"youtube-upload-{upload_id[:8]}").start()


def get_upload_dispatcher():
    return start_upload_thread


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ProviderDep = Annotated[YouTubeProvider, Depends(get_youtube_provider)]
DispatcherDep = Annotated[Any, Depends(get_upload_dispatcher)]

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

ERROR_STATUS = {
    "not_connected": status.HTTP_409_CONFLICT,
    "client_not_configured": status.HTTP_409_CONFLICT,
    "auth_expired": status.HTTP_401_UNAUTHORIZED,
    "insufficient_scope": status.HTTP_403_FORBIDDEN,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "wrong_channel": status.HTTP_409_CONFLICT,
    "quota_exceeded": status.HTTP_429_TOO_MANY_REQUESTS,
    "api_disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
    "network_timeout": status.HTTP_503_SERVICE_UNAVAILABLE,
    "network_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    "provider_error": status.HTTP_502_BAD_GATEWAY,
    "storage_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    "not_found": status.HTTP_404_NOT_FOUND,
    "deleted_on_youtube": status.HTTP_410_GONE,
    "already_uploaded": status.HTTP_409_CONFLICT,
    "unknown_outcome": status.HTTP_409_CONFLICT,
    "already_published": status.HTTP_409_CONFLICT,
    "not_private": status.HTTP_409_CONFLICT,
    "not_exported": status.HTTP_409_CONFLICT,
    "export_outdated": status.HTTP_409_CONFLICT,
    "export_missing": status.HTTP_409_CONFLICT,
    "revision_conflict": status.HTTP_409_CONFLICT,
}


def _error(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
        detail={"status": code, "message": message, **extra},
    )


def _api_error(exc: YouTubeApiError) -> HTTPException:
    return _error(exc.code, exc.message)


def _refused(exc: uploads.UploadRefused) -> HTTPException:
    extra = {"upload": jsonable_encoder(uploads.serialize_upload(exc.upload))} if exc.upload is not None else {}
    return _error(exc.code, exc.message, **extra)


def _upload_or_404(db: Session, upload_id: str) -> YouTubeUpload:
    upload = db.get(YouTubeUpload, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="YouTube upload not found")
    return upload


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class OAuthClientUpdate(BaseModel):
    client_id: str = Field(min_length=8, max_length=300)
    client_secret: SecretStr | None = None


@router.get("/connection")
def get_connection_route(db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    return connection.serialize_connection(connection.get_connection(db), settings, store)


@router.put("/client")
def save_client_route(payload: OAuthClientUpdate, db: DbSession, store: StoreDep) -> dict:
    try:
        store.set_secret("YOUTUBE_OAUTH_CLIENT_ID", payload.client_id.strip())
        secret = payload.client_secret.get_secret_value().strip() if payload.client_secret else ""
        if secret:
            store.set_secret("YOUTUBE_OAUTH_CLIENT_SECRET", secret)
    except (KeyringError, ValueError) as exc:
        raise _error("storage_error", "Secure credential storage is unavailable.") from exc
    settings = refresh_settings()
    return connection.serialize_connection(connection.get_connection(db), settings, store)


@router.post("/connection/authorize")
def authorize_route(settings: SettingsDep) -> dict:
    try:
        return {"authorization_url": connection.begin_authorization(settings)}
    except YouTubeApiError as exc:
        raise _api_error(exc) from exc


@router.get("/oauth/callback", include_in_schema=False)
def oauth_callback_route(
    db: DbSession,
    settings: SettingsDep,
    store: StoreDep,
    provider: ProviderDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    origin = (settings.allowed_origins or ["http://localhost:3000"])[0].rstrip("/")
    target = f"{origin}/settings/integrations"
    if error or not code or not state:
        reason = "access_denied" if error == "access_denied" else "authorization_failed"
        return RedirectResponse(f"{target}?{urlencode({'youtube': 'error', 'reason': reason})}", status_code=303)
    try:
        connection.complete_authorization(db, settings, store, provider, code=code, state=state)
    except YouTubeApiError as exc:
        logger.warning("YouTube connection failed code=%s", exc.code)
        return RedirectResponse(f"{target}?{urlencode({'youtube': 'error', 'reason': exc.code})}", status_code=303)
    return RedirectResponse(f"{target}?{urlencode({'youtube': 'connected'})}", status_code=303)


@router.delete("/connection")
def disconnect_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    try:
        record = connection.disconnect(db, store, provider)
    except YouTubeApiError as exc:
        raise _api_error(exc) from exc
    return connection.serialize_connection(record, settings, store)


# ---------------------------------------------------------------------------
# Project panel, uploads, scheduling
# ---------------------------------------------------------------------------


class UploadCreate(BaseModel):
    base_revision: int
    force_new: bool = False


class ScheduleCreate(BaseModel):
    publish_at: datetime


class ManualMetricCreate(BaseModel):
    name: str = Field(max_length=64)
    value: float
    note: str | None = Field(default=None, max_length=500)


def _render_status(db: Session, project, settings: Settings, channel_id: str | None) -> dict[str, Any]:
    try:
        target = uploads.resolve_export(project, settings)
    except uploads.UploadRefused as exc:
        return {"uploadable": False, "code": exc.code, "message": exc.message}
    sha = uploads.cached_sha256(target.path)
    existing = None
    if channel_id:
        existing = db.scalar(select(YouTubeUpload).where(YouTubeUpload.idempotency_key == uploads.idempotency_key(channel_id, project.id, sha)))
    return {
        "uploadable": existing is None or (existing.youtube_video_id is None and existing.state == "failed" and existing.last_error_code != "session_expired_unknown_outcome"),
        "code": "already_uploaded" if existing is not None and existing.youtube_video_id else None,
        "render_revision": target.render_revision,
        "project_revision": target.project_revision,
        "render_sha256": sha,
        "existing_upload_id": existing.id if existing else None,
        "message": f"Already uploaded as {existing.youtube_video_id}" if existing is not None and existing.youtube_video_id else None,
    }


@router.get("/projects/{project_id}")
def project_youtube_route(project_id: str, db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    record = connection.active_connection(db)
    rows = db.scalars(
        select(YouTubeUpload).where(YouTubeUpload.project_id == project_id).order_by(YouTubeUpload.created_at.desc())
    ).all()
    current = _render_status(db, project, settings, record.channel_id if record else None)
    focus = next((item for item in rows if item.id == current.get("existing_upload_id")), None)
    focus = focus or next((item for item in rows if item.youtube_video_id and item.idempotency_key), None) or (rows[0] if rows else None)
    return {
        "connection": connection.serialize_connection(connection.get_connection(db), settings, store),
        "current_render": current,
        "uploads": [uploads.serialize_upload(item) for item in rows],
        "focus_upload_id": focus.id if focus else None,
        "performance": learning.performance_report(db, focus, min_sample=settings.youtube_baseline_min_sample) if focus else {"status": "not_uploaded"},
    }


@router.post("/projects/{project_id}/uploads", status_code=status.HTTP_202_ACCEPTED)
def create_upload_route(
    project_id: str,
    payload: UploadCreate,
    db: DbSession,
    settings: SettingsDep,
    dispatch: DispatcherDep,
) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.base_revision != project.current_revision:
        raise _error("revision_conflict", "Project changed; reload before uploading.")
    record = connection.active_connection(db)
    if record is None:
        raise _error("not_connected", "Connect a YouTube channel first.")
    try:
        upload, target, should_run = uploads.request_upload(
            db, project, settings, channel_id=record.channel_id, force_new=payload.force_new
        )
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    except RevisionConflict as exc:
        raise _error("revision_conflict", str(exc)) from exc
    if should_run:
        dispatch(upload.id, target.path)
        db.refresh(upload)
    return {"upload": uploads.serialize_upload(upload), "started": should_run}


@router.post("/uploads/{upload_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_upload_route(upload_id: str, db: DbSession, settings: SettingsDep, dispatch: DispatcherDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    if upload.youtube_video_id:
        raise _error("already_uploaded", f"Already uploaded as {upload.youtube_video_id}.")
    if upload.state != "failed" or upload.idempotency_key is None:
        raise _error("not_retryable", "Only a failed upload can be retried.")
    if upload.last_error_code == "session_expired_unknown_outcome":
        raise _error("unknown_outcome", "An earlier upload may have completed on YouTube. Check YouTube Studio, then confirm a new upload from the project.")
    project = get_project(db, upload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        target = uploads.resolve_export(project, settings)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    dispatch(upload.id, target.path)
    db.refresh(upload)
    return {"upload": uploads.serialize_upload(upload), "started": True}


@router.post("/uploads/{upload_id}/schedule")
def schedule_route(upload_id: str, payload: ScheduleCreate, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        uploads.schedule_publication(db, upload, payload.publish_at, settings, store, provider)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    except YouTubeApiError as exc:
        raise _api_error(exc) from exc
    return {"upload": uploads.serialize_upload(upload)}


@router.post("/uploads/{upload_id}/sync")
def sync_route(upload_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        uploads.sync_status(db, upload, settings, store, provider)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    except YouTubeApiError as exc:
        raise _api_error(exc) from exc
    return {"upload": uploads.serialize_upload(upload)}


@router.post("/uploads/{upload_id}/analytics/refresh")
def refresh_analytics_route(upload_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        result = analytics.refresh_analytics(db, upload, settings, store, provider)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    db.refresh(upload)
    return {"result": result, "performance": learning.performance_report(db, upload, min_sample=settings.youtube_baseline_min_sample)}


@router.get("/uploads/{upload_id}/performance")
def performance_route(upload_id: str, db: DbSession, settings: SettingsDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    return learning.performance_report(db, upload, min_sample=settings.youtube_baseline_min_sample)


@router.post("/uploads/{upload_id}/manual-metrics", status_code=status.HTTP_201_CREATED)
def manual_metric_route(upload_id: str, payload: ManualMetricCreate, db: DbSession) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        snapshot = analytics.record_manual_metric(db, upload, payload.name, payload.value, note=payload.note)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    return {"snapshot_id": snapshot.id, "source": snapshot.source}


@router.post("/analytics/sync-due")
def sync_due_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    return analytics.sync_due(db, settings, store, provider)


@router.get("/learning")
def learning_route(db: DbSession, settings: SettingsDep) -> dict:
    record = connection.active_connection(db)
    if record is None:
        raise _error("not_connected", "Connect a YouTube channel first.")
    return {
        "baseline": learning.channel_baseline(db, record.channel_id, min_sample=settings.youtube_baseline_min_sample),
        "table": learning.learning_table(db, record.channel_id),
    }
