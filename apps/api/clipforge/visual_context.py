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


def planetary_setting_evidence(scene: dict, state: dict | None, metadata: str) -> dict:
    """A topical label cannot override a caption's contradictory setting.

    This is a bounded metadata veto, not another visual judge or a provider
    authenticity grant. Missing captions still use the existing authorities.
    Discovery queries and exclusions cannot request an illustrative exception.
    """
    own = scene.get("visual_intent") or {}
    own = {k: v for k, v in own.items() if k != "media_queries"} if isinstance(own, dict) else {}
    direction = " ".join((str(scene.get("visual_goal") or ""), positive_intent(own)))
    text = " ".join((str(scene.get("narration") or ""), direction, scene_story_context(scene, state))).casefold()
    words = set(re.findall(r"\w+", text))
    named = {"moon", "mond", "lunar", "mars", "martian", "venus", "mercury",
             "jupiter", "saturn", "uranus", "neptune", "merkur", "neptun"}
    planetary = bool(words & (named | {"planet", "planets", "planetary", "planetar", "planeten"}))
    direct = set(re.findall(r"\w+", direction.casefold()))
    if direct & {"earth", "erde", "terrestrial"} and not direct & named:
        planetary = False  # An explicit Earth comparison may show Earth.
    def affirmed_matches(pattern: str, caption: str):
        for match in re.finditer(pattern, caption, re.IGNORECASE):
            prefix = caption[max(0, match.start() - 40):match.start()]
            if not re.search(r"\b(?:no|not|without|avoid|lacks|keine?|ohne)\s+(?:\w+\s+){0,2}$", prefix, re.IGNORECASE):
                yield match

    illustrative = any(affirmed_matches(
        r"\b(?:cosplay|costume|themed|reenactment|re[- ]enactment|simulation|simulator|analogue|analog)\b",
        direction,
    ))
    environment = bool(words & {
        "surface", "landscape", "terrain", "ground", "soil", "crater", "sky", "dust",
        "atmosphere", "atmospheric", "gravity", "rain", "weather", "environment",
        "oberfläche", "landschaft", "himmel", "staub", "staubteilchen", "atmosphäre",
        "schwerkraft", "anziehungskraft", "regen", "actual", "real", "echte",
    })
    required = planetary and environment and not illustrative
    markers = []
    if required:
        # Captions describe staged representations or incompatible surroundings;
        # do not mistake their planet name, sign text or suit for that setting.
        patterns = {
            "staged_representation": r"\b(?:cosplay|costume|themed|reenactment|re[- ]enactment|replica|merchandise|souvenir|simulation|simulator|hitchhik\w*)\b",
            "terrestrial_surroundings": r"\b(?:roadside|highway|asphalt|paved road|grass|grassland|meadow|shopping street)\b",
            "lookalike_setting": r"\bpretending\b|\b(?:resembling|look(?:s|ing)? like|analogue|analog)\s+(?:\w+\s+){0,3}(?:" + "|".join(sorted(named | {"planet", "planetary"})) + r")\b",
            "held_prop": r"\b(?:cardboard sign|holding (?:a |the )?sign)\b",
        }
        caption = metadata.casefold()
        for label, pattern in patterns.items():
            # A caption such as 'surface without grass' does not assert grass.
            if any(affirmed_matches(pattern, caption)):
                markers.append(label)
        # A person 'on <planet>' supplies a topical label, not the requested
        # environment. Genuine mission captions can establish that setting
        # with soil/surface/crater evidence; provider identity alone cannot.
        actor = bool(re.search(r"\b(?:astronaut|spacesuit|space suit|person|couple|cosmonaut)\b", caption))
        physical = {"surface", "landscape", "terrain", "ground", "soil", "crater", "sky",
                    "dust", "atmosphere", "oberfläche", "landschaft", "himmel", "staub"}
        if actor and not set(re.findall(r"\w+", caption)) & physical:
            markers.append("actor_without_environment_evidence")
    return {"required": required, "mismatch": bool(markers), "markers": markers}
