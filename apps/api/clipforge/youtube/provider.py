"""Google / YouTube HTTP boundary.

Everything that talks to Google lives behind ``YouTubeProvider`` so tests use
fakes and never upload a real video.  Official endpoints (YouTube Data API v3,
YouTube Analytics API v2, Google OAuth 2.0 for installed/local apps):

* OAuth: ``accounts.google.com/o/oauth2/v2/auth`` (PKCE S256, offline access),
  ``oauth2.googleapis.com/token`` and ``oauth2.googleapis.com/revoke``.
* Channel identity: ``youtube/v3/channels?part=snippet&mine=true``.
* Upload: resumable ``upload/youtube/v3/videos?uploadType=resumable``.
* Status/schedule: ``youtube/v3/videos`` list/update (``part=status``);
  ``publishAt`` is only honoured on a private video that was never published.
* Channel schedule (Smart Slot Planner): the uploads playlist from
  ``channels?part=contentDetails&mine=true``
  (``contentDetails.relatedPlaylists.uploads``), paged with
  ``playlistItems?part=contentDetails`` (``maxResults`` <= 50,
  ``nextPageToken``), then ``videos.list`` by id in batches of <= 50.
  ``search.list`` (100 quota units per call) is never used here.
* Topic Intelligence (read-only discovery): ``videos.list?chart=mostPopular``
  (1 unit), ``channels.list`` by id (1 unit per <= 50 ids) and, bounded to a
  few calls per discovery refresh and cached for a day, ``search.list``.
* Analytics: ``youtubeanalytics.googleapis.com/v2/reports``.

Secrets never leave this module in a log line or an error message: errors
carry a stable ``code`` plus a user-safe message taken from Google's
``reason``/``message`` fields, which do not contain credentials.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DATA_API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_API = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"

# Minimum scopes: upload + read the channel/video status, edit the video's
# status for scheduling (videos.update accepts only youtube / youtube.force-ssl
# / youtubepartner; youtube.upload alone cannot set publishAt after upload),
# and read-only analytics.  ``youtube`` subsumes upload and readonly.
SCOPE_MANAGE = "https://www.googleapis.com/auth/youtube"
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
REQUESTED_SCOPES = (SCOPE_MANAGE, SCOPE_ANALYTICS)

CAPABILITY_SCOPES: dict[str, tuple[str, ...]] = {
    "upload": (SCOPE_MANAGE, SCOPE_UPLOAD, SCOPE_FORCE_SSL),
    "read": (SCOPE_MANAGE, SCOPE_READONLY, SCOPE_FORCE_SSL),
    "schedule": (SCOPE_MANAGE, SCOPE_FORCE_SSL),
    "analytics": (SCOPE_ANALYTICS, "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"),
}

UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024  # a multiple of 256 KiB, as the API requires
PAGE_SIZE = 50  # playlistItems.list maxResults maximum; also the videos.list id batch limit

logger = logging.getLogger(__name__)


def has_capability(scopes: list[str] | tuple[str, ...], capability: str) -> bool:
    granted = set(scopes or ())
    return any(scope in granted for scope in CAPABILITY_SCOPES[capability])


class YouTubeApiError(RuntimeError):
    """A classified, secret-free Google API failure."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None, retryable: bool = False, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.reason = reason


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    expires_in: float
    refresh_token: str | None = None
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never render tokens
        return f"TokenGrant(expires_in={self.expires_in}, scopes={self.scopes})"


@dataclass(frozen=True)
class ChannelIdentity:
    channel_id: str
    title: str


@dataclass
class UploadProgress:
    """Result of querying or sending bytes to a resumable session."""

    complete: bool
    offset: int = 0
    video: dict[str, Any] | None = None


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None
    redirect_uri: str

    def __repr__(self) -> str:
        return f"OAuthClient(client_id={self.client_id!r}, redirect_uri={self.redirect_uri!r})"


class YouTubeProvider(Protocol):
    def exchange_code(self, client: OAuthClient, code: str, code_verifier: str) -> TokenGrant: ...

    def refresh_access_token(self, client: OAuthClient, refresh_token: str) -> TokenGrant: ...

    def revoke(self, token: str) -> None: ...

    def get_my_channel(self, access_token: str) -> ChannelIdentity: ...

    def get_uploads_playlist_id(self, access_token: str) -> str: ...

    def list_playlist_items(self, access_token: str, playlist_id: str, page_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]: ...

    def start_resumable_upload(self, access_token: str, body: dict[str, Any], size: int, content_type: str, *, notify_subscribers: bool = True) -> str: ...

    def query_upload(self, access_token: str, session_uri: str, size: int) -> UploadProgress: ...

    def upload_chunk(self, access_token: str, session_uri: str, data: bytes, offset: int, size: int) -> UploadProgress: ...

    def list_videos(self, access_token: str, video_ids: list[str], parts: str) -> list[dict[str, Any]]: ...

    def update_video(self, access_token: str, body: dict[str, Any], parts: str) -> dict[str, Any]: ...

    def analytics_report(self, access_token: str, params: dict[str, str]) -> dict[str, Any]: ...

    def set_thumbnail(self, access_token: str, video_id: str, data: bytes, content_type: str) -> dict[str, Any]: ...

    def list_categories(self, access_token: str, region_code: str, language: str) -> list[dict[str, Any]]: ...

    def list_popular_videos(self, access_token: str, region_code: str, category_id: str | None, max_results: int) -> list[dict[str, Any]]: ...

    def list_channels(self, access_token: str, channel_ids: list[str], parts: str) -> list[dict[str, Any]]: ...

    def search_videos(self, access_token: str, params: dict[str, str]) -> list[dict[str, Any]]: ...


class _Redactor(logging.Filter):
    """Keep resumable-upload session identifiers out of third-party HTTP logs."""

    pattern = re.compile(r"(upload_id|token|code)=[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        if "googleapis.com" in message or "accounts.google.com" in message:
            record.msg = self.pattern.sub(r"\1=[redacted]", message)
            record.args = ()
        return True


_REDACTOR = _Redactor()
for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if _REDACTOR not in _logger.filters:
        _logger.addFilter(_REDACTOR)


def _google_error(response: httpx.Response, *, context: str) -> YouTubeApiError:
    """Map a Google error response to a stable code with a safe message."""
    status = response.status_code
    reason = ""
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        details = error.get("errors") if isinstance(error.get("errors"), list) else []
        if details and isinstance(details[0], dict):
            reason = str(details[0].get("reason") or "")
        if not reason:
            for detail in error.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason = str(detail["reason"])
                    break
        if not reason and error.get("status"):
            reason = str(error["status"])
    elif isinstance(error, str):
        # OAuth token endpoint shape: {"error": "invalid_grant", "error_description": "..."}
        reason = error
        message = str(payload.get("error_description") or "")
    message = re.sub(r"(access_token|refresh_token|token)=\S+", r"\1=[redacted]", message)[:400]
    lowered = reason.casefold()
    if reason in {"invalid_grant", "unauthorized_client", "invalid_client"} or status == 401:
        return YouTubeApiError("auth_expired", "YouTube access expired or was revoked. Reconnect YouTube.", status_code=status, reason=reason)
    if lowered in {"quotaexceeded", "dailylimitexceeded", "ratelimitexceeded", "uploadlimitexceeded", "userratelimitexceeded", "uploadratelimitexceeded"}:
        return YouTubeApiError("quota_exceeded", message or "The YouTube API quota or upload limit was reached. Try again later.", status_code=status, reason=reason, retryable=True)
    if lowered in {"accessnotconfigured", "service_disabled"} or "has not been used in project" in message or "is disabled" in message:
        return YouTubeApiError("api_disabled", message or f"The Google API needed for {context} is not enabled for this OAuth project.", status_code=status, reason=reason)
    scope_message = "insufficient" in message.casefold() and "scope" in message.casefold()
    if lowered in {"insufficientpermissions", "access_token_scope_insufficient", "insufficient_scope", "permission_denied"} or scope_message:
        return YouTubeApiError("insufficient_scope", "ClipForge is missing a YouTube permission. Reconnect and grant all requested permissions.", status_code=status, reason=reason)
    if status == 404 or lowered in {"videonotfound", "notfound"}:
        return YouTubeApiError("not_found", message or "YouTube could not find this item.", status_code=status, reason=reason)
    if status == 403:
        return YouTubeApiError("forbidden", message or f"YouTube refused the {context} request.", status_code=status, reason=reason)
    if status == 400:
        return YouTubeApiError("bad_request", message or f"YouTube rejected the {context} request.", status_code=status, reason=reason)
    if status in {408, 429} or status >= 500:
        return YouTubeApiError("provider_error", message or "YouTube is temporarily unavailable. Try again.", status_code=status, reason=reason, retryable=True)
    return YouTubeApiError("provider_error", message or f"YouTube could not complete the {context} request.", status_code=status, reason=reason)


def _network_error(exc: httpx.HTTPError, context: str) -> YouTubeApiError:
    if isinstance(exc, httpx.TimeoutException):
        return YouTubeApiError("network_timeout", f"The YouTube {context} request timed out.", retryable=True)
    return YouTubeApiError("network_error", f"YouTube could not be reached for {context}.", retryable=True)


class GoogleYouTubeProvider:
    """Real implementation using httpx against Google's official endpoints."""

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 30.0, upload_timeout: float = 180.0) -> None:
        self._client = client
        self._timeout = timeout
        self._upload_timeout = upload_timeout

    def _send(self, method: str, url: str, *, context: str, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        try:
            if self._client is not None:
                return self._client.request(method, url, timeout=timeout or self._timeout, **kwargs)
            with httpx.Client(timeout=timeout or self._timeout, follow_redirects=False) as client:
                return client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise _network_error(exc, context) from None

    @staticmethod
    def _grant(payload: dict[str, Any]) -> TokenGrant:
        token = str(payload.get("access_token") or "")
        if not token:
            raise YouTubeApiError("provider_error", "Google returned no access token.")
        return TokenGrant(
            access_token=token,
            expires_in=float(payload.get("expires_in") or 3600),
            refresh_token=payload.get("refresh_token") or None,
            scopes=tuple(str(payload.get("scope") or "").split()),
        )

    def exchange_code(self, client: OAuthClient, code: str, code_verifier: str) -> TokenGrant:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
        }
        if client.client_secret:
            data["client_secret"] = client.client_secret
        response = self._send("POST", TOKEN_URL, context="sign-in", data=data)
        if response.status_code != 200:
            raise _google_error(response, context="sign-in")
        return self._grant(response.json())

    def refresh_access_token(self, client: OAuthClient, refresh_token: str) -> TokenGrant:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client.client_id}
        if client.client_secret:
            data["client_secret"] = client.client_secret
        response = self._send("POST", TOKEN_URL, context="token refresh", data=data)
        if response.status_code != 200:
            raise _google_error(response, context="token refresh")
        return self._grant(response.json())

    def revoke(self, token: str) -> None:
        # Token in the form body, never in the URL (URLs end up in logs).
        response = self._send("POST", REVOKE_URL, context="disconnect", data={"token": token})
        if response.status_code not in {200, 400}:  # 400: already invalid
            raise _google_error(response, context="disconnect")

    @staticmethod
    def _auth(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def get_my_channel(self, access_token: str) -> ChannelIdentity:
        response = self._send(
            "GET", f"{DATA_API}/channels", context="channel lookup",
            params={"part": "snippet", "mine": "true"}, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="channel lookup")
        items = response.json().get("items") or []
        if not items:
            raise YouTubeApiError("no_channel", "This Google account has no YouTube channel.")
        item = items[0]
        return ChannelIdentity(channel_id=str(item.get("id")), title=str((item.get("snippet") or {}).get("title") or ""))

    def get_uploads_playlist_id(self, access_token: str) -> str:
        response = self._send(
            "GET", f"{DATA_API}/channels", context="channel schedule",
            params={"part": "contentDetails", "mine": "true"}, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="channel schedule")
        items = response.json().get("items") or []
        uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") if items else None
        if not uploads:
            raise YouTubeApiError("no_channel", "YouTube returned no uploads playlist for this channel.")
        return str(uploads)

    def list_playlist_items(self, access_token: str, playlist_id: str, page_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": str(PAGE_SIZE)}
        if page_token:
            params["pageToken"] = page_token
        response = self._send(
            "GET", f"{DATA_API}/playlistItems", context="channel schedule", params=params, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="channel schedule")
        payload = response.json()
        return list(payload.get("items") or []), (payload.get("nextPageToken") or None)

    def start_resumable_upload(self, access_token: str, body: dict[str, Any], size: int, content_type: str, *, notify_subscribers: bool = True) -> str:
        headers = {
            **self._auth(access_token),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": content_type,
        }
        response = self._send(
            "POST", UPLOAD_API, context="upload",
            params={
                "uploadType": "resumable",
                "part": ",".join(body.keys()),
                # videos.insert query parameter (default true on YouTube's side).
                "notifySubscribers": "true" if notify_subscribers else "false",
            },
            headers=headers, json=body,
        )
        if response.status_code != 200 or not response.headers.get("Location"):
            raise _google_error(response, context="upload")
        return str(response.headers["Location"])

    @staticmethod
    def _progress(response: httpx.Response) -> UploadProgress:
        if response.status_code in {200, 201}:
            return UploadProgress(complete=True, offset=0, video=response.json())
        if response.status_code == 308:
            match = re.match(r"bytes=0-(\d+)", response.headers.get("Range", ""))
            return UploadProgress(complete=False, offset=int(match.group(1)) + 1 if match else 0)
        if response.status_code in {404, 410}:
            raise YouTubeApiError("upload_session_expired", "The YouTube upload session expired.", status_code=response.status_code)
        raise _google_error(response, context="upload")

    def query_upload(self, access_token: str, session_uri: str, size: int) -> UploadProgress:
        response = self._send(
            "PUT", session_uri, context="upload",
            headers={**self._auth(access_token), "Content-Range": f"bytes */{size}", "Content-Length": "0"},
        )
        return self._progress(response)

    def upload_chunk(self, access_token: str, session_uri: str, data: bytes, offset: int, size: int) -> UploadProgress:
        end = offset + len(data) - 1
        response = self._send(
            "PUT", session_uri, context="upload", timeout=self._upload_timeout,
            headers={**self._auth(access_token), "Content-Range": f"bytes {offset}-{end}/{size}", "Content-Length": str(len(data))},
            content=data,
        )
        return self._progress(response)

    def list_videos(self, access_token: str, video_ids: list[str], parts: str) -> list[dict[str, Any]]:
        response = self._send(
            "GET", f"{DATA_API}/videos", context="video status",
            params={"part": parts, "id": ",".join(video_ids)}, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="video status")
        return list(response.json().get("items") or [])

    def update_video(self, access_token: str, body: dict[str, Any], parts: str) -> dict[str, Any]:
        response = self._send(
            "PUT", f"{DATA_API}/videos", context="scheduling",
            params={"part": parts}, headers={**self._auth(access_token), "Content-Type": "application/json"}, json=body,
        )
        if response.status_code != 200:
            raise _google_error(response, context="scheduling")
        return response.json()

    def analytics_report(self, access_token: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._send(
            "GET", ANALYTICS_API, context="analytics", params=params, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="analytics")
        return response.json()

    def set_thumbnail(self, access_token: str, video_id: str, data: bytes, content_type: str) -> dict[str, Any]:
        response = self._send(
            "POST", THUMBNAIL_API, context="thumbnail", timeout=self._upload_timeout,
            params={"videoId": video_id, "uploadType": "media"},
            headers={**self._auth(access_token), "Content-Type": content_type}, content=data,
        )
        if response.status_code != 200:
            error = _google_error(response, context="thumbnail")
            if error.code == "forbidden":
                error.code = "thumbnail_not_allowed"
                error.message = (
                    "YouTube does not allow custom thumbnails for this channel yet "
                    "(verify the channel in YouTube Studio to enable them)."
                )
            raise error
        return response.json()

    def list_categories(self, access_token: str, region_code: str, language: str) -> list[dict[str, Any]]:
        response = self._send(
            "GET", f"{DATA_API}/videoCategories", context="categories",
            params={"part": "snippet", "regionCode": region_code, "hl": language}, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="categories")
        return list(response.json().get("items") or [])

    def list_popular_videos(self, access_token: str, region_code: str, category_id: str | None, max_results: int) -> list[dict[str, Any]]:
        params = {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": region_code,
            "maxResults": str(max(1, min(PAGE_SIZE, max_results))),
        }
        if category_id:
            params["videoCategoryId"] = category_id
        response = self._send(
            "GET", f"{DATA_API}/videos", context="topic discovery", params=params, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="topic discovery")
        return list(response.json().get("items") or [])

    def list_channels(self, access_token: str, channel_ids: list[str], parts: str) -> list[dict[str, Any]]:
        response = self._send(
            "GET", f"{DATA_API}/channels", context="topic discovery",
            params={"part": parts, "id": ",".join(channel_ids[:PAGE_SIZE]), "maxResults": str(PAGE_SIZE)},
            headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="topic discovery")
        return list(response.json().get("items") or [])

    def search_videos(self, access_token: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self._send(
            "GET", f"{DATA_API}/search", context="topic discovery",
            params={"part": "snippet", "type": "video", **params}, headers=self._auth(access_token),
        )
        if response.status_code != 200:
            raise _google_error(response, context="topic discovery")
        return list(response.json().get("items") or [])


def read_chunks(handle: BinaryIO, offset: int, chunk_size: int = UPLOAD_CHUNK_BYTES):
    handle.seek(offset)
    while True:
        data = handle.read(chunk_size)
        if not data:
            return
        yield offset, data
        offset += len(data)
