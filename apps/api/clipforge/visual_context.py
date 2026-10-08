"""Bounded positive scene/story evidence shared by routing and acceptance.

Exclusions, research citations and unrelated story units are not visual intent.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any

POSITIVE_INTENT_FIELDS = (
    "visual_goal", "subject", "objects", "actions", "context", "action_state",
    "key_detail", "subjects_to_show", "named_entities", "media_queries", "entities", "location", "time_period",
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


# Geographic evidence, not a topic blacklist or a provider authenticity rule.
# Country names come from the public-domain ISO table shipped by the existing
# tzdata dependency. US state names cover captions that omit their country.
_US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming",
]


@lru_cache(maxsize=1)
def _earth_landscape_location_pattern() -> str:
    countries = [
        line.split("\t", 1)[1].strip()
        for line in files("tzdata").joinpath("zoneinfo", "iso3166.tab").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    regions = "|".join(re.escape(name) for name in sorted(set(countries + _US_STATES), key=len, reverse=True))
    # A place must locate the depicted environment. Credits such as 'Mars
    # landscape processed by a laboratory in California' are not that claim.
    return (
        r"\b(?:landscape|desert|terrain|canyon|mountains?|valley|beach|coast|forest|"
        r"landschaft|wüste|gebirge|tal)\s+(?:in|near|at|of|bei|nahe|aus|von)\s+"
        r"(?:[\w'-]+[ ,]+){0,4}(?:" + regions + r")\b"
    )


def affirmed_matches(pattern: str, caption: str):
    for match in re.finditer(pattern, caption, re.IGNORECASE):
        prefix = caption[max(0, match.start() - 40):match.start()]
        if not re.search(r"\b(?:no|not|without|avoid|lacks|keine?|ohne)\s+(?:\w+\s+){0,2}$", prefix, re.IGNORECASE):
            yield match


def _infer_planetary_environment(scene: dict, state: dict | None) -> dict | None:
    """Derive the actual setting from local factual direction, never queries.

    Explicit Earth comparisons and illustrative demonstrations retain their
    existing exception; query text and exclusions cannot grant that exception.
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
    if not required:
        return None
    aliases = {"martian": "mars", "lunar": "moon", "mond": "moon", "merkur": "mercury", "neptun": "neptune"}
    entities = sorted({aliases.get(word, word) for word in words & named})
    entity = entities[0] if len(entities) == 1 else "planetary"
    incompatible = ["built_environment", "road_transport"]
    if entity in {"mars", "moon", "venus", "mercury"}:
        incompatible.append("vegetation")
        # An explicitly requested ancient ocean/reconstruction or another
        # body's sea is not inferred to require a dry present-day surface.
        if not direct & {"ocean", "sea", "lake", "oceans", "seas", "ozean", "meer"}:
            incompatible.append("open_water")
    return {"version": 1, "domain": "planetary", "entity": entity,
            "representation": "actual", "incompatible": incompatible,
            "context_key": hashlib.sha256(text.encode()).hexdigest()[:16]}


def environment_requirement(scene: dict, state: dict | None) -> dict | None:
    """Persist a factual constraint, refreshing it after edits from local context.

    The scene/block/facts establish it, never the search query or an exclusion.
    Re-derivation also upgrades old snapshots which predate this field.
    """
    requirement = _infer_planetary_environment(scene, state)
    if requirement:
        scene["required_environment"] = requirement
    else:
        scene.pop("required_environment", None)
    return requirement


# Four observable conflict classes, shared by metadata and the existing local
# verifier. Colour/sky/cloud/desert similarity supplies no contradiction.
SETTING_CONFLICTS = {
    "built_environment": (r"\b(?:city(?:scape)?|skyline|buildings?|skyscrapers?|town|village|urban|stadt|gebäude)\b",
                          ["a landscape on Earth with city buildings on the horizon", "a photograph of a city skyline with buildings"]),
    "open_water": (r"\b(?:ocean|open sea|sea surface|seascape|shoreline|ozean|meer)\b",
                   ["an ocean sunset over open water with waves", "a seascape with a sea surface and shoreline"]),
    "vegetation": (r"\b(?:trees?|forest|vegetation|scrub|bushes|grass|meadow|wald|bäume)\b",
                   ["a terrestrial landscape with trees and bushes", "a desert landscape with scrub bushes and living vegetation"]),
    "road_transport": (r"\b(?:highway|paved road|asphalt|traffic|cars|roadside)\b",
                       ["a terrestrial landscape with roads and cars", "a photograph of a paved highway with vehicles"]),
}


def setting_verdict(requirement: dict | None, evidence: dict | None) -> bool:
    """A recorded contradiction is authoritative only for the matching constraint."""
    evidence = evidence or {}
    recorded = evidence.get("requirement") or {}
    return bool(requirement and evidence.get("mismatch") and all(
        recorded.get(key) == requirement.get(key) for key in ("domain", "entity", "representation", "incompatible")
    ))


def planetary_setting_evidence(scene: dict, state: dict | None, metadata: str) -> dict:
    requirement = environment_requirement(scene, state)
    required = bool(requirement)
    named = {"moon", "mond", "lunar", "mars", "martian", "venus", "mercury", "jupiter", "saturn", "uranus", "neptune", "merkur", "neptun"}
    markers = []
    if required:
        # Captions describe staged representations or incompatible surroundings;
        # do not mistake their planet name, sign text or suit for that setting.
        patterns = {
            "staged_representation": r"\b(?:cosplay|costume|themed|reenactment|re[- ]enactment|replica|merchandise|souvenir|simulation|simulator|hitchhik\w*)\b",
            "terrestrial_surroundings": r"\b(?:roadside|highway|asphalt|paved road|grass|grassland|meadow|shopping street)\b",
            "earth_geography": _earth_landscape_location_pattern(),
            # Explicit terrestrial descriptions are contradictions. Blue sky
            # or clouds alone are not: real planetary imagery may contain them.
            "terrestrial_sky": r"\b(?:earth(?:[- ]like)?|terrestrial|irdisch(?:er|en|e)?)\s+(?:\w+\s+){0,2}(?:sky|clouds?|himmel|wolken)\b",
            "lookalike_setting": r"\bpretending\b|\b(?:resembling|look(?:s|ing)? like|analogue|analog)\s+(?:\w+\s+){0,3}(?:" + "|".join(sorted(named | {"planet", "planetary"})) + r")\b",
            "held_prop": r"\b(?:cardboard sign|holding (?:a |the )?sign)\b",
        }
        patterns.update({name: SETTING_CONFLICTS[name][0] for name in requirement["incompatible"]})
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
    return {"required": required, "requirement": requirement, "mismatch": bool(markers), "markers": markers, "source": "metadata"}
