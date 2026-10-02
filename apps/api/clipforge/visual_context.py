"""Bounded positive scene/story evidence shared by routing and acceptance.

Exclusions, research citations and unrelated story units are not visual intent.
"""
from __future__ import annotations

import re
from typing import Any

POSITIVE_INTENT_FIELDS = (
    "visual_goal", "subject", "objects", "actions", "context", "action_state",
    "key_detail", "subjects_to_show", "named_entities", "media_queries",
)
HISTORICAL_WORDS = {"history", "historical", "archival", "archive", "historic", "century", "antique", "geschichte", "historisch"}


def positive_intent(intent: Any) -> str:
    if not isinstance(intent, dict):
        return ""
    values = []
    for key in POSITIVE_INTENT_FIELDS:
        value = intent.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(v for v in value[:8] if isinstance(v, str))
    return " ".join(values)[:6000]


def scene_story_context(scene: dict, state: dict | None, *, routing: bool = False) -> str:
    state = state or {}
    script = state.get("script") or {}
    blocks = script.get("blocks", []) if isinstance(script, dict) else []
    block = next((b for b in blocks if isinstance(b, dict) and b.get("id") == scene.get("block_id")), {})
    fact_ids = set(scene.get("fact_ids") or block.get("fact_ids") or []) | set(scene.get("story_unit_ids") or [])
    arc = state.get("story_arc") or {}
    units = arc.get("units", []) if isinstance(arc, dict) else []
    claims = [str(u.get("claim") or "") for u in units if isinstance(u, dict) and (
        u.get("id") in fact_ids or routing and u.get("id") == arc.get("primary_answer_id")
    ) and not u.get("off_question")]
    return " ".join([str(block.get("text") or block.get("statement") or ""), positive_intent(block.get("visual_intent")), *claims])[:6000]


def historical_requirement(scene: dict, state: dict | None) -> dict:
    intent = positive_intent(scene.get("visual_intent")) or str(scene.get("visual_goal") or "")
    # Explicit present-day B-roll may support a historical story. Exclusion
    # lists (e.g. 'must not show modern traffic') cannot grant that exception.
    if re.search(r"\b(?:present[- ]day|modern|current|contemporary|today|heute)\b", intent, re.IGNORECASE):
        return {"required": False, "years": []}
    text = " ".join([str(scene.get("narration") or ""), intent, scene_story_context(scene, state)])
    years = sorted({int(y) for y in re.findall(r"(?<!\d)(1\d{3})(?!\d)", text)})
    words = set(re.findall(r"\w+", text.casefold()))
    return {"required": bool(years or words & HISTORICAL_WORDS), "years": years}
