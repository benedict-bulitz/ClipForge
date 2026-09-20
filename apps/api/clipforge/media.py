from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .progress import ProgressCallback, report_progress
from .visual_verifier import (
    VISUAL_THRESHOLD,
    get_visual_verifier,
    global_subject_text,
    visual_intent_text,
)

PEXELS_API = "https://api.pexels.com/v1"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
MAX_VIDEO_BYTES = 150 * 1024 * 1024
MAX_PHOTO_BYTES = 30 * 1024 * 1024


class MediaProviderError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class MediaCandidate:
    provider_id: str
    kind: str
    download_url: str
    source_url: str
    creator: str
    creator_url: str | None
    width: int
    height: int
    duration: float | None
    query: str
    rank: float
    provider: str = "pexels"
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    preview_url: str = ""
    verification_url: str = ""

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.kind}:{self.provider_id}"


class PexelsMediaClient:
    """Small Pexels client that keeps credentials out of representations and errors."""

    __slots__ = ("_api_key", "_client")

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
        )

    def __repr__(self) -> str:
        return "PexelsMediaClient(api_key=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def search_videos(
        self, query: str, *, portrait: bool, scene_duration: float
    ) -> list[MediaCandidate]:
        payload = self._get_json(
            f"{PEXELS_API}/videos/search",
            {
                "query": query,
                "orientation": "portrait" if portrait else "landscape",
                "size": "medium",
                "per_page": 18,
            },
        )
        return parse_video_results(
            payload, query=query, portrait=portrait, scene_duration=scene_duration
        )

    def search_photos(self, query: str, *, portrait: bool) -> list[MediaCandidate]:
        payload = self._get_json(
            f"{PEXELS_API}/search",
            {
                "query": query,
                "orientation": "portrait" if portrait else "landscape",
                "size": "large",
                "per_page": 15,
            },
        )
        return parse_photo_results(payload, query=query, portrait=portrait)

    def download(self, candidate: MediaCandidate, destination: Path) -> Path:
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        parsed = urlparse(candidate.download_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise MediaProviderError("provider_error", "Pexels returned an unusable media URL.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        limit = MAX_VIDEO_BYTES if candidate.kind == "video" else MAX_PHOTO_BYTES
        for attempt in range(2):
            try:
                with self._client.stream("GET", candidate.download_url) as response:
                    if response.status_code >= 500 and attempt == 0:
                        continue
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > limit:
                        raise MediaProviderError(
                            "provider_error", "The selected Pexels asset is too large to cache safely."
                        )
                    written = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 256):
                            written += len(chunk)
                            if written > limit:
                                raise MediaProviderError(
                                    "provider_error",
                                    "The selected Pexels asset exceeded the download limit.",
                                )
                            handle.write(chunk)
                partial.replace(destination)
                return destination
            except MediaProviderError:
                partial.unlink(missing_ok=True)
                raise
            except (httpx.HTTPError, OSError) as exc:
                partial.unlink(missing_ok=True)
                if attempt == 1:
                    raise MediaProviderError(
                        "network_error", "Pexels media could not be downloaded right now."
                    ) from exc
        raise MediaProviderError("network_error", "Pexels media could not be downloaded right now.")

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": self._api_key},
                )
                if response.status_code == 401:
                    raise MediaProviderError(
                        "invalid_credentials", "Pexels rejected the configured credentials."
                    )
                if response.status_code == 429:
                    raise MediaProviderError("rate_limited", "Pexels is currently rate limited.")
                if response.status_code >= 500 and attempt == 0:
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("Unexpected Pexels response")
                return payload
            except MediaProviderError:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                if attempt == 1:
                    category = "network_error" if isinstance(exc, httpx.RequestError) else "provider_error"
                    raise MediaProviderError(
                        category, "Pexels search is temporarily unavailable."
                    ) from exc
        raise MediaProviderError("provider_error", "Pexels search is temporarily unavailable.")


class WikimediaMediaClient:
    """Free still-image fallback using Wikimedia Commons' public API."""

    __slots__ = ("_client",)

    def __init__(self, *, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": "ClipForge/0.2 (local video editor)"},
        )

    def close(self) -> None:
        self._client.close()

    def search_photos(self, query: str, *, portrait: bool) -> list[MediaCandidate]:
        try:
            response = self._client.get(
                WIKIMEDIA_API,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": f"filetype:bitmap {query}",
                    "gsrnamespace": 6,
                    "gsrlimit": 10,
                    "prop": "imageinfo",
                    "iiprop": "url|size|extmetadata",
                    "iiurlwidth": 1600,
                    "format": "json",
                },
            )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", {})
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MediaProviderError(
                "wikimedia_unavailable", "Wikimedia media search is temporarily unavailable."
            ) from exc
        candidates: list[MediaCandidate] = []
        for position, page in enumerate(pages.values() if isinstance(pages, dict) else []):
            info = next(iter(page.get("imageinfo") or []), None)
            if not isinstance(info, dict):
                continue
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            download_url = info.get("thumburl") or info.get("url")
            if not download_url or width < 640 or height < 640:
                continue
            metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
            artist = metadata.get("Artist") if isinstance(metadata.get("Artist"), dict) else {}
            creator = _plain_metadata(str(artist.get("value") or "Wikimedia contributor"))
            orientation_bonus = 20 if (height >= width) == portrait else 0
            candidates.append(
                MediaCandidate(
                    provider_id=str(page.get("pageid") or page.get("title") or position),
                    kind="photo",
                    download_url=str(download_url),
                    source_url=str(info.get("descriptionurl") or "https://commons.wikimedia.org/"),
                    creator=creator or "Wikimedia contributor",
                    creator_url=None,
                    width=width,
                    height=height,
                    duration=None,
                    query=query,
                    rank=60 - position * 2 + orientation_bonus,
                    provider="wikimedia",
                    title=_plain_metadata(str(page.get("title") or "")),
                    description=_plain_metadata(str((metadata.get("ImageDescription") or {}).get("value") or "")) if isinstance(metadata.get("ImageDescription"), dict) else "",
                    preview_url=str(download_url),
                )
            )
        return sorted(candidates, key=lambda item: item.rank, reverse=True)

    def download(self, candidate: MediaCandidate, destination: Path) -> Path:
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        parsed = urlparse(candidate.download_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise MediaProviderError(
                "provider_error", "Wikimedia returned an unusable media URL."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._client.stream("GET", candidate.download_url) as response:
                response.raise_for_status()
                written = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        written += len(chunk)
                        if written > MAX_PHOTO_BYTES:
                            raise MediaProviderError(
                                "provider_error", "The Wikimedia asset exceeded the download limit."
                            )
                        handle.write(chunk)
            if written == 0:
                raise MediaProviderError("provider_error", "Wikimedia returned an empty media file.")
            partial.replace(destination)
            return destination
        except MediaProviderError:
            partial.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as exc:
            partial.unlink(missing_ok=True)
            raise MediaProviderError(
                "wikimedia_unavailable", "Wikimedia media could not be downloaded right now."
            ) from exc


def parse_video_results(
    payload: dict[str, Any], *, query: str, portrait: bool, scene_duration: float
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for position, item in enumerate(payload.get("videos") or []):
        if not isinstance(item, dict):
            continue
        files = [
            value
            for value in item.get("video_files") or []
            if isinstance(value, dict)
            and value.get("file_type") == "video/mp4"
            and value.get("link")
            and value.get("width")
            and value.get("height")
        ]
        if not files:
            continue
        target_ratio = 9 / 16 if portrait else 16 / 9
        media_file = max(
            files,
            key=lambda value: (
                -abs(float(value["width"]) / float(value["height"]) - target_ratio),
                min(int(value["width"]) * int(value["height"]), 1920 * 1920),
            ),
        )
        verification_file = min(files, key=lambda value: int(value["width"]) * int(value["height"]))
        width = int(media_file["width"])
        height = int(media_file["height"])
        duration = float(item.get("duration") or 0)
        orientation_bonus = 30 if (height >= width) == portrait else 0
        resolution = min(width * height / (1080 * 1920), 1.5) * 16
        coverage = min(duration / max(1.0, scene_duration), 1.0) * 22
        too_short = 35 if duration < max(2.0, scene_duration * 0.6) else 0
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        candidates.append(
            MediaCandidate(
                provider_id=str(item.get("id")),
                kind="video",
                download_url=str(media_file["link"]),
                source_url=str(item.get("url") or "https://www.pexels.com/videos/"),
                creator=str(user.get("name") or "Pexels contributor"),
                creator_url=str(user.get("url")) if user.get("url") else None,
                width=width,
                height=height,
                duration=duration,
                query=query,
                rank=100 - position * 2 + orientation_bonus + resolution + coverage - too_short,
                title=str(item.get("title") or ""),
                description=str(item.get("description") or item.get("alt") or ""),
                tags=tuple(str(tag) for tag in (item.get("tags") or []) if tag),
                preview_url=str(item.get("image") or ""),
                verification_url=str(verification_file.get("link") or ""),
            )
        )
    return sorted(candidates, key=lambda item: item.rank, reverse=True)


def parse_photo_results(
    payload: dict[str, Any], *, query: str, portrait: bool
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for position, item in enumerate(payload.get("photos") or []):
        if not isinstance(item, dict):
            continue
        sources = item.get("src") if isinstance(item.get("src"), dict) else {}
        download_url = sources.get("portrait" if portrait else "landscape") or sources.get(
            "large2x"
        ) or sources.get("original")
        if not download_url:
            continue
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        orientation_bonus = 25 if (height >= width) == portrait else 0
        candidates.append(
            MediaCandidate(
                provider_id=str(item.get("id")),
                kind="photo",
                download_url=str(download_url),
                source_url=str(item.get("url") or "https://www.pexels.com/"),
                creator=str(item.get("photographer") or "Pexels contributor"),
                creator_url=str(item.get("photographer_url")) if item.get("photographer_url") else None,
                width=width,
                height=height,
                duration=None,
                query=query,
                rank=70 - position * 2 + orientation_bonus,
                title=str(item.get("title") or item.get("alt") or ""),
                description=str(item.get("alt") or ""),
                tags=tuple(str(tag) for tag in (item.get("tags") or []) if tag),
                preview_url=str(sources.get("tiny") or sources.get("small") or download_url),
            )
        )
    return sorted(candidates, key=lambda item: item.rank, reverse=True)


def derive_search_queries(scene: dict[str, Any], state: dict[str, Any]) -> list[str]:
    stop = {
        "about", "after", "also", "and", "because", "before", "could", "from", "have",
        "into", "more", "only", "over", "that", "their", "there", "these", "this", "through",
        "video", "visual", "what", "when", "where", "which", "with", "would", "your", "illustrate",
        "aber", "auch", "dass", "dies", "eine", "einer", "eines", "für", "fuer", "haben", "hier",
        "mehr", "nicht", "oder", "über", "ueber", "sich", "sind", "sein", "wenn", "wird", "zeigen", "beim", "zuerst", "erst", "danach", "dann", "der", "die", "das",
        "answer", "cause", "context", "detail", "hook", "intro", "outro", "payoff",
        "setup", "support", "turn",
        "why", "warum",
    }
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    # User/editor changes to visual_goal supersede stale structured intent.
    intent_goal = str(visual_intent.get("visual_goal") or "").strip()
    intent_current = not scene.get("visual_goal") or scene.get("visual_goal") == intent_goal
    supplied = [
        query
        for value in ((visual_intent.get("media_queries") or []) if intent_current else [])
        if (query := _semantic_query(str(value), stop, limit=7))
    ] + [
        query
        for value in scene.get("search_queries") or []
        if (query := _semantic_query(str(value), stop, limit=6))
    ]
    scene_text = " ".join(
        value
        for value in (
            str(scene.get("edit_instruction") or ""),
            str((visual_intent.get("visual_goal") if intent_current else None) or scene.get("visual_goal") or ""),
            str(scene.get("narration") or ""),
        )
        if value
    )
    primary = _semantic_query(scene_text, stop, limit=6)
    broader = _semantic_query(str(state.get("intent", {}).get("topic") or ""), stop, limit=4)
    subject_terms = set(broader.split())
    contextual = []
    for query in [*supplied, primary]:
        query_terms = set(query.split())
        if subject_terms and not subject_terms.issubset(query_terms):
            query = " ".join(dict.fromkeys([*broader.split(), *query.split()]))
        contextual.append(_semantic_query(query, stop, limit=6))
    queries = [query for query in [*contextual, broader] if query]
    return list(dict.fromkeys(_provider_query(query) for query in queries))[:3]


_PROVIDER_TERMS = {
    "hausbau": "house construction", "hausbaues": "house construction", "haus": "house", "häuser": "houses",
    "fundament": "foundation", "fundaments": "foundation", "gießen": "pouring", "gegossen": "poured",
    "bauen": "building", "bau": "construction", "wände": "walls", "wand": "wall", "dach": "roof",
    "beton": "concrete", "arbeiter": "workers", "arbeitern": "workers", "erde": "earth", "materie": "matter",
    "kapazität": "capacity", "sparen": "saving", "geld": "money",
}


def _provider_query(query: str) -> str:
    words = query.split()
    translated = [_PROVIDER_TERMS.get(word.casefold(), word) for word in words]
    return " ".join(translated)


def _semantic_query(text: str, stop: set[str], *, limit: int) -> str:
    text = re.sub(
        r"(?i)^\s*(?:illustrate|show|visuali[sz]e)\s+"
        r"(?:answer|cause|context|detail|hook|intro|outro|payoff|setup|support|turn)\s*[:—-]?",
        "",
        text,
    )
    words = [
        word
        for word in re.findall(r"[\wäöüß-]+", text.casefold(), flags=re.UNICODE)
        if len(word) > 2 and word not in stop and not word.isdigit()
    ]
    return " ".join(list(dict.fromkeys(words))[:limit])


def _plain_metadata(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()


_RELEVANCE_STOP = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with", "from", "that", "this",
    "ein", "eine", "einer", "einem", "der", "die", "das", "und", "oder", "zu", "von", "im", "mit", "für",
    "wird", "werden", "ist", "sind", "war", "auf", "als", "auch", "nur", "bereits", "dass", "wenn", "beim",
    "new", "first", "then", "more", "only", "people", "person", "thing", "things", "show", "illustrate",
}
_GENERIC_DETAIL_TERMS = {
    "glass", "hole", "holes", "pane", "panes", "window", "windows",
    "lens", "element", "elements", "ash", "cloud", "clouds", "damage",
}


def _semantic_terms(value: str) -> set[str]:
    aliases = {
        "building": "build", "built": "build", "constructing": "construct", "construction": "construct",
        "pouring": "pour", "poured": "pour", "gegossen": "giessen", "gießen": "giessen",
        "bauen": "build", "moved": "move", "moving": "move", "assembled": "assemble",
        "assembling": "assemble", "airplanes": "airplane", "windows": "window",
        "smartphones": "smartphone", "cameras": "camera", "volcanoes": "volcano",
        "houses": "house", "volcanic": "volcano", "eruption": "volcano",
    }
    return {
        aliases.get(token, token) for token in re.findall(r"[\wäöüß-]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 2 and token not in _RELEVANCE_STOP and not token.isdigit()
    }


def media_relevance(candidate: MediaCandidate, scene: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    scene_text = " ".join(str(scene.get(key) or "") for key in ("narration", "visual_goal", "edit_instruction"))
    structured = " ".join(str(visual_intent.get(key) or "") for key in ("objects", "actions", "context", "visual_goal"))
    global_terms = _semantic_terms(global_subject_text(state))
    local_terms = _semantic_terms(scene_text + " " + structured)
    expected = local_terms | global_terms
    metadata = _semantic_terms(" ".join((candidate.title, candidate.description, *candidate.tags)))
    query_terms = _semantic_terms(candidate.query)
    local_matches = local_terms & metadata
    metadata_matches = expected & metadata
    action_expected = _semantic_terms(str(scene.get("narration") or "")) & {"build", "construct", "pour", "move", "assemble", "fall", "rise", "increase", "decrease"}
    action_matches = action_expected & metadata
    query_matches = expected & query_terms
    global_matches = global_terms & metadata
    global_subject_matches = global_terms & metadata
    matched = sorted(metadata_matches | query_matches)
    score = (
        len(local_matches) * 18
        + len(action_matches) * 16
        + len(global_matches) * 10
        + len(query_matches) * 2
    )
    if metadata and not metadata_matches:
        score -= 45
    subject_required = bool(global_terms)
    subject_missing = subject_required and not global_subject_matches
    generic_local_only = bool(local_matches) and local_matches <= _GENERIC_DETAIL_TERMS
    if metadata and subject_missing:
        score -= 80 if generic_local_only else 25
    if not metadata:
        confidence = "unknown"
    elif not metadata_matches or (subject_missing and generic_local_only):
        confidence = "rejected"
    elif subject_missing:
        confidence = "unknown"
    else:
        confidence = "high" if len(global_subject_matches) >= 1 and len(local_matches) >= 1 else "acceptable"
    if not matched and not metadata:
        score -= 60
    return {
        "score": float(score),
        "matched_terms": matched,
        "confidence": confidence,
        "subject_terms": sorted(global_terms),
        "subject_matches": sorted(global_subject_matches),
        "scene_matches": sorted(local_matches),
    }


def _best_unused(candidates: Iterable[MediaCandidate], used: set[str], scene: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> MediaCandidate | None:
    available = [candidate for candidate in candidates if candidate.identity not in used]
    if scene is None:
        return max(available, key=lambda item: item.rank, default=None)
    ranked = []
    for candidate in available:
        relevance = media_relevance(candidate, scene, state)
        ranked.append((relevance["score"], candidate.rank, candidate, relevance))
    ranked = [row for row in ranked if row[3]["confidence"] in {"high", "acceptable"}]
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def verify_media_shortlist(
    candidates: list[MediaCandidate],
    scene: dict[str, Any],
    state: dict[str, Any],
    verifier: Any | None = None,
    *,
    limit: int = 6,
) -> list[tuple[MediaCandidate, dict[str, Any]]]:
    """Verify only a small metadata-ranked shortlist; never resurrect rejected metadata."""
    visual = verifier or get_visual_verifier()
    if getattr(visual, "status", "") == "model_not_prepared" and hasattr(visual, "prepare_model"):
        try:
            visual.prepare_model()
        except (ImportError, RuntimeError, OSError, ValueError, httpx.HTTPError):
            # Metadata relevance remains the safe fallback when local weights
            # cannot be prepared in this process.
            pass
    rows: list[tuple[MediaCandidate, dict[str, Any]]] = []
    metadata_rows = sorted(
        ((candidate, media_relevance(candidate, scene, state)) for candidate in candidates),
        key=lambda row: (row[1]["confidence"] != "rejected", row[1]["score"], row[0].rank),
        reverse=True,
    )
    metadata_rows = [row for row in metadata_rows if row[1]["confidence"] != "rejected"][:limit]
    texts = visual_intent_text(scene, state)
    for candidate, metadata in metadata_rows:
        if metadata["confidence"] == "rejected":
            continue
        result = visual.verify_candidate(candidate, texts) if getattr(visual, "status", "unavailable_dependency") == "available" else None
        visual_data = {
            "status": getattr(visual, "status", "unavailable_dependency"),
            "score": None,
            "subject_score": None,
            "scene_score": None,
            "provenance": None,
            "frame_scores": [],
            "frame_count": 0,
        }
        if result is not None:
            visual_data = {
                "status": result.status,
                "score": result.score,
                "subject_score": result.subject_score,
                "scene_score": result.scene_score,
                "provenance": result.provenance,
                "frame_scores": list(result.frame_scores),
                "frame_count": result.frame_count,
            }
        combined = dict(metadata, visual=visual_data)
        if result is not None and result.status == "verified" and result.score is not None and result.score < VISUAL_THRESHOLD:
            combined["confidence"] = "rejected"
        elif metadata["confidence"] == "unknown" and result is not None and result.status == "verified" and (result.score or 0) >= VISUAL_THRESHOLD:
            combined["confidence"] = "acceptable"
        rows.append((candidate, combined))
    return rows


def prepare_project_media(
    state: dict[str, Any],
    project_id: str,
    settings: Settings,
    *,
    client: PexelsMediaClient | None = None,
    fallback_client: WikimediaMediaClient | None = None,
    progress: ProgressCallback | None = None,
    visual_verifier: Any | None = None,
) -> dict[str, Any]:
    """Attach cached real media to scenes, degrading to scene cards on provider failure."""
    assets = state.setdefault("assets", {})
    assets.setdefault("license_manifest", [])
    pexels = client or (PexelsMediaClient(settings.pexels_api_key) if settings.pexels_api_key else None)
    wikimedia = fallback_client or WikimediaMediaClient()
    asset_root = settings.render_root.resolve() / project_id / "assets"
    portrait = int(state["timeline"]["height"]) >= int(state["timeline"]["width"])
    used: set[str] = set()
    manifest: list[dict[str, Any]] = []
    selected_count = 0
    generated_card_count = 0
    replacement_failed_count = 0
    failure: MediaProviderError | None = None
    selected_media: list[dict[str, Any]] = []
    scenes = state.get("scenes", [])
    total_scenes = len(scenes)
    report_progress(
        progress,
        "media",
        "Finding visuals",
        phase="start",
        completed_units=0,
        total_units=total_scenes,
    )

    for scene_index, scene in enumerate(scenes, 1):
        existing = scene.get("media") if isinstance(scene.get("media"), dict) else None
        if existing:
            identity = str(existing.get("identity") or "")
            path = settings.render_root.resolve() / str(existing.get("cache_path") or "")
            if identity and path.is_file() and scene.get("asset_status") != "replacement_required":
                used.add(identity)
                manifest.append(existing)
                selected_media.append(existing)
                selected_count += 1
                report_progress(
                    progress,
                    "media",
                    "Finding visuals",
                    completed_units=scene_index,
                    total_units=total_scenes,
                    cached=True,
                )
                continue
        existing_usable = bool(
            existing
            and str(existing.get("identity") or "")
            and path.is_file()
        )

        queries = derive_search_queries(scene, state)
        scene["search_queries"] = queries
        duration = max(1.0, float(scene.get("end", 0)) - float(scene.get("start", 0)))
        candidate: MediaCandidate | None = None
        candidate_relevance: dict[str, Any] | None = None
        try:
            video_candidates: list[MediaCandidate] = []
            photo_candidates: list[MediaCandidate] = []
            if pexels is not None:
                preferred_kind = scene.get("preferred_media") or "video"
                if preferred_kind == "photo":
                    try:
                        for query in queries:
                            photo_candidates.extend(
                                pexels.search_photos(query, portrait=portrait)
                            )
                            candidate = _best_unused(photo_candidates, used, scene, state)
                    except MediaProviderError as exc:
                        failure = exc
                if candidate is None:
                    try:
                        for query in queries:
                            video_candidates.extend(
                                pexels.search_videos(
                                    query, portrait=portrait, scene_duration=duration
                                )
                            )
                            candidate = _best_unused(video_candidates, used, scene, state)
                        candidate = _best_unused(video_candidates, used, scene, state)
                    except MediaProviderError as exc:
                        failure = exc
                if candidate is None and preferred_kind != "photo":
                    try:
                        for query in queries:
                            photo_candidates.extend(
                                pexels.search_photos(query, portrait=portrait)
                            )
                            candidate = _best_unused(photo_candidates, used, scene, state)
                    except MediaProviderError as exc:
                        failure = exc
                combined = [*video_candidates, *photo_candidates]
                if combined:
                    shortlist = [item for item in combined if item.identity not in used]
                    verified = verify_media_shortlist(shortlist, scene, state, visual_verifier)
                    verified = [row for row in verified if row[1]["confidence"] in {"high", "acceptable"}]
                    if verified:
                        selected_row = max(
                            verified,
                            key=lambda row: (
                                float(row[1].get("visual", {}).get("score") or -1),
                                float(row[1]["score"]),
                                3 if row[0].kind == preferred_kind else 0,
                                row[0].rank,
                            ),
                        )
                        candidate, candidate_relevance = selected_row
            if candidate is None:
                commons_candidates: list[MediaCandidate] = []
                for query in queries:
                    commons_candidates.extend(
                        wikimedia.search_photos(query, portrait=portrait)
                    )
                    candidate = _best_unused(commons_candidates, used, scene, state)
            if candidate is None:
                related = _related_media(queries, selected_media)
                if related is not None:
                    scene["media"] = dict(related)
                    scene["asset_status"] = "related_media_reused"
                    manifest.append(dict(related))
                    selected_count += 1
                    report_progress(
                        progress,
                        "media",
                        "Finding visuals",
                        completed_units=scene_index,
                        total_units=total_scenes,
                    )
                    continue
            if candidate is None:
                if existing_usable:
                    scene["media"] = existing
                    scene["asset_status"] = "replacement_failed"
                    scene["fallback_reason"] = "No replacement media was found; the previous asset was kept."
                    manifest.append(existing)
                    selected_media.append(existing)
                    used.add(str(existing["identity"]))
                    selected_count += 1
                    replacement_failed_count += 1
                    report_progress(
                        progress,
                        "media",
                        "Finding visuals",
                        completed_units=scene_index,
                        total_units=total_scenes,
                    )
                    continue
                scene["asset_status"] = "generated_card_fallback"
                scene["fallback_reason"] = "No relevant real media was found after staged search."
                scene.pop("media", None)
                generated_card_count += 1
                report_progress(
                    progress,
                    "media",
                    "Finding visuals",
                    completed_units=scene_index,
                    total_units=total_scenes,
                )
                continue

            suffix = ".mp4" if candidate.kind == "video" else ".jpg"
            root = asset_root / candidate.provider
            destination = root / f"{candidate.kind}-{candidate.provider_id}{suffix}"
            downloader = pexels if candidate.provider == "pexels" else wikimedia
            assert downloader is not None
            downloaded = downloader.download(candidate, destination)
            relative = downloaded.relative_to(settings.render_root.resolve()).as_posix()
            metadata = {
                "identity": candidate.identity,
                "provider": candidate.provider,
                "provider_id": candidate.provider_id,
                "kind": candidate.kind,
                "cache_path": relative,
                "source_url": candidate.source_url,
                "creator": candidate.creator,
                "creator_url": candidate.creator_url,
                "width": candidate.width,
                "height": candidate.height,
                "duration": candidate.duration,
                "query": candidate.query,
                "title": candidate.title,
                "description": candidate.description,
                "tags": list(candidate.tags),
                "preview_url": candidate.preview_url,
                "relevance": candidate_relevance or media_relevance(candidate, scene, state),
            }
            scene["media"] = metadata
            scene["preferred_media"] = candidate.kind
            scene["asset_status"] = f"{candidate.kind}_ready"
            used.add(candidate.identity)
            manifest.append(metadata)
            selected_media.append(metadata)
            selected_count += 1
        except MediaProviderError as exc:
            failure = exc
            if existing_usable:
                scene["media"] = existing
                scene["asset_status"] = "replacement_failed"
                scene["fallback_reason"] = f"{exc}; the previous asset was kept."
                manifest.append(existing)
                selected_media.append(existing)
                used.add(str(existing["identity"]))
                selected_count += 1
                replacement_failed_count += 1
                report_progress(
                    progress,
                    "media",
                    "Finding visuals",
                    completed_units=scene_index,
                    total_units=total_scenes,
                )
                continue
            scene["asset_status"] = "generated_card_fallback"
            scene["fallback_reason"] = str(exc)
            scene.pop("media", None)
            generated_card_count += 1
        report_progress(
            progress,
            "media",
            "Finding visuals",
            completed_units=scene_index,
            total_units=total_scenes,
        )

    assets["license_manifest"] = manifest
    assets["selected_count"] = selected_count
    assets["generated_card_count"] = generated_card_count
    if replacement_failed_count:
        diagnostic = (
            f"Could not replace {replacement_failed_count} scene(s); previous media was kept."
        )
        if failure:
            diagnostic = f"{diagnostic} {failure}"
        assets.update(
            status="partial_fallback",
            provider=_provider_summary(manifest),
            diagnostic=diagnostic,
        )
    elif failure:
        assets.update(
            status="partial_fallback" if selected_count else "fallback_only",
            provider=failure.category,
            diagnostic=str(failure),
        )
    elif selected_count and generated_card_count:
        assets.update(
            status="partial_fallback",
            provider=_provider_summary(manifest),
            diagnostic=f"{generated_card_count} scene(s) use generated card fallback.",
        )
    elif selected_count:
        assets.update(status="media_ready", provider=_provider_summary(manifest), diagnostic=None)
    else:
        assets.update(
            status="fallback_only",
            provider="pexels+wikimedia" if settings.pexels_api_key else "wikimedia",
            diagnostic="No relevant real media was found; generated scene cards were used.",
        )
    if client is None and pexels is not None:
        pexels.close()
    if fallback_client is None:
        wikimedia.close()
    report_progress(
        progress,
        "media",
        "Finding visuals",
        phase="complete",
        completed_units=total_scenes,
        total_units=total_scenes,
    )
    return state


def _query_terms(value: str) -> set[str]:
    return {word for word in value.casefold().split() if len(word) > 2}


def _related_media(
    queries: list[str], selected_media: list[dict[str, Any]]
) -> dict[str, Any] | None:
    desired = set().union(*(_query_terms(query) for query in queries)) if queries else set()
    best: tuple[float, dict[str, Any]] | None = None
    for media in selected_media:
        existing = _query_terms(str(media.get("query") or ""))
        overlap = desired & existing
        score = len(overlap) / max(1, min(len(desired), len(existing)))
        if overlap and score >= 0.4 and (best is None or score > best[0]):
            best = (score, media)
    return dict(best[1]) if best else None


def _provider_summary(manifest: list[dict[str, Any]]) -> str:
    providers = list(dict.fromkeys(str(item.get("provider") or "unknown") for item in manifest))
    return "+".join(providers)
