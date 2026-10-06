"""Instagram HTTP boundary: Instagram API with Facebook Login for Business.

ClipForge uploads the *local* final MP4 directly to Meta, which Meta supports
only through the resumable Reels upload, and only for apps using Facebook
Login for Business (``graph.facebook.com`` + ``rupload.facebook.com``).  The
"Instagram API with Instagram Login" (graph.instagram.com) requires a public
``video_url`` instead, which would mean hosting the video - so it is not used.

Official endpoints (Graph API, version from settings):

* OAuth dialog ``www.facebook.com/<v>/dialog/oauth`` (``scope`` or a Login for
  Business ``config_id``), code exchange and long-lived token exchange via
  ``/<v>/oauth/access_token`` (``fb_exchange_token``; ~60 days),
  ``/me/permissions`` (granted permissions) and ``DELETE /me/permissions``.
* Accounts: ``/me/accounts?fields=...,instagram_business_account{...}`` -
  only Instagram *professional* (Business/Creator) accounts linked to a
  Facebook Page can publish.
* Reels: ``POST /<ig-user-id>/media`` with ``media_type=REELS`` and
  ``upload_type=resumable`` (caption, share_to_feed, thumb_offset) returns a
  container id and an ``uri`` on rupload.facebook.com; the bytes are POSTed
  there with ``Authorization: OAuth <token>``, ``offset`` and ``file_size``
  headers; ``GET /<container>?fields=status_code,status`` until FINISHED
  (EXPIRED / ERROR / IN_PROGRESS / PUBLISHED); then
  ``POST /<ig-user-id>/media_publish?creation_id=<container>``.
* ``GET /<ig-user-id>/content_publishing_limit`` (100 API posts per 24 h).

Tokens travel only in headers or form bodies (never in a logged URL) and no
error message carries one.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .errors import PublishingApiError, redact

DIALOG_HOST = "https://www.facebook.com"
GRAPH_HOST = "https://graph.facebook.com"
REQUESTED_SCOPES = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")
PUBLISH_PERMISSION = "instagram_content_publish"
CONTAINER_TERMINAL = ("FINISHED", "ERROR", "EXPIRED", "PUBLISHED")
UPLOAD_BLOCK = 4 * 1024 * 1024


@dataclass(frozen=True)
class MetaApp:
    app_id: str
    app_secret: str
    redirect_uri: str
    graph_version: str
    login_config_id: str | None = None

    def __repr__(self) -> str:
        return f"MetaApp(app_id={self.app_id!r}, redirect_uri={self.redirect_uri!r}, graph_version={self.graph_version!r})"

    @property
    def graph(self) -> str:
        return f"{GRAPH_HOST}/{self.graph_version}"


@dataclass(frozen=True)
class MetaToken:
    access_token: str
    expires_in: float | None

    def __repr__(self) -> str:
        return f"MetaToken(expires_in={self.expires_in})"


class InstagramApi(Protocol):
    def exchange_code(self, app: MetaApp, code: str) -> MetaToken: ...

    def long_lived_token(self, app: MetaApp, short_lived: str) -> MetaToken: ...

    def facebook_user(self, app: MetaApp, token: str) -> dict[str, Any]: ...

    def permissions(self, app: MetaApp, token: str) -> list[str]: ...

    def instagram_accounts(self, app: MetaApp, token: str) -> list[dict[str, Any]]: ...

    def revoke(self, app: MetaApp, token: str) -> None: ...

    def create_reel_container(self, app: MetaApp, token: str, ig_user_id: str, params: dict[str, Any]) -> tuple[str, str]: ...

    def upload_video(self, app: MetaApp, token: str, upload_uri: str, path: Path, size: int) -> dict[str, Any]: ...

    def container_status(self, app: MetaApp, token: str, container_id: str) -> tuple[str, str | None]: ...

    def publish_container(self, app: MetaApp, token: str, ig_user_id: str, container_id: str) -> str: ...

    def media_permalink(self, app: MetaApp, token: str, media_id: str) -> str | None: ...

    def publishing_limit(self, app: MetaApp, token: str, ig_user_id: str) -> dict[str, Any]: ...


# Graph API error codes (``error.code``), see Meta's error-handling reference.
_AUTH_CODES = {190, 102, 463, 467}
_PERMISSION_CODES = {10, 200, 3}
_RATE_CODES = {4, 17, 32, 613, 80001, 80002}
_TRANSIENT_CODES = {1, 2}
# Instagram content publishing sub-codes.
_MEDIA_NOT_READY = {2207027, 9007}
_LIMIT_SUBCODES = {2207042}
_MEDIA_SUBCODES = {2207026, 2207004, 2207005, 2207006, 2207009, 2207013, 2207052, 2207053}


def _error(response: httpx.Response, *, context: str) -> PublishingApiError:
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code") if isinstance(error.get("code"), int) else None
    subcode = error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None
    text = redact(str(error.get("error_user_msg") or error.get("message") or ""))[:300]
    provider = f"{code}/{subcode}" if code is not None else None
    if code in _AUTH_CODES or status == 401:
        return PublishingApiError("auth_expired", "Instagram access expired or was revoked. Reconnect this Instagram account.", status_code=status, provider_code=provider)
    if subcode in _LIMIT_SUBCODES:
        return PublishingApiError("provider_limit", "Instagram's limit of API-published posts in 24 hours was reached.", retryable=False, status_code=status, provider_code=provider)
    if code in _MEDIA_NOT_READY or subcode in _MEDIA_NOT_READY:
        return PublishingApiError("media_not_ready", "Instagram is still processing the video.", status_code=status, provider_code=provider)
    if subcode in _MEDIA_SUBCODES:
        return PublishingApiError("invalid_media", text or "Instagram rejected the video.", retryable=False, status_code=status, provider_code=provider)
    if code in _PERMISSION_CODES or (code is not None and 200 <= code <= 299):
        return PublishingApiError("insufficient_scope", "ClipForge is missing an Instagram permission (instagram_content_publish). Reconnect and grant all requested permissions.", status_code=status, provider_code=provider)
    if code in _RATE_CODES or status == 429:
        return PublishingApiError("rate_limited", "Instagram's API rate limit was reached.", status_code=status, provider_code=provider)
    if error.get("is_transient") or code in _TRANSIENT_CODES or status >= 500:
        return PublishingApiError("provider_error", f"Instagram is temporarily unavailable ({context}).", status_code=status, provider_code=provider)
    return PublishingApiError("provider_rejected", text or f"Instagram refused the {context} request.", retryable=False, status_code=status, provider_code=provider)


def _file_blocks(path: Path, block: int = UPLOAD_BLOCK) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            data = handle.read(block)
            if not data:
                return
            yield data


class GraphInstagramApi:
    """Real implementation using httpx against graph.facebook.com / rupload.facebook.com."""

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 30.0, upload_timeout: float = 600.0) -> None:
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
            raise PublishingApiError("network_timeout", f"The Instagram {context} request timed out.") from None
        except httpx.HTTPError:
            raise PublishingApiError("network_error", f"Instagram could not be reached for {context}.") from None

    def _json(self, method: str, url: str, *, context: str, token: str | None = None, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self._send(method, url, context=context, headers=headers, **kwargs)
        if response.status_code != 200:
            raise _error(response, context=context)
        try:
            payload = response.json()
        except ValueError:
            raise PublishingApiError("provider_error", f"Instagram returned an unreadable {context} response.") from None
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _token(payload: dict[str, Any]) -> MetaToken:
        token = str(payload.get("access_token") or "")
        if not token:
            raise PublishingApiError("provider_error", "Meta returned no access token.", retryable=False)
        expires = payload.get("expires_in")
        return MetaToken(access_token=token, expires_in=float(expires) if expires else None)

    def exchange_code(self, app: MetaApp, code: str) -> MetaToken:
        # Form body (POST) keeps the code and app secret out of request URLs.
        return self._token(self._json("POST", f"{app.graph}/oauth/access_token", context="sign-in", data={
            "client_id": app.app_id, "client_secret": app.app_secret, "redirect_uri": app.redirect_uri, "code": code,
        }))

    def long_lived_token(self, app: MetaApp, short_lived: str) -> MetaToken:
        return self._token(self._json("POST", f"{app.graph}/oauth/access_token", context="token exchange", data={
            "grant_type": "fb_exchange_token", "client_id": app.app_id, "client_secret": app.app_secret, "fb_exchange_token": short_lived,
        }))

    def facebook_user(self, app: MetaApp, token: str) -> dict[str, Any]:
        return self._json("GET", f"{app.graph}/me", context="account lookup", token=token, params={"fields": "id,name"})

    def permissions(self, app: MetaApp, token: str) -> list[str]:
        payload = self._json("GET", f"{app.graph}/me/permissions", context="permission check", token=token)
        return [str(item.get("permission")) for item in payload.get("data") or [] if isinstance(item, dict) and item.get("status") == "granted"]

    def instagram_accounts(self, app: MetaApp, token: str) -> list[dict[str, Any]]:
        fields = "id,name,instagram_business_account{id,username,name,profile_picture_url}"
        accounts: list[dict[str, Any]] = []
        after: str | None = None
        for _page in range(20):  # bounded paging by cursor (never the paging.next URL)
            params = {"fields": fields, "limit": "50"}
            if after:
                params["after"] = after
            payload = self._json("GET", f"{app.graph}/me/accounts", context="account lookup", token=token, params=params)
            for page in payload.get("data") or []:
                ig = page.get("instagram_business_account") if isinstance(page, dict) else None
                if isinstance(ig, dict) and ig.get("id"):
                    accounts.append({**ig, "page_id": page.get("id"), "page_name": page.get("name")})
            after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (payload.get("paging") or {}).get("next"):
                break
        return accounts

    def revoke(self, app: MetaApp, token: str) -> None:
        response = self._send("DELETE", f"{app.graph}/me/permissions", context="disconnect", headers={"Authorization": f"Bearer {token}"})
        if response.status_code not in {200, 400, 401}:
            raise _error(response, context="disconnect")

    def create_reel_container(self, app: MetaApp, token: str, ig_user_id: str, params: dict[str, Any]) -> tuple[str, str]:
        body = {key: (str(value).lower() if isinstance(value, bool) else str(value)) for key, value in params.items() if value is not None}
        payload = self._json(
            "POST", f"{app.graph}/{ig_user_id}/media", context="Reel container", token=token,
            data={**body, "media_type": "REELS", "upload_type": "resumable"},
        )
        container, uri = payload.get("id"), payload.get("uri")
        if not container or not uri or not str(uri).startswith("https://rupload.facebook.com/"):
            raise PublishingApiError("provider_error", "Instagram returned no resumable upload destination.", retryable=False)
        return str(container), str(uri)

    def upload_video(self, app: MetaApp, token: str, upload_uri: str, path: Path, size: int) -> dict[str, Any]:
        if not upload_uri.startswith("https://rupload.facebook.com/"):
            raise PublishingApiError("provider_error", "Unexpected Instagram upload destination.", retryable=False)
        response = self._send(
            "POST", upload_uri, context="video upload", timeout=self._upload_timeout,
            headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"},
            content=_file_blocks(path),
        )
        if response.status_code != 200:
            raise _error(response, context="video upload")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("success") is False:
            raise PublishingApiError("provider_error", "Instagram did not accept the uploaded bytes.")
        return payload if isinstance(payload, dict) else {}

    def container_status(self, app: MetaApp, token: str, container_id: str) -> tuple[str, str | None]:
        payload = self._json("GET", f"{app.graph}/{container_id}", context="processing status", token=token, params={"fields": "status_code,status"})
        return str(payload.get("status_code") or "IN_PROGRESS"), (str(payload["status"])[:300] if payload.get("status") else None)

    def publish_container(self, app: MetaApp, token: str, ig_user_id: str, container_id: str) -> str:
        payload = self._json("POST", f"{app.graph}/{ig_user_id}/media_publish", context="publishing", token=token, data={"creation_id": container_id})
        media = payload.get("id")
        if not media:
            raise PublishingApiError("provider_error", "Instagram published without returning a media id.", retryable=False)
        return str(media)

    def media_permalink(self, app: MetaApp, token: str, media_id: str) -> str | None:
        payload = self._json("GET", f"{app.graph}/{media_id}", context="permalink", token=token, params={"fields": "permalink"})
        return str(payload["permalink"]) if payload.get("permalink") else None

    def publishing_limit(self, app: MetaApp, token: str, ig_user_id: str) -> dict[str, Any]:
        payload = self._json("GET", f"{app.graph}/{ig_user_id}/content_publishing_limit", context="publishing limit", token=token, params={"fields": "quota_usage,config"})
        data = payload.get("data") or []
        return data[0] if data and isinstance(data[0], dict) else {}
