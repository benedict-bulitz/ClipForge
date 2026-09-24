"""Compact, factuality-first viewer-reaction planning.

Reaction labels explain why an existing scene earns its place.  They never add
scenes, claims, or artificial stakes, and remain intentionally small enough to
be useful to the current hook, visual, and pacing systems.
"""
from __future__ import annotations

import re
from typing import Any

_CLICKBAIT = re.compile(
    r"(?i)\b(?:shocking|insane|unbelievable|mind[- ]blowing|outrageous|"
    r"you won'?t believe|schockierend|unglaublich|irre|wahnsinn)\b"
)
_SAFE_REACTIONS = {
    "curiosity", "surprise", "tension", "anticipation", "insight", "awe", "humor",
    "empathy", "satisfaction", "understanding", "certainty", "expectation",
}


def _normalise_reaction(value: object, fallback: str) -> str:
    candidate = " ".join(str(value or "").casefold().split())
    return candidate if candidate in _SAFE_REACTIONS else fallback


def reaction_arc(
    intent: dict[str, Any], payoff_plan: dict[str, Any], format_plan: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Derive a proportionate arc from the actual payoff, not hype language."""
    payoff_type = str(payoff_plan.get("payoff_type") or "answer").casefold()
    selected_format = str((format_plan or {}).get("selected_format") or "").casefold()
    content_type = str(intent.get("content_type") or "").casefold()
    protected = str(payoff_plan.get("reveal_policy") or "") == "after_supporting_information"
    desired = _normalise_reaction(payoff_plan.get("desired_viewer_reaction"), "insight")
    if selected_format == "quiz":
        return {
            "primary_reaction": "curiosity",
            "supporting_reactions": ["curiosity", "anticipation", "satisfaction"],
            "hook_reaction": "curiosity",
            "buildup_reaction": "anticipation",
            "payoff_reaction": "satisfaction",
            "ending_reaction": "satisfaction",
            "rationale": "The explicit challenge supports anticipation followed by a clear answer.",
            "format": selected_format,
        }
    if "comparison" in payoff_type or "ranking" in payoff_type or selected_format in {"comparison", "ranking"}:
        return {
            "primary_reaction": "surprise" if protected else "satisfaction",
            "supporting_reactions": ["curiosity", "anticipation", "satisfaction"],
            "hook_reaction": "curiosity",
            "buildup_reaction": "anticipation" if protected else "understanding",
            "payoff_reaction": "surprise" if protected else "satisfaction",
            "ending_reaction": "satisfaction",
            "rationale": "The actual comparison result can earn curiosity and a clear resolved reveal.",
            "format": selected_format,
        }
    if payoff_type in {"explanation", "answer"} or "explain" in content_type:
        return {
            "primary_reaction": "insight",
            "supporting_reactions": ["curiosity", "understanding"],
            "hook_reaction": "curiosity",
            "buildup_reaction": "understanding",
            "payoff_reaction": "insight",
            "ending_reaction": "understanding",
            "rationale": "The payoff explains the topic, so clarity and understanding take priority over surprise.",
            "format": selected_format,
        }
    if payoff_type in {"correction", "reveal"}:
        return {
            "primary_reaction": "surprise",
            "supporting_reactions": ["curiosity", "clarity"],
            "hook_reaction": "curiosity",
            "buildup_reaction": "anticipation" if protected else "understanding",
            "payoff_reaction": "surprise",
            "ending_reaction": "understanding",
            "rationale": "The supported correction or reveal can create proportionate surprise before clarity.",
            "format": selected_format,
        }
    if "story" in content_type:
        return {
            "primary_reaction": "tension",
            "supporting_reactions": ["curiosity", "tension", "satisfaction"],
            "hook_reaction": "curiosity",
            "buildup_reaction": "tension",
            "payoff_reaction": desired,
            "ending_reaction": "satisfaction",
            "rationale": "The story structure supports tension without inventing stakes.",
            "format": selected_format,
        }
    return {
        "primary_reaction": desired,
        "supporting_reactions": ["curiosity", "understanding"],
        "hook_reaction": "curiosity",
        "buildup_reaction": "understanding",
        "payoff_reaction": desired,
        "ending_reaction": "understanding",
        "rationale": "The reaction follows the persisted payoff plan and remains proportionate to the content.",
        "format": selected_format,
    }


def _visual_guidance(reaction: str, protected: bool) -> str:
    if protected and reaction in {"curiosity", "anticipation", "tension"}:
        return "Show the comparison or setup without showing the protected answer."
    return {
        "surprise": "Use a clear, factual contrast or reveal.",
        "awe": "Use scale, a wide framing, or a grounded comparison.",
        "insight": "Use a simple process, diagram, or transformation that clarifies the mechanism.",
        "understanding": "Keep the visual concrete and easy to follow.",
        "satisfaction": "Show the resolved comparison or clean final state.",
        "tension": "Focus attention on the unresolved, factual detail.",
    }.get(reaction, "Keep the visual concrete and relevant to the narration.")


# Information role -> reaction for non-hook scenes (all members of _SAFE_REACTIONS).
_STORY_REACTIONS = {
    "explanation": ("insight", "This scene explains how or why, so clarity comes first."),
    "evidence": ("understanding", "This scene supplies evidence the answer rests on."),
    "comparison": ("understanding", "This scene supplies one side of the comparison."),
    "ranked_item": ("anticipation", "This scene advances the ranking toward the top item."),
}


def _story_reaction(scene: dict[str, Any], arc: dict[str, Any]) -> tuple[str, str] | None:
    """Reaction from the scene's information role in the story arc, if known."""
    role = str(scene.get("story_role") or "")
    if not role:
        return None
    if scene.get("is_primary_answer"):
        return arc["payoff_reaction"], "This scene answers the primary question."
    if scene.get("is_final_payoff"):
        if role == "secondary_insight":
            return "surprise", "The final payoff is a supported extra insight beyond the answer."
        return arc.get("ending_reaction") or "understanding", "This scene delivers the final payoff."
    if role in _STORY_REACTIONS:
        return _STORY_REACTIONS[role]
    return None


def _roles(state: dict[str, Any]) -> dict[str, str]:
    return {
        str(block.get("id") or ""): str(block.get("role") or "").casefold()
        for block in state.get("script", {}).get("blocks", [])
    }


def build_reaction_plan(state: dict[str, Any], arc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map one compact arc to existing scenes; it never creates scene records."""
    payoff_plan = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    arc = arc or reaction_arc(intent, payoff_plan, state.get("format_plan"))
    protected = str(payoff_plan.get("reveal_policy") or "") == "after_supporting_information"
    roles = _roles(state)
    pacing = state.get("pacing_analysis") if isinstance(state.get("pacing_analysis"), dict) else {}
    pacing_by_scene = {str(item.get("scene_id") or ""): item for item in pacing.get("scene_assessments", [])}
    scene_entries: list[dict[str, Any]] = []
    payoff_seen = False
    scenes = list(state.get("scenes") or [])
    for index, scene in enumerate(scenes):
        scene_id = str(scene.get("id") or f"scene_{index + 1}")
        role = roles.get(str(scene.get("block_id") or ""), "")
        story = _story_reaction(scene, arc) if role != "hook" else None
        if role == "payoff" or scene.get("is_final_payoff"):
            payoff_seen = True
        if story is not None:
            reaction, reason = story
        elif role == "hook":
            reaction = arc["hook_reaction"]
            reason = "The opening establishes the honest reason to keep watching."
        elif role == "payoff":
            reaction = arc["payoff_reaction"]
            reason = "This scene delivers the persisted payoff."
        elif payoff_seen or index == len(scenes) - 1:
            reaction = arc["ending_reaction"]
            reason = "This is the final resolving beat after the payoff."
        else:
            reaction = arc["buildup_reaction"]
            reason = "This scene supplies context needed for the payoff or explanation."
        pacing_item = pacing_by_scene.get(scene_id, {})
        visual_guidance = _visual_guidance(reaction, protected and role != "payoff")
        visual_intent = scene.get("visual_intent")
        if isinstance(visual_intent, dict):
            visual_intent["reaction_direction"] = visual_guidance
        scene_entries.append(
            {
                "scene_id": scene_id,
                "story_role": scene.get("story_role"),
                "intended_reaction": reaction,
                "intensity": "high" if role in {"hook", "payoff"} else "medium",
                "reason": reason,
                "narration_support": str(scene.get("narration") or ""),
                "visual_guidance": visual_guidance,
                "leads_toward_payoff": not payoff_seen,
                "pacing_context": {
                    "purpose": pacing_item.get("purpose"),
                    "recommendation": pacing_item.get("recommendation"),
                },
            }
        )
    triple = state.get("script", {}).get("triple_hook")
    if isinstance(triple, dict):
        triple["intended_reaction"] = arc["hook_reaction"]
        visual = triple.get("visual_hook")
        if isinstance(visual, dict):
            visual["reaction_direction"] = _visual_guidance(arc["hook_reaction"], protected)
    return {
        "status": "ready",
        **arc,
        "scene_reactions": scene_entries,
        "rationale": arc["rationale"],
        "applied_changes": [],
    }


def reaction_quality_issues(state: dict[str, Any]) -> list[str]:
    """Structured safeguards for proportional, clarity-first emotional framing."""
    plan = state.get("reaction_plan") if isinstance(state.get("reaction_plan"), dict) else {}
    if not plan:
        return []
    issues: list[str] = []
    triple = state.get("script", {}).get("triple_hook") if isinstance(state.get("script", {}).get("triple_hook"), dict) else {}
    hook_text = " ".join(str(triple.get(key) or "") for key in ("verbal_hook", "on_screen_text_hook"))
    facts_text = " ".join(str(fact.get("claim") or "") for fact in state.get("facts", []))
    if _CLICKBAIT.search(hook_text) and not _CLICKBAIT.search(facts_text):
        issues.append("unsupported_emotional_framing")
    if str(triple.get("intended_reaction") or "") in {"shock", "outrage"}:
        issues.append("fake_clickbait_reaction")
    payoff_type = str(state.get("payoff_plan", {}).get("payoff_type") or "").casefold()
    payoff_reaction = str(plan.get("payoff_reaction") or "")
    if "explanation" in payoff_type and payoff_reaction == "surprise":
        issues.append("payoff_reaction_mismatch")
    if (
        str(triple.get("intended_reaction") or "") == "surprise"
        and payoff_reaction in {"insight", "understanding", "certainty"}
    ):
        issues.append("hook_reaction_exceeds_payoff")
    entries = list(plan.get("scene_reactions") or [])
    if len(entries) >= 3 and all(item.get("intended_reaction") == "surprise" for item in entries):
        issues.append("unnecessary_emotional_escalation")
    roles = _roles(state)
    payoff_scene_exists = any(
        roles.get(str(scene.get("block_id") or "")) == "payoff"
        for scene in state.get("scenes", [])
    )
    if state.get("payoff_plan", {}).get("payoff") and entries and not payoff_scene_exists:
        issues.append("missing_final_resolution_reaction")
    if (
        state.get("payoff_plan", {}).get("reveal_policy") == "immediate_context_allowed"
        and payoff_type == "explanation"
        and any(item.get("intended_reaction") == "tension" for item in entries)
    ):
        issues.append("reaction_conflicts_with_clarity")
    if any((item.get("pacing_context") or {}).get("recommendation") == "REPLACE_VISUAL" for item in entries):
        issues.append("visual_direction_conflicts_with_reaction")
    return issues


def plan_viewer_reactions(state: dict[str, Any], arc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist a failure-safe plan without changing script text or scene count."""
    try:
        state["reaction_plan"] = build_reaction_plan(state, arc)
    except Exception as exc:  # noqa: BLE001 - enrichment cannot stop generation
        fallback = arc or reaction_arc(
            state.get("intent") if isinstance(state.get("intent"), dict) else {},
            state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {},
            state.get("format_plan") if isinstance(state.get("format_plan"), dict) else {},
        )
        state["reaction_plan"] = {
            "status": "fallback",
            **fallback,
            "scene_reactions": [],
            "applied_changes": [],
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    return state["reaction_plan"]
