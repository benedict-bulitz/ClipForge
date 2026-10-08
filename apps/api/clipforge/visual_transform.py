"""Smart transformation planning: how a chosen asset becomes Shorts-ready.

Every decision must support the narration; nothing is added for decoration.
The plan is structured and validated. The renderer applies what it supports
(subject-aware 9:16 crop via smart_crop, bounded still motion); techniques
the renderer does not implement yet (subject tracking for video, 2.5D
parallax, subject isolation, outpainting) are planned only where they would
be appropriate and are marked ``applied: false`` so diagnostics never claim
an effect that was not rendered. Outpainting is never planned for real
evidence (archival, scientific, documentary): it would invent pixels.
"""

from __future__ import annotations

from typing import Any

from .visual_judge import MIN_REFRAME_LONG_PIXELS, reframe_geometry

VERSION = 1
_STATIC_SOURCES = {"simple_graphic"}
_NEXT_MOVE = {"push_in": "pan", "pan": "pull_out", "pull_out": "push_in", "static": "push_in"}


def _portrait(state: dict[str, Any]) -> bool:
    timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    try:
        return int(timeline.get("height") or 1920) >= int(timeline.get("width") or 1080)
    except (TypeError, ValueError):
        return True


def plan_transformation(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else None
    if not media:
        return None
    from .media import REAL_MEDIA_PROVIDERS, media_source

    source = media_source(media)
    kind = str(media.get("kind") or "")
    strategy = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    role = str(strategy.get("visual_role") or strategy.get("story_role") or "")
    geometry = reframe_geometry(int(media.get("width") or 0), int(media.get("height") or 0), portrait=_portrait(state))
    real = source in REAL_MEDIA_PROVIDERS
    from .visual_judge import _style_family

    archival = real and _style_family(media) == "archival"
    steps: list[dict[str, Any]] = []
    reasons: list[str] = []

    if source in _STATIC_SOURCES:
        reframe = "native_frame"
        reasons.append("graphic_rendered_at_timeline_size")
    elif not geometry.get("known"):
        reframe = "smart_reframe"
        reasons.append("dimensions_unknown_subject_crop_at_render")
    elif geometry["retained_share"] >= 0.95:
        reframe = "native_vertical"
    else:
        reframe = "smart_reframe"
        reasons.append(f"{geometry['orientation']}_source_keeps_{geometry['retained_share']:.0%}_after_9x16_crop")
    steps.append({"technique": reframe, "applied": True})

    if kind == "video" and reframe == "smart_reframe":
        # The renderer crops around one subject-aware focal point; continuous
        # tracking is planned but not implemented.
        steps.append({"technique": "subject_tracking", "applied": False, "fallback": "static_subject_focal_crop"})

    pattern = None
    if kind == "photo" and source not in _STATIC_SOURCES:
        # Speed stays with pacing (scene["motion"]); the plan picks the move.
        text_heavy = str(intent.get("visual_strategy") or "") == "diagram_or_card"
        scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
        position = next((i for i, item in enumerate(scenes) if item is scene), None)
        previous = scenes[position - 1] if position else None
        continued = bool(
            previous and isinstance(previous.get("media"), dict) and media.get("identity")
            and previous["media"].get("identity") == media.get("identity")
        )
        if text_heavy:
            pattern, why = "static", "explanatory_detail_must_stay_readable"
        elif continued:
            # The same base continues: always a different move than the previous cut.
            prior = (previous.get("visual_transform") or {}).get("motion_pattern") if isinstance(previous.get("visual_transform"), dict) else None
            if prior in _NEXT_MOVE:
                pattern, why = _NEXT_MOVE[prior], "continuation_changes_the_move"
            else:
                why = "continuation_keeps_renderer_alternation"
        elif archival or role == "evidence":
            pattern, why = "push_in", "documentary_ken_burns_on_evidence"
        elif geometry.get("known") and geometry["orientation"] == "landscape" and geometry["retained_share"] < 0.5:
            pattern, why = "pan", "pan_reveals_more_of_wide_scene"
        elif role == "hook":
            pattern, why = "push_in", "push_in_builds_tension"
        elif role == "final_payoff":
            pattern, why = "pull_out", "pull_out_resolves_to_context"
        else:
            why = "renderer_alternation_for_variety"
        steps.append({"technique": "ken_burns" if pattern != "static" else "static_hold",
                      "pattern": pattern, "applied": True, "reason": why})
        if role == "hook" and not real:
            steps.append({"technique": "parallax_2_5d", "applied": False, "reason": "generated_hook_depth_candidate"})

    overlay = strategy.get("overlay_spec") if isinstance(strategy.get("overlay_spec"), dict) else None
    if overlay:
        steps.append({"technique": f"overlay_{overlay.get('kind')}", "applied": True, "reason": "supports_narrated_fact"})

    outpaint_reason = None
    if geometry.get("known") and geometry["retained_share"] < 0.3:
        if real:
            outpaint_reason = "never_outpaint_real_evidence"
        else:
            outpaint_reason = "outpainting_not_supported_by_renderer"
        steps.append({"technique": "outpainting", "applied": False, "reason": outpaint_reason})

    failures: list[str] = []
    if geometry.get("known") and source not in _STATIC_SOURCES:
        if geometry["effective_long_pixels"] < MIN_REFRAME_LONG_PIXELS:
            failures.append("insufficient_resolution_after_reframe")
        if geometry["retained_share"] < 0.2:
            failures.append("reframe_keeps_too_little_of_source")
    return {
        "version": VERSION,
        "source": source,
        "kind": kind,
        "geometry": geometry,
        "reframe": reframe,
        "motion_pattern": pattern,
        "steps": steps,
        "reasons": reasons,
        "valid": not failures,
        "failures": failures,
    }


def apply_transformation(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Persist the plan; the renderer reads ``motion_pattern`` and the crop is subject-aware."""
    plan = plan_transformation(scene, state)
    if plan is None:
        scene.pop("visual_transform", None)
        return None
    scene["visual_transform"] = plan
    return plan
