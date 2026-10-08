"""When AI-generated imagery may (and should) replace retrieved real media.

Real, licensed material is the default. A generated image is preferred only
when the best real candidate is weak AND the scene does not depend on showing
a specific real person, place, object, event or scientific observation. A
generated image is never made of a real, named person, and a historical
event is only ever generated as a clearly illustrative reconstruction, never
as a fake archival photograph.
"""

from __future__ import annotations

from typing import Any

from .visual_search_planner import sensitivity as scene_sensitivity

FACTUAL_SENSITIVITIES = {"historical_event", "real_place_or_object", "scientific_specific"}


def generation_restriction(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """What kind of generated image this scene may receive at all."""
    level = scene_sensitivity(scene, state)
    if level == "real_person":
        return {"allowed": False, "style": None, "reason": "real_person_never_generated", "sensitivity": level}
    if level == "historical_event":
        return {"allowed": True, "style": "illustrative_reconstruction", "reason": "historical_event_illustrative_only", "sensitivity": level}
    return {"allowed": True, "style": "photographic", "reason": "generation_permitted", "sensitivity": level}


def ai_fallback_decision(
    scene: dict[str, Any],
    state: dict[str, Any],
    winner_judge: dict[str, Any] | None,
    *,
    generation_available: bool,
) -> dict[str, Any]:
    """Whether to try a generated image BEFORE accepting the best real candidate.

    ``prefer`` = generate first, keep the real candidate as the fallback.
    """
    restriction = generation_restriction(scene, state)
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    abstract = str(intent.get("visual_strategy") or "") in {"process", "diagram_or_card"}
    base = {"sensitivity": restriction["sensitivity"], "abstract": abstract}
    if not restriction["allowed"]:
        return {**base, "prefer": False, "reason": restriction["reason"]}
    if winner_judge is None:
        return {**base, "prefer": generation_available, "reason": "no_factually_safe_real_visual"}
    weak = bool(winner_judge.get("weak")) or winner_judge.get("confidence") == "low"
    if not weak:
        return {**base, "prefer": False, "reason": "real_media_strong_enough"}
    if restriction["sensitivity"] in FACTUAL_SENSITIVITIES:
        # A weak but genuine view of the real subject beats an invented one.
        return {**base, "prefer": False, "reason": "factual_scene_prefers_real_evidence"}
    if not generation_available:
        return {**base, "prefer": False, "reason": "generation_unavailable"}
    return {
        **base,
        "prefer": True,
        "reason": "abstract_concept_weak_real_media" if abstract else "low_retrieval_confidence",
        "weak_score": winner_judge.get("final_score"),
    }
