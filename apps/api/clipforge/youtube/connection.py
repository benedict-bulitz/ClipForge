"""The single YouTube connection authority.

Owns the OAuth flow (browser + PKCE + local callback), the connected channel
identity and access tokens.  The refresh token is written only to the OS
keyring (``SecretStore``); access tokens live only in process memory.
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
from ..models import YouTubeConnection
from ..security.secrets import SecretStore
from .provider import (
    AUTH_URL,
    REQUESTED_SCOPES,
    OAuthClient,
    YouTubeApiError,
    YouTubeProvider,
    has_capability,
)

PRIMARY = "primary"
REFRESH_TOKEN_SECRET = "YOUTUBE_REFRESH_TOKEN"
PENDING_TTL_SECONDS = 600
ACCESS_TOKEN_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class _PendingAuthorization:
    verifier: str
    created: float


_PENDING: dict[str, _PendingAuthorization] = {}
_ACCESS: dict[str, tuple[str, str, float]] = {}  # slot -> (channel_id, token, expires_monotonic)
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


def get_connection(db: Session) -> YouTubeConnection | None:
    return db.scalar(select(YouTubeConnection).where(YouTubeConnection.slot == PRIMARY))


def active_connection(db: Session) -> YouTubeConnection | None:
    connection = get_connection(db)
    return connection if connection is not None and connection.status != "disconnected" else None


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def begin_authorization(settings: Settings) -> str:
    """Browser URL for Google's consent screen (offline access, PKCE, CSRF state)."""
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "Add a Google OAuth client ID for YouTube first.")
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    now = time.monotonic()
    with _LOCK:
        for key in [key for key, item in _PENDING.items() if now - item.created > PENDING_TTL_SECONDS]:
            _PENDING.pop(key, None)
        _PENDING[state] = _PendingAuthorization(verifier=verifier, created=now)
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


def _cache_access(slot: str, channel_id: str, token: str, expires_in: float) -> None:
    with _LOCK:
        _ACCESS[slot] = (channel_id, token, time.monotonic() + max(0.0, expires_in - ACCESS_TOKEN_MARGIN_SECONDS))


def _set_error(db: Session, connection: YouTubeConnection, error: YouTubeApiError | None, *, status: str | None = None) -> None:
    connection.last_error_code = error.code if error else None
    connection.last_error_message = error.message if error else None
    if status:
        connection.status = status
    connection.updated_at = datetime.now(UTC)
    db.commit()


def complete_authorization(
    db: Session,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    code: str,
    state: str,
) -> YouTubeConnection:
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "Add a Google OAuth client ID for YouTube first.")
    with _LOCK:
        pending = _PENDING.pop(state, None)
    if pending is None or time.monotonic() - pending.created > PENDING_TTL_SECONDS:
        raise YouTubeApiError("invalid_state", "This YouTube sign-in link expired or was already used. Start again.")
    grant = provider.exchange_code(client, code, pending.verifier)
    identity = provider.get_my_channel(grant.access_token)
    existing = get_connection(db)
    refresh_token = grant.refresh_token
    if not refresh_token:
        try:
            stored = store.get_secret(REFRESH_TOKEN_SECRET)
        except KeyringError:
            stored = None
        if stored and existing is not None and existing.channel_id == identity.channel_id:
            refresh_token = stored
    if not refresh_token:
        raise YouTubeApiError("no_refresh_token", "Google did not grant offline access. Remove ClipForge from your Google account permissions and connect again.")
    try:
        store.set_secret(REFRESH_TOKEN_SECRET, refresh_token)
    except KeyringError as exc:
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable, so YouTube was not connected.") from exc
    scopes = list(grant.scopes) or list(REQUESTED_SCOPES)
    now = datetime.now(UTC)
    if existing is None:
        existing = YouTubeConnection(slot=PRIMARY, channel_id=identity.channel_id, connected_at=now)
        db.add(existing)
    existing.channel_id = identity.channel_id
    existing.channel_title = identity.title[:200]
    existing.granted_scopes = scopes
    existing.status = "connected"
    existing.last_error_code = None
    existing.last_error_message = None
    existing.connected_at = now
    existing.updated_at = now
    db.commit()
    db.refresh(existing)
    _cache_access(PRIMARY, identity.channel_id, grant.access_token, grant.expires_in)
    return existing


def access_token(
    db: Session,
    settings: Settings,
    store: SecretStore,
    provider: YouTubeProvider,
    *,
    capability: str,
) -> tuple[YouTubeConnection, str]:
    """A valid access token for the connected channel, refreshed when needed."""
    connection = active_connection(db)
    if connection is None:
        raise YouTubeApiError("not_connected", "Connect a YouTube channel first.")
    if not has_capability(connection.granted_scopes or [], capability):
        raise YouTubeApiError("insufficient_scope", f"The YouTube connection lacks the permission needed for {capability}. Reconnect and grant all requested permissions.")
    with _LOCK:
        cached = _ACCESS.get(PRIMARY)
    if cached and cached[0] == connection.channel_id and cached[2] > time.monotonic():
        return connection, cached[1]
    client = oauth_client(settings)
    if client is None:
        raise YouTubeApiError("client_not_configured", "The Google OAuth client for YouTube is not configured.")
    try:
        refresh_token = store.get_secret(REFRESH_TOKEN_SECRET)
    except KeyringError as exc:
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable.") from exc
    if not refresh_token:
        error = YouTubeApiError("auth_expired", "The YouTube sign-in is missing. Reconnect YouTube.")
        _set_error(db, connection, error, status="auth_expired")
        raise error
    try:
        grant = provider.refresh_access_token(client, refresh_token)
    except YouTubeApiError as exc:
        if exc.code == "auth_expired":
            with _LOCK:
                _ACCESS.pop(PRIMARY, None)
            _set_error(db, connection, exc, status="auth_expired")
        raise
    if grant.scopes:
        connection.granted_scopes = list(grant.scopes)
    if connection.status != "connected" or connection.last_error_code:
        connection.status = "connected"
        _set_error(db, connection, None)
    elif grant.scopes:
        db.commit()
    _cache_access(PRIMARY, connection.channel_id, grant.access_token, grant.expires_in)
    return connection, grant.access_token


def disconnect(db: Session, store: SecretStore, provider: YouTubeProvider) -> YouTubeConnection | None:
    """Revoke (best effort), forget the refresh token, keep the historic identity."""
    connection = get_connection(db)
    try:
        refresh_token = store.get_secret(REFRESH_TOKEN_SECRET)
    except KeyringError:
        refresh_token = None
    if refresh_token:
        try:
            provider.revoke(refresh_token)
        except YouTubeApiError:
            pass  # local disconnect must still succeed offline
    try:
        store.delete_secret(REFRESH_TOKEN_SECRET)
    except KeyringError as exc:
        raise YouTubeApiError("storage_error", "Secure credential storage is unavailable.") from exc
    with _LOCK:
        _ACCESS.pop(PRIMARY, None)
    if connection is not None:
        connection.status = "disconnected"
        connection.last_error_code = None
        connection.last_error_message = None
        connection.updated_at = datetime.now(UTC)
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


def serialize_connection(connection: YouTubeConnection | None, settings: Settings, store: SecretStore) -> dict[str, Any]:
    client = client_status(settings, store)
    if connection is None or connection.status == "disconnected":
        return {
            "status": "not_connected",
            "channel_id": None,
            "channel_title": None,
            "previous_channel_id": connection.channel_id if connection else None,
            "capabilities": {},
            "client": client,
            "error": None,
        }
    scopes = connection.granted_scopes or []
    return {
        "status": connection.status,
        "channel_id": connection.channel_id,
        "channel_title": connection.channel_title,
        "channel_url": f"https://www.youtube.com/channel/{connection.channel_id}",
        "connected_at": connection.connected_at.replace(tzinfo=UTC) if connection.connected_at and connection.connected_at.tzinfo is None else connection.connected_at,
        "capabilities": {name: has_capability(scopes, name) for name in ("upload", "read", "schedule", "analytics")},
        "client": client,
        "error": {"code": connection.last_error_code, "message": connection.last_error_message} if connection.last_error_code else None,
    }
