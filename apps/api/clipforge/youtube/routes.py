"""HTTP API for the YouTube Learning Loop (connection, uploads, analytics)."""
from __future__ import annotations

import base64
import binascii
import logging
from threading import Thread
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from keyring.errors import KeyringError
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings, refresh_settings
from ..database import SessionLocal, get_db
from ..integrations import get_secret_store
from ..models import YouTubeUpload
from ..security.secrets import SecretStore
from ..services import RevisionConflict, effective_revision_state, get_project
from . import (
    analytics,
    connection,
    learning,
    library,
    lifecycle,
    publishing,
    schedule_learning,
    uploads,
)
from . import schedule as schedule_authority
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
                uploads.after_upload(db, upload, settings, store, provider)

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
    "not_rendered": status.HTTP_409_CONFLICT,
    "render_missing": status.HTTP_409_CONFLICT,
    "preflight_failed": 422,
    "thumbnail_not_allowed": status.HTTP_403_FORBIDDEN,
    "slot_taken": status.HTTP_409_CONFLICT,
    "slot_missed": status.HTTP_409_CONFLICT,
    "schedule_unverified": status.HTTP_409_CONFLICT,
    "invalid_schedule": 422,
    "learned_unavailable": status.HTTP_409_CONFLICT,
    "schedule_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "schedule_conflict": status.HTTP_409_CONFLICT,
}


def _error(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
        detail={"status": code, "message": message, **extra},
    )


def _api_error(exc: YouTubeApiError) -> HTTPException:
    return _error(exc.code, exc.message)


def _refused(exc: uploads.UploadRefused) -> HTTPException:
    extra: dict[str, Any] = {"upload": jsonable_encoder(uploads.serialize_upload(exc.upload))} if exc.upload is not None else {}
    if exc.issues:
        extra["issues"] = exc.issues
    return _error(exc.code, exc.message, **extra)


def _slot_unavailable(exc: schedule_authority.SlotUnavailable) -> HTTPException:
    return _error(exc.code, exc.message, **jsonable_encoder(exc.detail))


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
    options: publishing.PublishOptions
    region: str = Field(default="US", min_length=2, max_length=2)
    language: str = Field(default="en", max_length=20)
    force_new: bool = False
    # The user chose "Use cached schedule" after YouTube could not be re-checked.
    allow_cached_schedule: bool = False
    # The user chose "Keep anyway" for a manual time that collides with the calendar.
    accept_schedule_conflict: bool = False


class PreflightCreate(BaseModel):
    options: publishing.PublishOptions
    region: str = Field(default="US", min_length=2, max_length=2)
    language: str = Field(default="en", max_length=20)


class CustomThumbnailCreate(BaseModel):
    data_base64: str = Field(max_length=28_000_000)


class AudienceUpdate(BaseModel):
    made_for_kids: bool


class ManualMetricCreate(BaseModel):
    name: str = Field(max_length=64)
    value: float
    note: str | None = Field(default=None, max_length=500)


def _categories(db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, region: str, language: str) -> tuple[list[dict[str, Any]] | None, dict[str, str] | None]:
    try:
        _connection, token = connection.access_token(db, settings, store, provider, capability="read")
        return publishing.list_categories(provider, token, region, language), None
    except YouTubeApiError as exc:
        return None, {"code": exc.code, "message": exc.message}


def _render_status(db: Session, project, settings: Settings, channel_id: str | None) -> dict[str, Any]:
    """Is the current render uploadable? Revision-level check: nothing is hashed or mixed here."""
    state = effective_revision_state(project)
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    if render.get("status") != "complete" or render.get("stale"):
        return {"uploadable": False, "code": "not_rendered", "message": "Render this revision before uploading it to YouTube."}
    render_revision = int(render.get("revision") or project.current_revision)
    existing = None
    if channel_id:
        existing = db.scalar(
            select(YouTubeUpload)
            .where(
                YouTubeUpload.project_id == project.id,
                YouTubeUpload.channel_id == channel_id,
                YouTubeUpload.render_revision == render_revision,
                YouTubeUpload.idempotency_key.is_not(None),
            )
            .order_by(YouTubeUpload.created_at.desc())
        )
    retry = existing is not None and existing.youtube_video_id is None and existing.state == "failed" and existing.last_error_code != "session_expired_unknown_outcome"
    return {
        "uploadable": existing is None or retry,
        "code": "already_uploaded" if existing is not None and existing.youtube_video_id else None,
        "render_revision": render_revision,
        "project_revision": project.current_revision,
        "existing_upload_id": existing.id if existing else None,
        "message": f"Already uploaded as {existing.youtube_video_id}" if existing is not None and existing.youtube_video_id else None,
    }


def _project_or_404(db: Session, project_id: str):
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}")
def project_youtube_route(project_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    """Results-page state. YouTube is asked (freshness-gated) so a page open or a
    poll never re-shows a stale local schedule as the current truth."""
    project = _project_or_404(db, project_id)
    record = connection.active_connection(db)
    rows = db.scalars(
        select(YouTubeUpload).where(YouTubeUpload.project_id == project_id).order_by(YouTubeUpload.created_at.desc())
    ).all()
    current = _render_status(db, project, settings, record.channel_id if record else None)
    focus = next((item for item in rows if item.id == current.get("existing_upload_id")), None)
    focus = focus or next((item for item in rows if item.youtube_video_id and item.idempotency_key), None) or (rows[0] if rows else None)
    if focus is not None and record is not None and focus.channel_id == record.channel_id:
        uploads.reconcile_if_due(db, focus, settings, store, provider)
    return {
        "connection": connection.serialize_connection(connection.get_connection(db), settings, store),
        "current_render": current,
        "uploads": [uploads.serialize_upload(item) for item in rows],
        "focus_upload_id": focus.id if focus else None,
        "performance": learning.performance_report(db, focus, min_sample=settings.youtube_baseline_min_sample) if focus else {"status": "not_uploaded"},
    }


@router.get("/projects/{project_id}/draft")
def publishing_draft_route(
    project_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep,
    region: str = "US", language: str = "en", timezone: str | None = None,
) -> dict:
    """Everything the publishing sheet needs, with only user-saved defaults applied.

    With Smart Schedule on, YouTube's real schedule is re-checked when the
    cache is stale and the next free preferred slot is pre-selected; it is
    only pre-selected when YouTube was verified (never guessed).
    """
    project = _project_or_404(db, project_id)
    state = effective_revision_state(project)
    defaults = publishing.load_defaults(db)
    record = connection.active_connection(db)
    preset = publishing.last_used_preset(db, record.channel_id if record else None)
    applied = preset if publishing.preset_applies(db, preset) else None
    smart = None
    if record is not None:
        hint = timezone if timezone and publishing.valid_timezone(timezone) else ((applied or {}).get("timezone") or defaults.timezone)
        try:
            smart = schedule_authority.smart_state(db, settings, store, provider, record.channel_id, timezone_hint=hint)
        except SQLAlchemyError:
            # The sheet still opens; it just cannot claim to know a free slot.
            db.rollback()
            logger.exception("Publishing schedule unavailable for the draft")
            smart = {"status": "unverified", "enabled": True, "recommendation": None, "cached_recommendation": None}
    recommended = (smart or {}).get("recommendation")
    draft = publishing.options_with_defaults(
        publishing.default_metadata(state, project.title), defaults, applied,
        smart_enabled=bool(smart and smart["enabled"]),
        smart=publishing.ScheduleChoice(**recommended["choice"]) if recommended else None,
    )
    choices = publishing.thumbnail_choices(state, project.id, settings)
    selected = publishing.default_thumbnail(choices)
    draft["thumbnail"] = {"source": selected["source"], "asset": selected["asset"]} if selected else None
    categories, category_error = _categories(db, settings, store, provider, region, language)
    suggestion = publishing.suggest_category(categories or [], state)
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    render_revision = int(render.get("revision") or project.current_revision)
    fingerprint_state = {rev.number: rev.state for rev in project.revisions}.get(render_revision, state)
    from .fingerprint import build_fingerprint

    fingerprint = build_fingerprint(fingerprint_state, project_id=project.id, render_revision=render_revision, render_sha256="", file_size=0)
    return {
        "options": draft,
        "defaults": defaults.model_dump(),
        # Where the reusable answers came from: "last_upload" | "settings".
        "preset_source": "last_upload" if applied else "settings",
        "smart_schedule": smart or {"status": "not_connected", "enabled": False},
        "thumbnails": choices,
        "categories": categories or [],
        "category_error": category_error,
        "suggested_category": suggestion,
        "synthetic_suggestion": publishing.synthetic_suggestion(fingerprint),
        "allowed_visibilities": publishing.allowed_visibilities(defaults),
        "limits": {"title": publishing.TITLE_LIMIT, "description_bytes": publishing.DESCRIPTION_LIMIT_BYTES, "tags": publishing.TAGS_LIMIT_CHARS},
        "catalog": publishing.settings_catalog(),
        "render_status": _render_status(db, project, settings, record.channel_id if (record := connection.active_connection(db)) else None),
    }


@router.post("/projects/{project_id}/preflight")
def preflight_route(project_id: str, payload: PreflightCreate, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    project = _project_or_404(db, project_id)
    categories, _error = _categories(db, settings, store, provider, payload.region, payload.language)
    issues, resolution, _source = uploads.preflight(db, project, settings, payload.options, categories=categories)
    return {
        "issues": issues,
        "schedule": resolution.as_dict() if resolution else None,
        "schedule_conflict": _manual_conflict(db, payload.options, resolution),
        "ready": not issues,
    }


def _manual_conflict(db: Session, options: publishing.PublishOptions, resolution, *, fresh: tuple[Settings, SecretStore, YouTubeProvider] | None = None) -> dict | None:
    """Conflict warning for a user-chosen time (automatic slots are checked when claimed).

    The preflight runs while typing, so it reads the cached schedule only;
    the upload passes ``fresh`` to re-read YouTube when the cache is old.
    """
    if options.visibility != "schedule" or options.schedule_source == "auto" or resolution is None or resolution.status != "ok":
        return None
    record = connection.active_connection(db)
    if record is None:
        return None
    try:
        if fresh is not None:
            settings, store, provider = fresh
            schedule_authority.refresh_if_due(db, settings, store, provider, record.channel_id, max_age=schedule_authority.CONFLICT_CHECK_MAX_AGE)
        schedule = schedule_authority.ensure_schedule(db, record.channel_id)
        return schedule_authority.manual_conflict(db, schedule, resolution.publish_at)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Manual schedule conflict check failed")
        return None


@router.post("/projects/{project_id}/thumbnails")
def custom_thumbnail_route(project_id: str, payload: CustomThumbnailCreate, db: DbSession, settings: SettingsDep) -> dict:
    project = _project_or_404(db, project_id)
    try:
        data = base64.b64decode(payload.data_base64.split(",", 1)[-1], validate=True)
        name = publishing.save_custom_thumbnail(project.id, data, settings)
    except (ValueError, binascii.Error) as exc:
        raise _error("invalid_thumbnail", str(exc) or "The image could not be read.") from exc
    return {"asset": name, "thumbnails": publishing.thumbnail_choices(effective_revision_state(project), project.id, settings)}


@router.post("/schedule/resolve")
def resolve_schedule_route(payload: publishing.ScheduleChoice) -> dict:
    return publishing.resolve_schedule(payload).as_dict()


@router.get("/categories")
def categories_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep, region: str = "US", language: str = "en") -> dict:
    categories, error = _categories(db, settings, store, provider, region[:2], language[:20])
    return {"categories": categories or [], "error": error}


@router.get("/defaults")
def get_defaults_route(db: DbSession) -> dict:
    defaults = publishing.load_defaults(db)
    return {"defaults": defaults.model_dump(), "allowed_visibilities": publishing.allowed_visibilities(defaults), "catalog": publishing.settings_catalog()}


@router.put("/defaults")
def save_defaults_route(payload: publishing.UploadDefaults, db: DbSession) -> dict:
    try:
        defaults = publishing.save_defaults(db, payload)
    except ValueError as exc:
        raise _error("invalid_defaults", str(exc)) from exc
    return {"defaults": defaults.model_dump(), "allowed_visibilities": publishing.allowed_visibilities(defaults), "catalog": publishing.settings_catalog()}


@router.post("/projects/{project_id}/uploads", status_code=status.HTTP_202_ACCEPTED)
def create_upload_route(
    project_id: str,
    payload: UploadCreate,
    db: DbSession,
    settings: SettingsDep,
    store: StoreDep,
    provider: ProviderDep,
    dispatch: DispatcherDep,
) -> dict:
    project = _project_or_404(db, project_id)
    if payload.base_revision != project.current_revision:
        raise _error("revision_conflict", "Project changed; reload before uploading.")
    record = connection.active_connection(db)
    if record is None:
        raise _error("not_connected", "Connect a YouTube channel first.")
    categories, _error_detail = _categories(db, settings, store, provider, payload.region, payload.language)
    options = payload.options
    reservation = None
    if options.visibility == "schedule" and options.schedule is not None:
        issues, resolution, _source = uploads.preflight(db, project, settings, options, categories=categories)
        if issues:
            raise _error("preflight_failed", f"{len(issues)} item{'s' if len(issues) != 1 else ''} need{'s' if len(issues) == 1 else ''} attention.", issues=issues)
        try:
            if options.schedule_source == "auto":
                # Fresh double-booking check + exclusive claim before YouTube is asked.
                reservation = schedule_authority.claim_auto_slot(
                    db, settings, store, provider, channel_id=record.channel_id, choice=options.schedule,
                    project_id=project.id, allow_cached=payload.allow_cached_schedule,
                )
            elif resolution is not None and resolution.publish_at is not None:
                # The user's own time is kept as is - but never scheduled into a
                # collision without the user's explicit "Keep anyway".
                conflict = _manual_conflict(db, options, resolution, fresh=(settings, store, provider))
                if conflict is not None and not payload.accept_schedule_conflict:
                    raise _error("schedule_conflict", conflict["message"], conflict=jsonable_encoder(conflict))
                reservation = schedule_authority.reserve(
                    db, record.channel_id, publish_at=resolution.publish_at, local_time=resolution.local_time or options.schedule.time,
                    timezone=options.schedule.timezone, source="manual", project_id=project.id,
                )
        except schedule_authority.SlotUnavailable as exc:
            raise _slot_unavailable(exc) from exc
    try:
        upload, source, should_run = uploads.request_upload(
            db, project, settings, channel_id=record.channel_id, options=options,
            categories=categories, force_new=payload.force_new,
        )
    except uploads.UploadRefused as exc:
        schedule_authority.release_reservation(db, reservation, f"refused:{exc.code}")
        raise _refused(exc) from exc
    except RevisionConflict as exc:
        schedule_authority.release_reservation(db, reservation, "revision_conflict")
        raise _error("revision_conflict", str(exc)) from exc
    if reservation is not None:
        if should_run:
            schedule_authority.attach_upload(db, reservation, upload)
        else:
            schedule_authority.release_reservation(db, reservation, "duplicate_request")
    if should_run:
        dispatch(upload.id, source.path)
        db.refresh(upload)
    return {"upload": uploads.serialize_upload(upload), "started": should_run, "warnings": list(source.warnings)}


@router.post("/uploads/{upload_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_upload_route(upload_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep, dispatch: DispatcherDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    if upload.youtube_video_id:
        raise _error("already_uploaded", f"Already uploaded as {upload.youtube_video_id}.")
    if upload.state != "failed" or upload.idempotency_key is None:
        raise _error("not_retryable", "Only a failed upload can be retried.")
    if upload.last_error_code == "session_expired_unknown_outcome":
        raise _error("unknown_outcome", "An earlier upload may have completed on YouTube. Check YouTube Studio, then confirm a new upload from the project.")
    project = _project_or_404(db, upload.project_id)
    try:
        source = uploads.resolve_upload_source(project, settings)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    options = publishing.PublishOptions.model_validate((upload.upload_settings or {}).get("options") or {"title": upload.title})
    if options.visibility == "schedule" and options.schedule is not None and options.schedule_source == "auto":
        # The failed attempt released its slot; claim it again or say it is gone.
        try:
            reservation = schedule_authority.claim_auto_slot(
                db, settings, store, provider, channel_id=upload.channel_id, choice=options.schedule, project_id=upload.project_id,
            )
        except schedule_authority.SlotUnavailable as exc:
            if exc.code == "slot_taken":
                exc.message = "That slot was just taken. Open Upload to YouTube again to use the next free slot."
            raise _slot_unavailable(exc) from exc
        schedule_authority.attach_upload(db, reservation, upload)
    dispatch(upload.id, source.path)
    db.refresh(upload)
    return {"upload": uploads.serialize_upload(upload), "started": True}


@router.post("/uploads/{upload_id}/thumbnail/retry")
def retry_thumbnail_route(upload_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        uploads.apply_thumbnail(db, upload, settings, store, provider)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    return {"upload": uploads.serialize_upload(upload)}


@router.post("/uploads/{upload_id}/audience")
def audience_route(upload_id: str, payload: AudienceUpdate, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        uploads.set_audience(db, upload, payload.made_for_kids, settings, store, provider)
    except uploads.UploadRefused as exc:
        raise _refused(exc) from exc
    except YouTubeApiError as exc:
        raise _api_error(exc) from exc
    return {"upload": uploads.serialize_upload(upload)}


@router.post("/uploads/{upload_id}/schedule")
def schedule_route(upload_id: str, payload: publishing.ScheduleChoice, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    upload = _upload_or_404(db, upload_id)
    try:
        uploads.schedule_publication(db, upload, payload, settings, store, provider)
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


# ---------------------------------------------------------------------------
# Publishing schedule (Smart Slot Planner)
# ---------------------------------------------------------------------------


def _schedule_channel(db: Session) -> str:
    record = connection.active_connection(db)
    if record is None:
        raise _error("not_connected", "Connect a YouTube channel first.")
    return record.channel_id


def _schedule_unavailable(db: Session) -> HTTPException:
    """A genuine load failure: a structured, CORS-readable error instead of a bare 500."""
    db.rollback()
    logger.exception("Publishing schedule could not be loaded")
    return _error("schedule_unavailable", "Could not load publishing schedule.")


def _schedule_payload(db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, *, timezone: str | None = None, refresh: bool = True, force: bool = False) -> dict:
    channel_id = _schedule_channel(db)
    try:
        state = schedule_authority.smart_state(db, settings, store, provider, channel_id, timezone_hint=timezone, refresh=refresh, force=force, days=7)
        record = schedule_authority.ensure_schedule(db, channel_id)
        return {**state, "learning": schedule_learning.analyze(db, record)}
    except SQLAlchemyError as exc:
        raise _schedule_unavailable(db) from exc


@router.get("/schedule")
def get_schedule_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep, timezone: str | None = None) -> dict:
    """Cadence, upcoming overview (YouTube re-checked when stale) and learning status."""
    return _schedule_payload(db, settings, store, provider, timezone=timezone)


@router.put("/schedule")
def save_schedule_route(payload: schedule_authority.ScheduleUpdate, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    channel_id = _schedule_channel(db)
    try:
        schedule_authority.save_schedule(db, channel_id, payload)
    except schedule_authority.ScheduleInvalid as exc:
        raise _error("invalid_schedule", str(exc), errors=exc.errors) from exc
    return _schedule_payload(db, settings, store, provider, refresh=False)


@router.post("/schedule/refresh")
def refresh_schedule_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    """Explicit "Refresh schedule": always asks YouTube (errors are reported, not hidden)."""
    return _schedule_payload(db, settings, store, provider, force=True)


@router.get("/schedule/next")
def next_slot_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep, refresh: bool = True) -> dict:
    """The sheet's recalculation after "That slot was just taken" / Retry."""
    channel_id = _schedule_channel(db)
    try:
        return schedule_authority.smart_state(db, settings, store, provider, channel_id, refresh=refresh)
    except SQLAlchemyError as exc:
        raise _schedule_unavailable(db) from exc


@router.post("/schedule/learned/apply")
def apply_learned_schedule_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    """The user's explicit approval of a learned proposal (never automatic)."""
    channel_id = _schedule_channel(db)
    record = schedule_authority.ensure_schedule(db, channel_id)
    analysis = schedule_learning.analyze(db, record)
    if not analysis["available"] or not analysis["suggested"]:
        raise _error("learned_unavailable", analysis.get("reason") or "Not enough channel data for a learned schedule yet.")
    try:
        schedule_authority.apply_learned_slots(db, record, analysis["suggested"], int(analysis["based_on"]))
    except schedule_authority.ScheduleInvalid as exc:
        raise _error("invalid_schedule", str(exc), errors=exc.errors) from exc
    return _schedule_payload(db, settings, store, provider, refresh=False)


@router.get("/archive")
def archive_route(db: DbSession, settings: SettingsDep) -> dict:
    """Read-only list of uploaded videos whose local project was deleted.

    Kept for API compatibility; the user-facing destination is the Video
    Library (``/api/videos?project=archived``), which reads the same records.
    """
    return {
        "entries": [
            lifecycle.serialize_archive_entry(db, upload, archive, min_sample=settings.youtube_baseline_min_sample)
            for upload, archive in lifecycle.archived_uploads(db)
        ]
    }


@router.get("/archive/{upload_id}")
def archive_entry_route(upload_id: str, db: DbSession, settings: SettingsDep) -> dict:
    entry = next(((upload, archive) for upload, archive in lifecycle.archived_uploads(db) if upload.id == upload_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Archived video not found")
    upload, archive = entry
    fingerprint = learning.fingerprint_for(db, upload)
    return {
        **lifecycle.serialize_archive_entry(db, upload, archive, min_sample=settings.youtube_baseline_min_sample),
        "performance": learning.performance_report(db, upload, min_sample=settings.youtube_baseline_min_sample),
        "fingerprint": {key: fingerprint.get(key) for key in ("content", "hook", "visual", "pacing", "quality", "audio")},
    }


# ---------------------------------------------------------------------------
# Video Library (project-independent; persisted state only on the index)
# ---------------------------------------------------------------------------

videos_router = APIRouter(prefix="/api/videos", tags=["videos"])


@videos_router.get("")
def list_videos_route(
    db: DbSession,
    settings: SettingsDep,
    status_filter: Annotated[str, Query(alias="status")] = "all",
    project: str = "all",
    analytics_filter: Annotated[str, Query(alias="analytics")] = "all",
    q: Annotated[str, Query(max_length=200)] = "",
    sort: str = "newest",
    limit: Annotated[int, Query(ge=1, le=library.MAX_PAGE_SIZE)] = library.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Every successfully uploaded video. Never asks YouTube: it reads the
    persisted status/statistics and a summary of the latest analytics."""
    return jsonable_encoder(library.list_videos(
        db, settings, status=status_filter, project=project, analytics=analytics_filter,
        query=q, sort=sort, limit=limit, offset=offset,
    ))


@videos_router.post("/refresh-recent")
def refresh_recent_videos_route(db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    """Explicit "Refresh recent videos": the existing status + due-analytics sync
    for the newest videos on the connected channel only (bounded)."""
    record = connection.active_connection(db)
    if record is None:
        raise _error("not_connected", "Connect a YouTube channel first.")
    results = []
    for upload in library.recent_videos(db, record.channel_id):
        try:
            outcome = analytics.refresh_analytics(db, upload, settings, store, provider, due_only=True)
        except uploads.UploadRefused as exc:
            outcome = {"status": "skipped", "reason": exc.code}
        results.append({"upload_id": upload.id, "video_id": upload.youtube_video_id, **outcome})
        if outcome.get("status") == "error" and (outcome.get("error") or {}).get("code") in {"auth_expired", "quota_exceeded", "not_connected", "insufficient_scope"}:
            break  # further calls would fail the same way
    errors = [item for item in results if item.get("status") == "error"]
    return {
        "checked": len(results),
        "errors": len(errors),
        "error": errors[0].get("error") if errors else None,
        "results": results,
    }


@videos_router.get("/{identifier}")
def video_detail_route(identifier: str, db: DbSession, settings: SettingsDep, store: StoreDep, provider: ProviderDep) -> dict:
    """One library video (by upload id or YouTube video id); works after project deletion."""
    upload = library.find_video(db, identifier)
    if upload is None:
        raise HTTPException(status_code=404, detail="Video not found")
    record = connection.active_connection(db)
    if record is not None and record.channel_id == upload.channel_id:
        uploads.reconcile_if_due(db, upload, settings, store, provider)  # freshness-gated, never raises
    return jsonable_encoder(library.video_detail(db, upload, settings))
