"""Free, reusable-license music metadata providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

_AUDIO_SUFFIXES = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
)
_AUDIO_MIME_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


class MusicProviderError(RuntimeError):
    """A compact, safe provider failure classification for persisted diagnostics."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MusicTrend:
    """Normalized 0–1 signals supplied by a trusted source, never inferred."""

    popularity_score: float | None = None
    trend_score: float | None = None
    trend_velocity: float | None = None
    usage_count: int | None = None
    trend_source: str | None = None
    trend_updated_at: str | None = None
    known_track: bool | None = None
    trend_confidence: float | None = None


@dataclass(frozen=True)
class PlatformTrackRecommendation:
    title: str
    artist: str
    platform_id: str
    trend: MusicTrend
    fit_score: float | None = None
    mode: str = "PLATFORM_TREND_TRACK"


class TrendSignalProvider(Protocol):
    def lookup(self, source: str, track_id: str) -> MusicTrend | None: ...

    def recommendations(self, style_terms: tuple[str, ...]) -> list[PlatformTrackRecommendation]: ...


@dataclass(frozen=True)
class MusicCandidate:
    provider: str
    provider_id: str
    title: str
    download_url: str
    source_url: str
    license: str
    attribution: str | None
    mood: str
    energy: str
    tags: tuple[str, ...]
    duration_seconds: float | None = None
    content_type: str | None = None
    description: str | None = None
    trend: MusicTrend | None = None


class MusicProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[MusicCandidate]: ...

    def download(self, candidate: MusicCandidate, destination: Any) -> Any: ...


def reusable_license(value: object) -> bool:
    text = " ".join(str(value or "").casefold().split())
    if not text or any(token in text for token in ("all rights", "noncommercial", "non-commercial", "-nc", " nc", " no derivatives", "-nd", " nd")):
        return False
    return bool(
        re.fullmatch(r"(?:cc0|public domain|cc[ -]by)(?:[ -]\d\.\d)?", text)
        or re.fullmatch(r"https?://creativecommons\.org/(?:licenses/by/\d\.\d|publicdomain/(?:zero/1\.0|mark/1\.0))/?", text)
    )


def music_like(title: object, description: object, tags: tuple[str, ...] = ()) -> bool:
    text = " ".join((str(title or ""), str(description or ""), *tags)).casefold()
    if re.search(r"\b(soundscape\w*|ambience|environmental sound|field recording|noise|drone\w*|sound effect\w*|sfx|podcast\w*|speech|lecture\w*|audiobook\w*|isolated tones?|pure atmosphere|tone study|experimental study)\b", text):
        return False
    if re.search(r"\b(?:fractal|tone|experimental) study\b|\b(?:deep relaxation|sleep ambience|meditation ambience)\b", text):
        return False
    if any(term in text for term in ("podcast", "audiobook", "lecture", "spoken word", "voice sample", "speech", "sound effect")):
        return False
    return any(
        term in text
        for term in (
            "music",
            "instrumental",
            "song",
            "underscore",
            "orchestra",
            "piano",
            "guitar",
            "synth",
            "beat",
            "composition",
            "soundtrack",
            "melody",
            "score",
            "track",
        )
    )


def audio_suffix(url: object, content_type: object = None) -> str | None:
    suffix = Path(urlsplit(str(url or "")).path).suffix.casefold()
    if suffix in _AUDIO_SUFFIXES:
        return suffix
    mime = str(content_type or "").split(";", 1)[0].strip().casefold()
    return _AUDIO_MIME_SUFFIXES.get(mime)


def is_supported_audio(url: object, content_type: object = None) -> bool:
    return audio_suffix(url, content_type) is not None


def provider_error_code(error: Exception) -> str:
    if isinstance(error, MusicProviderError):
        return error.code
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_status_{error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.TransportError):
        text = str(error).casefold()
        if "ssl" in text or "tls" in text or "certificate" in text:
            return "tls_error"
        if "name or service" in text or "nodename" in text or "dns" in text:
            return "dns_error"
        return "connection_error"
    if isinstance(error, ValueError):
        return "invalid_json"
    if isinstance(error, (AttributeError, KeyError, TypeError)):
        return "response_schema_mismatch"
    return "unexpected_error"


class _HttpMusicProvider:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(10, connect=4),
            follow_redirects=True,
            headers={"User-Agent": "ClipForge/0.2 (local video editor)"},
        )

    def download(self, candidate: MusicCandidate, destination: Any) -> Any:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.client.stream("GET", candidate.download_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not is_supported_audio(candidate.download_url, content_type):
                raise ValueError("Provider did not return audio")
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        if not destination.is_file() or destination.stat().st_size < 1024:
            destination.unlink(missing_ok=True)
            raise ValueError("Downloaded audio is invalid")
        return destination


class InternetArchiveMusicProvider(_HttpMusicProvider):
    provider_name = "internet_archive"
    SEARCH = "https://archive.org/advancedsearch.php"
    METADATA = "https://archive.org/metadata"

    def search(self, query: str, *, limit: int) -> list[MusicCandidate]:
        try:
            # Archive's advanced search treats adjacent terms as a restrictive
            # intersection. Discovery queries are music labels, so let any
            # label match while retaining the audio collection constraint.
            terms = re.findall(r"[\w-]+", query.casefold())[:4]
            music_query = " OR ".join(f'"{term}"' for term in terms) or '"music"'
            response = self.client.get(self.SEARCH, params={"q": f"mediatype:audio AND ({music_query})", "fl[]": ["identifier", "title", "licenseurl", "rights", "subject", "description"], "rows": min(limit, 12), "output": "json"})
            response.raise_for_status()
            payload = response.json()
            docs = payload.get("response", {}).get("docs", [])
            if not isinstance(docs, list):
                raise MusicProviderError("response_schema_mismatch")
        except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError) as exc:
            raise MusicProviderError(provider_error_code(exc)) from exc
        candidates: list[MusicCandidate] = []
        self.last_search_counts = {
            "result_count": len(docs), "license_valid_count": 0,
            "real_song_count": 0, "audio_valid_count": 0,
            "license_rejected_count": 0, "not_song_rejected_count": 0,
            "audio_rejected_count": 0,
        }
        for item in docs:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("identifier") or "")
            license_name = str(item.get("licenseurl") or item.get("rights") or "")
            title = str(item.get("title") or "")
            description = str(item.get("description") or "")
            tags = tuple(str(item.get("subject") or "").split(";"))
            if not identifier or not reusable_license(license_name):
                self.last_search_counts["license_rejected_count"] += 1
                continue
            self.last_search_counts["license_valid_count"] += 1
            if not music_like(title, description, tags):
                self.last_search_counts["not_song_rejected_count"] += 1
                continue
            self.last_search_counts["real_song_count"] += 1
            try:
                metadata_response = self.client.get(f"{self.METADATA}/{identifier}")
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
                files = metadata.get("files") or []
            except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError) as exc:
                raise MusicProviderError(
                    f"metadata_{provider_error_code(exc)}"
                ) from exc
            if not isinstance(files, list):
                raise MusicProviderError("metadata_response_schema_mismatch")
            audio = next(
                (
                    file
                    for file in files
                    if isinstance(file, dict)
                    and is_supported_audio(
                        file.get("name"), file.get("mime_type") or file.get("format")
                    )
                ),
                None,
            )
            if not isinstance(audio, dict):
                self.last_search_counts["audio_rejected_count"] += 1
                continue
            filename = str(audio.get("name") or "")
            self.last_search_counts["audio_valid_count"] += 1
            candidates.append(MusicCandidate("internet_archive", identifier, title, f"https://archive.org/download/{identifier}/{filename}", f"https://archive.org/details/{identifier}", license_name, None, "documentary", "low", tags, content_type=str(audio.get("mime_type") or audio.get("format") or "") or None, description=description or None))
        return candidates


class WikimediaMusicProvider(_HttpMusicProvider):
    provider_name = "wikimedia"
    API = "https://commons.wikimedia.org/w/api.php"

    def search(self, query: str, *, limit: int) -> list[MusicCandidate]:
        try:
            response = self.client.get(self.API, params={"action": "query", "generator": "search", "gsrsearch": f"filetype:audio {query}", "gsrnamespace": 6, "gsrlimit": min(limit, 12), "prop": "imageinfo", "iiprop": "url|mime|extmetadata", "format": "json"})
            response.raise_for_status()
            payload = response.json()
            pages = payload.get("query", {}).get("pages", {})
            if not isinstance(pages, dict):
                raise MusicProviderError("response_schema_mismatch")
        except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError) as exc:
            raise MusicProviderError(provider_error_code(exc)) from exc
        candidates: list[MusicCandidate] = []
        self.last_search_counts = {
            "result_count": len(pages), "license_valid_count": 0,
            "real_song_count": 0, "audio_valid_count": 0,
            "license_rejected_count": 0, "not_song_rejected_count": 0,
            "audio_rejected_count": 0,
        }
        for page in pages.values():
            if not isinstance(page, dict):
                continue
            info = next(iter(page.get("imageinfo") or []), {})
            meta = info.get("extmetadata") or {}
            license_name = str((meta.get("LicenseShortName") or {}).get("value") or "")
            title = str(page.get("title") or "")
            description = str((meta.get("ImageDescription") or {}).get("value") or "")
            url = str(info.get("url") or "")
            content_type = str(info.get("mime") or "") or None
            if not reusable_license(license_name):
                self.last_search_counts["license_rejected_count"] += 1
                continue
            self.last_search_counts["license_valid_count"] += 1
            if not url or not is_supported_audio(url, content_type):
                self.last_search_counts["audio_rejected_count"] += 1
                continue
            self.last_search_counts["audio_valid_count"] += 1
            if not music_like(title, description):
                self.last_search_counts["not_song_rejected_count"] += 1
                continue
            self.last_search_counts["real_song_count"] += 1
            candidates.append(MusicCandidate("wikimedia", str(page.get("pageid") or title), title, url, str(info.get("descriptionurl") or "https://commons.wikimedia.org/"), license_name, str((meta.get("Artist") or {}).get("value") or "").strip() or None, "documentary", "low", (), content_type=content_type, description=description or None))
        return candidates
