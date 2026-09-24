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
    SCENE_VISUAL_THRESHOLD,
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


def _visual_query_tokens(value: object) -> set[str]:
    tokens = re.findall(r"[\wäöüß-]+", str(value or "").casefold(), flags=re.UNICODE)
    return {_VISUAL_QUERY_ALIASES.get(token, token) for token in tokens if len(token) > 2}


def _visual_subjects(scene: dict[str, Any], state: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    values = [
        scene.get("visual_goal"), scene.get("narration"), visual_intent.get("visual_goal"),
        visual_intent.get("objects"), visual_intent.get("actions"), visual_intent.get("media_queries"),
        intent.get("topic"), intent.get("question"),
    ]
    tokens = set().union(*(_visual_query_tokens(value) for value in values))
    core_order = ("airplane", "airspace", "island", "archipelago", "pyramid", "breath", "condensation", "droplets", "cheetah", "animals")
    primary = [term for term in core_order if term in tokens]
    entities = [term for term in ("sweden", "indonesia", "egypt", "sudan") if term in tokens]
    supporting = [term for term in ("sky", "winter", "border", "country", "landscape", "city", "coast", "map") if term in tokens]
    return primary, entities, supporting


def _query_is_concrete(raw: str, query: str, primary: list[str], entities: list[str]) -> bool:
    if not query:
        return False
    raw_tokens = set(re.findall(r"[\wäöüß-]+", raw.casefold(), flags=re.UNICODE))
    canonical = _visual_query_tokens(query)
    if len(raw_tokens & _VISUAL_ABSTRACT_TERMS) >= 1:
        return False
    if raw_tokens & {"welches", "welche", "welcher", "which", "warum", "why", "oder", "or"}:
        return False
    glue_count = len(raw_tokens & _VISUAL_QUERY_STOP)
    subject_matches = canonical & set(primary + entities)
    if glue_count >= 2 and len(subject_matches) < 2:
        return False
    if not subject_matches and len(query.split()) < 2:
        return False
    return len(query.split()) <= 6


def _comparison_query_variants(primary: list[str], entities: list[str], supporting: list[str], excluded_entities: set[str] | None = None) -> list[str]:
    core = "archipelago" if "archipelago" in primary else "islands" if "island" in primary else " ".join(primary[:2])
    if not core:
        return []
    excluded_entities = excluded_entities or set()
    queries: list[str] = []
    for entity in entities[:2]:
        if entity in excluded_entities:
            continue
        label = _VISUAL_ENTITY_ADJECTIVES.get(entity, entity)
        queries.append(f"{label} {core}")
    queries.append(f"{core} aerial")
    return queries


def _generated_visual_queries(primary: list[str], entities: list[str], supporting: list[str], *, format_name: str, excluded_entities: set[str] | None = None) -> list[str]:
    if "island" in primary or "archipelago" in primary:
        return _comparison_query_variants(primary, entities, supporting, excluded_entities)
    if "airplane" in primary:
        return ["commercial airplane flying", "airplane over country", "airspace border map"]
    if "pyramid" in primary:
        excluded_entities = excluded_entities or set()
        return [
            f"{_VISUAL_ENTITY_ADJECTIVES.get(entity, entity)} pyramids"
            for entity in entities[:2]
            if entity not in excluded_entities
        ] + ["pyramids aerial"]
    if "breath" in primary or "condensation" in primary or "droplets" in primary:
        return ["visible breath winter", "water vapor condensation", "cold air breath"]
    if "cheetah" in primary:
        return ["cheetah running", "cheetah sprinting", "fast animal running"]
    if "animals" in primary and format_name == "ranking":
        return ["fast animals", "animals running"]
    if primary:
        return [" ".join(primary[:3])]
    return []


def build_visual_query_plan(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Build a small, concrete query plan before provider calls."""
    stop = _VISUAL_QUERY_STOP
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    intent_goal = str(visual_intent.get("visual_goal") or "").strip()
    narration = str(scene.get("narration") or "").strip()
    scene_goal = str(scene.get("visual_goal") or "").strip()
    intent_coherent = _scene_text_coherent(narration, _intent_text(visual_intent))
    goal_coherent = _scene_text_coherent(narration, scene_goal)
    explicitly_refreshed = isinstance(scene.get("search_queries"), list) and not scene["search_queries"]
    if explicitly_refreshed:
        query_values = []
        visual_goal = scene_goal
    elif isinstance(visual_intent.get("media_queries"), list) and visual_intent.get("media_queries"):
        # Explicit visual queries are stronger acquisition intent than a
        # narration-overlap check; keep them even when the narration is an
        # abstract setup sentence.
        query_values = visual_intent.get("media_queries") or []
        visual_goal = intent_goal
    elif intent_coherent:
        query_values = visual_intent.get("media_queries") or []
        visual_goal = intent_goal
    elif goal_coherent:
        query_values = [] if scene.get("edit_instruction") else scene.get("search_queries") or []
        visual_goal = scene_goal
    else:
        query_values = []
        visual_goal = ""
    primary, entities, supporting = _visual_subjects(scene, state)
    protected_text = " ".join(
        str(value or "")
        for value in (
            visual_intent.get("must_not_show"),
            scene.get("must_not_show"),
            (state.get("payoff_plan") or {}).get("hook_must_not_reveal") if isinstance(state.get("payoff_plan"), dict) else "",
        )
    )
    protected_entities = _visual_query_tokens(protected_text) & set(entities)
    # A comparison payoff often names both sides while protecting only the
    # winner/reveal.  Treat that broad context as non-specific; only an
    # unambiguous single protected entity suppresses its visual query.
    if len(protected_entities) > 1:
        protected_entities = set()
    supplied: list[str] = []
    for value in query_values:
        raw = str(value).strip()
        query = _provider_query(_semantic_query(raw, stop, limit=6))
        if _query_is_concrete(raw, query, primary, entities):
            supplied.append(query)
    generated = _generated_visual_queries(
        primary,
        entities,
        supporting,
        format_name=str((state.get("format_plan") or {}).get("selected_format") or ""),
        excluded_entities=protected_entities,
    )
    scene_text = " ".join(value for value in (str(scene.get("edit_instruction") or ""), visual_goal, narration) if value)
    primary_query = _provider_query(_semantic_query(scene_text, stop, limit=6))
    broader_query = _provider_query(_semantic_query(str((state.get("intent") or {}).get("topic") or ""), stop, limit=5))
    fallback = [query for query in (primary_query, broader_query) if _query_is_concrete(query, query, primary, entities)]
    # Explicitly authored visual queries remain first when they are concrete;
    # otherwise generated subject-first concepts replace narration fragments.
    def payoff_safe_query(query: str) -> bool:
        return not (_visual_query_tokens(query) & protected_entities)

    queries = list(dict.fromkeys(query for query in [*supplied, *generated, *fallback] if payoff_safe_query(query)))[:3]
    if not queries:
        queries = [" ".join(primary[:3]) or "nature landscape"]
    comparison_coverage = {
        entity: any(entity in _visual_query_tokens(query) for query in queries)
        for entity in entities[:2]
    }
    shared_subject = bool(primary and any(term in _visual_query_tokens(query) for query in queries for term in primary))
    return {
        "queries": queries,
        "primary_subjects": primary,
        "secondary_subjects": entities,
        "supporting_context": supporting,
        "comparison_coverage": comparison_coverage,
        "protected_entities": sorted(protected_entities),
        "shared_subject_coverage": shared_subject,
        "query_quality": "subject_first" if generated and not supplied else "explicit_visual_intent",
    }


def derive_search_queries(scene: dict[str, Any], state: dict[str, Any]) -> list[str]:
    return build_visual_query_plan(scene, state)["queries"]


_PROVIDER_TERMS = {
    "hausbau": "house construction", "hausbaues": "house construction", "haus": "house", "häuser": "houses",
    "fundament": "foundation", "fundaments": "foundation", "gießen": "pouring", "gegossen": "poured",
    "bauen": "building", "bau": "construction", "wände": "walls", "wand": "wall", "dach": "roof",
    "beton": "concrete", "arbeiter": "workers", "arbeitern": "workers", "erde": "earth", "materie": "matter",
    "kapazität": "capacity", "sparen": "saving", "geld": "money",
    "atem": "breath", "atemluft": "breath air", "winter": "winter", "sichtbarer": "visible",
    "qualm": "smoke", "nebel": "mist", "nebelwölkchen": "mist cloud", "wolken": "clouds",
    "kalt": "cold", "kalte": "cold", "außenluft": "outside air", "aussenluft": "outside air", "warme": "warm",
    "kondensiert": "condensation", "gasförmige": "water vapor", "wasser": "water",
    "schweben": "floating", "verschwinden": "dissipating",
    "luft": "air", "feinen": "fine", "winzigen": "tiny",
    "insel": "island", "inseln": "islands", "inselstaat": "island", "archipel": "archipelago",
    "flugzeug": "airplane", "flugzeuge": "airplanes", "passagierflugzeug": "commercial airplane",
    "luftraum": "airspace", "grenze": "border", "grenzen": "borders", "landesgrenze": "country border",
    "pyramide": "pyramid", "pyramiden": "pyramids",
    "kondensation": "condensation",
    "schweden": "sweden", "indonesien": "indonesia", "ägypten": "egypt", "aegypten": "egypt",
    "sudan": "sudan", "norden": "north", "küsten": "coast", "kuesten": "coast",
}

# Small, deterministic visual vocabulary used to turn scene intent into
# provider-friendly concepts.  This is deliberately not a translation system;
# it only covers the recurring concrete subjects needed by media retrieval.
_VISUAL_QUERY_ALIASES = {
    "insel": "island", "inseln": "island", "island": "island", "islands": "island", "inselstaat": "island", "inselstaaten": "island",
    "archipel": "archipelago", "archipelagos": "archipelago", "archipelago": "archipelago",
    "flugzeug": "airplane", "flugzeuge": "airplane", "passagierflugzeug": "airplane",
    "airliner": "airplane", "aircraft": "airplane", "airplane": "airplane", "airplanes": "airplane",
    "luftraum": "airspace", "airspace": "airspace", "grenze": "border", "grenzen": "border",
    "landesgrenze": "border", "pyramide": "pyramid", "pyramiden": "pyramid", "pyramid": "pyramid", "pyramids": "pyramid",
    "atem": "breath", "atemluft": "breath", "breath": "breath", "kondensation": "condensation",
    "kondensiert": "condensation", "kondensieren": "condensation", "tröpfchen": "droplets",
    "droplets": "droplets", "winter": "winter", "cheetah": "cheetah", "gepard": "cheetah",
    "tiere": "animals", "tier": "animals", "animals": "animals",
    "schweden": "sweden", "sweden": "sweden", "swedish": "sweden", "indonesien": "indonesia", "indonesia": "indonesia", "indonesian": "indonesia",
    "ägypten": "egypt", "aegypten": "egypt", "egypt": "egypt", "egyptian": "egypt", "sudan": "sudan", "sudanese": "sudan",
}
_VISUAL_QUERY_STOP = {
    "about", "after", "also", "and", "because", "before", "could", "from", "have", "into", "more",
    "only", "over", "that", "their", "there", "these", "this", "through", "video", "visual", "what",
    "when", "where", "which", "with", "would", "your", "illustrate", "aber", "auch", "dass", "dies",
    "ein", "eine", "einer", "eines", "für", "fuer", "haben", "hat", "hier", "mehr", "nicht", "oder",
    "über", "ueber", "sich", "sind", "sein", "wenn", "wird", "zeigen", "beim", "zuerst", "erst", "danach",
    "dann", "der", "die", "das", "man", "sieht", "sehen", "seine", "seinen", "seinem", "seiner", "diese",
    "dieser", "dabei", "zuvor", "wir", "als", "ist", "wie", "bei", "und", "aus", "zu", "sondern", "kein",
    "keine", "unsere", "unser", "ähnlich", "kleine", "winziger", "kurz", "wieder", "answer", "cause",
    "context", "detail", "hook", "intro", "outro", "payoff", "setup", "support", "turn", "why", "warum",
    "welches", "welche", "welcher", "wieso", "führt", "kommt", "liegt", "damit", "trotzdem",
    "wegen", "ihrer", "gesamtes", "besteht", "überwiegend", "etwa", "rein", "reinen", "platz",
    "land", "länder", "country", "countries", "scene", "question", "format", "concept", "fact", "reason",
    "permission", "difference", "therefore", "dürfen", "ausländische", "ausländisch", "landes",
}
_VISUAL_ABSTRACT_TERMS = {
    "because", "therefore", "permission", "reason", "difference", "concept", "fact", "context", "answer",
    "warum", "wegen", "deshalb", "daher", "grund", "unterschied", "erlaubnis", "durchgang", "führte",
    "dürfen", "ausländische", "ausländisch", "landes", "most", "people", "guess", "compare", "count", "counts",
    "weltweit", "meisten", "zwar", "aber", "besonders", "viele", "stark", "inselzahl", "sechs", "kommt",
    "liegt", "damit", "trotzdem", "besteht", "überwiegend", "gesamtes",
    "genehmigung", "nötig", "noetig", "dessen", "überflug", "freigabe", "route",
}
_VISUAL_ENTITY_ADJECTIVES = {
    "sweden": "swedish", "indonesia": "indonesian", "egypt": "egyptian", "sudan": "sudanese",
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
    text = re.sub(
        r"(?i)\b(?:kein(?:e|en|em|er)?|ohne|no|not)\s+[\wäöüß-]+",
        " ",
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
    "man", "seine", "seinen", "seinem", "seiner", "sehen", "sieht", "diese", "dieser", "dabei",
    "zuvor", "ähnlich", "kleine", "kleiner", "winzig", "winziger", "unsere", "unser", "kurz",
    "dann", "wieder", "sondern", "kein", "keine", "also",
}

_TEXT_HEAVY_METADATA_MARKERS = (
    "flashcard",
    "flash card",
    "study card",
    "quiz card",
    "quote card",
    "social media card",
    "presentation slide",
    "document page",
    "worksheet",
    "text overlay",
    "text-heavy",
    "screenshot of",
    "infographic template",
)

_AMBIGUOUS_LOCAL_TERMS = {
    "glass",
    "hole",
    "pane",
    "window",
    "lens",
    "element",
    "ash",
    "cloud",
    "damage",
}


def _semantic_terms(value: str) -> set[str]:
    aliases = {
        "building": "build", "built": "build", "constructing": "construct", "construction": "construct",
        "pouring": "pour", "poured": "pour", "gegossen": "giessen", "gießen": "giessen",
        "bauen": "build", "moved": "move", "moving": "move", "assembled": "assemble",
        "assembling": "assemble", "airplanes": "airplane", "windows": "window",
        "smartphones": "smartphone", "cameras": "camera", "volcanoes": "volcano",
        "houses": "house", "volcanic": "volcano", "eruption": "volcano",
        "atem": "breath", "atemluft": "breath", "sichtbarer": "visible", "qualm": "smoke",
        "nebel": "mist", "nebelwölkchen": "mist", "wolken": "cloud", "kühlt": "cool",
        "kalte": "cold", "außenluft": "air", "warme": "warm", "kondensiert": "condensation",
        "gasförmige": "vapor", "wasser": "water", "tröpfchen": "droplet",
        "schweben": "float", "verschwinden": "dissipate", "droplets": "droplet",
        "condenses": "condensation", "condensing": "condensation", "condensed": "condensation",
        # German narration must match English provider metadata for the
        # subjects the visual query planner searches ("Inseln" vs "islands").
        "insel": "island", "inseln": "island", "islands": "island",
        "archipel": "archipelago", "archipelagos": "archipelago",
        "schweden": "sweden", "swedish": "sweden", "indonesien": "indonesia", "indonesian": "indonesia",
    }
    return {
        aliases.get(token, token) for token in re.findall(r"[\wäöüß-]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 2 and token not in _RELEVANCE_STOP and not token.isdigit()
    }


def _negated_terms(value: str) -> set[str]:
    terms = re.findall(
        r"(?i)\b(?:kein(?:e|en|em|er)?|ohne|no|not)\s+([\wäöüß-]+)",
        value,
    )
    return _semantic_terms(" ".join(terms))


def _intent_text(intent: dict[str, Any]) -> str:
    return " ".join(
        [
            str(intent.get("visual_goal") or ""),
            *(str(value) for key in ("objects", "actions", "context") for value in intent.get(key, [])),
        ]
    )


def _scene_text_coherent(narration: str, candidate_text: str) -> bool:
    narration_terms = _semantic_terms(narration)
    candidate_terms = _semantic_terms(candidate_text)
    # A deliberately concise shot direction (for example, "lighthouse") can
    # validly stand in for a pronoun-heavy sentence.  Longer, unrelated
    # directions are much more likely to be stale state from an earlier
    # revision and must not steer retrieval.
    return (
        not narration_terms
        or len(candidate_terms) <= 2
        or bool(narration_terms & candidate_terms)
    )


def _metadata_presentation_risk(candidate: MediaCandidate) -> dict[str, Any]:
    text = " ".join((candidate.title, candidate.description, *candidate.tags)).casefold()
    markers = [marker for marker in _TEXT_HEAVY_METADATA_MARKERS if marker in text]
    return {
        "rejected": bool(markers),
        "source": "metadata" if markers else None,
        "markers": markers,
    }


def is_real_media_allowed(value: MediaCandidate | dict[str, Any]) -> bool:
    """Shared hard gate for candidates and persisted assets, independent of fit."""
    data = value if isinstance(value, dict) else vars(value)
    if data.get("kind") not in {"photo", "video"}:
        return False
    provider = str(data.get("provider") or str(data.get("identity", "")).split(":")[0])
    if not provider:
        parts = Path(str(data.get("cache_path") or "")).parts
        provider = next((part for part in parts if part in {"pexels", "wikimedia"}), "")
    if provider not in {"pexels", "wikimedia"}:
        return False
    markers = " ".join(str(data.get(key) or "") for key in ("type", "source_type", "asset_type", "cache_path", "identity" )).casefold()
    if any(term in markers for term in ("generated_card", "text_card", "flashcard", "diagram_or_card", "placeholder", "synthetic_visual")):
        return False
    text = " ".join((str(data.get("title") or ""), str(data.get("description") or ""), *data.get("tags", ()))).casefold()
    if any(marker in text for marker in (*_TEXT_HEAVY_METADATA_MARKERS, "quote card", "text card", "informational card", "generated card")):
        return False
    relevance = data.get("relevance") or {}
    return not ((relevance.get("presentation_risk") or {}).get("rejected") or (relevance.get("visual") or {}).get("presentation_risk"))


def media_relevance(candidate: MediaCandidate, scene: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    narration = str(scene.get("narration") or "")
    intent_text = _intent_text(visual_intent)
    scene_goal = str(scene.get("visual_goal") or "")
    explicitly_refreshed = isinstance(scene.get("search_queries"), list) and not scene["search_queries"]
    if explicitly_refreshed:
        structured = scene_goal
        matching_goal = scene_goal
    elif _scene_text_coherent(narration, intent_text):
        structured = intent_text
        matching_goal = str(visual_intent.get("visual_goal") or "")
    elif _scene_text_coherent(narration, scene_goal):
        structured = scene_goal
        matching_goal = scene_goal
    else:
        structured = ""
        matching_goal = ""
    scene_text = " ".join(
        value for value in (narration, structured, str(scene.get("edit_instruction") or "")) if value
    )
    global_terms = _semantic_terms(global_subject_text(state))
    negated_terms = _negated_terms(narration)
    local_terms = _semantic_terms(scene_text) - negated_terms
    metadata = _semantic_terms(" ".join((candidate.title, candidate.description, *candidate.tags)))
    query_terms = _semantic_terms(candidate.query)
    goal_terms = _semantic_terms(matching_goal)
    # A concise visual direction may name a principal object plus its setting
    # ("lighthouse by the sea").  Matching its principal object is useful
    # evidence; a global topic alone is not.  This remains local evidence
    # because it comes from the scene's own coherent visual direction.
    goal_direct_match = bool(goal_terms & metadata)
    local_matches = local_terms & metadata
    scene_specific_terms = local_terms - global_terms
    scene_specific_matches = scene_specific_terms & metadata
    action_expected = _semantic_terms(str(scene.get("narration") or "")) & {"build", "construct", "pour", "move", "assemble", "fall", "rise", "increase", "decrease"}
    action_matches = action_expected & metadata
    query_matches = local_terms & query_terms
    global_matches = global_terms & metadata
    presentation_risk = _metadata_presentation_risk(candidate)
    if not is_real_media_allowed(candidate):
        presentation_risk = {"rejected": True, "source": "eligibility", "markers": ["non_real_or_card"]}
    matched = sorted(local_matches | global_matches)
    contextual_only = not local_matches and len(global_matches) >= 2
    global_only_match = (
        bool(local_matches)
        and not scene_specific_matches
        and bool(scene_specific_terms)
        and local_matches <= global_terms
        and not goal_direct_match
    )
    ambiguous_without_context = (
        bool(local_matches)
        and local_matches <= _AMBIGUOUS_LOCAL_TERMS
        and not global_matches
    )
    score = (
        len(local_matches) * 12
        + len(scene_specific_matches) * 18
        + len(action_matches) * 16
        + len(global_matches) * 4
    )
    if metadata and not local_matches:
        score -= 45
    if contextual_only:
        # Retain this as a ranking signal only. Acceptance below remains
        # unknown until local visual evidence is available.
        score += 70
    if not metadata:
        confidence = "unknown"
    elif presentation_risk["rejected"] or ambiguous_without_context:
        confidence = "rejected"
    elif contextual_only or global_only_match:
        # Topic overlap is only a plausibility guard. It needs a strong local
        # visual verification before it can become eligible media.
        confidence = "unknown"
    elif not local_matches:
        confidence = "rejected"
    else:
        confidence = "high" if len(scene_specific_matches) >= 2 or bool(action_matches) else "acceptable"
    if not matched and not metadata:
        score -= 60
    return {
        "score": float(score),
        "matched_terms": matched,
        "confidence": confidence,
        "subject_terms": sorted(global_terms),
        "subject_matches": sorted(global_matches),
        "scene_matches": sorted(local_matches),
        "scene_specific_matches": sorted(scene_specific_matches),
        "query_matches": sorted(query_matches),
        "presentation_risk": presentation_risk,
        "selection_tier": 3 if scene_specific_matches or action_matches else (2 if local_matches and not global_only_match else 0),
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
        ((candidate, media_relevance(candidate, scene, state)) for candidate in candidates if is_real_media_allowed(candidate)),
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
            "presentation_score": None,
            "photographic_score": None,
            "diagram_score": None,
            "presentation_risk": False,
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
                "presentation_score": result.presentation_score,
                "photographic_score": result.photographic_score,
                "diagram_score": result.diagram_score,
                "presentation_risk": result.presentation_risk,
            }
        combined = dict(metadata, visual=visual_data)
        if result is not None and result.status == "verified":
            scene_score = result.scene_score if result.scene_score is not None else result.score
            if result.presentation_risk:
                combined["confidence"] = "rejected"
                combined["presentation_risk"] = {
                    "rejected": True,
                    "source": "vision",
                    "markers": [],
                }
            elif scene_score is None or scene_score < SCENE_VISUAL_THRESHOLD:
                combined["confidence"] = "rejected"
            elif metadata["confidence"] == "unknown" and (result.score or 0) >= VISUAL_THRESHOLD:
                combined["confidence"] = "acceptable"
                combined["selection_tier"] = 1
        rows.append((candidate, combined))
    return rows


def _rank_verified(
    candidates: list[MediaCandidate],
    scene: dict[str, Any],
    state: dict[str, Any],
    preferred_kind: str,
    used: set[str],
    verifier: Any | None,
) -> list[tuple[MediaCandidate, dict[str, Any]]]:
    unique: dict[str, MediaCandidate] = {}
    for candidate in candidates:
        if candidate.identity not in used:
            unique.setdefault(candidate.identity, candidate)
    shortlist = list(unique.values())
    rows = verify_media_shortlist(shortlist, scene, state, verifier)
    eligible = [row for row in rows if row[1]["confidence"] in {"high", "acceptable"}]
    return sorted(
        eligible,
        key=lambda row: (
            int(row[1].get("selection_tier") or 0),
            float(row[1].get("visual", {}).get("scene_score") or -1),
            float(row[1]["score"]),
            int(row[0].kind == preferred_kind),
            row[0].rank,
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Adaptive (staged) scene search
#
# The planned query list is executed one query at a time.  After each stage
# the accumulated, de-duplicated candidates are verified and the scene's
# subject coverage is re-evaluated; searching stops as soon as coverage is
# strong, and a fallback query is chosen for the subject that is still weak.
# ---------------------------------------------------------------------------

MAX_SCENE_QUERY_BUDGET = 3
COVERAGE_STRONG = "strong"
COVERAGE_PARTIAL = "partial"
COVERAGE_WEAK = "weak"
COVERAGE_NONE = "none"
_COVERAGE_RANK = {COVERAGE_NONE: 0, COVERAGE_WEAK: 1, COVERAGE_PARTIAL: 2, COVERAGE_STRONG: 3}
# A passing scene similarity is only "present"; strong coverage needs a margin.
STRONG_SCENE_VISUAL_SCORE = SCENE_VISUAL_THRESHOLD + 0.02
MIN_USABLE_SHORT_SIDE = 480
_COVERAGE_SYNONYMS = {
    "island": {"island", "archipelago"},
    "archipelago": {"archipelago", "island"},
    "airplane": {"airplane", "plane", "jet"},
    "condensation": {"condensation", "droplet", "droplets", "vapor"},
    "droplets": {"droplets", "droplet", "condensation"},
    "breath": {"breath"},
}
_COMPARISON_FORMATS = {"comparison", "quiz"}
SCENE_TARGET = "scene"


def _coverage_tokens(value: object) -> set[str]:
    text = str(value or "")
    semantic = {_VISUAL_QUERY_ALIASES.get(token, token) for token in _semantic_terms(text)}
    return _visual_query_tokens(text) | semantic


def _target_terms(target: str) -> set[str]:
    return _COVERAGE_SYNONYMS.get(target, {target})


def scene_coverage_targets(
    scene: dict[str, Any], state: dict[str, Any], query_plan: dict[str, Any]
) -> dict[str, Any]:
    """Return the visual subjects this scene must show, keyed by target -> role."""
    visual_intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    local = set().union(*(
        _coverage_tokens(value)
        for value in (
            scene.get("narration"), scene.get("visual_goal"), scene.get("edit_instruction"),
            visual_intent.get("visual_goal"), visual_intent.get("objects"), visual_intent.get("actions"),
            visual_intent.get("media_queries"),
        )
    ))
    format_name = str((state.get("format_plan") or {}).get("selected_format") or "")
    primary = list(query_plan.get("primary_subjects") or [])
    entities = list(query_plan.get("secondary_subjects") or [])
    protected = set(query_plan.get("protected_entities") or [])
    local_primary = [term for term in primary if term in local]
    if format_name == "ranking" and len(local_primary) > 1 and "animals" in local_primary:
        # The ranked item itself (for example "cheetah") is the subject; the
        # category is context.
        local_primary.remove("animals")
    targets: dict[str, str] = {}
    comparison = format_name in _COMPARISON_FORMATS or len(entities) >= 2
    if comparison:
        sides = [entity for entity in entities[:2] if entity in local and entity not in protected]
        for role, entity in zip(("subject_a", "subject_b"), sides):
            targets[entity] = role
    shared = (local_primary or primary)[:1]
    if shared:
        targets[shared[0]] = "shared" if comparison else "primary"
    if not targets:
        targets[SCENE_TARGET] = "scene"
    mode = "comparison" if comparison and len(targets) > 1 else "single" if SCENE_TARGET not in targets else "generic"
    return {"mode": mode, "targets": targets, "format": format_name or None}


def _usable_quality(candidate: MediaCandidate, scene_duration: float) -> bool:
    if min(candidate.width, candidate.height) < MIN_USABLE_SHORT_SIDE:
        return False
    if candidate.kind == "video":
        return float(candidate.duration or 0) >= max(2.0, scene_duration * 0.6)
    return True


def candidate_target_coverage(
    candidate: MediaCandidate, relevance: dict[str, Any], target: str, scene_duration: float
) -> str:
    """Deterministic per-target coverage for one verified candidate.

    Strong coverage always needs two independent signals: metadata naming the
    subject plus either OpenCLIP or trusted query provenance.  A high OpenCLIP
    similarity alone never produces strong coverage.
    """
    if relevance.get("confidence") not in {"high", "acceptable"}:
        return COVERAGE_NONE
    if (relevance.get("presentation_risk") or {}).get("rejected"):
        return COVERAGE_NONE
    metadata_tokens = _coverage_tokens(" ".join((candidate.title, candidate.description, *candidate.tags)))
    tier = int(relevance.get("selection_tier") or 0)
    if target == SCENE_TARGET:
        subject_metadata = tier >= 2
        subject_query = bool(relevance.get("query_matches"))
    else:
        terms = _target_terms(target)
        subject_metadata = bool(metadata_tokens & terms)
        subject_query = bool(_coverage_tokens(candidate.query) & terms)
    if not subject_metadata and not subject_query:
        return COVERAGE_WEAK
    visual = relevance.get("visual") or {}
    verified = visual.get("status") == "verified"
    scene_score = visual.get("scene_score") if visual.get("scene_score") is not None else visual.get("score")
    visual_strong = (
        verified
        and scene_score is not None
        and float(scene_score) >= STRONG_SCENE_VISUAL_SCORE
        and float(visual.get("score") or 0) >= VISUAL_THRESHOLD
    )
    # Context-only: the metadata matches the topic but none of this scene's own
    # words.  Metadata naming a scene-local target is not context.
    context_only = not relevance.get("scene_matches") if target != SCENE_TARGET else tier <= 1
    if not _usable_quality(candidate, scene_duration):
        return COVERAGE_PARTIAL
    if verified:
        if visual_strong and subject_metadata and not context_only:
            return COVERAGE_STRONG
        if visual_strong and subject_query and not metadata_tokens:
            # Untitled provider media: targeted query provenance + local vision.
            return COVERAGE_STRONG
        return COVERAGE_PARTIAL
    # OpenCLIP unavailable: be conservative, require metadata + provenance.
    if subject_metadata and subject_query and not context_only:
        return COVERAGE_STRONG
    return COVERAGE_PARTIAL


def summarize_coverage(
    rows: Iterable[tuple[MediaCandidate, dict[str, Any]]],
    targets: dict[str, str],
    scene_duration: float,
) -> dict[str, Any]:
    levels = {target: COVERAGE_NONE for target in targets}
    for candidate, relevance in rows:
        for target in targets:
            level = candidate_target_coverage(candidate, relevance, target, scene_duration)
            if _COVERAGE_RANK[level] > _COVERAGE_RANK[levels[target]]:
                levels[target] = level
    overall = min(levels.values(), key=_COVERAGE_RANK.__getitem__) if levels else COVERAGE_NONE
    return {"overall": overall, "targets": levels}


def select_fallback_query(
    remaining: list[str], coverage: dict[str, Any], targets: dict[str, str]
) -> str | None:
    """Pick the remaining planned query that addresses a still-weak subject."""
    levels = coverage["targets"]
    weak = [target for target, level in levels.items() if level != COVERAGE_STRONG]
    strong_sides = {
        target for target, role in targets.items()
        if role.startswith("subject_") and levels.get(target) == COVERAGE_STRONG
    }
    best: tuple[int, str] | None = None
    for query in remaining:
        tokens = _coverage_tokens(query)
        if tokens & strong_sides:
            continue  # never spend budget repeating an already strong side
        addressed = sum(
            1 for target in weak
            if target == SCENE_TARGET or tokens & _target_terms(target)
        )
        if addressed and (best is None or addressed > best[0]):
            best = (addressed, query)
    if best is not None:
        return best[1]
    if all(level == COVERAGE_NONE for level in levels.values()):
        # Nothing usable yet: any remaining planned query beats giving up.
        return next((query for query in remaining if not _coverage_tokens(query) & strong_sides), None)
    return None


def _fallback_trigger(stage: dict[str, Any], coverage: dict[str, Any]) -> str:
    if stage["errors"] and not stage["new"]:
        return "provider_error"
    if not stage["new"] and not stage["duplicates"]:
        return "no_results"
    if coverage["overall"] == COVERAGE_NONE:
        return "no_verified_candidates"
    weak = sorted(target for target, level in coverage["targets"].items() if level != COVERAGE_STRONG)
    return f"{coverage['overall']}_coverage:{','.join(weak)}"


def _candidate_coverage_score(
    candidate: MediaCandidate, relevance: dict[str, Any], targets: dict[str, str], scene_duration: float
) -> int:
    return sum(
        _COVERAGE_RANK[candidate_target_coverage(candidate, relevance, target, scene_duration)]
        for target in targets
    )


@dataclass
class StagedSearchResult:
    ranked: list[tuple[MediaCandidate, dict[str, Any]]]
    candidates: list[MediaCandidate]
    provenance: dict[str, Any]
    failure: MediaProviderError | None
    evaluated: set[str]
    # Verified relevance per identity, reused by later fallbacks so the same
    # asset is never sent through OpenCLIP twice for one scene.
    verified: dict[str, dict[str, Any]]
    verifier_failed: bool


_GENERIC_SOURCE_PATHS = {"", "video", "videos", "photo", "photos", "wiki"}


def _canonical_source(url: str) -> str | None:
    """Stable asset URL key; generic provider landing pages identify nothing."""
    parsed = urlparse(str(url or "").strip())
    path = parsed.path.strip("/").casefold()
    if not parsed.netloc or path in _GENERIC_SOURCE_PATHS:
        return None
    return f"{parsed.netloc.casefold().removeprefix('www.')}/{path}"


def _claim_logical_query(provenance: dict[str, Any], query: str) -> bool:
    """Record a logical query string; refuses a new string once the budget is spent."""
    executed = provenance["executed_queries"]
    if query in executed:
        return True
    if len(executed) >= provenance["query_budget"]:
        return False
    executed.append(query)
    provenance["logical_queries_executed"] = provenance["executed_query_count"] = len(executed)
    return True


def _count_provider_request(provenance: dict[str, Any], source: str) -> None:
    provenance["provider_requests_executed"] += 1
    by_source = provenance["provider_requests_by_source"]
    by_source[source] = by_source.get(source, 0) + 1


def _safe_verify(
    candidates: list[MediaCandidate],
    scene: dict[str, Any],
    state: dict[str, Any],
    verifier: Any | None,
) -> tuple[list[tuple[MediaCandidate, dict[str, Any]]], bool]:
    try:
        return verify_media_shortlist(candidates, scene, state, verifier), True
    except Exception:  # noqa: BLE001 - verification must never fail media search
        rows = verify_media_shortlist(candidates, scene, state, _METADATA_ONLY_VERIFIER)
        return rows, False


class _MetadataOnlyVerifier:
    status = "verification_failed"

    def verify_candidate(self, _candidate: Any, _texts: list[str]) -> None:
        return None


_METADATA_ONLY_VERIFIER = _MetadataOnlyVerifier()


def run_staged_scene_search(
    queries: list[str],
    scene: dict[str, Any],
    state: dict[str, Any],
    query_plan: dict[str, Any],
    *,
    pexels: Any | None,
    wikimedia: Any,
    preferred_kind: str,
    portrait: bool,
    scene_duration: float,
    used: set[str],
    verifier: Any | None,
    budget: int = MAX_SCENE_QUERY_BUDGET,
) -> StagedSearchResult:
    """Execute planned queries one stage at a time within a hard query budget."""
    budget = max(1, min(int(budget), MAX_SCENE_QUERY_BUDGET))
    planned = list(dict.fromkeys(query for query in queries if query))
    coverage_targets = scene_coverage_targets(scene, state, query_plan)
    targets = coverage_targets["targets"]
    remaining = planned[:budget]
    seen: dict[str, MediaCandidate] = {}
    seen_sources: set[str] = set()
    rows: dict[str, tuple[MediaCandidate, dict[str, Any]]] = {}
    failure: MediaProviderError | None = None
    stages: list[dict[str, Any]] = []
    fallback_reasons: list[str] = []
    requests = 0
    duplicates = 0
    verification_ok = True
    active_verifier = verifier
    coverage = summarize_coverage([], targets, scene_duration)
    coverage_before_fallback: dict[str, Any] | None = None
    stop_reason = "no_queries"
    alternate = "photo" if preferred_kind == "video" else "video"
    search_plan = (
        [("pexels", preferred_kind), ("pexels", alternate)] if pexels is not None else [("wikimedia", "photo")]
    )
    query = remaining.pop(0) if remaining else None
    while query is not None:
        stage = {"query": query, "requests": 0, "new": 0, "duplicates": 0, "errors": []}
        for position, (provider_name, kind) in enumerate(search_plan):
            if position and coverage["overall"] == COVERAGE_STRONG:
                break  # the preferred kind already covers the scene
            try:
                if provider_name == "wikimedia":
                    results = wikimedia.search_photos(query, portrait=portrait)
                elif kind == "video":
                    results = pexels.search_videos(query, portrait=portrait, scene_duration=scene_duration)
                else:
                    results = pexels.search_photos(query, portrait=portrait)
            except MediaProviderError as exc:
                failure = exc
                stage["errors"].append(exc.category)
                results = []
            requests += 1
            stage["requests"] += 1
            fresh: list[MediaCandidate] = []
            for candidate in results or []:
                source = _canonical_source(candidate.source_url)
                if candidate.identity in seen or (source and source in seen_sources):
                    stage["duplicates"] += 1
                    continue
                seen[candidate.identity] = candidate
                if source:
                    seen_sources.add(source)
                if candidate.identity not in used:
                    fresh.append(candidate)
            stage["new"] += len(fresh)
            if fresh:
                verified, ok = _safe_verify(fresh, scene, state, active_verifier)
                if not ok:
                    # One failure is enough evidence; do not retry per stage.
                    verification_ok = False
                    active_verifier = _METADATA_ONLY_VERIFIER
                for candidate, relevance in verified:
                    rows.setdefault(candidate.identity, (candidate, relevance))
                coverage = summarize_coverage(rows.values(), targets, scene_duration)
        duplicates += stage["duplicates"]
        stage["coverage"] = coverage["overall"]
        stages.append(stage)
        if coverage["overall"] == COVERAGE_STRONG:
            stop_reason = "strong_coverage"
            break
        if not remaining:
            stop_reason = "budget_exhausted" if len(stages) >= budget else "plan_exhausted"
            break
        next_query = select_fallback_query(remaining, coverage, targets)
        if next_query is None:
            stop_reason = "no_targeted_query"
            break
        if coverage_before_fallback is None:
            coverage_before_fallback = coverage
        fallback_reasons.append(_fallback_trigger(stage, coverage))
        remaining.remove(next_query)
        query = next_query

    eligible = [
        row for row in rows.values()
        if row[1]["confidence"] in {"high", "acceptable"}
        and not (row[1].get("presentation_risk") or {}).get("rejected")
    ]
    ranked = sorted(
        eligible,
        key=lambda row: (
            int(row[1].get("selection_tier") or 0),
            _candidate_coverage_score(row[0], row[1], targets, scene_duration),
            float(row[1].get("visual", {}).get("scene_score") or -1),
            float(row[1]["score"]),
            int(row[0].kind == preferred_kind),
            row[0].rank,
        ),
        reverse=True,
    )
    executed = [stage["query"] for stage in stages]
    provenance = {
        "version": 1,
        "query_budget": budget,
        "planned_queries": planned[:MAX_SCENE_QUERY_BUDGET],
        "executed_queries": executed,
        "planned_query_count": min(len(planned), budget),
        # Logical query strings (hard-capped by the budget) and provider
        # search requests (one logical query may hit several providers/kinds)
        # are tracked separately.
        "logical_queries_executed": len(executed),
        "executed_query_count": len(executed),
        "provider_requests_executed": requests,
        "provider_requests_by_source": {"staged_search": requests},
        "early_stop": stop_reason == "strong_coverage" and len(executed) < min(len(planned), budget),
        "stop_reason": stop_reason,
        "fallback_count": len(fallback_reasons),
        "fallback_reason": fallback_reasons[0] if fallback_reasons else None,
        "fallback_reasons": fallback_reasons,
        "coverage_mode": coverage_targets["mode"],
        "coverage_targets": targets,
        "coverage_before_fallback": coverage_before_fallback,
        "coverage_after_fallback": coverage if fallback_reasons else None,
        "final_coverage": coverage["overall"],
        "stages": [
            {key: stage[key] for key in ("query", "requests", "new", "duplicates", "coverage")}
            | ({"errors": stage["errors"]} if stage["errors"] else {})
            for stage in stages
        ],
        "duplicate_count": duplicates,
        "visual_verification": "ok" if verification_ok else "failed_metadata_fallback",
    }
    return StagedSearchResult(
        ranked,
        list(seen.values()),
        provenance,
        failure,
        set(rows),
        {identity: relevance for identity, (_candidate, relevance) in rows.items()},
        not verification_ok,
    )


def _record_search_winner(provenance: dict[str, Any], metadata: dict[str, Any] | None, source: str) -> dict[str, Any]:
    if metadata is None:
        provenance.update(winning_query=None, winning_asset=None, winning_source=source)
        return provenance
    provenance.update(
        winning_query=metadata.get("query"),
        winning_source=source,
        winning_asset={
            key: metadata.get(key)
            for key in ("identity", "provider", "provider_id", "kind", "source_url")
        },
    )
    return provenance


_SEARCH_TOTAL_KEYS = (
    "scenes_searched",
    "planned_query_count",
    "logical_queries_executed",
    "provider_requests_executed",
    "early_stop_count",
    "fallback_count",
)


def _accumulate_search_totals(totals: dict[str, int], provenance: dict[str, Any]) -> None:
    for key in ("planned_query_count", "logical_queries_executed", "provider_requests_executed", "fallback_count"):
        totals[key] += int(provenance.get(key) or 0)
    totals["early_stop_count"] += int(bool(provenance.get("early_stop")))


def _cache_candidate(
    candidate: MediaCandidate,
    relevance: dict[str, Any],
    *,
    asset_root: Path,
    render_root: Path,
    pexels: Any | None,
    wikimedia: Any,
) -> dict[str, Any]:
    if not is_real_media_allowed(candidate):
        raise MediaProviderError("ineligible_media", "Cards and synthetic placeholders are not allowed.")
    suffix = ".mp4" if candidate.kind == "video" else ".jpg"
    destination = asset_root / candidate.provider / f"{candidate.kind}-{candidate.provider_id}{suffix}"
    downloader = pexels if candidate.provider == "pexels" else wikimedia
    if downloader is None:
        raise MediaProviderError("provider_error", "No downloader is available for this candidate.")
    downloaded = downloader.download(candidate, destination)
    relative = downloaded.relative_to(render_root.resolve()).as_posix()
    return {
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
        "relevance": relevance,
    }


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
    """Attach real media; broaden or reuse real footage, never synthesize cards."""
    assets = state.setdefault("assets", {})
    assets.setdefault("license_manifest", [])
    pexels = client or (PexelsMediaClient(settings.pexels_api_key) if settings.pexels_api_key else None)
    wikimedia = fallback_client or WikimediaMediaClient()
    asset_root = settings.render_root.resolve() / project_id / "assets"
    portrait = int(state["timeline"]["height"]) >= int(state["timeline"]["width"])
    used: set[str] = set()
    manifest: list[dict[str, Any]] = []
    selected_count = 0
    missing_media_count = 0
    replacement_failed_count = 0
    failure: MediaProviderError | None = None
    selected_media: list[dict[str, Any]] = []
    search_totals = {key: 0 for key in _SEARCH_TOTAL_KEYS}
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
            if identity and path.is_file() and is_real_media_allowed(existing) and scene.get("asset_status") != "replacement_required":
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
            and is_real_media_allowed(existing)
        )

        query_plan = build_visual_query_plan(scene, state)
        queries = query_plan["queries"]
        scene["search_queries"] = queries
        scene["visual_query_plan"] = {
            key: value
            for key, value in query_plan.items()
            if key != "queries"
        }
        duration = max(1.0, float(scene.get("end", 0)) - float(scene.get("start", 0)))
        preferred_kind = str(scene.get("preferred_media") or "video")
        if preferred_kind not in {"video", "photo"}:
            preferred_kind = "video"
        metadata: dict[str, Any] | None = None
        commons_candidates: list[MediaCandidate] = []
        # Staged search: query 1, verify, stop when coverage is strong,
        # otherwise spend the bounded budget on the still-weak subject.
        staged = run_staged_scene_search(
            queries,
            scene,
            state,
            query_plan,
            pexels=pexels,
            wikimedia=wikimedia,
            preferred_kind=preferred_kind,
            portrait=portrait,
            scene_duration=duration,
            used=used,
            verifier=visual_verifier,
        )
        search_provenance = staged.provenance
        scene["media_search"] = search_provenance
        search_totals["scenes_searched"] += 1
        failure = staged.failure or failure
        pexels_candidates = staged.candidates if pexels is not None else []
        commons_candidates = [] if pexels is not None else list(staged.candidates)
        winning_source = "staged_search"
        for candidate, relevance in staged.ranked:
            try:
                metadata = _cache_candidate(
                    candidate,
                    relevance,
                    asset_root=asset_root,
                    render_root=settings.render_root,
                    pexels=pexels,
                    wikimedia=wikimedia,
                )
                break
            except MediaProviderError as exc:
                failure = exc

        # One verifier for every fallback of this scene; a verifier that already
        # failed in the staged search is not retried.
        scene_verifier = _METADATA_ONLY_VERIFIER if staged.verifier_failed else visual_verifier

        # A failed Pexels download must not skip the remaining free source.
        # Only already executed query strings are reused: no new logical query.
        if metadata is None and pexels is not None:
            winning_source = "wikimedia_fallback"
            for query in list(search_provenance["executed_queries"]):
                _count_provider_request(search_provenance, "wikimedia_fallback")
                try:
                    commons_candidates.extend(
                        wikimedia.search_photos(query, portrait=portrait)
                    )
                except MediaProviderError as exc:
                    failure = exc
            search_provenance["wikimedia_fallback"] = True
            try:
                wikimedia_ranked = _rank_verified(
                    commons_candidates, scene, state, preferred_kind, used | staged.evaluated, scene_verifier
                )
            except Exception:  # noqa: BLE001 - verification must never fail media search
                search_provenance["visual_verification"] = "failed_metadata_fallback"
                scene_verifier = _METADATA_ONLY_VERIFIER
                wikimedia_ranked = _rank_verified(
                    commons_candidates, scene, state, preferred_kind, used | staged.evaluated, scene_verifier
                )
            for candidate, relevance in wikimedia_ranked:
                try:
                    metadata = _cache_candidate(
                        candidate,
                        relevance,
                        asset_root=asset_root,
                        render_root=settings.render_root,
                        pexels=pexels,
                        wikimedia=wikimedia,
                    )
                    break
                except MediaProviderError as exc:
                    failure = exc

        if metadata is None:
            # Relevance is relaxed only here; the presentation/source gate never is.
            winning_source = "relaxed_fallback"
            search_provenance["relaxed_fallback"] = True
            pools = [pexels_candidates + commons_candidates]
            protected = set(query_plan.get("protected_entities") or [])
            # Broad strings are payoff-safe and only spend logical budget that
            # the staged search left unused; re-running an executed string on
            # the same providers would only return the pool already searched.
            broad_queries = [
                query
                for query in dict.fromkeys(
                    " ".join(word for word in query.split() if not _visual_query_tokens(word) & protected)
                    for query in (
                        " ".join(queries[0].split()[:2]) if queries else "nature",
                        global_subject_text(state), "nature landscape", "ocean water", "trees outdoors",
                    )
                )
                if query and query not in search_provenance["executed_queries"]
            ]
            visual = scene_verifier or get_visual_verifier()
            verified_rows = staged.verified
            for broad_query in [None, *broad_queries]:
                batch = pools[0] if broad_query is None else []
                if broad_query:
                    if not _claim_logical_query(search_provenance, broad_query):
                        break  # logical query budget exhausted
                    search_provenance.setdefault("relaxed_queries", []).append(broad_query)
                    for provider in (pexels, wikimedia):
                        if provider is None:
                            continue
                        _count_provider_request(search_provenance, "relaxed_fallback")
                        try:
                            batch.extend(provider.search_photos(broad_query, portrait=portrait))
                        except MediaProviderError as exc:
                            failure = exc
                for candidate in batch[:24]:
                    if candidate.identity in used or not is_real_media_allowed(candidate):
                        continue
                    known = verified_rows.get(candidate.identity)
                    if known is not None:
                        if (known.get("visual") or {}).get("presentation_risk") or (
                            (known.get("presentation_risk") or {}).get("source") == "vision"
                        ):
                            continue
                    elif getattr(visual, "status", "") == "available":
                        try:
                            result = visual.verify_candidate(candidate, visual_intent_text(scene, state))
                        except Exception:  # noqa: BLE001 - verification is advisory here
                            result = None
                        if result is not None and result.presentation_risk:
                            continue
                    try:
                        relevance = media_relevance(candidate, scene, state)
                        relevance["fallback_stage"] = "real_media_only_relaxed_fit"
                        metadata = _cache_candidate(candidate, relevance, asset_root=asset_root, render_root=settings.render_root, pexels=pexels, wikimedia=wikimedia)
                        break
                    except MediaProviderError as exc:
                        failure = exc
                if metadata is not None:
                    break
        _record_search_winner(search_provenance, metadata, winning_source)
        _accumulate_search_totals(search_totals, search_provenance)
        if metadata is None:
            safe_selected = [item for item in selected_media if is_real_media_allowed(item)]
            related = _related_media(queries, safe_selected) or next(iter(safe_selected), None)
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
        if metadata is None:
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
            scene["asset_status"] = "real_media_unavailable"
            scene["fallback_reason"] = (
                str(failure)
                if failure
                else "No relevant real media was found after staged search."
            )
            scene.pop("media", None)
            missing_media_count += 1
            report_progress(
                progress,
                "media",
                "Finding visuals",
                completed_units=scene_index,
                total_units=total_scenes,
            )
            continue

        candidate_identity = str(metadata["identity"])
        scene["media"] = metadata
        scene["preferred_media"] = metadata["kind"]
        scene["asset_status"] = f"{metadata['kind']}_ready"
        used.add(candidate_identity)
        manifest.append(metadata)
        selected_media.append(metadata)
        selected_count += 1
        report_progress(
            progress,
            "media",
            "Finding visuals",
            completed_units=scene_index,
            total_units=total_scenes,
        )

    # Earlier scenes may reuse a real asset discovered for a later scene.
    if selected_media:
        for scene in scenes:
            if scene.get("asset_status") == "real_media_unavailable":
                scene["media"] = dict(selected_media[0])
                scene["asset_status"] = "real_media_reused"
                manifest.append(scene["media"])
                selected_count += 1
                missing_media_count -= 1
    assets["license_manifest"] = manifest
    assets["selected_count"] = selected_count
    assets.pop("generated_card_count", None)
    assets["missing_media_count"] = missing_media_count
    if search_totals["scenes_searched"]:
        # Compact internal QA accounting only; not surfaced in the UI.
        assets["media_search_summary"] = search_totals
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
    elif selected_count and missing_media_count:
        assets.update(
            status="partial_fallback",
            provider=_provider_summary(manifest),
            diagnostic=f"{missing_media_count} scene(s) have no real media; rendering is blocked.",
        )
    elif selected_count:
        assets.update(status="media_ready", provider=_provider_summary(manifest), diagnostic=None)
    else:
        assets.update(
            status="fallback_only",
            provider="pexels+wikimedia" if settings.pexels_api_key else "wikimedia",
            diagnostic="No real media is available. Rendering is blocked; retry free media discovery.",
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
    # Compare the focused scene query only; the final broad topic query is a
    # plausibility fallback and must not make unrelated scenes look reusable.
    desired = _query_terms(queries[0]) if queries else set()
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
