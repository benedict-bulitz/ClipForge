from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .config import Settings
from .media import (
    MediaCandidate,
    MediaProviderError,
    PexelsMediaClient,
    WikimediaMediaClient,
    derive_search_queries,
    media_relevance,
    verify_media_shortlist,
)
from .services import RevisionConflict, mutate_project_state

MAX_CANDIDATES = 5
MAX_SETS = 64
CANDIDATE_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class CandidateSet:
    project_id: str
    scene_number: int
    base_revision: int
    candidates: tuple[MediaCandidate, ...]
    created_at: float


_SETS: dict[str, CandidateSet] = {}


class CandidateError(RuntimeError):
    pass


def _prune() -> None:
    now = time.monotonic()
    expired = [key for key, value in _SETS.items() if now - value.created_at > CANDIDATE_TTL_SECONDS]
    for key in expired:
        _SETS.pop(key, None)
    while len(_SETS) > MAX_SETS:
        _SETS.pop(next(iter(_SETS)))


def clear_candidate_sets() -> None:
    _SETS.clear()


def _ordered(candidates: list[MediaCandidate], preferred: str, used: set[str], scene: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> list[MediaCandidate]:
    unique: dict[str, MediaCandidate] = {}
    for candidate in candidates:
        if candidate.identity not in used:
            unique.setdefault(candidate.identity, candidate)
    verified = list(unique.values())
    if scene is not None:
        verified = [item for item in verified if media_relevance(item, scene, state).get("confidence") != "rejected"]

    def key(item: MediaCandidate) -> tuple[int, float, int, float, str, str]:
        relevance = media_relevance(item, scene, state) if scene is not None else {"score": 0}
        confidence_weight = {"high": 3, "acceptable": 2, "unknown": 1, "rejected": 0}.get(relevance.get("confidence"), 0)
        return (confidence_weight, float(relevance["score"]), int(item.kind == preferred), item.rank, item.provider, item.provider_id)
    return sorted(verified, key=key, reverse=True)


def discover_scene_media_candidates(
    state: dict[str, Any],
    project_id: str,
    scene_number: int,
    base_revision: int,
    settings: Settings,
    *,
    client: Any | None = None,
    fallback_client: Any | None = None,
    limit: int = MAX_CANDIDATES,
    visual_verifier: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    scenes = state.get("scenes") or []
    if scene_number < 1 or scene_number > len(scenes):
        raise CandidateError("Scene not found.")
    scene = scenes[scene_number - 1]
    existing = scene.get("media") if isinstance(scene.get("media"), dict) else None
    preferred = str((existing or {}).get("kind") or scene.get("preferred_media") or "video")
    if preferred not in {"video", "photo"}:
        preferred = "video"
    used = {
        str(media.get("identity"))
        for item in scenes
        if isinstance(item, dict)
        for media in [item.get("media")]
        if isinstance(media, dict) and media.get("identity")
    }
    # One focused query keeps this scene-level browser bounded; providers already return a ranked page.
    queries = derive_search_queries(scene, state)[:3] or [str(scene.get("visual_goal") or "cinematic")]
    pexels = client or (PexelsMediaClient(settings.pexels_api_key) if settings.pexels_api_key else None)
    wikimedia = fallback_client or WikimediaMediaClient()
    found: list[MediaCandidate] = []
    portrait = int(state["timeline"]["height"]) >= int(state["timeline"]["width"])
    scene_duration = float(scene["end"] - scene["start"])
    for query in queries:
        if pexels is not None:
            search = pexels.search_videos if preferred == "video" else pexels.search_photos
            kwargs = {"portrait": portrait}
            if preferred == "video":
                kwargs["scene_duration"] = scene_duration
            try:
                found.extend(search(query, **kwargs))
            except MediaProviderError:
                pass
        if len(_ordered(found, preferred, used, scene, state)) < limit and pexels is not None:
            search = pexels.search_photos if preferred == "video" else pexels.search_videos
            kwargs = {"portrait": portrait}
            if search.__name__ == "search_videos":
                kwargs["scene_duration"] = scene_duration
            try:
                found.extend(search(query, **kwargs))
            except MediaProviderError:
                pass
        if len(_ordered(found, preferred, used, scene, state)) >= limit:
            continue
    # Wikimedia is a photo fallback and is queried only when the bounded Pexels pass is short.
    if len(_ordered(found, preferred, used, scene, state)) < limit:
        for query in queries:
            try:
                found.extend(wikimedia.search_photos(query, portrait=portrait))
            except MediaProviderError:
                pass
    ordered = _ordered(found, preferred, used, scene, state)
    verified = verify_media_shortlist(ordered, scene, state, visual_verifier)
    selected = tuple(
        candidate
        for candidate, relevance in verified
        if relevance.get("confidence") != "rejected"
    )[: max(1, min(limit, MAX_CANDIDATES))]
    _prune()
    token = uuid.uuid4().hex
    _SETS[token] = CandidateSet(project_id, scene_number, base_revision, selected, time.monotonic())
    return token, [serialize_candidate(f"{token}:{index}", candidate) for index, candidate in enumerate(selected)]


def serialize_candidate(token: str, candidate: MediaCandidate, selected: bool = False) -> dict[str, Any]:
    return {
        "token": token,
        "provider": candidate.provider,
        "provider_id": candidate.provider_id,
        "kind": candidate.kind,
        "preview_url": candidate.preview_url or candidate.download_url,
        "verification_url": candidate.verification_url,
        "source_url": candidate.source_url,
        "creator": candidate.creator,
        "creator_url": candidate.creator_url,
        "query": candidate.query,
        "width": candidate.width,
        "height": candidate.height,
        "duration": candidate.duration,
        "selected": selected,
        "title": candidate.title,
        "description": candidate.description,
        "tags": list(candidate.tags),
    }


def apply_scene_media_candidate(
    db: Session,
    project: Any,
    scene_number: int,
    token: str,
    settings: Settings,
    *,
    client: Any | None = None,
    fallback_client: Any | None = None,
    auto_render: bool = True,
) -> Any:
    _prune()
    set_token, _, index_text = token.partition(":")
    candidate_set = _SETS.get(set_token)
    if candidate_set is None or candidate_set.project_id != project.id or candidate_set.scene_number != scene_number:
        raise CandidateError("That media candidate is no longer available. Fetch alternatives again.")
    if candidate_set.base_revision != project.current_revision:
        raise RevisionConflict("Project changed since alternatives were fetched; reload before applying.")
    try:
        candidate = candidate_set.candidates[int(index_text)]
    except (ValueError, IndexError):
        raise CandidateError("That media candidate is not part of the fetched alternatives.")
    downloader = client if candidate.provider == "pexels" else fallback_client
    if downloader is None:
        downloader = PexelsMediaClient(settings.pexels_api_key) if candidate.provider == "pexels" and settings.pexels_api_key else WikimediaMediaClient()
    suffix = ".mp4" if candidate.kind == "video" else ".jpg"
    destination = settings.render_root.resolve() / project.id / "assets" / candidate.provider / f"{candidate.kind}-{candidate.provider_id}{suffix}"
    try:
        downloaded = downloader.download(candidate, destination)
        if not downloaded.is_file() or downloaded.stat().st_size <= 0:
            raise MediaProviderError("provider_error", "The selected media could not be cached.")
    except (MediaProviderError, OSError) as exc:
        raise CandidateError(str(exc)) from exc
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
    }

    def mutate(state: dict[str, Any]) -> str:
        scenes = state.get("scenes") or []
        if scene_number < 1 or scene_number > len(scenes):
            raise CandidateError("Scene not found.")
        scene = scenes[scene_number - 1]
        scene["media"] = metadata
        scene["preferred_media"] = candidate.kind
        scene["asset_status"] = f"{candidate.kind}_ready"
        scene.pop("fallback_reason", None)
        assets = state.setdefault("assets", {})
        manifest = [item for item in assets.get("license_manifest", []) if item.get("identity") != candidate.identity]
        manifest.append(metadata)
        assets["license_manifest"] = manifest
        assets["status"] = "media_ready"
        return f"Applied {candidate.kind} media {candidate.provider_id} to scene {scene_number}."

    result = mutate_project_state(
        db, project, instruction=f"Choose media for scene {scene_number}", changed_roots={"assets"}, mutate=mutate,
        settings=settings, base_revision=project.current_revision, auto_render=auto_render,
    )
    _SETS.pop(token, None)
    return result
