from __future__ import annotations

"""TikTok and Instagram sign-in, per-account credentials and access tokens.

Each account's long-lived credential is stored only in its own keyring entry
(``TIKTOK_REFRESH_TOKEN:<account id>`` / ``INSTAGRAM_TOKEN:<account id>``);
access tokens are cached in memory per account.  ``access_token`` always
resolves the credential of exactly the account it was asked for, so one
account's token can never be used for another account's post.
"""

import time
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import urlencode

from keyring.errors import KeyringError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import PublishingAccount
from ..security.secrets import SecretStore
from . import accounts, oauth
from . import instagram as ig
from . import tiktok as tt
from .errors import PublishingApiError

ACCESS_MARGIN_SECONDS = 120
INSTAGRAM_RENEW_WARNING = timedelta(days=7)

_ACCESS: dict[str, tuple[str, str, float]] = {}  # account id -> (external id, token, expires_monotonic)
_LOCK = Lock()


def reset_cache() -> None:
    with _LOCK:
        _ACCESS.clear()
    oauth.reset()


def _cache(account: PublishingAccount, token: str, expires_in: float | None) -> None:
    with _LOCK:
        _ACCESS[account.id] = (account.external_account_id, token, time.monotonic() + max(0.0, (expires_in or 3600) - ACCESS_MARGIN_SECONDS))


def _cached(account: PublishingAccount) -> str | None:
    with _LOCK:
        item = _ACCESS.get(account.id)
    if item and item[0] == account.external_account_id and item[2] > time.monotonic():
        return item[1]
    return None


def _drop(account_id: str) -> None:
    with _LOCK:
        _ACCESS.pop(account_id, None)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Developer-app configuration (never serialized beyond "configured")
# ---------------------------------------------------------------------------


def tiktok_client(settings: Settings) -> tt.TikTokClient | None:
    key, secret = (settings.tiktok_client_key or "").strip(), (settings.tiktok_client_secret or "").strip()
    if not key or not secret:
        return None
    return tt.TikTokClient(client_key=key, client_secret=secret, redirect_uri=settings.tiktok_oauth_redirect_uri)


def meta_app(settings: Settings, config: dict[str, Any] | None = None) -> ig.MetaApp | None:
    app_id, secret = (settings.meta_app_id or "").strip(), (settings.meta_app_secret or "").strip()
    if not app_id or not secret:
        return None
    return ig.MetaApp(
        app_id=app_id, app_secret=secret, redirect_uri=settings.instagram_oauth_redirect_uri,
        graph_version=settings.meta_graph_version, login_config_id=((config or {}).get("login_config_id") or None),
    )


def client_status(platform: str, settings: Settings, store: SecretStore, config: dict[str, Any]) -> dict[str, Any]:
    names = {"tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"), "instagram": ("META_APP_ID", "META_APP_SECRET")}[platform]
    values = {"tiktok": (settings.tiktok_client_key, settings.tiktok_client_secret), "instagram": (settings.meta_app_id, settings.meta_app_secret)}[platform]

    def source(name: str, value: str | None) -> str | None:
        try:
            stored = store.get_secret(name)  # type: ignore[arg-type]
        except KeyringError:
            stored = None
        return "keyring" if stored else ("environment" if value else None)

    configured = tiktok_client(settings) is not None if platform == "tiktok" else meta_app(settings) is not None
    status: dict[str, Any] = {
        "configured": configured,
        "client_id_source": source(names[0], values[0]),
        "client_secret_configured": bool(values[1]),
        "redirect_uri": settings.tiktok_oauth_redirect_uri if platform == "tiktok" else settings.instagram_oauth_redirect_uri,
        "scopes": list(tt.REQUESTED_SCOPES if platform == "tiktok" else ig.REQUESTED_SCOPES),
    }
    if platform == "tiktok":
        status["app_audited"] = bool(config.get("app_audited"))
    else:
        status["graph_version"] = settings.meta_graph_version
        status["login_config_id"] = config.get("login_config_id")
    return status


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


def tiktok_restrictions(config: dict[str, Any], scopes: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    restrictions: list[dict[str, Any]] = []
    if not config.get("app_audited"):
        restrictions.append({
            "code": "unaudited_client",
            "message": "Public Direct Post requires TikTok app approval. Until the app passes TikTok's audit, posts are private (Only me) and the TikTok account must be set to private.",
            "blocks_publishing": False,
        })
    if scopes and tt.PUBLISH_SCOPE not in scopes:
        restrictions.append({"code": "insufficient_scope", "message": "Posting permission (video.publish) was not granted. Reconnect and allow posting.", "blocks_publishing": True})
    return restrictions


def begin_tiktok(settings: Settings, *, account_id: str | None = None) -> str:
    client = tiktok_client(settings)
    if client is None:
        raise PublishingApiError("client_not_configured", "Add the TikTok app's client key and secret first.", retryable=False)
    state, verifier = oauth.begin("tiktok", account_id=account_id)
    query = {
        "client_key": client.client_key,
        "scope": ",".join(tt.REQUESTED_SCOPES),
        "response_type": "code",
        "redirect_uri": client.redirect_uri,
        "state": state,
        "code_challenge": tt.code_challenge(verifier),
        "code_challenge_method": "S256",
        # Always show the account picker so "Add account" can sign in as another creator.
        "disable_auto_auth": "1",
    }
    return f"{tt.AUTH_URL}?{urlencode(query)}"


def complete_tiktok(db: Session, settings: Settings, store: SecretStore, api: tt.TikTokApi, *, code: str, state: str) -> PublishingAccount:
    client = tiktok_client(settings)
    if client is None:
        raise PublishingApiError("client_not_configured", "Add the TikTok app's client key and secret first.", retryable=False)
    pending = oauth.complete("tiktok", state)
    grant = api.exchange_code(client, code, pending.verifier)
    if not grant.refresh_token:
        raise PublishingApiError("no_refresh_token", "TikTok did not return a refresh token. Connect again.", retryable=False)
    user = api.user_info(grant.access_token)
    open_id = str(user.get("open_id") or grant.open_id)
    if open_id != grant.open_id:
        raise PublishingApiError("identity_mismatch", "TikTok returned a different account than the one that signed in.", retryable=False)
    handle = None
    creator: dict[str, Any] = {}
    if tt.PUBLISH_SCOPE in grant.scopes or not grant.scopes:
        try:
            info = api.creator_info(grant.access_token)
            handle, creator = info.username, info.as_dict()
        except PublishingApiError:
            pass  # identity is enough to connect; creator info is re-queried before every post
    config = accounts.platform_config(db, "tiktok")
    account, _created = accounts.upsert_identity(db, accounts.AccountIdentity(
        platform="tiktok",
        external_account_id=open_id,
        display_name=str(user.get("display_name") or creator.get("nickname") or handle or "TikTok account"),
        handle=handle,
        avatar_url=str(user.get("avatar_url") or creator.get("avatar_url") or "") or None,
        granted_scopes=grant.scopes,
        restrictions=tuple(tiktok_restrictions(config, grant.scopes)),
        details={
            "profile_url": f"https://www.tiktok.com/@{handle}" if handle else None,
            "creator_info": creator or None,
            "refresh_expires_at": (_now() + timedelta(seconds=grant.refresh_expires_in)).isoformat() if grant.refresh_expires_in else None,
        },
    ))
    try:
        store.set_account_secret("TIKTOK_REFRESH_TOKEN", account.id, grant.refresh_token)
    except (KeyringError, ValueError) as exc:
        accounts.set_error(db, account, "storage_error", "Secure credential storage is unavailable.", status="auth_expired")
        raise PublishingApiError("storage_error", "Secure credential storage is unavailable, so TikTok was not connected.", retryable=False) from exc
    _cache(account, grant.access_token, grant.expires_in)
    return account


def _tiktok_token(db: Session, settings: Settings, store: SecretStore, api: tt.TikTokApi, account: PublishingAccount) -> str:
    cached = _cached(account)
    if cached:
        return cached
    client = tiktok_client(settings)
    if client is None:
        raise PublishingApiError("client_not_configured", "The TikTok app is not configured.", retryable=False)
    try:
        refresh_token = store.get_account_secret("TIKTOK_REFRESH_TOKEN", account.id)
    except (KeyringError, ValueError) as exc:
        raise PublishingApiError("storage_error", "Secure credential storage is unavailable.", retryable=True) from exc
    if not refresh_token:
        accounts.set_error(db, account, "auth_expired", "The TikTok sign-in is missing. Reconnect this account.", status="auth_expired")
        raise PublishingApiError("auth_expired", "The TikTok sign-in is missing. Reconnect this account.", retryable=False)
    try:
        grant = api.refresh(client, refresh_token)
    except PublishingApiError as exc:
        if exc.code in {"auth_expired", "insufficient_scope"}:
            _drop(account.id)
            accounts.set_error(db, account, exc.code, exc.message, status="auth_expired")
        raise
    if grant.open_id != account.external_account_id:
        raise PublishingApiError("identity_mismatch", "The stored TikTok credential belongs to a different account. Reconnect this account.", retryable=False)
    if grant.refresh_token and grant.refresh_token != refresh_token:
        store.set_account_secret("TIKTOK_REFRESH_TOKEN", account.id, grant.refresh_token)  # TikTok may rotate it
    if account.status != "connected" or account.last_error_code:
        account.status = "connected"
        accounts.set_error(db, account, None, None)
    _cache(account, grant.access_token, grant.expires_in)
    return grant.access_token


# ---------------------------------------------------------------------------
# Instagram (Facebook Login for Business)
# ---------------------------------------------------------------------------


def begin_instagram(db: Session, settings: Settings, *, account_id: str | None = None) -> str:
    app = meta_app(settings, accounts.platform_config(db, "instagram"))
    if app is None:
        raise PublishingApiError("client_not_configured", "Add the Meta app ID and secret first.", retryable=False)
    state, _verifier = oauth.begin("instagram", account_id=account_id)
    query = {"client_id": app.app_id, "redirect_uri": app.redirect_uri, "state": state, "response_type": "code"}
    if app.login_config_id:
        query["config_id"] = app.login_config_id  # Facebook Login for Business configuration
    else:
        query["scope"] = ",".join(ig.REQUESTED_SCOPES)
    # Lets the user add Pages/Instagram accounts not granted the first time.
    query["auth_type"] = "rerequest"
    return f"{ig.DIALOG_HOST}/{app.graph_version}/dialog/oauth?{urlencode(query)}"


def instagram_restrictions(permissions: list[str]) -> list[dict[str, Any]]:
    if ig.PUBLISH_PERMISSION in permissions:
        return []
    return [{"code": "insufficient_scope", "message": "Content publishing permission (instagram_content_publish) was not granted. Reconnect and allow it.", "blocks_publishing": True}]


def complete_instagram(db: Session, settings: Settings, store: SecretStore, api: ig.InstagramApi, *, code: str, state: str) -> list[PublishingAccount]:
    """One Facebook sign-in can grant several Instagram professional accounts;
    each becomes its own ClipForge account with its own keyring entry."""
    app = meta_app(settings, accounts.platform_config(db, "instagram"))
    if app is None:
        raise PublishingApiError("client_not_configured", "Add the Meta app ID and secret first.", retryable=False)
    oauth.complete("instagram", state)
    short = api.exchange_code(app, code)
    token = api.long_lived_token(app, short.access_token)
    user = api.facebook_user(app, token.access_token)
    permissions = api.permissions(app, token.access_token)
    found = api.instagram_accounts(app, token.access_token)
    if not found:
        raise PublishingApiError(
            "no_professional_account",
            "No Instagram professional (Business or Creator) account linked to a Facebook Page was shared. Personal Instagram accounts cannot publish through Meta's API.",
            retryable=False,
        )
    expires_at = (_now() + timedelta(seconds=token.expires_in)).isoformat() if token.expires_in else None
    connected: list[PublishingAccount] = []
    for item in found:
        account, _created = accounts.upsert_identity(db, accounts.AccountIdentity(
            platform="instagram",
            external_account_id=str(item["id"]),
            display_name=str(item.get("name") or item.get("username") or "Instagram account"),
            handle=str(item["username"]) if item.get("username") else None,
            avatar_url=str(item["profile_picture_url"]) if item.get("profile_picture_url") else None,
            granted_scopes=tuple(permissions),
            restrictions=tuple(instagram_restrictions(permissions)),
            details={
                "page_id": item.get("page_id"),
                "page_name": item.get("page_name"),
                "facebook_user_id": user.get("id"),
                "token_expires_at": expires_at,
                "account_type": "professional",
                "profile_url": f"https://www.instagram.com/{item['username']}/" if item.get("username") else None,
            },
        ))
        try:
            store.set_account_secret("INSTAGRAM_TOKEN", account.id, token.access_token)
        except (KeyringError, ValueError) as exc:
            accounts.set_error(db, account, "storage_error", "Secure credential storage is unavailable.", status="auth_expired")
            raise PublishingApiError("storage_error", "Secure credential storage is unavailable, so Instagram was not connected.", retryable=False) from exc
        _cache(account, token.access_token, token.expires_in)
        connected.append(account)
    return connected


def _instagram_token(db: Session, store: SecretStore, account: PublishingAccount) -> str:
    cached = _cached(account)
    if cached:
        return cached
    try:
        token = store.get_account_secret("INSTAGRAM_TOKEN", account.id)
    except (KeyringError, ValueError) as exc:
        raise PublishingApiError("storage_error", "Secure credential storage is unavailable.", retryable=True) from exc
    expires = (account.details or {}).get("token_expires_at")
    expired = False
    if expires:
        try:
            expired = datetime.fromisoformat(str(expires)) <= _now()
        except ValueError:
            expired = False
    if not token or expired:
        message = "The Instagram sign-in expired (Meta tokens last about 60 days). Reconnect this account."
        accounts.set_error(db, account, "auth_expired", message, status="auth_expired")
        raise PublishingApiError("auth_expired", message, retryable=False)
    remaining = None
    if expires:
        remaining = (datetime.fromisoformat(str(expires)) - _now()).total_seconds()
    _cache(account, token, min(remaining, 3600) if remaining else 3600)
    return token


def instagram_token_warning(account: PublishingAccount) -> dict[str, Any] | None:
    expires = (account.details or {}).get("token_expires_at")
    if not expires:
        return None
    try:
        at = datetime.fromisoformat(str(expires))
    except ValueError:
        return None
    if at - _now() > INSTAGRAM_RENEW_WARNING:
        return None
    return {"code": "token_expiring", "message": f"The Instagram sign-in expires on {at.date().isoformat()}. Reconnect to renew it.", "blocks_publishing": False}


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def access_token(
    db: Session, settings: Settings, store: SecretStore, *, account: PublishingAccount,
    tiktok_api: tt.TikTokApi | None = None,
) -> str:
    """A valid access token for exactly ``account``."""
    if not accounts.is_active(account):
        raise PublishingApiError("not_connected", "This account is disconnected. Reconnect it in Settings → Integrations.", retryable=False)
    if account.platform == "tiktok":
        if tiktok_api is None:
            raise ValueError("tiktok_api is required")
        return _tiktok_token(db, settings, store, tiktok_api, account)
    if account.platform == "instagram":
        return _instagram_token(db, store, account)
    raise ValueError(f"Unsupported platform for this authority: {account.platform}")


def disconnect(
    db: Session, settings: Settings, store: SecretStore, account: PublishingAccount, *,
    tiktok_api: tt.TikTokApi | None = None, instagram_api: ig.InstagramApi | None = None,
) -> PublishingAccount:
    """Forget this account's credential (revoking it where that cannot affect
    another connected account) and keep its identity and history."""
    base = "TIKTOK_REFRESH_TOKEN" if account.platform == "tiktok" else "INSTAGRAM_TOKEN"
    try:
        stored = store.get_account_secret(base, account.id)  # type: ignore[arg-type]
    except (KeyringError, ValueError):
        stored = None
    if stored and account.platform == "tiktok" and tiktok_api is not None:
        client = tiktok_client(settings)
        if client is not None:
            try:
                grant = tiktok_api.refresh(client, stored)
                tiktok_api.revoke(client, grant.access_token)
            except PublishingApiError:
                pass  # local disconnect must still succeed offline
    if stored and account.platform == "instagram" and instagram_api is not None:
        # One Facebook authorization can cover several Instagram accounts:
        # revoke it at Meta only when no other connected account shares it.
        user_id = (account.details or {}).get("facebook_user_id")
        shared = any(
            other.id != account.id and (other.details or {}).get("facebook_user_id") == user_id
            for other in accounts.list_accounts(db, "instagram")
        )
        app = meta_app(settings)
        if not shared and app is not None:
            try:
                instagram_api.revoke(app, stored)
            except PublishingApiError:
                pass
    try:
        store.delete_account_secret(base, account.id)  # type: ignore[arg-type]
    except (KeyringError, ValueError) as exc:
        raise PublishingApiError("storage_error", "Secure credential storage is unavailable.", retryable=False) from exc
    _drop(account.id)
    return accounts.mark_disconnected(db, account)
