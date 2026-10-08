"""Search Planner V2: several faceted retrieval queries per scene.

A scene's narration fragment is a poor search string ("Er ist vor allem
deshalb rot ..." has no visual subject). The planner reads the scene's
structured visual intent, its sibling scenes of the same fact, the full
script block, the linked Story Arc facts and the project topic, and emits a
bounded set of *facets*: concrete subject + action, entity + period, entity +
location, alternate terminology, a narrow factual query and a broader context
query. It also classifies the scene's visual *domain* for the Source Router
and names per-source-family phrasings (an archive catalogue needs
"berlin wall 1961", a stock library needs "soldiers building concrete wall").

Deterministic and topic-agnostic: no vocabulary of topics, only language
vocabulary for domain cues. Optional structured fields the script planner may
supply (``entities``, ``location``, ``time_period``, ``alternate_terms``,
``factual_sensitivity``) sharpen the plan; without them the planner degrades
to the intent's own objects/actions/context. Protection of the payoff (what
must not be shown before the reveal) is applied by the caller with the same
authority that filters every other query.
"""

from __future__ import annotations

import re
from typing import Any

from .visual_context import HISTORICAL_WORDS, positive_intent, scene_story_context

VERSION = 2
MAX_FACETS = 8
MAX_SOURCE_QUERIES = 3
NEAR_DUPLICATE_JACCARD = 0.8

# Source families: how a provider's search engine behaves, not its identity.
SOURCE_FAMILIES = {
    "pexels": "stock",
    "pixabay": "stock",
    "wikimedia": "commons",
    "openverse": "commons",
    "flickr": "commons",
    "nasa": "space",
    "loc": "archive",
    "europeana": "archive",
}

# Facet preference per source family. Catalogue-style engines (archives,
# NASA) match titles/subjects, so named entities and dates come first; stock
# libraries match visual descriptions, so the scene's logical query stays.
_FAMILY_FACETS = {
    "archive": ("entity_period", "entity_location", "narrow", "entity", "alternate", "location_subject"),
    "space": ("narrow", "entity", "entity_period", "alternate", "subject_action"),
    "commons": ("narrow", "entity", "entity_period", "alternate", "entity_location"),
    "stock": (),
}
# Domains whose catalogue search may return records about events/artifacts.
# Everywhere else a bare name ("mars", "red planet") sent to a catalogue-style
# engine returns books, newspaper pages, covers and stamps that MENTION the
# subject; those families get the name together with the visual subject.
DOCUMENT_TOLERANT_DOMAINS = frozenset({"historical", "art_culture"})
# Without a name-bearing facet, those engines keep the visual logical query.
_DEPICTION_FAMILY_FACETS = {
    "archive": ("narrow", "alternate_subject", "entity_period", "entity_location"),
    "space": ("narrow", "alternate_subject", "entity_period", "subject_action"),
    "commons": ("narrow", "alternate_subject", "entity_period", "entity_location"),
    "stock": (),
}

# ---------------------------------------------------------------------------
# Domain classification (language vocabulary only; no topic tables)
# ---------------------------------------------------------------------------

SPACE_WORDS = frozenset({
    "space", "astronomy", "astronomical", "planet", "planets", "planetary", "spacecraft", "astronaut",
    "astronauts", "galaxy", "galaxies", "lunar", "orbit", "orbital", "satellite", "cosmos", "cosmic",
    "weltraum", "weltall", "astronomie", "mars", "martian", "moon", "mond", "jupiter", "saturn", "venus",
    "neptune", "neptun", "uranus", "pluto", "comet", "komet", "asteroid", "asteroids", "nebula",
    "exoplanet", "meteor", "meteorite", "meteorit", "rocket", "rakete", "telescope", "teleskop", "hubble",
    "nasa", "esa", "iss", "eclipse", "sonnenfinsternis", "mondfinsternis", "sonnensystem", "milchstraße",
    "milky", "supernova", "rover", "spacewalk", "raumstation", "raumfahrt", "raumsonde", "probe",
})
_SPACE_PHRASES = ("solar system", "space station", "black hole", "schwarzes loch", "milky way")
ARCHIVAL_WORDS = frozenset(HISTORICAL_WORDS | {
    "medieval", "mittelalter", "mittelalterlich", "ancient", "antike", "antiken", "empire", "kaiserreich",
    "dynasty", "dynastie", "pharaoh", "pharao", "weltkrieg", "wwi", "wwii", "revolution", "colonial",
    "kolonial", "victorian", "viktorianisch", "renaissance", "baroque", "barock", "industrialization",
    "industrialisierung", "vintage", "archivaufnahme", "zeitgeschichte",
})
ART_WORDS = frozenset({
    "painting", "paintings", "gemälde", "sculpture", "skulptur", "statue", "museum", "artifact",
    "artefact", "artefakt", "manuscript", "handschrift", "fresco", "fresko", "mosaic", "mosaik",
    "tapestry", "artwork", "kunstwerk", "engraving", "kupferstich", "woodcut", "holzschnitt", "pottery",
    "keramik", "relic", "reliquie", "codex", "papyrus", "portrait", "porträt", "masterpiece",
})
DIAGRAM_WORDS = frozenset({
    "diagram", "diagramm", "schematic", "schema", "cross-section", "querschnitt", "illustration",
    "blueprint", "bauplan", "chart", "anatomical", "lithograph",
})
ANATOMY_WORDS = frozenset({
    "anatomy", "anatomical", "medical", "organ", "organs", "tissue", "neuron", "neurons", "anatomie",
    "physiology", "physiologie", "microscope", "mikroskop", "microscopic", "mikroskopisch", "cell",
    "cells", "zelle", "zellen", "bacteria", "bakterien", "virus", "viren", "skeleton", "skelett",
    "dna", "blood", "blut", "gewebe",
})
NATURE_WORDS = frozenset({
    "species", "spezies", "wildlife", "insect", "insekt", "insekten", "bird", "birds", "vogel", "vögel",
    "mammal", "säugetier", "reptile", "reptil", "amphibian", "amphibie", "fish", "fisch", "fungus",
    "pilz", "plant", "pflanze", "pflanzen", "predator", "raubtier", "habitat", "coral", "koralle",
})
# Scientific binomial (Genus species) in an English planner field.
_BINOMIAL = re.compile(r"\b[A-Z][a-z]{2,} [a-z]{3,}\b")

DOMAIN_REASONS = {
    "space": "space_or_earth_observation",
    "historical": "historical_or_archival",
    "art_culture": "art_culture_or_artifact",
    "anatomy": "factual_anatomical",
    "diagram": "diagram_or_illustration",
    "entity_place": "concrete_entity_or_place",
    "nature": "nature_or_species",
    "general": "everyday_action_or_general_photo",
}

_WORD = re.compile(r"[\wäöüß-]+", re.UNICODE)
_YEAR = re.compile(r"(?<!\d)(1\d{3}|20\d{2})(?!\d)")
_DECADE = re.compile(r"(?<!\d)(1\d{2}0|20[0-2]0)s\b|\b(1\d{2}0|20[0-2]0)er\b", re.IGNORECASE)
_PROPER = re.compile(r"\b[A-Z][\w'’-]*(?:\s+(?:of|de|la|le|von|van|der|du|del|[A-Z0-9][\w'’-]*))*")


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(str(text or "").casefold())}


def _list(value: Any, limit: int = 6) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [" ".join(str(item).split())[:120] for item in value[:limit] if str(item or "").strip()]


def _intent(scene: dict[str, Any]) -> dict[str, Any]:
    intent = scene.get("visual_intent")
    return intent if isinstance(intent, dict) else {}


def _planned(intent: dict[str, Any]) -> bool:
    """A planner-authored (English, structured) intent, not narration words."""
    return bool(intent) and intent.get("source") != "narration_fallback"


def sibling_intent(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """A planned intent of the same fact/block when this scene only has narration words."""
    own = _intent(scene)
    if _planned(own):
        return own
    block_id = scene.get("block_id")
    for other in state.get("scenes") or []:
        if other is scene or not isinstance(other, dict) or not block_id or other.get("block_id") != block_id:
            continue
        intent = _intent(other)
        if _planned(intent):
            return intent
    return {}


def classify_domain(text: str, intent: dict[str, Any] | None = None, plan: dict[str, Any] | None = None) -> tuple[str, str]:
    """(domain, routing reason) from positive scene/story evidence only."""
    intent = intent if isinstance(intent, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    folded = str(text or "").casefold()
    words = _words(folded)
    declared = str(intent.get("source_domain") or "").casefold()
    if declared in DOMAIN_REASONS:
        return declared, DOMAIN_REASONS[declared]
    atmospheric = bool(words & {"atmosphere", "atmospheric", "atmosphäre"}) and bool(
        words & {"dust", "particles", "climate", "weather", "meteorology", "staub", "staubteilchen", "partikel"}
    )
    if atmospheric or words & SPACE_WORDS or any(phrase in folded for phrase in _SPACE_PHRASES):
        return "space", DOMAIN_REASONS["space"]
    if words & ARCHIVAL_WORDS or re.search(r"\b1\d{3}\b", folded) or str(intent.get("time_period") or "").strip():
        return "historical", DOMAIN_REASONS["historical"]
    if words & ART_WORDS:
        return "art_culture", DOMAIN_REASONS["art_culture"]
    if words & ANATOMY_WORDS:
        return "anatomy", DOMAIN_REASONS["anatomy"]
    if words & DIAGRAM_WORDS:
        return "diagram", DOMAIN_REASONS["diagram"]
    objects = _list(intent.get("objects"))
    named_object = any(re.fullmatch(r"[A-Z][\w'-]+(?: [A-Z][\w'-]+)+", obj.strip()) for obj in objects)
    if (
        words & {"landmark", "monument", "city", "named_entity"}
        or named_object
        or plan.get("protected_entities")
        or intent.get("named_entities")
        or intent.get("entities")
    ):
        return "entity_place", DOMAIN_REASONS["entity_place"]
    if words & NATURE_WORDS or any(_BINOMIAL.search(value) for value in objects):
        return "nature", DOMAIN_REASONS["nature"]
    return "general", DOMAIN_REASONS["general"]


def routing_text(scene: dict[str, Any], state: dict[str, Any], query: str = "") -> str:
    intent = _intent(scene)
    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    return " ".join((
        query, str(scene.get("narration") or ""), str(scene.get("visual_goal") or ""),
        positive_intent(intent), scene_story_context(scene, state, routing=True),
        " ".join(plan.get("primary_subjects") or []),
    ))


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _entities(intent: dict[str, Any]) -> list[str]:
    explicit = _list(intent.get("entities"), 4) + _list(intent.get("named_entities"), 4)
    found: list[str] = []
    if _planned(intent):
        # English planner fields: capitalised spans are names. A single
        # capitalised word at the very start of a phrase is only sentence case.
        for value in [*_list(intent.get("objects")), *_list(intent.get("subjects_to_show")),
                      *_list(intent.get("context")), str(intent.get("visual_goal") or "")]:
            for match in _PROPER.finditer(value):
                span = match.group(0).strip()
                # Only multi-word or numbered names ("Berlin Wall", "Apollo 11")
                # are inferred; a lone capitalised word may be sentence case or
                # sloppy styling. Single names come from the explicit field.
                if not span or (" " not in span and not re.search(r"\d", span)):
                    continue
                found.append(span)
    result: list[str] = []
    for value in [*explicit, *found]:
        cleaned = re.sub(r"(?:\s+(?:of|de|la|le|von|van|der|du|del))+$", "", " ".join(value.split()).strip(" ,.;:"))
        if len(cleaned) >= 2 and cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)
    return result[:3]


def _period(scene: dict[str, Any], state: dict[str, Any], intent: dict[str, Any]) -> str:
    stated = " ".join(str(intent.get("time_period") or "").split())[:40]
    if stated:
        decade = _DECADE.search(stated)
        year = _YEAR.search(stated)
        if decade:
            return (decade.group(1) or decade.group(2)) + "s"
        if year:
            return year.group(1)
        return stated
    from .visual_context import historical_requirement

    years = historical_requirement(scene, state).get("years") or []
    return str(min(years)) if years else ""


def sensitivity(scene: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    """How factually specific this scene's visual must be (the AI fallback rules read this)."""
    intent = sibling_intent(scene, state or {}) or _intent(scene)
    declared = str(intent.get("factual_sensitivity") or "").casefold()
    if declared in {"none", "real_place_or_object", "real_person", "historical_event", "scientific_specific"}:
        return declared
    text = routing_text(scene, state or {})
    domain, _reason = classify_domain(text, intent)
    if domain == "historical":
        return "historical_event"
    if domain in {"space", "anatomy"}:
        return "scientific_specific"
    if _entities(intent):
        return "real_place_or_object"
    return "none"


def scene_signals(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    intent = sibling_intent(scene, state)
    own = _intent(scene)
    text = routing_text(scene, state)
    domain, reason = classify_domain(text, intent or own, scene.get("visual_query_plan"))
    return {
        "intent_source": str((intent or own).get("source") or ("planner" if intent else "none")),
        "borrowed_block_intent": bool(intent) and intent is not own,
        "objects": _list(intent.get("objects"), 4),
        "actions": _list(intent.get("actions"), 3),
        "context": _list(intent.get("context"), 3),
        "entities": _entities(intent),
        "location": " ".join(str(intent.get("location") or "").split())[:80],
        "period": _period(scene, state, intent or own),
        "alternate_terms": _list(intent.get("alternate_terms"), 3),
        "domain": domain,
        "domain_reason": reason,
        "sensitivity": sensitivity(scene, state),
    }


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def _clean(text: str, limit: int = 6) -> str:
    from .media import _VISUAL_QUERY_STOP, _semantic_query

    return _semantic_query(str(text or ""), _VISUAL_QUERY_STOP, limit=limit)


# Direction/meta wording that describes how to film, never what is visible.
_META_WORDS = frozenset({
    "show", "shows", "showing", "depict", "depicting", "illustrate", "illustrating", "real-world",
    "mechanism", "relevant", "visible", "visual", "image", "footage", "clip", "scene", "concept",
    "progression", "resolved", "final", "chronological", "write", "story", "fictional", "zeige", "zeigen",
    "the", "and", "its", "their", "his", "her", "for", "with", "into", "onto", "den", "dem",
})
# Facets that add factual precision the authored stock queries usually lack.
FACTUAL_FACETS = frozenset({
    "entity_period", "entity_location", "narrow", "entity", "alternate", "alternate_subject", "period_subject",
})


def _join(*parts: str, limit: int = 6) -> str:
    seen: list[str] = []
    for part in parts:
        # Years/decades are factual search terms (the shared cleaner drops digits).
        words = [part.strip().casefold()] if _YEAR.fullmatch(part.strip()) or re.fullmatch(r"\d{3,4}s", part.strip()) else _clean(part, limit=limit).split()
        for word in words:
            if word not in seen and word not in _META_WORDS:
                seen.append(word)
    return " ".join(seen[:limit])


def query_tokens(query: str) -> set[str]:
    return {word for word in _words(query) if len(word) > 2}


def near_duplicate(first: str, second: str) -> bool:
    a, b = query_tokens(first), query_tokens(second)
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= NEAR_DUPLICATE_JACCARD


def dedupe_queries(queries: list[str]) -> list[str]:
    kept: list[str] = []
    for query in queries:
        if query and not any(near_duplicate(query, other) for other in kept):
            kept.append(query)
    return kept


def build_facets(scene: dict[str, Any], state: dict[str, Any], signals: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    signals = signals or scene_signals(scene, state)
    subject = signals["objects"][0] if signals["objects"] else ""
    action = signals["actions"][0] if signals["actions"] else ""
    entity = signals["entities"][0] if signals["entities"] else ""
    location, period = signals["location"], signals["period"]
    context = signals["context"][0] if signals["context"] else ""
    rows = [
        ("subject_action", _join(subject, action), "concrete subject and its visible action"),
        ("entity_period", _join(entity, period) if entity and period else "", "named entity at the stated time"),
        ("entity_location", _join(entity, location) if entity and location and not query_tokens(location) <= query_tokens(entity) else "", "named entity in its place"),
        ("narrow", _join(entity, subject) if entity and subject else "", "most specific factual query"),
        ("entity", _join(entity), "named entity alone"),
        *(("alternate", _join(term), "alternate terminology") for term in signals["alternate_terms"]),
        *(
            ("alternate_subject", _join(term, subject), "alternate terminology with the visual subject")
            for term in signals["alternate_terms"]
            if subject and signals["domain"] not in DOCUMENT_TOLERANT_DOMAINS
        ),
        ("location_subject", _join(location, subject) if location and subject else "", "subject in its location"),
        ("period_subject", _join(subject, period) if period and subject and not entity else "", "subject at the stated time"),
        ("broader", _join(context, subject, limit=5) if context else "", "broader visual context"),
    ]
    facets: list[dict[str, Any]] = []
    for facet, query, purpose in rows:
        if not query or len(query.split()) < 1:
            continue
        if any(near_duplicate(query, item["query"]) for item in facets):
            continue
        facets.append({"facet": facet, "query": query, "purpose": purpose})
    return facets[:MAX_FACETS]


def source_queries(facets: list[dict[str, Any]], domain: str = "general") -> dict[str, list[str]]:
    by_facet: dict[str, str] = {}
    for item in facets:
        by_facet.setdefault(item["facet"], item["query"])
    families = _FAMILY_FACETS if domain in DOCUMENT_TOLERANT_DOMAINS else _DEPICTION_FAMILY_FACETS
    result: dict[str, list[str]] = {}
    for family, order in families.items():
        queries = dedupe_queries([by_facet[name] for name in order if name in by_facet])[:MAX_SOURCE_QUERIES]
        if queries:
            result[family] = queries
    return result


def plan_scene_search(
    scene: dict[str, Any],
    state: dict[str, Any],
    *,
    authored: list[str],
    allowed: Any = None,
) -> dict[str, Any]:
    """The faceted search plan for one scene.

    ``authored`` are the planner's own provider-facing queries (already
    cleaned); they stay first. ``allowed`` is the caller's protection filter:
    every facet query must pass it before it can reach a provider.
    """
    signals = scene_signals(scene, state)
    facets = build_facets(scene, state, signals)
    rejected: list[dict[str, str]] = []
    safe: list[dict[str, Any]] = []
    for item in facets:
        if allowed is not None and not allowed(item["query"]):
            rejected.append({"facet": item["facet"], "query": item["query"], "reason": "protected_before_reveal"})
            continue
        if any(near_duplicate(item["query"], query) for query in authored):
            continue  # the planner's own query already covers it
        if authored and item["facet"] not in FACTUAL_FACETS:
            # Authored visual queries already describe the shot; only factual
            # precision (names, dates, places, alternate terms) is added.
            item = {**item, "retrieval": False}
        safe.append(item)
    return {
        "version": VERSION,
        "domain": signals["domain"],
        "domain_reason": signals["domain_reason"],
        "sensitivity": signals["sensitivity"],
        "signals": {key: signals[key] for key in (
            "intent_source", "borrowed_block_intent", "entities", "location", "period", "alternate_terms",
        )},
        "authored_queries": list(authored),
        "facets": safe,
        "protected_facets": rejected,
        "source_queries": source_queries(safe, signals["domain"]),
    }


def source_family(provider: str) -> str:
    return SOURCE_FAMILIES.get(str(provider or ""), "stock")


def query_for_source(scene: dict[str, Any], provider: str, logical_query: str, stage_index: int) -> str:
    """The phrasing one provider family receives for this logical query stage."""
    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    search_plan = plan.get("search_plan") if isinstance(plan.get("search_plan"), dict) else {}
    queries = (search_plan.get("source_queries") or {}).get(source_family(provider)) or []
    if 0 <= stage_index < len(queries):
        return str(queries[stage_index])
    return logical_query
