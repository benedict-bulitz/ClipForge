"""Final visual quality gate before render.

Every scene must have a valid visual whose recorded judgement (for this
destination scene) meets the semantic and factual floors, with acceptable
rights, adequate resolution, no unsafe repeat and a valid transformation
plan. A failing scene is handed back to the existing permitted fallback chain
(alternate candidate / generated image / reuse / graphic) instead of being
silently rendered as weak filler. User-locked visuals are reported, never
replaced.
"""

from __future__ import annotations

from typing import Any

from .visual_judge import FACTUAL_FLOOR, MIN_SHORT_SIDE, SEMANTIC_FLOOR

VERSION = 1
# Reuse/continuity statuses are deliberate decisions of the existing
# destination gates, not accidental repeats.
DELIBERATE_REUSE = {"related_media_reused", "real_media_reused", "generated_media_reused", "block_visual_continued"}
# Failures that justify replacing an asset at render admission.
REPLACEABLE = {
    "missing_visual", "rights_not_usable", "judge_rejected", "semantic_below_floor", "factual_below_floor",
    "resolution_too_low", "unsafe_repeat", "transform_invalid",
}


def user_owned(scene: dict[str, Any]) -> bool:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    director = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
    return bool(scene.get("user_locked_visual") or media.get("manually_selected") or director.get("manually_selected"))


def evaluate_scene(scene: dict[str, Any], state: dict[str, Any], settings: Any | None = None) -> dict[str, Any]:
    from .media import (
        REAL_MEDIA_PROVIDERS,
        cached_scene_asset_path,
        is_scene_asset_allowed,
        media_source,
        scene_acceptance_key,
    )
    from .visual_rights import evaluate_rights
    from .visual_transform import plan_transformation

    media = scene.get("media") if isinstance(scene.get("media"), dict) else None
    checks: dict[str, Any] = {}
    failures: list[str] = []
    if not media or (settings is not None and cached_scene_asset_path(media, settings) is None):
        checks["visual_exists"] = False
        return {"version": VERSION, "passed": False, "checks": checks, "failures": ["missing_visual"]}
    checks["visual_exists"] = True
    source = media_source(media)
    real = source in REAL_MEDIA_PROVIDERS
    if real:
        status = evaluate_rights(media.get("rights")).status
        checks["license"] = status
        if status != "usable":
            failures.append("rights_not_usable")
    else:
        checks["license"] = "provenance" if is_scene_asset_allowed(media) else "invalid_provenance"
        if not is_scene_asset_allowed(media):
            failures.append("rights_not_usable")
    judge = (media.get("relevance") or {}).get("judge") if isinstance(media.get("relevance"), dict) else None
    same_destination = media.get("acceptance_scene_key") == scene_acceptance_key(scene, state)
    if real and isinstance(judge, dict) and same_destination:
        scores = judge.get("scores") or {}
        checks["semantic_match"] = scores.get("semantic_match")
        checks["factual_match"] = scores.get("factual_match")
        checks["judge_final_score"] = judge.get("final_score")
        if judge.get("reject"):
            failures.append("judge_rejected")
        if float(scores.get("semantic_match") or 0) < SEMANTIC_FLOOR:
            failures.append("semantic_below_floor")
        if float(scores.get("factual_match") or 0) < FACTUAL_FLOOR:
            failures.append("factual_below_floor")
    else:
        checks["judgement"] = "not_applicable" if not real else "no_destination_judgement"
    if real:
        short = min(int(media.get("width") or 0), int(media.get("height") or 0))
        checks["short_side"] = short
        if short and short < MIN_SHORT_SIDE:
            failures.append("resolution_too_low")
    identity = str(media.get("identity") or "")
    repeats = []
    if identity and scene.get("asset_status") not in DELIBERATE_REUSE:
        for other in state.get("scenes") or []:
            if other is scene or not isinstance(other, dict) or not isinstance(other.get("media"), dict):
                continue
            if str(other["media"].get("identity") or "") != identity:
                continue
            if other.get("asset_status") in DELIBERATE_REUSE or (scene.get("block_id") and other.get("block_id") == scene.get("block_id")):
                continue
            repeats.append(str(other.get("id") or ""))
    checks["repeats"] = repeats
    if repeats:
        # Only the later occurrence is unsafe; the first keeps its asset.
        scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
        position = next((i for i, item in enumerate(scenes) if item is scene), 0)
        earlier = {str(item.get("id") or "") for item in scenes[:position]}
        if earlier & set(repeats):
            failures.append("unsafe_repeat")
    plan = plan_transformation(scene, state)
    checks["transform_valid"] = bool(plan and plan["valid"])
    if plan and not plan["valid"] and real:
        failures.append("transform_invalid")
    return {"version": VERSION, "passed": not failures, "checks": checks, "failures": list(dict.fromkeys(failures))}


def needs_replacement(gate: dict[str, Any], scene: dict[str, Any]) -> bool:
    return not gate["passed"] and not user_owned(scene) and bool(set(gate["failures"]) & REPLACEABLE)


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    scenes = [scene for scene in state.get("scenes") or [] if isinstance(scene, dict)]
    failures: dict[str, int] = {}
    for scene in scenes:
        for reason in (scene.get("visual_gate") or {}).get("failures") or []:
            failures[reason] = failures.get(reason, 0) + 1
    summary = {
        "version": VERSION,
        "scenes": len(scenes),
        "passed": sum(1 for scene in scenes if (scene.get("visual_gate") or {}).get("passed")),
        "failures": failures,
    }
    state["visual_quality_gate"] = summary
    return summary
