"""Optional free Pixabay provider (photos and videos) for the real-media pipeline.

Enabled only when ``PIXABAY_API_KEY`` is configured.  The key is sent as the
API's query parameter, so request errors are re-raised without their original
exception (whose URL would contain it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .media import MAX_PHOTO_BYTES, MAX_VIDEO_BYTES, MediaCandidate, MediaProviderError

PIXABAY_API = "https://pixabay.com/api/"
PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_LICENSE = "Pixabay Content License"


def _tags(value: object) -> tuple[str, ...]:
    return tuple(tag.strip() for tag in str(value or "").split(",") if tag.strip())


def parse_pixabay_photos(payload: dict[str, Any], *, query: str, portrait: bool) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for position, item in enumerate(payload.get("hits") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        download_url = str(item.get("largeImageURL") or item.get("webformatURL") or "")
        width, height = int(item.get("imageWidth") or 0), int(item.get("imageHeight") or 0)
        if not download_url or not width or not height:
            continue
        tags = _tags(item.get("tags"))
        candidates.append(
            MediaCandidate(
                provider_id=str(item["id"]),
                kind="photo",
                download_url=download_url,
                source_url=str(item.get("pageURL") or "https://pixabay.com/"),
                creator=str(item.get("user") or "Pixabay contributor"),
                creator_url=f"https://pixabay.com/users/{item['user_id']}/" if item.get("user_id") else None,
                width=width,
                height=height,
                duration=None,
                query=query,
                rank=65 - position * 2 + (25 if (height >= width) == portrait else 0),
                provider="pixabay",
                title="",
                description=", ".join(tags),
                tags=tags,
                preview_url=str(item.get("webformatURL") or item.get("previewURL") or download_url),
            )
        )
    return sorted(candidates, key=lambda item: item.rank, reverse=True)


def parse_pixabay_videos(
    payload: dict[str, Any], *, query: str, portrait: bool, scene_duration: float
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for position, item in enumerate(payload.get("hits") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        files = item.get("videos") if isinstance(item.get("videos"), dict) else {}
        usable = [
            value for value in (files.get(key) for key in ("large", "medium", "small", "tiny"))
            if isinstance(value, dict) and value.get("url") and value.get("width") and value.get("height")
        ]
        if not usable:
            continue
        chosen = next((value for value in usable if int(value["width"]) * int(value["height"]) <= 1920 * 1920), usable[-1])
        smallest = usable[-1]
        width, height = int(chosen["width"]), int(chosen["height"])
        duration = float(item.get("duration") or 0)
        tags = _tags(item.get("tags"))
        too_short = 35 if duration < max(2.0, scene_duration * 0.6) else 0
        candidates.append(
            MediaCandidate(
                provider_id=str(item["id"]),
                kind="video",
                download_url=str(chosen["url"]),
                source_url=str(item.get("pageURL") or "https://pixabay.com/videos/"),
                creator=str(item.get("user") or "Pixabay contributor"),
                creator_url=f"https://pixabay.com/users/{item['user_id']}/" if item.get("user_id") else None,
                width=width,
                height=height,
                duration=duration,
                query=query,
                rank=90 - position * 2 + (30 if (height >= width) == portrait else 0) - too_short,
                provider="pixabay",
                title="",
                description=", ".join(tags),
                tags=tags,
                preview_url=str(chosen.get("thumbnail") or ""),
                verification_url=str(smallest.get("url") or ""),
            )
        )
    return sorted(candidates, key=lambda item: item.rank, reverse=True)


class PixabayMediaClient:
    """Pixabay search/download with credentials kept out of reprs and errors."""

    __slots__ = ("_api_key", "_client")
    provider = "pixabay"
    license = PIXABAY_LICENSE

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=True)

    def __repr__(self) -> str:
        return "PixabayMediaClient(api_key=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.get(url, params={**params, "key": self._api_key})
        except httpx.HTTPError:
            raise MediaProviderError("network_error", "Pixabay search is temporarily unavailable.") from None
        if response.status_code in {400, 401, 403}:
            raise MediaProviderError("invalid_credentials", "Pixabay rejected the configured credentials.")
        if response.status_code == 429:
            raise MediaProviderError("rate_limited", "Pixabay is currently rate limited.")
        if response.status_code >= 300:
            raise MediaProviderError("provider_error", "Pixabay search is temporarily unavailable.")
        try:
            payload = response.json()
        except ValueError:
            raise MediaProviderError("provider_error", "Pixabay returned an unreadable response.") from None
        return payload if isinstance(payload, dict) else {}

    def search_photos(self, query: str, *, portrait: bool) -> list[MediaCandidate]:
        payload = self._get_json(PIXABAY_API, {
            "q": query[:100], "image_type": "photo", "orientation": "vertical" if portrait else "horizontal",
            "per_page": 20, "safesearch": "true",
        })
        return parse_pixabay_photos(payload, query=query, portrait=portrait)

    def search_videos(self, query: str, *, portrait: bool, scene_duration: float) -> list[MediaCandidate]:
        payload = self._get_json(PIXABAY_VIDEO_API, {"q": query[:100], "per_page": 20, "safesearch": "true"})
        return parse_pixabay_videos(payload, query=query, portrait=portrait, scene_duration=scene_duration)

    def download(self, candidate: MediaCandidate, destination: Path) -> Path:
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        parsed = urlparse(candidate.download_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise MediaProviderError("provider_error", "Pixabay returned an unusable media URL.")
        limit = MAX_VIDEO_BYTES if candidate.kind == "video" else MAX_PHOTO_BYTES
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._client.stream("GET", candidate.download_url) as response:
                response.raise_for_status()
                written = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        written += len(chunk)
                        if written > limit:
                            raise MediaProviderError("provider_error", "The Pixabay asset exceeded the download limit.")
                        handle.write(chunk)
            if written == 0:
                raise MediaProviderError("provider_error", "Pixabay returned an empty media file.")
            partial.replace(destination)
            return destination
        except MediaProviderError:
            partial.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError):
            partial.unlink(missing_ok=True)
            raise MediaProviderError("network_error", "Pixabay media could not be downloaded right now.") from None
