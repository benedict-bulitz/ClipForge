from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .config import Settings
from .hashing import attach_hashes
from .image_generation import get_image_generator, model_label, quality_label
from .media import (
    MediaCandidate,
    MediaProviderError,
    PexelsMediaClient,
    WikimediaMediaClient,
    _optional_real_clients,
    build_visual_query_plan,
    candidate_reveals_protected,
    derive_search_queries,
    is_real_media_allowed,
    media_relevance,
    protected_candidate_terms,
    real_media_quality_gate,
    verify_media_shortlist,
)
from .renderer import RenderUnavailable, replace_scene_video
from .services import (
    RevisionConflict,
    _append_revision,
    _next_revision_number,
    effective_revision_state,
)

MAX_CANDIDATES = 8
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
        if is_real_media_allowed(candidate) and candidate.identity not in used:
            unique.setdefault(candidate.identity, candidate)
    verified = list(unique.values())
    if scene is not None:
        # This is an acceptance gate, not merely a ranking input.  Unknown
        # metadata may be promoted later by strong scene-level visual evidence,
        # but it must not be presented as a selectable fallback by itself.
        verified = [
            item
            for item in verified
            if media_relevance(item, scene, state).get("confidence") in {"high", "acceptable", "unknown"}
        ]

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
    extra_clients: list[Any] | None = None,
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
        for item in [scene]
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
    eligible: dict[str, MediaCandidate] = {}
    checked: set[str] = set()
    # Same Story Arc reveal protection as automatic selection.
    protected_terms = protected_candidate_terms(state, build_visual_query_plan(scene, state))
    def accept_new() -> None:
        ordered = _ordered(found, preferred, used | checked, scene, state)
        for offset in range(0, min(len(ordered), 24), 6):
            batch = ordered[offset:offset + 6]
            checked.update(item.identity for item in batch)
            for candidate, relevance in verify_media_shortlist(batch, scene, state, visual_verifier):
                # The same final quality gate as automatic selection.
                if (
                    relevance.get("confidence") in {"high", "acceptable"}
                    and real_media_quality_gate(candidate, relevance)[0]
                    and not candidate_reveals_protected(candidate, protected_terms)
                ):
                    eligible.setdefault(candidate.identity, candidate)
            if len(eligible) >= limit:
                break

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
        accept_new()
        if len(eligible) < limit and pexels is not None:
            search = pexels.search_photos if preferred == "video" else pexels.search_videos
            kwargs = {"portrait": portrait}
            if search.__name__ == "search_videos":
                kwargs["scene_duration"] = scene_duration
            try:
                found.extend(search(query, **kwargs))
            except MediaProviderError:
                pass
        accept_new()
        if len(eligible) >= limit:
            break
    # Optional free providers (Pixabay) use the same bounded query list.
    extras = _optional_real_clients(settings) if extra_clients is None else list(extra_clients)
    for extra in extras:
        if len(eligible) >= limit:
            break
        for query in queries:
            try:
                if preferred == "video" and hasattr(extra, "search_videos"):
                    found.extend(extra.search_videos(query, portrait=portrait, scene_duration=scene_duration))
                else:
                    found.extend(extra.search_photos(query, portrait=portrait))
            except MediaProviderError:
                pass
            accept_new()
            if len(eligible) >= limit:
                break
    if extra_clients is None:
        for extra in extras:
            extra.close()
    # Wikimedia is a photo fallback and is queried only when the bounded Pexels pass is short.
    if len(eligible) < limit:
        for query in queries:
            try:
                found.extend(wikimedia.search_photos(query, portrait=portrait))
            except MediaProviderError:
                pass
            accept_new()
            if len(eligible) >= limit:
                break
    selected = tuple(eligible.values())[: max(1, min(limit, MAX_CANDIDATES))]
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
        "preview_url": candidate.download_url if candidate.kind == "video" else candidate.preview_url or candidate.download_url,
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
        if not index_text.isdecimal():
            raise ValueError("Invalid candidate index")
        candidate = candidate_set.candidates[int(index_text)]
    except (ValueError, IndexError):
        raise CandidateError("That media candidate is not part of the fetched alternatives.")
    downloader = client if candidate.provider == "pexels" else fallback_client
    if not is_real_media_allowed(candidate):
        raise CandidateError("Only real images and videos may replace a scene.")
    if candidate.provider == "pixabay" and downloader is None:
        downloader = next((extra for extra in _optional_real_clients(settings) if extra.provider == "pixabay"), None)
        if downloader is None:
            raise CandidateError("Pixabay is not configured.")
    if downloader is None:
        downloader = PexelsMediaClient(settings.pexels_api_key) if candidate.provider == "pexels" and settings.pexels_api_key else WikimediaMediaClient()
    suffix = ".mp4" if candidate.kind == "video" else ".jpg"
    destination = settings.render_root.resolve() / project.id / "replacements" / candidate.provider / f"{candidate.kind}-{candidate.provider_id}{suffix}"
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
        "source": candidate.provider,
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
        "manually_selected": True,
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
        _mark_manual(scene, "real_media_user_selected", f"stock_{candidate.kind}")
        assets = state.setdefault("assets", {})
        manifest = [item for item in assets.get("license_manifest", []) if item.get("identity") != candidate.identity]
        manifest.append(metadata)
        assets["license_manifest"] = manifest
        assets["status"] = "media_ready"
        return f"Applied {candidate.kind} media {candidate.provider_id} to scene {scene_number}."

    state = effective_revision_state(project)
    mutate(state)
    if auto_render:
        try:
            replace_scene_video(state, project.id, project.title, scene_number, _next_revision_number(db, project.id), settings)
        except RenderUnavailable as exc:
            raise CandidateError(str(exc)) from exc
    else:
        state.setdefault("render", {}).update(status="regeneration_required", stale=True)
    result = _append_revision(db, project, instruction=f"Choose media for scene {scene_number}", state=attach_hashes(state), changed=["assets", "scenes", "render"], base_revision=project.current_revision, status="rendered" if auto_render else "ready_for_production")
    _SETS.pop(set_token, None)
    return result


def _mark_manual(scene: dict[str, Any], reason: str, resolved_type: str) -> None:
    director = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
    director.update(
        decision="ACCEPTED_REAL" if resolved_type.startswith("stock_") else "GENERATE_FALLBACK",
        resolved_type=resolved_type,
        decision_reason=reason,
        manually_selected=True,
    )
    scene["visual_director"] = director


def scene_generation_option(state: dict[str, Any], scene_number: int, settings: Settings) -> dict[str, Any]:
    """What the Change Media UI may offer for AI generation; never exposes secrets."""
    from .visual_director import build_generation_prompt, generation_counts, plan_scene_strategy

    scenes = state.get("scenes") or []
    if scene_number < 1 or scene_number > len(scenes):
        raise CandidateError("Scene not found.")
    scene = scenes[scene_number - 1]
    model = settings.generated_image_model
    quality = settings.generated_image_quality
    policy = (state.get("visual_director") or {}).get("policy") if isinstance(state.get("visual_director"), dict) else None
    if isinstance(policy, dict):
        quality = str(policy.get("generated_image_quality") or quality)
    strategy = plan_scene_strategy(scene, state, build_visual_query_plan(scene, state))
    built = build_generation_prompt(scene, state, strategy)
    available = bool(settings.openai_api_key) and built is not None
    reason = None if available else ("no_api_key" if not settings.openai_api_key else "no_reveal_safe_subject")
    return {
        "available": available,
        "unavailable_reason": reason,
        "model": model,
        "model_label": model_label(model),
        "quality": quality,
        "quality_label": quality_label(quality),
        "uses_paid_credits": True,
        "prompt": built["prompt"] if built else "",
        "prompt_summary": built["summary"] if built else "",
        "generated_images": generation_counts(state)["generated_images"],
    }


def generate_scene_media(
    db: Session,
    project: Any,
    scene_number: int,
    settings: Settings,
    *,
    prompt: str | None = None,
    generator: Any | None = None,
    visual_verifier: Any | None = None,
    auto_render: bool = True,
) -> Any:
    """Manual, user-confirmed generation through the shared director path."""
    from .visual_director import GENERATED_IMAGE, generate_scene_image, plan_scene_strategy
    from .visual_verifier import get_visual_verifier

    generator = generator or get_image_generator(settings, automatic=False)
    if generator is None:
        raise CandidateError("AI image generation needs an OpenAI API key in Settings.")
    state = effective_revision_state(project)
    scenes = state.get("scenes") or []
    if scene_number < 1 or scene_number > len(scenes):
        raise CandidateError("Scene not found.")
    scene = scenes[scene_number - 1]
    query_plan = build_visual_query_plan(scene, state)
    scene.setdefault("search_queries", query_plan["queries"])
    scene.setdefault("visual_query_plan", {key: value for key, value in query_plan.items() if key != "queries"})
    strategy = plan_scene_strategy(scene, state, query_plan)
    metadata, record = generate_scene_image(
        scene,
        state,
        strategy,
        project_id=project.id,
        settings=settings,
        generator=generator,
        verifier=visual_verifier or get_visual_verifier(),
        trigger="manual",
        reason="user_requested",
        prompt_override=prompt,
    )
    if metadata is None:
        messages = {
            "skipped_no_safe_prompt": "No safe visual prompt is available for this scene before its reveal.",
            "rejected": "The generated image did not match the scene and was discarded.",
            "failed": "Image generation failed; the current scene is unchanged.",
        }
        if record.get("billed"):
            # A paid but rejected image stays auditable; the scene is unchanged.
            _append_revision(
                db, project, instruction=f"AI image attempt for scene {scene_number}", state=attach_hashes(state),
                changed=["assets"], base_revision=project.current_revision, status=project.status, kind="system",
            )
        raise CandidateError(messages.get(record.get("status"), "Image generation did not produce a usable image."))
    strategy["generation"] = {"status": record["status"], "model": record.get("model"), "quality": record.get("quality")}
    scene["visual_director"] = strategy
    scene["media"] = metadata
    scene["asset_status"] = "generated_image_ready"
    scene.pop("fallback_reason", None)
    _mark_manual(scene, "user_generated_image", GENERATED_IMAGE)
    assets = state.setdefault("assets", {})
    manifest = [item for item in assets.get("license_manifest", []) if item.get("identity") != metadata["identity"]]
    manifest.append(metadata)
    assets["license_manifest"] = manifest
    if auto_render:
        try:
            replace_scene_video(state, project.id, project.title, scene_number, _next_revision_number(db, project.id), settings)
        except RenderUnavailable as exc:
            raise CandidateError(str(exc)) from exc
    else:
        state.setdefault("render", {}).update(status="regeneration_required", stale=True)
    return _append_revision(
        db, project, instruction=f"Generate AI image for scene {scene_number}", state=attach_hashes(state),
        changed=["assets", "scenes", "render"], base_revision=project.current_revision,
        status="rendered" if auto_render else "ready_for_production",
    )
