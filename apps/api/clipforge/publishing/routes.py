from __future__ import annotations

"""HTTP API for multi-platform publishing (accounts, Instagram/TikTok posts, scheduler).

YouTube keeps its own mature endpoints (``/api/youtube/...``); this router
lists YouTube channels next to Instagram and TikTok accounts and delegates
YouTube connect/disconnect to ``youtube.connection``.  No response ever
contains a credential: developer-app secrets are reported as configured or
not, account tokens never leave the keyring.
"""

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
from sqlalchemy.orm import Session

from ..config import Settings, get_settings, refresh_settings
from ..database import SessionLocal, get_db
from ..integrations import get_secret_store
from ..models import PublishingAccount, SocialPublication
from ..security.secrets import SecretStore
from ..services import get_project
from ..youtube import connection as youtube_connection
from ..youtube.provider import YouTubeApiError, YouTubeProvider
from ..youtube.publishing import ScheduleChoice
from ..youtube.routes import get_youtube_provider
from . import accounts, connections, publications, read_model, scheduler
from .capabilities import account_capabilities, platform_capabilities
from .errors import PublishingApiError
from .instagram import GraphInstagramApi
from .publications import Apis, PublicationRefused, PublicationRequest
from .tiktok import HttpTikTokApi

logger = logging.getLogger(__name__)
CALLBACK_PREFIX = "/api/publishing/"


class _CallbackLogRedactor(logging.Filter):
    """uvicorn logs full request paths; OAuth callbacks carry one-time codes."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and CALLBACK_PREFIX in str(args[2]) and "/oauth/callback" in str(args[2]):
            path = str(args[2]).split("?", 1)[0]
            record.args = (*args[:2], path + "?[redacted]", *args[3:])
        return True


_REDACTOR = _CallbackLogRedactor()
if not any(isinstance(item, _CallbackLogRedactor) for item in logging.getLogger("uvicorn.access").filters):
    logging.getLogger("uvicorn.access").addFilter(_REDACTOR)


def get_publishing_apis() -> Apis:
    return Apis(tiktok=HttpTikTokApi(), instagram=GraphInstagramApi())


def start_publication_thread(publication_id: str) -> None:
    def work() -> None:
        scheduler.drive(SessionLocal, publication_id, get_settings(), SecretStore(), get_publishing_apis())

    Thread(target=work, daemon=True, name=f"publication-{publication_id[:8]}").start()


def get_publication_dispatcher():
    return start_publication_thread


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ApisDep = Annotated[Apis, Depends(get_publishing_apis)]
YouTubeDep = Annotated[YouTubeProvider, Depends(get_youtube_provider)]
DispatcherDep = Annotated[Any, Depends(get_publication_dispatcher)]

router = APIRouter(prefix="/api/publishing", tags=["publishing"])

ERROR_STATUS = {
    "client_not_configured": status.HTTP_409_CONFLICT,
    "not_connected": status.HTTP_409_CONFLICT,
    "unknown_account": status.HTTP_404_NOT_FOUND,
    "auth_expired": status.HTTP_401_UNAUTHORIZED,
    "insufficient_scope": status.HTTP_403_FORBIDDEN,
    "storage_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    "network_timeout": status.HTTP_503_SERVICE_UNAVAILABLE,
    "network_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    "provider_error": status.HTTP_502_BAD_GATEWAY,
    "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
    "preflight_failed": 422,
    "revision_conflict": status.HTTP_409_CONFLICT,
    "already_published": status.HTTP_409_CONFLICT,
    "already_scheduled": status.HTTP_409_CONFLICT,
    "unknown_outcome": status.HTTP_409_CONFLICT,
    "not_rendered": status.HTTP_409_CONFLICT,
    "render_missing": status.HTTP_409_CONFLICT,
    "not_cancellable": status.HTTP_409_CONFLICT,
    "not_reschedulable": status.HTTP_409_CONFLICT,
    "not_retryable": status.HTTP_409_CONFLICT,
    "not_missed": status.HTTP_409_CONFLICT,
    "invalid_schedule": 422,
}


def _error(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST), detail={"status": code, "message": message, **extra})


def _refused(exc: PublicationRefused) -> HTTPException:
    extra: dict[str, Any] = {}
    if exc.publication is not None:
        extra["publication"] = jsonable_encoder(publications.serialize(exc.publication))
    if exc.issues:
        extra["issues"] = exc.issues
    return _error(exc.code, exc.message, **extra)


def _account_or_404(db: Session, account_id: str) -> PublishingAccount:
    account = accounts.get_account(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _publication_or_404(db: Session, publication_id: str) -> SocialPublication:
    row = db.get(SocialPublication, publication_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return row


def _project_or_404(db: Session, project_id: str):
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def serialize_account(db: Session, account: PublishingAccount) -> dict[str, Any]:
    config = accounts.platform_config(db, account.platform)
    youtube_scopes = youtube_connection.capabilities(account) if account.platform == "youtube" else None
    caps = account_capabilities(account, config=config, youtube_scopes=youtube_scopes)
    restrictions = list(account.restrictions or [])
    if account.platform == "instagram" and accounts.is_active(account):
        warning = connections.instagram_token_warning(account)
        if warning:
            restrictions.append(warning)
    if account.platform == "tiktok" and accounts.is_active(account):
        # The audit state is a ClipForge setting; keep the card in sync with it.
        restrictions = [item for item in restrictions if item.get("code") != "unaudited_client"]
        if not config.get("app_audited"):
            restrictions = connections.tiktok_restrictions(config, ()) + restrictions
    return accounts.serialize_account(account, caps) | {"restrictions": restrictions}


def accounts_overview(db: Session, settings: Settings, store: SecretStore) -> dict[str, Any]:
    rows = accounts.list_accounts(db, include_disconnected=False)
    platforms: dict[str, Any] = {}
    for platform in accounts.PLATFORMS:
        config = accounts.platform_config(db, platform)
        client = youtube_connection.client_status(settings, store) if platform == "youtube" else connections.client_status(platform, settings, store, config)
        platforms[platform] = {
            "platform": platform,
            "label": accounts.PLATFORM_LABELS[platform],
            "client": client,
            "config": config,
            "capabilities": platform_capabilities(platform, config=config),
            "accounts": [serialize_account(db, item) for item in rows if item.platform == platform],
            "default_account_id": (default.id if (default := accounts.default_account(db, platform)) else None),
        }
    return {"platforms": platforms, "scheduler": scheduler.status()}


@router.get("/accounts")
def accounts_route(db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    return jsonable_encoder(accounts_overview(db, settings, store))


@router.put("/accounts/{account_id}/default")
def default_account_route(account_id: str, db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    account = _account_or_404(db, account_id)
    try:
        accounts.set_default(db, account)
    except ValueError as exc:
        raise _error("not_connected", str(exc)) from exc
    return jsonable_encoder(accounts_overview(db, settings, store))


@router.delete("/accounts/{account_id}")
def disconnect_route(account_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep, youtube: YouTubeDep) -> dict:
    """Disconnect one account; every other account keeps its credentials."""
    account = _account_or_404(db, account_id)
    try:
        if account.platform == "youtube":
            youtube_connection.disconnect(db, store, youtube, account.id)
        else:
            connections.disconnect(db, settings, store, account, tiktok_api=apis.tiktok, instagram_api=apis.instagram)
    except (YouTubeApiError, PublishingApiError) as exc:
        raise _error(exc.code, exc.message) from exc
    return jsonable_encoder(accounts_overview(db, settings, store))


@router.post("/accounts/{account_id}/check")
def check_account_route(account_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep, youtube: YouTubeDep) -> dict:
    """Non-destructive live check: refresh the token and read identity/limits only."""
    account = _account_or_404(db, account_id)
    result: dict[str, Any] = {"ok": False}
    try:
        if account.platform == "youtube":
            _record, token = youtube_connection.access_token(db, settings, store, youtube, capability="read", account_id=account.id)
            identity = youtube.get_my_channel(token)
            result = {"ok": identity.channel_id == account.external_account_id, "identity": identity.title}
        elif account.platform == "tiktok":
            token = connections.access_token(db, settings, store, account=account, tiktok_api=apis.tiktok)
            creator = apis.tiktok.creator_info(token)
            details = dict(account.details or {})
            details["creator_info"] = creator.as_dict()
            account.details = details
            db.commit()
            result = {"ok": True, "creator_info": creator.as_dict()}
        else:
            token = connections.access_token(db, settings, store, account=account)
            app = connections.meta_app(settings, accounts.platform_config(db, "instagram"))
            if app is None:
                raise PublishingApiError("client_not_configured", "The Meta app is not configured.", retryable=False)
            result = {"ok": True, "publishing_limit": apis.instagram.publishing_limit(app, token, account.external_account_id)}
    except (YouTubeApiError, PublishingApiError) as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    db.refresh(account)
    return jsonable_encoder({**result, "account": serialize_account(db, account)})


@router.post("/{platform}/authorize")
def authorize_route(platform: str, db: DbSession, settings: SettingsDep, account_id: str | None = None) -> dict:
    """"Add account" (no ``account_id``) or "Reconnect" (an account's id)."""
    try:
        if platform == "youtube":
            return {"authorization_url": youtube_connection.begin_authorization(settings, account_id=account_id)}
        if platform == "tiktok":
            return {"authorization_url": connections.begin_tiktok(settings, account_id=account_id)}
        if platform == "instagram":
            return {"authorization_url": connections.begin_instagram(db, settings, account_id=account_id)}
    except (YouTubeApiError, PublishingApiError) as exc:
        raise _error(exc.code, exc.message) from exc
    raise HTTPException(status_code=404, detail="Unknown platform")


def _settings_redirect(settings: Settings, params: dict[str, str]) -> RedirectResponse:
    origin = (settings.allowed_origins or ["http://localhost:3000"])[0].rstrip("/")
    return RedirectResponse(f"{origin}/settings/integrations?{urlencode(params)}", status_code=303)


@router.get("/tiktok/oauth/callback", include_in_schema=False)
def tiktok_callback_route(
    db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep,
    code: str | None = None, state: str | None = None, error: str | None = None,
) -> RedirectResponse:
    if error or not code or not state:
        reason = "access_denied" if error == "access_denied" else "authorization_failed"
        return _settings_redirect(settings, {"platform": "tiktok", "result": "error", "reason": reason})
    try:
        account = connections.complete_tiktok(db, settings, store, apis.tiktok, code=code, state=state)
    except PublishingApiError as exc:
        logger.warning("TikTok connection failed code=%s", exc.code)
        return _settings_redirect(settings, {"platform": "tiktok", "result": "error", "reason": exc.code})
    return _settings_redirect(settings, {"platform": "tiktok", "result": "connected", "account": account.id})


@router.get("/instagram/oauth/callback", include_in_schema=False)
def instagram_callback_route(
    db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep,
    code: str | None = None, state: str | None = None, error: str | None = None,
) -> RedirectResponse:
    if error or not code or not state:
        reason = "access_denied" if error == "access_denied" else "authorization_failed"
        return _settings_redirect(settings, {"platform": "instagram", "result": "error", "reason": reason})
    try:
        connected = connections.complete_instagram(db, settings, store, apis.instagram, code=code, state=state)
    except PublishingApiError as exc:
        logger.warning("Instagram connection failed code=%s", exc.code)
        return _settings_redirect(settings, {"platform": "instagram", "result": "error", "reason": exc.code})
    return _settings_redirect(settings, {"platform": "instagram", "result": "connected", "count": str(len(connected))})


class ClientUpdate(BaseModel):
    client_id: str = Field(min_length=4, max_length=300)
    client_secret: SecretStr | None = None


CLIENT_SECRETS = {"tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"), "instagram": ("META_APP_ID", "META_APP_SECRET"), "youtube": ("YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET")}


@router.put("/{platform}/client")
def save_client_route(platform: str, payload: ClientUpdate, db: DbSession, store: StoreDep) -> dict:
    """Developer-app credentials go only to the keyring; the response says "configured"."""
    if platform not in CLIENT_SECRETS:
        raise HTTPException(status_code=404, detail="Unknown platform")
    id_name, secret_name = CLIENT_SECRETS[platform]
    try:
        store.set_secret(id_name, payload.client_id.strip())  # type: ignore[arg-type]
        secret = payload.client_secret.get_secret_value().strip() if payload.client_secret else ""
        if secret:
            store.set_secret(secret_name, secret)  # type: ignore[arg-type]
    except (KeyringError, ValueError) as exc:
        raise _error("storage_error", "Secure credential storage is unavailable.") from exc
    settings = refresh_settings()
    return jsonable_encoder(accounts_overview(db, settings, store))


@router.delete("/{platform}/client")
def delete_client_route(platform: str, db: DbSession, store: StoreDep) -> dict:
    if platform not in CLIENT_SECRETS:
        raise HTTPException(status_code=404, detail="Unknown platform")
    try:
        for name in CLIENT_SECRETS[platform]:
            store.delete_secret(name)  # type: ignore[arg-type]
    except KeyringError as exc:
        raise _error("storage_error", "Secure credential storage is unavailable.") from exc
    settings = refresh_settings()
    return jsonable_encoder(accounts_overview(db, settings, store))


class PlatformConfigUpdate(BaseModel):
    app_audited: bool | None = None
    login_config_id: str | None = Field(default=None, max_length=64, pattern=r"^[0-9]*$")


@router.put("/{platform}/config")
def save_config_route(platform: str, payload: PlatformConfigUpdate, db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    if platform not in ("tiktok", "instagram"):
        raise HTTPException(status_code=404, detail="Unknown platform")
    values = payload.model_dump(exclude_unset=True)
    if "login_config_id" in values:
        values["login_config_id"] = values["login_config_id"] or None
    accounts.save_platform_config(db, platform, values)
    return jsonable_encoder(accounts_overview(db, settings, store))


# ---------------------------------------------------------------------------
# Project publishing
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/targets")
def targets_route(project_id: str, db: DbSession, settings: SettingsDep, store: StoreDep) -> dict:
    """The unified sheet's account selector: every connected account (with
    capabilities) and the default per platform, plus this project's publications."""
    _project_or_404(db, project_id)
    overview = accounts_overview(db, settings, store)
    targets = [
        {**account, "label": f"{account['platform_label']} · {('@' + account['handle'].lstrip('@')) if account.get('handle') and account['platform'] != 'youtube' else account['display_name']}"}
        for platform in accounts.PLATFORMS
        for account in overview["platforms"][platform]["accounts"]
    ]
    defaults = {platform: overview["platforms"][platform]["default_account_id"] for platform in accounts.PLATFORMS}
    initial = defaults.get("youtube") or next((value for value in defaults.values() if value), None)
    return jsonable_encoder({
        "targets": targets,
        "defaults": defaults,
        "initial_account_id": initial,
        "publications": read_model.project_publications(db, project_id),
        "scheduler": overview["scheduler"],
    })


@router.get("/projects/{project_id}/draft")
def draft_route(project_id: str, db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep, account_id: Annotated[str, Query(max_length=36)]) -> dict:
    project = _project_or_404(db, project_id)
    account = _account_or_404(db, account_id)
    if account.platform == "youtube":
        raise _error("use_youtube_draft", "YouTube accounts use /api/youtube/projects/{id}/draft.")
    if not accounts.is_active(account):
        raise _error("not_connected", "This account is disconnected. Reconnect it in Settings → Integrations.")
    return jsonable_encoder(publications.draft(db, project, account, settings, store, apis))


@router.post("/projects/{project_id}/preflight")
def preflight_route(project_id: str, payload: PublicationRequest, db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep) -> dict:
    project = _project_or_404(db, project_id)
    try:
        issues, resolution, _account, creator = publications.preflight(db, project, settings, store, apis, payload)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    return jsonable_encoder({
        "issues": issues,
        "ready": not issues,
        "schedule": resolution.as_dict() if resolution else None,
        "creator_info": creator.as_dict() if creator else None,
    })


@router.post("/projects/{project_id}/publications", status_code=status.HTTP_202_ACCEPTED)
def create_publication_route(
    project_id: str, payload: PublicationRequest, db: DbSession, settings: SettingsDep, store: StoreDep, apis: ApisDep, dispatch: DispatcherDep,
) -> dict:
    project = _project_or_404(db, project_id)
    try:
        row, run_now = publications.request_publication(db, project, settings, store, apis, payload)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    if run_now:
        dispatch(row.id)
        db.refresh(row)
    return jsonable_encoder({"publication": publications.serialize(row), "started": run_now, "scheduler": scheduler.status()})


@router.get("/publications")
def list_publications_route(
    db: DbSession,
    project_id: Annotated[str | None, Query(max_length=36)] = None,
    platform: str | None = None,
    account_id: Annotated[str | None, Query(max_length=36)] = None,
    state: str | None = None,
) -> dict:
    query = select(SocialPublication)
    if project_id:
        query = query.where(SocialPublication.project_id == project_id)
    if platform:
        query = query.where(SocialPublication.platform == platform)
    if account_id:
        query = query.where(SocialPublication.account_id == account_id)
    if state:
        query = query.where(SocialPublication.state == state)
    rows = db.scalars(query.order_by(SocialPublication.created_at.desc()).limit(200)).all()
    return jsonable_encoder({"publications": [publications.serialize(row) for row in rows], "scheduler": scheduler.status()})


@router.get("/publications/{publication_id}")
def publication_route(publication_id: str, db: DbSession) -> dict:
    return jsonable_encoder({"publication": publications.serialize(_publication_or_404(db, publication_id))})


@router.post("/publications/{publication_id}/cancel")
def cancel_route(publication_id: str, db: DbSession) -> dict:
    row = _publication_or_404(db, publication_id)
    try:
        publications.cancel(db, row)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    return jsonable_encoder({"publication": publications.serialize(row)})


@router.post("/publications/{publication_id}/publish-now", status_code=status.HTTP_202_ACCEPTED)
def publish_now_route(publication_id: str, db: DbSession, dispatch: DispatcherDep) -> dict:
    row = _publication_or_404(db, publication_id)
    try:
        publications.publish_missed_now(db, row)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    dispatch(row.id)
    db.refresh(row)
    return jsonable_encoder({"publication": publications.serialize(row)})


@router.post("/publications/{publication_id}/reschedule")
def reschedule_route(publication_id: str, payload: ScheduleChoice, db: DbSession) -> dict:
    row = _publication_or_404(db, publication_id)
    try:
        publications.reschedule(db, row, payload)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    return jsonable_encoder({"publication": publications.serialize(row)})


@router.post("/publications/{publication_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_route(publication_id: str, db: DbSession, dispatch: DispatcherDep) -> dict:
    row = _publication_or_404(db, publication_id)
    try:
        clone = publications.retry_as_new(db, row)
    except PublicationRefused as exc:
        raise _refused(exc) from exc
    dispatch(clone.id)
    db.refresh(clone)
    return jsonable_encoder({"publication": publications.serialize(clone), "previous": publications.serialize(row)})


@router.get("/scheduler")
def scheduler_route() -> dict:
    return jsonable_encoder(scheduler.status())
