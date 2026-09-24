"""Story / information arc: what the viewer learns, when, and in which role.

The arc is the shared semantic backbone for payoff, hook, script, scene,
reaction, pacing and visual planning.  It is keyed by the stable research
fact IDs (``fact_01`` ...) so every consumer refers to information by
identity instead of matching narration text.

The planner may supply an arc; it is validated and repaired here.  Without
one, a deterministic arc is derived from the facts, the selected format, the
novelty plan and the question.  Building the arc never fails generation: any
error yields a safe generic ordering.

Timing is not decided here.  The arc says WHAT must exist and in which order;
real TTS durations still decide how long it takes.
"""
from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

ARC_VERSION = 1
ROLES = (
    "primary_answer",
    "essential_context",
    "evidence",
    "comparison",
    "supporting_fact",
    "explanation",
    "secondary_insight",
    "ranked_item",
)
# How the viewer's question is resolved over time, per selected format.
STRUCTURES = {
    "comparison": "reveal",
    "quiz": "reveal",
    "ranking": "ranked_progression",
    "misconception_correction": "correction",
    "before_after": "progression",
    "story": "progression",
    "explanation": "answer_first",
}
# Script block role that best carries each information role.
BLOCK_ROLE = {
    "primary_answer": "answer",
    "explanation": "explanation",
    "ranked_item": "support",
}

_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "who", "why", "how",
    "are", "is", "was", "were", "has", "have", "than", "into", "its", "their", "there", "about",
    "der", "die", "das", "den", "dem", "des", "und", "für", "mit", "von", "welche", "welcher",
    "welches", "wer", "warum", "wieso", "wie", "ist", "sind", "hat", "haben", "als",
    "auf", "aus", "bei", "ein", "eine", "einer", "eines", "einem", "einen", "im", "in", "zu",
    "sich", "auch", "noch", "nur", "oder", "or", "vs", "versus", "anzahl", "number",
}
# Comparative grammar (not topic vocabulary): which dimension a question or a
# claim compares.  A claim comparing a different dimension than the question
# is an extra insight, not the answer.
_COMPARATIVE_FAMILIES = {
    "quantity": {"more", "most", "many", "mehr", "meisten", "meiste", "meist", "viele"},
    "fewer": {"less", "least", "fewer", "fewest", "weniger", "wenigsten", "wenigste"},
    "size": {"bigger", "biggest", "larger", "largest", "größer", "größte", "größten", "größter", "groesser", "groesste"},
    "smallness": {"smaller", "smallest", "kleiner", "kleinste", "kleinsten", "kleinster"},
    "speed": {"faster", "fastest", "schneller", "schnellste", "schnellsten", "schnellster"},
    "height": {"higher", "highest", "taller", "tallest", "höher", "höchste", "höchsten", "höchster"},
    "age": {"older", "oldest", "älter", "älteste", "ältesten", "ältester"},
    "quality": {"better", "best", "besser", "beste", "besten", "bester"},
    "length": {"longer", "longest", "länger", "längste", "längsten", "längster"},
}
_CAUSE = re.compile(
    r"(?i)\b(?:because|since|therefore|thus|hence|so that|due to|caused?|causes|leads? to|results? in|"
    r"weil|denn|dadurch|deshalb|daher|darum|deswegen|sodass|so dass|führt zu|entsteht|entstehen|entstand|entstanden)\b"
)
_RANK = re.compile(r"(?i)(?:#|\bplatz\s*|\brang\s*|\bnummer\s*|\bnumber\s*|\bno\.\s*|\brank\s*)(\d{1,2})\b|^\s*(\d{1,2})[.):]\s")
# Digits or spelled-out magnitudes (grammar, not topic vocabulary).
_QUANTITY = re.compile(
    r"(?i)\d|\b(?:hundreds?|thousands?|millions?|billions?|trillions?|dozens?|"
    r"hundert|tausend|million(?:en)?|milliarden?|billion(?:en)?|dutzend)\b"
)
_WORD = re.compile(r"[\wäöüß]+", re.UNICODE)
_OUTRO = re.compile(
    r"(?i)^\s*(?:thanks? for watching|follow for more|like and subscribe|see you|"
    r"danke fürs zuschauen|folge für mehr|like und abonniere|bis zum nächsten mal)\b"
)


def _words(value: object) -> set[str]:
    return {
        word for word in _WORD.findall(str(value or "").lower())
        if len(word) > 2 and word not in _STOP and not word.isdigit()
    }


def _same_word(first: str, second: str) -> bool:
    """Same-language inflection match (Baum/Bäume excluded; Insel/Inseln, tree/trees)."""
    if first == second:
        return True
    short, long = sorted((first, second), key=len)
    return len(short) >= 4 and long.startswith(short) and len(long) - len(short) <= 3


def _overlap(first: set[str], second: set[str]) -> int:
    return sum(1 for word in first if any(_same_word(word, other) for other in second))


def _families(words: set[str]) -> set[str]:
    return {name for name, members in _COMPARATIVE_FAMILIES.items() if words & members}


def comparison_sides(question: str) -> list[set[str]]:
    """Content words of the alternatives in "A or B" / "A oder B" / "A vs B" questions."""
    text = str(question or "").strip(" ?!.")
    parts = re.split(r"(?i)\s+(?:oder|or|vs\.?|versus)\s+", text, maxsplit=1)
    if len(parts) != 2:
        return []
    left = re.split(r"[:–—-]\s*|\?\s*", parts[0])[-1]
    left_words = _words(left)
    # Drop question/comparison scaffolding that precedes the first alternative.
    for family in _COMPARATIVE_FAMILIES.values():
        left_words -= family
    right_words = _words(re.split(r"[?.!,;]", parts[1])[0])
    return [side for side in (left_words, right_words) if side]


def _novelty_category(novelty: dict[str, Any], fact_id: str) -> str:
    for key, name in (
        ("redundant_candidates", "redundant"),
        ("explanatory_gain", "explanatory"),
        ("comparison_gain", "comparison"),
        ("distinctive_facts", "distinctive"),
        ("core_expected_facts", "core"),
        ("common_context", "common"),
    ):
        if fact_id in (novelty.get(key) or []):
            return name
    return "supporting"


def _rank(claim: str) -> int | None:
    match = _RANK.search(claim)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _structure(format_name: str, protected: bool) -> str:
    structure = STRUCTURES.get(format_name, "answer_first")
    if structure == "reveal" and format_name == "comparison" and not protected:
        # A comparison whose result is not protected answers first; the
        # contrast and explanation then become the arc.
        return "answer_first"
    return structure


def _classify(
    facts: list[dict[str, Any]], question: str, structure: str, novelty: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return per-fact units and the deterministic primary answer candidate."""
    question_words = _words(question)
    question_families = _families(question_words | set(_WORD.findall(question.lower())))
    sides = comparison_sides(question)
    units: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for index, fact in enumerate(facts):
        fact_id = str(fact["id"])
        claim = str(fact.get("claim") or "")
        claim_words = _words(claim)
        all_words = set(_WORD.findall(claim.lower()))
        families = _families(all_words)
        side_hits = sum(1 for side in sides if _overlap(claim_words, side))
        category = _novelty_category(novelty, fact_id)
        directness = _overlap(claim_words, question_words) / max(1, len(question_words))
        verdict = side_hits >= 2 and bool(families)
        family_match = bool(families & question_families)
        # A claim that compares both alternatives of the question is the verdict,
        # whatever comparative word it uses ("größer" asked, "mehr" answered).
        other_dimension = bool(families) and bool(question_families) and not family_match and not verdict
        score = (
            directness * 2.0
            + (3.0 if verdict else 0.0)
            + (2.0 if family_match and (side_hits or not sides) else 0.0)
            + (1.0 if category == "core" else 0.0)
            + float(fact.get("importance") or 0.0) * 0.5
            - (2.5 if other_dimension else 0.0)
            - index * 0.05
        )
        scores[fact_id] = score
        has_number = bool(_QUANTITY.search(claim))
        if structure == "ranked_progression" and _rank(claim) is not None:
            role = "ranked_item"
        elif other_dimension and side_hits:
            role = "secondary_insight"
        elif _CAUSE.search(claim) or category == "explanatory":
            role = "explanation"
        elif sides and side_hits and has_number:
            role = "comparison"
        elif has_number:
            role = "evidence"
        elif category == "common":
            role = "essential_context"
        else:
            role = "supporting_fact"
        units[fact_id] = {
            "id": fact_id,
            "fact_ids": [fact_id],
            "role": role,
            "claim": claim,
            "importance": round(float(fact.get("importance") or 0.0), 3),
            "novelty": category,
            "surprise_value": 0.8 if category == "distinctive" else 0.5 if role == "secondary_insight" else 0.2,
            "rank": _rank(claim) if role == "ranked_item" else None,
            "depends_on": [],
            "stage": 0,
            "may_appear_in_hook": True,
            "may_be_omitted": category in {"redundant", "common"},
            "requires_prior_context": False,
            "source": "deterministic",
        }
    candidates = [fact_id for fact_id in units if units[fact_id]["role"] not in {"secondary_insight", "ranked_item"}]
    primary = max(candidates or list(units), key=lambda fact_id: scores[fact_id], default=None)
    return units, primary


def _apply_supplied(
    units: dict[str, dict[str, Any]], supplied: dict[str, Any] | None, repairs: list[str]
) -> tuple[str | None, str | None, str]:
    """Merge a planner-supplied arc (by 1-based fact index) into the units."""
    if not isinstance(supplied, dict):
        return None, None, ""
    ids = list(units)

    def fact_id(value: object) -> str | None:
        try:
            position = int(value)
        except (TypeError, ValueError):
            return None
        return ids[position - 1] if 1 <= position <= len(ids) else None

    for item in supplied.get("units") or []:
        if not isinstance(item, dict):
            continue
        target = fact_id(item.get("fact_index"))
        if target is None:
            repairs.append("dropped_unit_with_unknown_fact")
            continue
        role = str(item.get("role") or "").strip().casefold()
        if role in ROLES:
            units[target]["role"] = role
            units[target]["source"] = "planner"
        elif role:
            repairs.append(f"unknown_role_{role[:24]}")
        deps = [dep for dep in (fact_id(value) for value in item.get("depends_on") or []) if dep and dep != target]
        if deps:
            units[target]["depends_on"] = deps
            units[target]["planner_dependencies"] = True
        if item.get("may_appear_in_hook") is False:
            units[target]["may_appear_in_hook"] = False
            units[target]["planner_hook_block"] = True
    primary = fact_id(supplied.get("primary_answer_index"))
    final = fact_id(supplied.get("final_payoff_index"))
    if supplied.get("primary_answer_index") is not None and primary is None:
        repairs.append("primary_answer_reference_repaired")
    if supplied.get("final_payoff_index") is not None and final is None:
        repairs.append("final_payoff_reference_repaired")
    return primary, final, " ".join(str(supplied.get("curiosity_gap") or "").split())[:240]


def _order_rank(role: str, structure: str) -> int:
    if structure == "reveal":
        order = ["essential_context", "evidence", "comparison", "supporting_fact", "primary_answer", "explanation", "secondary_insight"]
        return order.index(role) if role in order else len(order)
    # Answer-first and correction arcs keep research order after the answer:
    # it is the causal chain the evidence was collected in.
    if role == "secondary_insight":
        return 3
    if structure == "correction":
        return {"essential_context": 0, "primary_answer": 1}.get(role, 2)
    return 0 if role == "primary_answer" else 1


def _break_cycles(units: dict[str, dict[str, Any]], repairs: list[str]) -> None:
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        kept: list[str] = []
        for dep in units[node]["depends_on"]:
            if dep not in units:
                repairs.append("dropped_unknown_dependency")
                continue
            if state.get(dep) == 1:
                repairs.append(f"dependency_cycle_broken:{node}->{dep}")
                continue
            if state.get(dep) is None:
                visit(dep)
            kept.append(dep)
        units[node]["depends_on"] = kept
        state[node] = 2

    for node in list(units):
        if state.get(node) is None:
            visit(node)


def _dependents(units: dict[str, dict[str, Any]], root: str | None) -> set[str]:
    """Units that (transitively) depend on ``root``."""
    found: set[str] = set()
    changed = bool(root)
    while changed:
        changed = False
        for fact_id, unit in units.items():
            if fact_id not in found and any(dep == root or dep in found for dep in unit["depends_on"]):
                found.add(fact_id)
                changed = True
    return found


def _topological(units: dict[str, dict[str, Any]], priority: dict[str, tuple]) -> list[str]:
    placed: list[str] = []
    remaining = set(units)
    while remaining:
        ready = [node for node in remaining if all(dep in placed for dep in units[node]["depends_on"])]
        if not ready:  # defensive: cycles are broken before ordering
            ready = list(remaining)
        node = min(ready, key=lambda item: priority[item])
        placed.append(node)
        remaining.remove(node)
    return placed


def build_story_arc(
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    *,
    protected: bool = False,
    supplied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive (and repair) the structured information arc for one project."""
    facts = [fact for fact in (facts or []) if isinstance(fact, dict) and fact.get("id") and str(fact.get("claim") or "").strip()]
    format_name = str((format_plan or {}).get("selected_format") or "explanation")
    structure = _structure(format_name, protected)
    question = " ".join(str((intent or {}).get(key) or "") for key in ("question",)).strip() or str((intent or {}).get("topic") or "")
    novelty = novelty_plan if isinstance(novelty_plan, dict) else {}
    repairs: list[str] = []
    units, primary = _classify(facts, question, structure, novelty)
    supplied_primary, supplied_final, supplied_gap = _apply_supplied(units, supplied, repairs)
    if supplied_primary:
        primary = supplied_primary
    ranked = sorted((unit for unit in units.values() if unit["role"] == "ranked_item"), key=lambda unit: (unit["rank"] is None, unit["rank"] or 0))
    if structure == "ranked_progression":
        if len(ranked) < 2:
            # No explicit ranks: research order is the ranking basis.
            ranked = [unit for unit in units.values() if unit["role"] != "essential_context"]
            for position, unit in enumerate(ranked, 1):
                unit.update(role="ranked_item", rank=position)
            repairs.append("rank_basis_research_order")
        primary = ranked[0]["id"] if ranked else primary
    if primary and primary in units and units[primary]["role"] not in {"ranked_item"}:
        units[primary]["role"] = "primary_answer"
    for unit in units.values():
        if unit["id"] != primary and unit["role"] == "primary_answer":
            unit["role"] = "supporting_fact"
            repairs.append("duplicate_primary_answer_demoted")

    # Dependencies (planner-supplied ones are kept; the structure fills gaps).
    withhold = structure in {"reveal", "ranked_progression"}
    for unit in units.values():
        if unit.get("planner_dependencies"):
            continue
        role = unit["role"]
        if structure == "ranked_progression" and role == "ranked_item":
            deeper = [other["id"] for other in ranked if (other["rank"] or 0) == (unit["rank"] or 0) + 1]
            unit["depends_on"] = deeper
        elif role == "primary_answer" and structure in {"reveal", "correction"}:
            unit["depends_on"] = [
                other["id"] for other in units.values()
                if other["id"] != unit["id"]
                and _order_rank(other["role"], structure) < _order_rank("primary_answer", structure)
                and not other["may_be_omitted"]
            ]
        elif role in {"explanation", "secondary_insight"} and primary and unit["id"] != primary:
            unit["depends_on"] = [primary]
    # A causal chain keeps its research order: each explanation follows the previous one.
    explanations = [unit for unit in units.values() if unit["role"] == "explanation"]
    for previous, current in pairwise(explanations):
        if not current.get("planner_dependencies") and previous["id"] not in current["depends_on"]:
            current["depends_on"].append(previous["id"])
    _break_cycles(units, repairs)

    # Final payoff: may differ from the primary answer.
    secondary = [unit for unit in units.values() if unit["role"] == "secondary_insight" and unit["id"] != primary]
    key_surprise = max(secondary, key=lambda unit: (unit["surprise_value"], unit["importance"]), default=None)
    if supplied_final and supplied_final in units:
        final = supplied_final
    elif structure == "ranked_progression":
        final = primary
    elif key_surprise is not None and key_surprise["novelty"] != "redundant":
        final = key_surprise["id"]
    elif structure == "answer_first" and explanations:
        final = explanations[-1]["id"]
    else:
        final = primary

    fact_position = {fact_id: index for index, fact_id in enumerate(units)}
    # The final payoff closes the arc, except that an answer-first arc never
    # pushes its own answer to the end.
    final_last = final if (final != primary or structure in {"reveal", "ranked_progression"}) else None

    def priority(fact_id: str) -> tuple:
        unit = units[fact_id]
        if structure == "ranked_progression":
            rank = -(unit["rank"] or 0) if unit["role"] == "ranked_item" else -1000
            return (fact_id == final_last, rank, fact_position[fact_id])
        return (fact_id == final_last, _order_rank(unit["role"], structure), fact_position[fact_id])

    order = _topological(units, {fact_id: priority(fact_id) for fact_id in units})
    if final_last is None and len(order) > 1:
        final = order[-1]  # the last meaningful beat after an early answer
    after_answer = _dependents(units, primary)
    for position, fact_id in enumerate(order):
        unit = units[fact_id]
        unit["stage"] = 1 + max((units[dep]["stage"] for dep in unit["depends_on"]), default=0) if unit["depends_on"] else 1
        unit["order"] = position
        unit["requires_prior_context"] = bool(unit["depends_on"])
        # In a withheld arc nothing that presupposes the answer may open the video.
        if withhold and (fact_id in {primary, final} or fact_id in after_answer or unit["role"] == "secondary_insight"):
            unit["may_appear_in_hook"] = False
        if fact_id in {primary, final} or unit["role"] in {"primary_answer", "ranked_item"}:
            unit["may_be_omitted"] = False
    # Everything the primary answer or final payoff depends on is required.
    required: set[str] = set()
    stack = [fact_id for fact_id in (primary, final) if fact_id]
    while stack:
        current = stack.pop()
        if current in required or current not in units:
            continue
        required.add(current)
        stack.extend(units[current]["depends_on"])
    for fact_id in required:
        units[fact_id]["may_be_omitted"] = False
    for unit in units.values():
        unit["must_not_appear_before"] = list(unit["depends_on"])
        unit.pop("planner_dependencies", None)
        unit.pop("planner_hook_block", None)

    if primary is None:
        status = "empty"
    elif repairs:
        status = "repaired"
    else:
        status = "planned"
    gap_closer = primary if withhold or structure == "correction" else final
    arc = {
        "version": ARC_VERSION,
        "status": status,
        "source": "planner" if isinstance(supplied, dict) and supplied else "deterministic",
        "format": format_name,
        "structure": structure,
        "primary_question": question,
        "primary_answer_id": primary,
        "final_payoff_id": final,
        "key_surprise_id": key_surprise["id"] if key_surprise else None,
        "curiosity_gap": {
            "question": question,
            "planner_text": supplied_gap or None,
            "closed_by": gap_closer,
            "withhold_answer": withhold,
            "kind": {
                "reveal": "which_answer",
                "ranked_progression": "which_rank_wins",
                "correction": "what_is_actually_true",
            }.get(structure, "why_or_how"),
        },
        "hook": {
            "protected_ids": sorted(fact_id for fact_id, unit in units.items() if not unit["may_appear_in_hook"]),
            "allowed_ids": [fact_id for fact_id in order if units[fact_id]["may_appear_in_hook"]],
        },
        "order": order,
        "units": [units[fact_id] for fact_id in order],
        "repairs": repairs,
    }
    arc["issues"] = story_arc_issues(arc)
    return arc


def fallback_story_arc(
    intent: dict[str, Any], facts: list[dict[str, Any]] | None, format_plan: dict[str, Any] | None, error: str
) -> dict[str, Any]:
    """Safe generic ordering: facts in research order, first fact answers."""
    facts = [fact for fact in (facts or []) if isinstance(fact, dict) and fact.get("id")]
    ids = [str(fact["id"]) for fact in facts]
    question = str((intent or {}).get("question") or (intent or {}).get("topic") or "")
    return {
        "version": ARC_VERSION,
        "status": "fallback",
        "source": "fallback",
        "format": str((format_plan or {}).get("selected_format") or "explanation"),
        "structure": "answer_first",
        "primary_question": question,
        "primary_answer_id": ids[0] if ids else None,
        "final_payoff_id": ids[-1] if ids else None,
        "key_surprise_id": None,
        "curiosity_gap": {"question": question, "planner_text": None, "closed_by": ids[0] if ids else None, "withhold_answer": False, "kind": "why_or_how"},
        "hook": {"protected_ids": [], "allowed_ids": ids},
        "order": ids,
        "units": [
            {
                "id": fact_id, "fact_ids": [fact_id], "role": "primary_answer" if index == 0 else "supporting_fact",
                "claim": str(fact.get("claim") or ""), "importance": 0.0, "novelty": "supporting", "surprise_value": 0.0,
                "rank": None, "depends_on": [ids[index - 1]] if index else [], "must_not_appear_before": [ids[index - 1]] if index else [],
                "stage": index + 1, "order": index, "may_appear_in_hook": True, "may_be_omitted": False,
                "requires_prior_context": bool(index), "source": "fallback",
            }
            for index, (fact_id, fact) in enumerate(zip(ids, facts))
        ],
        "repairs": [],
        "issues": [],
        "error": error[:200],
    }


def safe_story_arc(
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    *,
    protected: bool = False,
    supplied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Story planning is enrichment: it must never stop generation."""
    try:
        return build_story_arc(intent, facts, format_plan, novelty_plan, protected=protected, supplied=supplied)
    except Exception as exc:  # noqa: BLE001 - fall back to a safe generic ordering
        return fallback_story_arc(intent, facts, format_plan, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Accessors used by consumers
# ---------------------------------------------------------------------------

def arc_units(arc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(arc, dict):
        return {}
    return {str(unit.get("id")): unit for unit in arc.get("units") or [] if isinstance(unit, dict) and unit.get("id")}


def unit_claim(arc: dict[str, Any] | None, unit_id: str | None) -> str:
    return str(arc_units(arc).get(str(unit_id or ""), {}).get("claim") or "")


def hook_safe_facts(facts: list[dict[str, Any]], arc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Facts that may feed an opening hook without spoiling the arc's reveal."""
    units = arc_units(arc)
    if not units:
        return facts
    blocked = {fact_id for fact_id, unit in units.items() if not unit.get("may_appear_in_hook", True)}
    return [fact for fact in facts if str(fact.get("id") or "") not in blocked]


def omittable_fact_ids(arc: dict[str, Any] | None) -> set[str]:
    return {fact_id for fact_id, unit in arc_units(arc).items() if unit.get("may_be_omitted")}


def essential_fact_ids(arc: dict[str, Any] | None) -> set[str]:
    return {fact_id for fact_id, unit in arc_units(arc).items() if not unit.get("may_be_omitted")}


def story_brief(arc: dict[str, Any] | None) -> dict[str, Any]:
    """Compact arc view for providers (writer, hook) and the triple hook."""
    if not isinstance(arc, dict) or not arc.get("units"):
        return {}
    units = arc_units(arc)
    return {
        "structure": arc.get("structure"),
        "format": arc.get("format"),
        "primary_question": arc.get("primary_question"),
        "primary_answer_id": arc.get("primary_answer_id"),
        "final_payoff_id": arc.get("final_payoff_id"),
        "key_surprise_id": arc.get("key_surprise_id"),
        "curiosity_gap": arc.get("curiosity_gap"),
        "information_order": [
            {
                "fact_id": fact_id,
                "role": units[fact_id]["role"],
                "depends_on": units[fact_id]["depends_on"],
                "may_appear_in_hook": units[fact_id]["may_appear_in_hook"],
                "may_be_omitted": units[fact_id]["may_be_omitted"],
            }
            for fact_id in arc.get("order") or []
            if fact_id in units
        ],
    }


# ---------------------------------------------------------------------------
# Script / scene mapping
# ---------------------------------------------------------------------------

def _block_units(block: dict[str, Any], units: dict[str, dict[str, Any]]) -> list[str]:
    return [fact_id for fact_id in block.get("fact_ids") or [] if fact_id in units]


def _dominant_role(unit_ids: list[str], units: dict[str, dict[str, Any]], arc: dict[str, Any]) -> str | None:
    if not unit_ids:
        return None
    if arc.get("primary_answer_id") in unit_ids:
        return units[arc["primary_answer_id"]]["role"]
    if arc.get("final_payoff_id") in unit_ids:
        return units[arc["final_payoff_id"]]["role"]
    return min((units[fact_id]["role"] for fact_id in unit_ids), key=lambda role: ROLES.index(role) if role in ROLES else len(ROLES))


def annotate_story_roles(state: dict[str, Any]) -> dict[str, Any] | None:
    """Map blocks and scenes to arc units by fact ID and validate the script.

    Scenes (and their visual intents) receive ``story_role`` metadata so
    reaction, pacing and visual planning know what information a scene carries.
    """
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else None
    if not arc:
        return None
    try:
        units = arc_units(arc)
        blocks = list(state.get("script", {}).get("blocks") or [])
        block_units = {str(block.get("id") or ""): _block_units(block, units) for block in blocks}
        withhold = bool(arc.get("curiosity_gap", {}).get("withhold_answer"))
        for scene in state.get("scenes") or []:
            if not isinstance(scene, dict):
                continue
            unit_ids = block_units.get(str(scene.get("block_id") or ""), [])
            role = _dominant_role(unit_ids, units, arc)
            is_answer = arc.get("primary_answer_id") in unit_ids
            before_reveal = withhold and not _reveal_reached(state, scene, arc, block_units)
            if is_answer:
                stage = "reveal"
            elif before_reveal:
                stage = "before_reveal"
            else:
                stage = "after_reveal" if withhold else "open"
            scene.update(
                story_role=role,
                story_unit_ids=unit_ids,
                is_primary_answer=is_answer,
                is_final_payoff=arc.get("final_payoff_id") in unit_ids,
                story_stage=stage,
            )
            intent = scene.get("visual_intent")
            if isinstance(intent, dict):
                # Clean semantic input for visual direction; no visual decisions here.
                intent["story_role"] = role
                intent["story_stage"] = stage
        arc["block_units"] = {block_id: unit_ids for block_id, unit_ids in block_units.items() if unit_ids}
        arc["script_issues"] = story_script_issues(state)
        covered = {fact_id for unit_ids in block_units.values() for fact_id in unit_ids}
        missing = sorted(essential_fact_ids(arc) - covered) if covered else []
        arc["completeness"] = {
            "mapped": bool(covered),
            "complete": bool(covered) and not missing and arc.get("primary_answer_id") in covered,
            "missing_required_ids": missing,
        }
    except Exception as exc:  # noqa: BLE001 - annotation is advisory
        arc["script_issues"] = [f"story_annotation_failed:{type(exc).__name__}"]
    return arc


def _reveal_reached(
    state: dict[str, Any], scene: dict[str, Any], arc: dict[str, Any], block_units: dict[str, list[str]]
) -> bool:
    primary = arc.get("primary_answer_id")
    for other in state.get("scenes") or []:
        if primary in block_units.get(str(other.get("block_id") or ""), []):
            return True
        if other is scene:
            return False
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def story_arc_issues(arc: dict[str, Any]) -> list[str]:
    """Structural checks of the arc itself."""
    units = arc_units(arc)
    issues: list[str] = []
    if not units:
        return issues
    primary, final = arc.get("primary_answer_id"), arc.get("final_payoff_id")
    if not primary:
        issues.append("no_primary_answer")
    elif primary not in units:
        issues.append("primary_answer_unknown_fact")
    if final and final not in units:
        issues.append("final_payoff_unknown_fact")
    if primary in units and units[primary]["role"] == "secondary_insight":
        issues.append("secondary_insight_as_primary_answer")
    position = {fact_id: index for index, fact_id in enumerate(arc.get("order") or [])}
    for fact_id, unit in units.items():
        for dep in unit.get("depends_on") or []:
            if dep not in units:
                issues.append(f"unknown_dependency:{fact_id}")
            elif position.get(dep, -1) > position.get(fact_id, -1):
                issues.append(f"dependency_order_violation:{fact_id}")
    if _has_cycle(units):
        issues.append("dependency_cycle")
    if arc.get("curiosity_gap", {}).get("withhold_answer") and primary in units and units[primary].get("may_appear_in_hook"):
        issues.append("protected_answer_allowed_in_hook")
    return issues


def _has_cycle(units: dict[str, dict[str, Any]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for dep in units[node].get("depends_on") or []:
            if dep in units and (state.get(dep) == 1 or (state.get(dep) is None and visit(dep))):
                return True
        state[node] = 2
        return False

    return any(state.get(node) is None and visit(node) for node in units)


def story_script_issues(state: dict[str, Any]) -> list[str]:
    """Advisory checks of the written script against the arc (never raises)."""
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else None
    units = arc_units(arc)
    blocks = [block for block in state.get("script", {}).get("blocks") or [] if isinstance(block, dict)]
    if not arc or not units or not blocks:
        return []
    issues: list[str] = []
    mapped = [(block, _block_units(block, units)) for block in blocks]
    if not any(unit_ids for _block, unit_ids in mapped):
        return ["script_not_mapped_to_story_arc"]
    primary, final = arc.get("primary_answer_id"), arc.get("final_payoff_id")
    first_seen: dict[str, int] = {}
    for index, (_block, unit_ids) in enumerate(mapped):
        for fact_id in unit_ids:
            first_seen.setdefault(fact_id, index)
    for fact_id in sorted(essential_fact_ids(arc)):
        if fact_id not in first_seen:
            issues.append(f"required_fact_missing:{fact_id}")
    if primary and primary not in first_seen:
        issues.append("primary_answer_missing_from_script")
    for fact_id, index in first_seen.items():
        for dep in units[fact_id].get("depends_on") or []:
            if dep in first_seen and first_seen[dep] > index:
                issues.append(f"fact_before_dependency:{fact_id}")
    withhold = bool(arc.get("curiosity_gap", {}).get("withhold_answer"))
    for block, unit_ids in mapped:
        if str(block.get("role") or "").casefold() == "hook" and withhold and set(unit_ids) & set(arc.get("hook", {}).get("protected_ids") or []):
            issues.append("protected_reveal_in_hook")
    answer_block = next((unit_ids for block, unit_ids in mapped if str(block.get("role") or "").casefold() == "answer" and unit_ids), None)
    if answer_block and primary not in answer_block and any(units[fact_id]["role"] == "secondary_insight" for fact_id in answer_block):
        issues.append("secondary_insight_presented_as_answer")
    previous: tuple[str, ...] | None = None
    covered: set[str] = set()
    for block, unit_ids in mapped:
        key = tuple(sorted(unit_ids))
        role = str(block.get("role") or "").casefold()
        if key and key != previous and role != "detail" and set(key) <= covered:
            issues.append(f"duplicate_information_block:{block.get('id') or ''}")
        covered |= set(unit_ids)
        previous = key if key else previous
    if final and final in first_seen:
        tail = mapped[max(index for index, (_b, unit_ids) in enumerate(mapped) if final in unit_ids) + 1:]
        for block, unit_ids in tail:
            new = [fact_id for fact_id in unit_ids if fact_id != final]
            if _OUTRO.search(str(block.get("text") or "")) or (not new and str(block.get("role") or "").casefold() != "detail"):
                issues.append("filler_after_final_payoff")
                break
    return list(dict.fromkeys(issues))


def story_quality_issues(state: dict[str, Any]) -> list[str]:
    """All arc and script issues for the review/QA layer."""
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else None
    if not arc:
        return []
    return list(dict.fromkeys([*story_arc_issues(arc), *story_script_issues(state)]))
