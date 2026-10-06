"""YouTube connections: any number of channels, each its own account.

Owns the OAuth flow (browser + PKCE + local callback), the channel identities
(``publishing_accounts`` rows with ``platform="youtube"``) and access tokens.
Every channel's refresh token is written only to its own OS keyring entry
(``YOUTUBE_REFRESH_TOKEN:<account id>``); access tokens live only in process
memory, keyed by account.  A video, schedule or analytics call always names
the channel it belongs to (``channel_id``), so one channel's token is never
used for another channel's video.

The pre-multi-account installation had one ``youtube_connections`` row
("primary") and one global ``YOUTUBE_REFRESH_TOKEN``; ``accounts`` adopts that
row and ``_refresh_token`` copies the global token to the adopted account's
own entry the first time it is needed (non-destructive, see the report).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from urllib.parse import urlencode

from keyring.errors import KeyringError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import PublishingAccount, YouTubeConnection
from ..publishing import accounts
from ..security.secrets import SecretStore
from .provider import (
    AUTH_URL,
    REQUESTED_SCOPES,
    OAuthClient,
    YouTubeApiError,
    YouTubeProvider,
    has_capability,
)

PLATFORM = "youtube"
PRIMARY = "primary"  # the legacy single-connection slot (migration only)
LEGACY_REFRESH_TOKEN_SECRET = "YOUTUBE_REFRESH_TOKEN"
REFRESH_TOKEN_BASE = "YOUTUBE_REFRESH_TOKEN"
PENDING_TTL_SECONDS = 600
ACCESS_TOKEN_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class _PendingAuthorization:
    verifier: str
    created: float
    # The account the user asked to reconnect (None = "Add account").
    account_id: str | None = None


_PENDING: dict[str, _PendingAuthorization] = {}
_ACCESS: dict[str, tuple[str, str, float]] = {}  # account id -> (channel_id, token, expires_monotonic)
_LOCK = Lock()


def reset_youtube_auth_cache() -> None:
    with _LOCK:
        _PENDING.clear()
        _ACCESS.clear()


def oauth_client(settings: Settings) -> OAuthClient | None:
    client_id = (settings.youtube_oauth_client_id or "").strip()
    if not client_id:
        return None
    secret = (settings.youtube_oauth_client_secret or "").strip() or None
    return OAuthClient(client_id=client_id, client_secret=secret, redirect_uri=settings.youtube_oauth_redirect_uri)


# ---------------------------------------------------------------------------
# Account lookups
# ---------------------------------------------------------------------------


def list_channels(db: Session, *, include_disconnected: bool = False) -> list[PublishingAccount]:
    return accounts.list_accounts(db, PLATFORM, include_disconnected=include_disconnected)


def get_connection(db: Session, account_id: str | None = None) -> PublishingAccount | None:
    """One YouTube account (``account_id``) or the default one; disconnected
    identities are returned too (they keep their history)."""
    if account_id:
        account = accounts.get_account(db, account_id)
        return account if account is not None and account.platform == PLATFORM else None
    current = accounts.default_account(db, PLATFORM)
    if current is not None:
        return current
    rows = accounts.list_accounts(db, PLATFORM, include_disconnected=True)
    return max(rows, key=lambda item: accounts.aware(item.updated_at) or datetime.min.replace(tzinfo=UTC)) if rows else None


def active_connection(db: Session, account_id: str | None = None) -> PublishingAccount | None:
    """A connected YouTube account: the named one, else the default channel."""
    connection = get_connection(db, account_id)
    return connection if accounts.is_active(connection) else None


def account_for_channel(db: Session, channel_id: str | None) -> PublishingAccount | None:
    """The active account of exactly this channel (never another channel's)."""
    if not channel_id:
        return None
    account = accounts.find_account(db, PLATFORM, channel_id)
    return account if accounts.is_active(account) else None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def begin_authorization(settings: Settings, *, account_id: str | None = None) -> str:
    """Browser URL for Google's consent screen (offline access, PKCE, CSRF state).

    Google's account chooser is always shown, so "Add account" can pick
    another Google account or brand channel.
    """
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "Add a Google OAuth client ID for YouTube first.")
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    now = time.monotonic()
    with _LOCK:
        for key in [key for key, item in _PENDING.items() if now - item.created > PENDING_TTL_SECONDS]:
            _PENDING.pop(key, None)
        _PENDING[state] = _PendingAuthorization(verifier=verifier, created=now, account_id=account_id)
    query = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(REQUESTED_SCOPES),
        "access_type": "offline",
        # Always return a refresh token, also on reconnect.
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def _cache_access(account_id: str, channel_id: str, token: str, expires_in: float) -> None:
    with _LOCK:
        _ACCESS[account_id] = (channel_id, token, time.monotonic() + max(0.0, expires_in - ACCESS_TOKEN_MARGIN_SECONDS))


def _drop_access(account_id: str) -> None:
    with _LOCK:
        _ACCESS.pop(account_id, None)


def _legacy_row(db: Session, account: PublishingAccount) -> YouTubeConnection | None:
    slot = (account.details or {}).get("legacy_slot")
    return db.get(YouTubeConnection, slot) if slot else None


def _refresh_token(store: SecretStore, account: PublishingAccount) -> str | None:
    """This account's refresh token; the migrated legacy account adopts the old global one."""
    token = store.get_account_secret(REFRESH_TOKEN_BASE, account.id)
    if token or not (account.details or {}).get("legacy_secret"):
        return token
    legacy = store.get_secret(LEGACY_REFRESH_TOKEN_SECRET)
    if not legacy:
        return None
    store.set_account_secret(REFRESH_TOKEN_BASE, account.id, legacy)
    if store.get_account_secret(REFRESH_TOKEN_BASE, account.id) != legacy:
        raise KeyringError("The migrated YouTube credential could not be verified.")
    return legacy


def peek_channel(db: Session, channel_id: str) -> PublishingAccount | YouTubeConnection | None:
    """Read-only lookup for diagnostics: the channel's account, or its legacy
    row when it has not been migrated yet (nothing is written)."""
    account = accounts.find_account(db, PLATFORM, channel_id, migrate=False)
    if account is not None:
        return account
    return next((row for row in db.scalars(select(YouTubeConnection)).all() if row.channel_id == channel_id), None)


def stored_refresh_token(store: SecretStore, record: PublishingAccount | YouTubeConnection | None) -> str | None:
    """Read-only (diagnostics): the refresh token ``access_token`` would use; never copies it."""
    if isinstance(record, PublishingAccount):
        token = store.get_account_secret(REFRESH_TOKEN_BASE, record.id)
        if token or not (record.details or {}).get("legacy_secret"):
            return token
        return store.get_secret(LEGACY_REFRESH_TOKEN_SECRET)
    if isinstance(record, YouTubeConnection) and record.slot == PRIMARY:
        return store.get_secret(LEGACY_REFRESH_TOKEN_SECRET)
    return None


def _forget_legacy_secret(db: Session, store: SecretStore, account: PublishingAccount) -> None:
    """The adopted global token is retired once the account has its own (or is disconnected)."""
    details = dict(account.details or {})
    if not details.get("legacy_secret"):
        return
    try:
        store.delete_secret(LEGACY_REFRESH_TOKEN_SECRET)
    except KeyringError:
        return
    details["legacy_secret"] = False
    account.details = details
    legacy = _legacy_row(db, account)
    if legacy is not None and legacy.channel_id == account.external_account_id:
        # Keep the old single-connection row truthful for an older ClipForge.
        legacy.status = account.status
    db.commit()


def complete_authorization(
    db: Session,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    code: str,
    state: str,
) -> PublishingAccount:
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "Add a Google OAuth client ID for YouTube first.")
    with _LOCK:
        pending = _PENDING.pop(state, None)
    if pending is None or time.monotonic() - pending.created > PENDING_TTL_SECONDS:
        raise YouTubeApiError("invalid_state", "This YouTube sign-in link expired or was already used. Start again.")
    grant = provider.exchange_code(client, code, pending.verifier)
    identity = provider.get_my_channel(grant.access_token)
    existing = accounts.find_account(db, PLATFORM, identity.channel_id)
    refresh_token = grant.refresh_token
    if not refresh_token and existing is not None:
        try:
            refresh_token = _refresh_token(store, existing)
        except KeyringError:
            refresh_token = None
    if not refresh_token:
        raise YouTubeApiError("no_refresh_token", "Google did not grant offline access. Remove ClipForge from your Google account permissions and connect again.")
    scopes = list(grant.scopes) or list(REQUESTED_SCOPES)
    account, _created = accounts.upsert_identity(db, accounts.AccountIdentity(
        platform=PLATFORM,
        external_account_id=identity.channel_id,
        display_name=identity.title[:200],
        granted_scopes=tuple(scopes),
        details={"profile_url": f"https://www.youtube.com/channel/{identity.channel_id}"},
    ))
    try:
        store.set_account_secret(REFRESH_TOKEN_BASE, account.id, refresh_token)
    except (KeyringError, ValueError) as exc:
        accounts.set_error(db, account, "storage_error", "Secure credential storage is unavailable.", status="auth_expired")
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable, so YouTube was not connected.") from exc
    _forget_legacy_secret(db, store, account)
    _cache_access(account.id, identity.channel_id, grant.access_token, grant.expires_in)
    return account


def access_token(
    db: Session,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    capability: str,
    channel_id: str | None = None,
    account_id: str | None = None,
) -> tuple[PublishingAccount, str]:
    """A valid access token for one channel, refreshed when needed.

    ``channel_id`` (a video's / schedule's own channel) is authoritative:
    the token returned always belongs to exactly that channel.  Without it,
    ``account_id`` or else the default YouTube account is used.
    """
    if channel_id:
        connection = account_for_channel(db, channel_id)
        if connection is None:
            raise YouTubeApiError("not_connected", "The YouTube channel this belongs to is not connected. Reconnect it in Settings → Integrations.")
    else:
        connection = active_connection(db, account_id)
    if connection is None:
        raise YouTubeApiError("not_connected", "Connect a YouTube channel first.")
    if not has_capability(connection.granted_scopes or [], capability):
        raise YouTubeApiError("insufficient_scope", f"The YouTube connection lacks the permission needed for {capability}. Reconnect and grant all requested permissions.")
    with _LOCK:
        cached = _ACCESS.get(connection.id)
    if cached and cached[0] == connection.external_account_id and cached[2] > time.monotonic():
        return connection, cached[1]
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "The Google OAuth client for YouTube is not configured.")
    try:
        refresh_token = _refresh_token(store, connection)
    except (KeyringError, ValueError) as exc:
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable.") from exc
    if not refresh_token:
        error = YouTubeApiError("auth_expired", "The YouTube sign-in is missing. Reconnect YouTube.")
        accounts.set_error(db, connection, error.code, error.message, status="auth_expired")
        raise error
    try:
        grant = provider.refresh_access_token(client, refresh_token)
    except YouTubeApiError as exc:
        if exc.code == "auth_expired":
            _drop_access(connection.id)
            accounts.set_error(db, connection, exc.code, exc.message, status="auth_expired")
        raise
    if grant.scopes:
        connection.granted_scopes = list(grant.scopes)
    if connection.status != "connected" or connection.last_error_code:
        connection.status = "connected"
        accounts.set_error(db, connection, None, None)
    elif grant.scopes:
        db.commit()
    _cache_access(connection.id, connection.external_account_id, grant.access_token, grant.expires_in)
    return connection, grant.access_token


def disconnect(db: Session, store: SecretStore, provider: YouTubeProvider, account_id: str | None = None) -> PublishingAccount | None:
    """Revoke (best effort), forget this channel's refresh token, keep its identity.

    Other channels' credentials are never touched.
    """
    connection = get_connection(db, account_id)
    if connection is None:
        return None
    try:
        refresh_token = _refresh_token(store, connection)
    except (KeyringError, ValueError):
        refresh_token = None
    if refresh_token:
        try:
            provider.revoke(refresh_token)
        except YouTubeApiError:
            pass  # local disconnect must still succeed offline
    try:
        store.delete_account_secret(REFRESH_TOKEN_BASE, connection.id)
    except (KeyringError, ValueError) as exc:
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable.") from exc
    _drop_access(connection.id)
    accounts.mark_disconnected(db, connection)
    _forget_legacy_secret(db, store, connection)
    legacy = _legacy_row(db, connection)
    if legacy is not None and legacy.channel_id == connection.external_account_id and legacy.status != "disconnected":
        legacy.status = "disconnected"
        db.commit()
    return connection


def client_status(settings: Settings, store: SecretStore) -> dict[str, Any]:
    def source(name: str, value: str | None) -> str | None:
        try:
            stored = store.get_secret(name)  # type: ignore[arg-type]
        except KeyringError:
            stored = None
        return "keyring" if stored else ("environment" if value else None)

    return {
        "configured": oauth_client(settings) is not None,
        "client_id_source": source("YOUTUBE_OAUTH_CLIENT_ID", settings.youtube_oauth_client_id),
        "client_secret_configured": bool(settings.youtube_oauth_client_secret),
        "redirect_uri": settings.youtube_oauth_redirect_uri,
    }


def capabilities(connection: PublishingAccount) -> dict[str, bool]:
    scopes = connection.granted_scopes or []
    return {name: has_capability(scopes, name) for name in ("upload", "read", "schedule", "analytics")}


def serialize_connection(connection: PublishingAccount | None, settings: Settings, store: SecretStore) -> dict[str, Any]:
    client = client_status(settings, store)
    if connection is None or connection.status == "disconnected":
        return {
            "status": "not_connected",
            "account_id": None,
            "channel_id": None,
            "channel_title": None,
            "previous_channel_id": connection.external_account_id if connection else None,
            "capabilities": {},
            "client": client,
            "error": None,
        }
    return {
        "status": connection.status,
        "account_id": connection.id,
        "channel_id": connection.external_account_id,
        "channel_title": connection.display_name,
        "channel_url": f"https://www.youtube.com/channel/{connection.external_account_id}",
        "connected_at": accounts.aware(connection.connected_at),
        "capabilities": capabilities(connection),
        "client": client,
        "error": {"code": connection.last_error_code, "message": connection.last_error_message} if connection.last_error_code else None,
    }
