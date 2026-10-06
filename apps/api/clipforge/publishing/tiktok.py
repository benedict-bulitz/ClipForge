"""TikTok HTTP boundary: Login Kit OAuth v2 and the Content Posting API (Direct Post).

Everything that talks to TikTok lives behind ``TikTokApi`` so tests use fakes.
Official endpoints (open.tiktokapis.com v2):

* OAuth: ``www.tiktok.com/v2/auth/authorize/`` (PKCE; the code challenge is the
  *hex* SHA-256 of the verifier, as TikTok's desktop flow requires),
  ``/v2/oauth/token/`` (authorization_code / refresh_token; access tokens
  last ~24 h, refresh tokens ~365 days and may be rotated) and
  ``/v2/oauth/revoke/``.
* Identity: ``/v2/user/info/?fields=open_id,union_id,avatar_url,display_name``
  (scope ``user.info.basic``).
* Direct Post (scope ``video.publish``):
  ``/v2/post/publish/creator_info/query/`` (must be queried before every post;
  its privacy_level_options, comment/duet/stitch flags and
  max_video_post_duration_sec are authoritative),
  ``/v2/post/publish/video/init/`` with ``source_info.source=FILE_UPLOAD``
  (returns ``publish_id`` + ``upload_url``), ``PUT upload_url`` with
  ``Content-Range: bytes first-last/total`` per chunk, and
  ``/v2/post/publish/status/fetch/`` (PROCESSING_UPLOAD, PROCESSING_DOWNLOAD,
  SEND_TO_USER_INBOX, PUBLISH_COMPLETE, FAILED).

Unaudited API clients may post only ``SELF_ONLY`` to private accounts (the
API answers ``unaudited_client_can_only_post_to_private_accounts``); ClipForge
never silently downgrades a requested public post.

No token, code or upload URL ever appears in a log line or an error message.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .errors import PublishingApiError

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API}/oauth/token/"
REVOKE_URL = f"{API}/oauth/revoke/"
USER_INFO_URL = f"{API}/user/info/"
CREATOR_INFO_URL = f"{API}/post/publish/creator_info/query/"
VIDEO_INIT_URL = f"{API}/post/publish/video/init/"
STATUS_URL = f"{API}/post/publish/status/fetch/"
REQUESTED_SCOPES = ("user.info.basic", "video.publish")
PUBLISH_SCOPE = "video.publish"

# FILE_UPLOAD chunk rules: chunks of 5-64 MB (the last may be up to 128 MB);
# a file under 5 MB is sent as one chunk; total_chunk_count =
# floor(video_size / chunk_size), the remainder joins the last chunk.
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
STATUS_TERMINAL = ("PUBLISH_COMPLETE", "FAILED")
PRIVATE_LEVEL = "SELF_ONLY"
PUBLIC_LEVEL = "PUBLIC_TO_EVERYONE"


def code_challenge(verifier: str) -> str:
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class TikTokClient:
    client_key: str
    client_secret: str
    redirect_uri: str

    def __repr__(self) -> str:
        return f"TikTokClient(client_key={self.client_key!r}, redirect_uri={self.redirect_uri!r})"


@dataclass(frozen=True)
class TikTokGrant:
    access_token: str
    expires_in: float
    open_id: str
    refresh_token: str | None = None
    refresh_expires_in: float | None = None
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never render tokens
        return f"TikTokGrant(open_id={self.open_id!r}, expires_in={self.expires_in}, scopes={self.scopes})"


@dataclass(frozen=True)
class CreatorInfo:
    """``creator_info/query``: the authority for what this creator may post now."""

    username: str | None
    nickname: str | None
    avatar_url: str | None
    privacy_level_options: tuple[str, ...]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "nickname": self.nickname,
            "avatar_url": self.avatar_url,
            "privacy_level_options": list(self.privacy_level_options),
            "comment_disabled": self.comment_disabled,
            "duet_disabled": self.duet_disabled,
            "stitch_disabled": self.stitch_disabled,
            "max_video_post_duration_sec": self.max_video_post_duration_sec,
        }


def chunk_plan(size: int, preferred: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """``(chunk_size, total_chunk_count)`` following TikTok's FILE_UPLOAD rules."""
    if size <= 0:
        raise PublishingApiError("invalid_media", "The video file is empty.")
    if size < MIN_CHUNK:
        return size, 1
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, preferred))
    return chunk, max(1, size // chunk)


def chunk_ranges(size: int, chunk_size: int, total_chunks: int) -> list[tuple[int, int]]:
    """Inclusive byte ranges; the remainder belongs to the final chunk."""
    ranges = []
    for index in range(total_chunks):
        first = index * chunk_size
        last = size - 1 if index == total_chunks - 1 else first + chunk_size - 1
        ranges.append((first, last))
    return ranges


class TikTokApi(Protocol):
    def exchange_code(self, client: TikTokClient, code: str, code_verifier: str) -> TikTokGrant: ...

    def refresh(self, client: TikTokClient, refresh_token: str) -> TikTokGrant: ...

    def revoke(self, client: TikTokClient, access_token: str) -> None: ...

    def user_info(self, access_token: str) -> dict[str, Any]: ...

    def creator_info(self, access_token: str) -> CreatorInfo: ...

    def init_direct_post(self, access_token: str, post_info: dict[str, Any], source_info: dict[str, Any]) -> tuple[str, str]: ...

    def upload_chunk(self, upload_url: str, data: bytes, first: int, last: int, total: int) -> int: ...

    def fetch_status(self, access_token: str, publish_id: str) -> dict[str, Any]: ...


# Content Posting / OAuth error codes -> (ClipForge code, retryable, user message)
_ERRORS: dict[str, tuple[str, bool, str]] = {
    "access_token_invalid": ("auth_expired", False, "TikTok access expired or was revoked. Reconnect this TikTok account."),
    "invalid_grant": ("auth_expired", False, "TikTok access expired or was revoked. Reconnect this TikTok account."),
    "scope_not_authorized": ("insufficient_scope", False, "This TikTok account did not grant the video.publish permission. Reconnect and allow posting."),
    "scope_permission_missed": ("insufficient_scope", False, "This TikTok account did not grant the video.publish permission. Reconnect and allow posting."),
    "rate_limit_exceeded": ("rate_limited", True, "TikTok's API rate limit was reached. ClipForge will retry shortly."),
    "spam_risk_too_many_posts": ("provider_limit", False, "TikTok's daily posting limit for this account was reached. Try again tomorrow."),
    "spam_risk_too_many_pending_share": ("provider_limit", False, "This TikTok account has too many pending posts. Finish or delete them in TikTok first."),
    "spam_risk_user_banned_from_posting": ("provider_policy", False, "TikTok does not currently allow this account to post."),
    "reached_active_user_cap": ("provider_limit", False, "This TikTok app reached its daily limit of posting users (unaudited apps are capped). Try again later or complete TikTok's app audit."),
    "unaudited_client_can_only_post_to_private_accounts": ("unaudited_client", False, "Public Direct Post requires TikTok app approval. Until the app passes TikTok's audit, only private (Only me) posts to a private account are allowed."),
    "privacy_level_option_mismatch": ("invalid_option", False, "TikTok no longer offers the chosen privacy option for this account. Open Upload again to choose from the current options."),
    "url_ownership_unverified": ("invalid_option", False, "TikTok refused the media source."),
    "invalid_file_upload": ("invalid_media", False, "TikTok rejected the video file."),
    "invalid_params": ("invalid_request", False, "TikTok rejected the post settings."),
    "internal_error": ("provider_error", True, "TikTok had a temporary problem. ClipForge will retry."),
}


def _error(response: httpx.Response, *, context: str) -> PublishingApiError:
    status = response.status_code
    provider_code = ""
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            provider_code = str(error.get("code") or "")
            message = str(error.get("message") or "")
        elif isinstance(error, str):  # OAuth token endpoint shape
            provider_code = error
            message = str(payload.get("error_description") or "")
    if provider_code in _ERRORS:
        code, retryable, text = _ERRORS[provider_code]
        return PublishingApiError(code, text, retryable=retryable, status_code=status, provider_code=provider_code)
    if status == 401:
        code, retryable, text = _ERRORS["access_token_invalid"]
        return PublishingApiError(code, text, retryable=retryable, status_code=status, provider_code=provider_code or None)
    if status == 429:
        return PublishingApiError("rate_limited", "TikTok's API rate limit was reached.", status_code=status, provider_code=provider_code or None)
    if status >= 500 or status == 408:
        return PublishingApiError("provider_error", f"TikTok is temporarily unavailable ({context}).", status_code=status, provider_code=provider_code or None)
    safe = message[:300] if message and "token" not in message.casefold() else ""
    return PublishingApiError("provider_rejected", safe or f"TikTok refused the {context} request.", retryable=False, status_code=status, provider_code=provider_code or None)


def _ok(payload: Any) -> bool:
    error = payload.get("error") if isinstance(payload, dict) else None
    return not isinstance(error, dict) or str(error.get("code") or "ok") == "ok"


class HttpTikTokApi:
    """Real implementation using httpx against TikTok's official endpoints."""

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 30.0, upload_timeout: float = 300.0) -> None:
        self._client = client
        self._timeout = timeout
        self._upload_timeout = upload_timeout

    def _send(self, method: str, url: str, *, context: str, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        try:
            if self._client is not None:
                return self._client.request(method, url, timeout=timeout or self._timeout, **kwargs)
            with httpx.Client(timeout=timeout or self._timeout, follow_redirects=False) as client:
                return client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise PublishingApiError("network_timeout", f"The TikTok {context} request timed out.") from None
        except httpx.HTTPError:
            raise PublishingApiError("network_error", f"TikTok could not be reached for {context}.") from None

    @staticmethod
    def _grant(payload: dict[str, Any]) -> TikTokGrant:
        token = str(payload.get("access_token") or "")
        if not token or not payload.get("open_id"):
            raise PublishingApiError("provider_error", "TikTok returned no access token.", retryable=False)
        return TikTokGrant(
            access_token=token,
            expires_in=float(payload.get("expires_in") or 86400),
            open_id=str(payload["open_id"]),
            refresh_token=payload.get("refresh_token") or None,
            refresh_expires_in=float(payload["refresh_expires_in"]) if payload.get("refresh_expires_in") else None,
            scopes=tuple(item for item in str(payload.get("scope") or "").replace(" ", ",").split(",") if item),
        )

    def _token(self, data: dict[str, str], context: str) -> TikTokGrant:
        response = self._send(
            "POST", TOKEN_URL, context=context, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        if response.status_code != 200 or (isinstance(error, str) and error) or not _ok(payload):
            raise _error(response, context=context)
        return self._grant(payload)

    def exchange_code(self, client: TikTokClient, code: str, code_verifier: str) -> TikTokGrant:
        return self._token({
            "client_key": client.client_key,
            "client_secret": client.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": client.redirect_uri,
            "code_verifier": code_verifier,
        }, "sign-in")

    def refresh(self, client: TikTokClient, refresh_token: str) -> TikTokGrant:
        return self._token({
            "client_key": client.client_key,
            "client_secret": client.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, "token refresh")

    def revoke(self, client: TikTokClient, access_token: str) -> None:
        response = self._send(
            "POST", REVOKE_URL, context="disconnect",
            data={"client_key": client.client_key, "client_secret": client.client_secret, "token": access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code not in {200, 400, 401}:
            raise _error(response, context="disconnect")

    @staticmethod
    def _auth(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}

    def _json(self, method: str, url: str, access_token: str, *, context: str, **kwargs: Any) -> dict[str, Any]:
        response = self._send(method, url, context=context, headers=self._auth(access_token), **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or not _ok(payload):
            raise _error(response, context=context)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def user_info(self, access_token: str) -> dict[str, Any]:
        data = self._json(
            "GET", USER_INFO_URL, access_token, context="account lookup",
            params={"fields": "open_id,union_id,avatar_url,display_name"},
        )
        user = data.get("user")
        return user if isinstance(user, dict) else {}

    def creator_info(self, access_token: str) -> CreatorInfo:
        data = self._json("POST", CREATOR_INFO_URL, access_token, context="creator info", json={})
        return parse_creator_info(data)

    def init_direct_post(self, access_token: str, post_info: dict[str, Any], source_info: dict[str, Any]) -> tuple[str, str]:
        data = self._json(
            "POST", VIDEO_INIT_URL, access_token, context="post initialization",
            json={"post_info": post_info, "source_info": source_info},
        )
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishingApiError("provider_error", "TikTok returned no upload destination.", retryable=False)
        return str(publish_id), str(upload_url)

    def upload_chunk(self, upload_url: str, data: bytes, first: int, last: int, total: int) -> int:
        response = self._send(
            "PUT", upload_url, context="video upload", timeout=self._upload_timeout,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)), "Content-Range": f"bytes {first}-{last}/{total}"},
            content=data,
        )
        if response.status_code not in {200, 201, 206}:
            raise _error(response, context="video upload")
        return response.status_code

    def fetch_status(self, access_token: str, publish_id: str) -> dict[str, Any]:
        return self._json("POST", STATUS_URL, access_token, context="post status", json={"publish_id": publish_id})


def parse_creator_info(data: dict[str, Any]) -> CreatorInfo:
    options = tuple(str(item) for item in data.get("privacy_level_options") or [] if item)
    duration = data.get("max_video_post_duration_sec")
    return CreatorInfo(
        username=(str(data["creator_username"]) if data.get("creator_username") else None),
        nickname=(str(data["creator_nickname"]) if data.get("creator_nickname") else None),
        avatar_url=(str(data["creator_avatar_url"]) if data.get("creator_avatar_url") else None),
        privacy_level_options=options,
        comment_disabled=bool(data.get("comment_disabled")),
        duet_disabled=bool(data.get("duet_disabled")),
        stitch_disabled=bool(data.get("stitch_disabled")),
        max_video_post_duration_sec=int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        raw=dict(data),
    )
