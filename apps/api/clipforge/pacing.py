"""Local, explainable pacing and scene-quality checks.

This module measures relationships inside one project's finished plan.  It does
not prescribe universal shot lengths or rewrite narration: a dense explanation
can earn a long scene, while a short sequence can still be too frantic for its
meaning.
"""
from __future__ import annotations

import re
from typing import Any

_STOP = {
    "the", "and", "that", "this", "with", "from", "into", "for", "are", "was", "were",
    "have", "has", "not", "but", "der", "die", "das", "und", "mit", "für", "von", "ist",
    "sind", "hat", "haben", "nicht", "aber", "eine", "einer", "einem", "den", "dem",
}
_OUTRO = re.compile(
    r"(?i)^\s*(?:thanks? for watching|thank you|follow for more|like and subscribe|"
    r"see you next time|danke fürs zuschauen|danke fürs ansehen|folge für mehr|"
    r"like und abonniere|bis zum nächsten mal)\b"
)


def _words(value: object) -> set[str]:
    return {
        word for word in re.findall(r"[a-zäöüß]{3,}", str(value or "").casefold())
        if word not in _STOP
    }


def _duration(scene: dict[str, Any]) -> float:
    return max(0.0, float(scene.get("end") or 0) - float(scene.get("start") or 0))


def _role_by_block(state: dict[str, Any]) -> dict[str, str]:
    return {
        str(block.get("id") or ""): str(block.get("role") or "").casefold()
        for block in state.get("script", {}).get("blocks", [])
    }


def _visual_text(scene: dict[str, Any]) -> str:
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    values = [scene.get("visual_goal"), intent.get("visual_goal"), *intent.get("objects", []), *scene.get("search_queries", [])]
    return " ".join(str(value or "") for value in values)


def _caption_overload(scene: dict[str, Any], captions: list[dict[str, Any]], wpm: float) -> bool:
    start, end = float(scene.get("start") or 0), float(scene.get("end") or 0)
    relevant = [caption for caption in captions if float(caption.get("start") or 0) < end and float(caption.get("end") or 0) > start]
    if not relevant:
        return False
    caption_words = sum(len(str(item.get("text") or "").split()) for item in relevant)
    readable_seconds = sum(
        max(0.0, min(end, float(item.get("end") or 0)) - max(start, float(item.get("start") or 0)))
        for item in relevant
    )
    # Compare captions to this project's selected narration rate, rather than a
    # platform-wide speed limit.  Captions may span a scene boundary, hence the
    # small tolerance.
    return bool(readable_seconds and caption_words / readable_seconds > (wpm / 60) * 1.5)


def _purpose(role: str, gain: float, scene: dict[str, Any], payoff_seen: bool) -> str:
    if role == "hook":
        return "hook"
    if role == "payoff":
        return "payoff"
    if payoff_seen:
        return "post_payoff_context"
    if gain >= 0.45:
        return "new_information"
    if _words(_visual_text(scene)):
        return "visual_reinforcement"
    return "needs_justification"


def _recommendation(
    *, role: str, gain: float, visual_value: float, duration_ratio: float,
    payoff_seen: bool, caption_overload: bool, chaotic: bool, transition_weak: bool,
    generic_outro: bool,
) -> tuple[str, list[str], str]:
    reasons: list[str] = []
    severity = "info"
    if role == "hook":
        return "KEEP", reasons, severity
    if caption_overload or chaotic:
        reasons.append("Meaning is arriving faster than the current narration/caption window can comfortably support.")
        return "MERGE_WITH_NEXT", reasons, "warning"
    if payoff_seen and generic_outro:
        reasons.append("A generic outro follows the payoff without adding understanding or a final beat.")
        return "SHORTEN_POST_PAYOFF", reasons, "warning"
    if payoff_seen and gain < 0.2:
        reasons.append("The payoff has landed and this segment adds little new understanding.")
        return "SHORTEN_POST_PAYOFF", reasons, "warning"
    if gain < 0.15 and visual_value < 0.15:
        reasons.append("The segment adds neither new information nor a useful visual contribution.")
        return "TRIM", reasons, "warning"
    if visual_value < 0.12 and gain >= 0.2:
        reasons.append("The narration advances, but the current visual plan does not support it.")
        return "REPLACE_VISUAL", reasons, "warning"
    if duration_ratio > 1.9 and gain < 0.35:
        reasons.append("Its allocated time is high relative to its narration and information gain.")
        return "TRIM", reasons, "warning"
    if transition_weak:
        reasons.append("It has little connective or informational value after the previous segment.")
        return "MERGE_WITH_PREVIOUS", reasons, "warning"
    return "KEEP", reasons, severity


def analyze_scene_quality(state: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable, non-destructive quality assessment for current scenes."""
    scenes = list(state.get("scenes") or [])
    blocks = _role_by_block(state)
    captions = list(state.get("captions", {}).get("items") or [])
    wpm = max(1.0, float(state.get("duration", {}).get("speaking_rate_wpm") or 165))
    seen_words: set[str] = set()
    analyzed: list[dict[str, Any]] = []
    payoff_seen = False
    short_chain = 0
    previous_words: set[str] = set()
    format_plan = state.get("format_plan") if isinstance(state.get("format_plan"), dict) else {}
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else {}
    required_ids = {
        str(unit.get("id")) for unit in arc.get("units") or []
        if isinstance(unit, dict) and not unit.get("may_be_omitted")
    }

    for index, scene in enumerate(scenes):
        narration = str(scene.get("narration") or "")
        words = _words(narration)
        role = blocks.get(str(scene.get("block_id") or ""), "")
        # The story arc's final payoff marks delivery even when the writer did
        # not label that block "payoff".
        if scene.get("is_final_payoff"):
            role = "payoff"
        if role == "payoff":
            payoff_seen = True
        gain = len(words - seen_words) / max(1, len(words))
        visual_words = _words(_visual_text(scene))
        visual_value = len(words & visual_words) / max(1, len(words))
        actual_duration = _duration(scene)
        natural_duration = max(0.01, len(narration.split()) / wpm * 60)
        duration_ratio = actual_duration / natural_duration if narration.split() else 0.0
        # A run is chaotic only when several scenes each have less time than
        # their own spoken meaning needs.  This deliberately has no seconds
        # constant and leaves concise, readable comparisons alone.
        short_chain = short_chain + 1 if narration.split() and duration_ratio < 0.55 else 0
        chaotic = short_chain >= 3
        caption_overload = _caption_overload(scene, captions, wpm)
        generic_outro = bool(_OUTRO.search(narration))
        transition_overlap = len(words & previous_words) / max(1, min(len(words), len(previous_words))) if previous_words else 0.0
        transition_weak = bool(index and gain < 0.2 and transition_overlap >= 0.75)
        recommendation, reasons, severity = _recommendation(
            role=role,
            gain=gain,
            visual_value=visual_value,
            duration_ratio=duration_ratio,
            payoff_seen=payoff_seen and role != "payoff",
            caption_overload=caption_overload,
            chaotic=chaotic,
            transition_weak=transition_weak,
            generic_outro=generic_outro,
        )
        story_required = bool(set(scene.get("story_unit_ids") or []) & required_ids)
        if story_required and recommendation in {"TRIM", "SHORTEN_POST_PAYOFF"}:
            # Information the arc requires is shortened in wording, never removed.
            recommendation, severity = "KEEP", "info"
            reasons = ["Carries information the story arc requires; tighten wording rather than removing it."]
        dead_air_risk = bool(duration_ratio > 1.9 and gain < 0.2 and visual_value < 0.2)
        if dead_air_risk and "The segment adds neither" not in " ".join(reasons):
            reasons.append("Long screen time is not matched by new narration or a visual change.")
        analyzed.append(
            {
                "scene_id": str(scene.get("id") or f"scene_{index + 1}"),
                "purpose": "primary_answer" if scene.get("is_primary_answer") and role != "payoff" else _purpose(role, gain, scene, payoff_seen),
                "story_role": scene.get("story_role"),
                "story_required": story_required,
                "information_gain": round(gain, 3),
                "visual_value": round(visual_value, 3),
                "pacing_quality": "compressed" if chaotic or caption_overload else ("lingering" if dead_air_risk else "earned"),
                "redundancy_risk": round(1 - gain, 3),
                "dead_air_risk": dead_air_risk,
                "payoff_relevance": "delivery" if role == "payoff" else ("after_payoff" if payoff_seen else "setup"),
                "recommendation": recommendation,
                "severity": severity,
                "reasons": reasons,
                "metrics": {
                    "duration_to_natural_ratio": round(duration_ratio, 3),
                    "caption_overload": caption_overload,
                    "transition_overlap": round(transition_overlap, 3),
                },
                "format_guidance": format_plan.get("pacing_guidance"),
            }
        )
        seen_words.update(words)
        previous_words = words

    weak = [item["scene_id"] for item in analyzed if item["recommendation"] != "KEEP"]
    score = round(max(0.0, 100 - sum(12 if item["severity"] == "warning" else 0 for item in analyzed)), 1)
    return {
        "status": "ready",
        "overall_score": score,
        "scene_assessments": analyzed,
        "weak_scene_ids": weak,
        "recommendations": [
            {"scene_id": item["scene_id"], "action": item["recommendation"], "reasons": item["reasons"]}
            for item in analyzed if item["recommendation"] != "KEEP"
        ],
        "applied_safe_fixes": [],
        "format_guidance": format_plan.get("pacing_guidance"),
        "format": format_plan.get("selected_format"),
    }


def apply_safe_pacing_fixes(state: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Remove only blank scene artifacts; recommendations otherwise remain advisory."""
    scenes = list(state.get("scenes") or [])
    blank_ids = [str(scene.get("id") or "") for scene in scenes if not str(scene.get("narration") or "").strip()]
    if blank_ids:
        state["scenes"] = [scene for scene in scenes if str(scene.get("id") or "") not in blank_ids]
        state.setdefault("timeline", {})["scene_ids"] = [scene["id"] for scene in state["scenes"]]
        state.setdefault("storyboard", {})["scene_count"] = len(state["scenes"])
        analysis["applied_safe_fixes"] = [
            {"action": "REMOVE", "scene_id": scene_id, "reason": "Removed an empty scene artifact."}
            for scene_id in blank_ids
        ]
    return analysis


def analyze_pacing(state: dict[str, Any]) -> dict[str, Any]:
    """Persist a safe fallback result if pacing enrichment itself fails."""
    try:
        analysis = analyze_scene_quality(state)
        state["pacing_analysis"] = apply_safe_pacing_fixes(state, analysis)
    except Exception as exc:  # noqa: BLE001 - quality enrichment cannot block rendering
        state["pacing_analysis"] = {
            "status": "fallback",
            "overall_score": None,
            "scene_assessments": [],
            "weak_scene_ids": [],
            "recommendations": [],
            "applied_safe_fixes": [],
            "format_guidance": None,
            "format": None,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    return state["pacing_analysis"]
