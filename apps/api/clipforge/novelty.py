"""Content-level novelty and information gain: the one authority for "does
this video tell the viewer something?".

Three levels are kept apart:

A. Topic novelty ("have we already made this video?") is NOT decided here:
   Topic Intelligence (``topic_intelligence.history.novelty_signal``) owns it.
B. Within-video information gain: every script unit must add supported
   information instead of repeating the hook, the question or an earlier
   unit (``assess_information_gain`` / ``prune_redundant_information``).
C. Audience information value: the research evidence is classified before
   writing (``build_novelty_plan``), and the finished script is checked for
   at least one supported, non-obvious gain and a payoff that really answers.

Everything is measured relative to the evidence already collected for the
current project.  It deliberately makes no claim about global internet-wide
originality, never performs additional research and never writes new text:
repairs only remove, merge or trim what the writer already said.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .story_arc import (
    _families,
    arc_units,
    comparison_sides,
    is_explanatory_question,
    mechanism_claims,
)
from .verbal_hook import (
    _NEGATED,
    _numbers,
    _related,
    _rounded_from,
    explains_mechanism,
    information_gain,
    is_salient_concept,
    proposition_words,
    spoken_simplicity,
)

_WORD_RE = re.compile(r"[a-zA-ZÀ-ÖØ-öø-ÿ0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is", "it",
    "of", "on", "or", "that", "the", "their", "this", "to", "was", "what", "when", "where", "which", "who", "why", "with",
    "der", "die", "das", "ein", "eine", "und", "ist", "sind", "zu", "von", "im", "auf", "oder", "wie", "warum", "welche", "wer",
}
_CAUSE_WORDS = re.compile(r"(?i)\b(?:because|since|therefore|due to|caused|cause|why|how|mechanism|built by|as a result|deshalb|weil|durch|mechanismus|entsteht|verursacht)\b")
_CONTRAST_WORDS = re.compile(r"(?i)\b(?:versus|vs\.?|compared|compare|more than|less than|higher than|lower than|\bthan\b|unlike|whereas|gegenüber|mehr als|weniger als|im vergleich)\b")
_SURPRISE_WORDS = re.compile(r"(?i)\b(?:unexpected|surprising|actually|despite|although|not what|entgegen|überraschend|trotz)\b")


def _words(value: object) -> set[str]:
    return {
        word.casefold()
        for word in _WORD_RE.findall(str(value or ""))
        if len(word) > 2 and word.casefold() not in _STOP_WORDS
    }


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _source_keys(fact: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for source in fact.get("sources") or []:
        if isinstance(source, dict):
            key = str(source.get("url") or source.get("label") or "").strip().casefold()
            if key:
                keys.add(key)
    return keys


def _fact_id(fact: dict[str, Any], index: int) -> str:
    return str(fact.get("id") or f"fact_{index + 1:02d}")


def _base_plan() -> dict[str, Any]:
    return {
        "status": "planned",
        "core_expected_facts": [],
        "common_context": [],
        "distinctive_facts": [],
        "explanatory_gain": [],
        "comparison_gain": [],
        "redundant_candidates": [],
        "recommended_angle": "",
        "novelty_risks": [],
        "source_support": {},
        "confidence": 0.0,
    }


def _recommended_angle(intent: dict[str, Any], plan: dict[str, Any]) -> str:
    question = " ".join(str(intent.get(key) or "") for key in ("question", "topic")).casefold()
    if plan["comparison_gain"]:
        return "Focus on the supported contrast and explain why the difference exists."
    if plan["explanatory_gain"]:
        return "Lead from the visible fact into the supported causal or mechanistic explanation."
    if "rank" in question or "top" in question:
        return "Prefer items that add distinct information rather than repeating obvious examples."
    if plan["distinctive_facts"]:
        return "Use the strongest supported additional detail without overstating its novelty."
    return "Keep the core answer clear and add only supported context that improves understanding."


def build_novelty_plan(
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None = None,
    information_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify only the current project's normalized research evidence."""
    plan = _base_plan()
    facts = [fact for fact in (facts or []) if isinstance(fact, dict) and str(fact.get("claim") or "").strip()]
    question_words = _words(" ".join(str(intent.get(key) or "") for key in ("question", "topic")))
    if not facts:
        plan.update(status="low_confidence", novelty_risks=["sparse_research"], recommended_angle=_recommended_angle(intent, plan))
        return plan

    records: list[dict[str, Any]] = []
    for index, fact in enumerate(facts):
        claim_words = _words(fact.get("claim"))
        directness = _similarity(claim_words, question_words)
        source_keys = _source_keys(fact)
        source_count = len(source_keys)
        confidence = float(fact.get("confidence") or 0.0)
        importance = float(fact.get("importance") or 0.0)
        records.append({
            "fact": fact,
            "id": _fact_id(fact, index),
            "words": claim_words,
            "directness": directness,
            "source_keys": source_keys,
            "source_count": source_count,
            "confidence": confidence,
            "importance": importance,
            "verification": str(fact.get("verification") or ""),
        })

    for index, record in enumerate(records):
        fact = record["fact"]
        claim = str(fact.get("claim") or "")
        duplicate_of: dict[str, Any] | None = None
        for previous in records[:index]:
            # The same proposition in other words (concepts, synonyms,
            # inflection) is a duplicate, not only the same words.
            same_words = _similarity(record["words"], previous["words"]) >= 0.72
            if same_words or not information_gain(previous["fact"].get("claim") or "", claim):
                duplicate_of = previous
                break
        if duplicate_of is not None:
            plan["redundant_candidates"].append(record["id"])
            continue

        has_support = record["confidence"] >= 0.75 and bool(record["source_keys"]) and record["verification"] not in {"unsupported", "uncertain"}
        core = (
            record["directness"] >= 0.34
            or str(fact.get("priority") or "").upper() == "MUST_KNOW"
            or (index == 0 and record["importance"] >= 0.7)
        )
        contrast = bool(_CONTRAST_WORDS.search(claim))
        explanatory = bool(_CAUSE_WORDS.search(claim))
        surprising = bool(_SURPRISE_WORDS.search(claim))

        if core:
            category = "EXPECTED_CORE_FACT"
            plan["core_expected_facts"].append(record["id"])
        elif explanatory and has_support:
            category = "EXPLANATORY_GAIN"
            plan["explanatory_gain"].append(record["id"])
        elif contrast and has_support:
            category = "CONTRAST_GAIN"
            plan["comparison_gain"].append(record["id"])
        elif surprising and has_support and record["source_count"] >= 2:
            category = "SURPRISING_BUT_SUPPORTED"
            plan["distinctive_facts"].append(record["id"])
        elif has_support and record["source_count"] >= 2 and record["importance"] >= 0.65:
            category = "DISTINCTIVE_DETAIL"
            plan["distinctive_facts"].append(record["id"])
        elif has_support:
            category = "SUPPORTING_DETAIL"
        else:
            category = "COMMON_CONTEXT"
            plan["common_context"].append(record["id"])

        record["category"] = category
        if record["source_count"] >= 2 and category == "EXPECTED_CORE_FACT":
            plan["common_context"].append(record["id"])

    if not plan["common_context"]:
        for record in records:
            if record.get("category") == "EXPECTED_CORE_FACT" and record["source_count"] >= 2:
                plan["common_context"].append(record["id"])
    plan["source_support"] = {
        record["id"]: sorted(record["source_keys"])
        for record in records
        if record["source_keys"]
    }
    evidence_count = sum(1 for record in records if record["source_keys"] and record["confidence"] >= 0.75)
    source_coverage = evidence_count / max(1, len(records))
    plan["confidence"] = round(min(0.95, max(0.15, source_coverage * 0.7 + min(1.0, len(records) / 5) * 0.3)), 2)
    if len(records) < 2 or evidence_count < 2:
        plan["status"] = "low_confidence"
        plan["novelty_risks"].append("sparse_research")
        plan["confidence"] = min(plan["confidence"], 0.55)
    else:
        plan["status"] = "planned"
    plan["recommended_angle"] = _recommended_angle(intent, plan)
    if not plan["distinctive_facts"] and not plan["explanatory_gain"] and not plan["comparison_gain"]:
        plan["novelty_risks"].append("no_clear_supported_gain")
    return plan


def novelty_quality_issues(state: dict[str, Any]) -> list[str]:
    plan = state.get("novelty_plan") if isinstance(state.get("novelty_plan"), dict) else {}
    if not plan:
        return []
    fact_ids = {str(fact.get("id") or "") for fact in state.get("facts", []) if isinstance(fact, dict)}
    referenced = {
        str(item)
        for key in ("core_expected_facts", "common_context", "distinctive_facts", "explanatory_gain", "comparison_gain", "redundant_candidates")
        for item in plan.get(key, [])
    }
    issues: list[str] = []
    if referenced - fact_ids:
        issues.append("novelty_references_unknown_fact")
    supported = set(plan.get("distinctive_facts", [])) | set(plan.get("explanatory_gain", [])) | set(plan.get("comparison_gain", []))
    if supported and not any(str(item) in plan.get("source_support", {}) for item in supported):
        issues.append("novelty_gain_lacks_source_support")
    if plan.get("confidence", 0) < 0.4 and plan.get("distinctive_facts"):
        issues.append("low_confidence_novelty_overstated")
    if plan.get("recommended_angle") and not (supported or plan.get("core_expected_facts")):
        issues.append("novelty_angle_lacks_support")
    return issues


def safe_novelty_plan(intent: dict[str, Any], facts: list[dict[str, Any]] | None = None, information_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return build_novelty_plan(intent, facts, information_plan)
    except Exception as exc:  # noqa: BLE001 - enrichment must never block generation
        fallback = _base_plan()
        fallback.update(status="fallback", novelty_risks=["planner_failure"], recommended_angle="Keep the core answer clear and supported.", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return fallback


# ---------------------------------------------------------------------------
# B + C on the written script: within-video information gain
# ---------------------------------------------------------------------------
# One classifier for every consumer (Pacing, local review, the pre-render
# quality gate and the generation-time repair).  The hook -> first body
# boundary uses the same primitive (``verbal_hook.information_gain``) as the
# pipeline's hook transition, so there is exactly one notion of "new".

GAIN_VERSION = 1
_SUPPORTED_VERIFICATION = {"supported", "source_attributed", "source_snippet"}
# Categories that add nothing; every other category adds information.
REDUNDANT_CATEGORIES = {"restatement", "paraphrase", "repeated_fact", "filler"}
_GAIN_CATEGORIES = ("mechanism", "quantitative", "contrast", "new_fact")
_NOVELTY_GAIN_KEYS = ("explanatory_gain", "comparison_gain", "distinctive_facts")
# Grammar of explanation (any topic): cause, mechanism, consequence.
_MECHANISM = re.compile(
    r"(?i),\s*(?:so|sodass)\s|\b(?:until|bis|because|since|therefore|thus|hence|so that|due to|caused?|causes|leads? to|results? in|"
    r"which means|this means|that means|in order to|as a result|by \w+ing|so (?:the|it|its|your|you|they|there|less|more)|"
    r"weil|denn|dadurch|deshalb|daher|darum|deswegen|sodass|so dass|führt zu|entsteht|entstehen|indem|damit)\b"
)
_CONTRAST_GAIN = re.compile(
    r"(?i)\b(?:but|instead|rather than|unlike|whereas|while|versus|vs\.?|compared|than|"
    r"aber|sondern|stattdessen|anders als|während|im vergleich|als)\b"
)
# A conclusion drawn from what was said ("Darum ...", "That's why ...").
_CONCLUSION = re.compile(
    r"(?i)^\W*(?:(?:and|und|so)\s+)?(?:darum|deshalb|daher|deswegen|dadurch|also|therefore|thus|hence|so|"
    r"that'?s why|this is why|which is why|das ist der grund|genau deshalb)\b"
)
# Sentences that only announce, react or sign off (any topic).
_FILLER = re.compile(
    r"(?i)^\W*(?:(?:and|so|now|but|und|also|jetzt|aber)\s*,?\s+)?(?:"
    r"let'?s\s+(?:find out|dive in|take a (?:closer )?look|see|break it down)|here'?s\s+(?:the thing|why|how|what happens)|"
    r"you won'?t believe|now you know|pretty (?:cool|wild|crazy)|isn'?t (?:that|it) (?:amazing|crazy|cool|wild|interesting)|"
    r"stick around|keep watching|but wait|there'?s more|the answer (?:might|may|will) surprise you|"
    r"thanks? for watching|follow for more|like and subscribe|see you next time|"
    r"lass(?:t)? uns (?:das )?(?:anschauen|herausfinden)|schauen wir mal|jetzt weißt du|ziemlich (?:cool|verrückt)|"
    r"die antwort wird dich überraschen|danke fürs zuschauen|folge für mehr|like und abonniere|bis zum nächsten mal"
    r")\b"
)
# Empty lead-ins that can be cut without touching the claim that follows.
_EMPTY_LEAD_IN = re.compile(
    r"(?i)^\s*(?:(?:and|so|now|well|okay|ok|und|also|nun)\s*,?\s+)?(?:"
    r"believe it or not|here'?s the thing|the thing is|as it turns out|it turns out(?: that)?|so basically|basically|"
    r"in other words|simply put|to put it simply|you see|interestingly(?: enough)?|fun fact|"
    r"ob du es glaubst oder nicht|im grunde(?: genommen)?|mit anderen worten|anders gesagt|interessanterweise"
    r")\s*[,:;—–-]?\s+"
)


def _text(block: dict[str, Any]) -> str:
    return " ".join(str(block.get("text") or "").split())


def _role(block: dict[str, Any]) -> str:
    return str(block.get("role") or "").casefold()


def _fact_ids(block: dict[str, Any]) -> list[str]:
    return [str(value) for value in block.get("fact_ids") or [] if str(value)]


def fact_is_supported(fact: dict[str, Any]) -> bool:
    """Evidence the video may build on: sourced, attributed, not low-confidence."""
    if not fact.get("sources") or str(fact.get("verification") or "") not in _SUPPORTED_VERIFICATION:
        return False
    confidence = fact.get("confidence")
    return confidence is None or float(confidence) >= 0.5


def strip_empty_lead_in(text: str) -> str:
    """``text`` without an empty lead-in ("Believe it or not, ..."); unchanged if
    nothing complete would remain."""
    match = _EMPTY_LEAD_IN.match(text)
    if not match:
        return text
    rest = text[match.end():].strip()
    if len(rest.split()) < 3 or not rest[:1].isalnum():
        return text
    return rest[:1].upper() + rest[1:]


def classify_gain(reference: str, sentence: str) -> dict[str, Any]:
    """What ``sentence`` adds to everything in ``reference`` (the one classifier).

    ``category`` is one of ``restatement`` (no new proposition), ``paraphrase``
    (the same proposition with at most one swapped word), ``filler``, or a gain
    category: ``mechanism``, ``quantitative``, ``contrast``, ``new_fact``.
    """
    said = proposition_words(sentence)
    gain = information_gain(reference, sentence)
    numbers = [item for item in gain if item.isdigit()]
    flipped = "negation" in gain
    words = [item for item in gain if item != "negation" and not item.isdigit()]
    coverage = 1 - len(words) / len(said) if said else 1.0
    lead_in = strip_empty_lead_in(sentence) != sentence
    filler = _FILLER.search(sentence)
    rest = sentence[: filler.start()] + sentence[filler.end():] if filler else sentence
    if filler and not numbers and len(information_gain(reference, rest)) <= 1:
        category = "filler"
    elif not words and not numbers and not flipped:
        category = "filler" if not said else "restatement"
    elif not numbers and not flipped and len(words) <= 1 and coverage >= 0.75 and not any(is_salient_concept(word) for word in words):
        # One swapped plain word is a synonym; one new relation (more often,
        # reversed, unfamiliar ...) is news.
        category = "paraphrase"
    elif _MECHANISM.search(sentence):
        category = "mechanism"
    elif numbers:
        category = "quantitative"
    elif flipped or _CONTRAST_GAIN.search(sentence):
        category = "contrast"
    else:
        category = "new_fact"
    return {
        "category": category,
        "gain_terms": [*words[:8], *numbers[:4], *(["negation"] if flipped else [])],
        "new_words": words,
        "new_numbers": numbers,
        "negation": flipped,
        "coverage": round(coverage, 3),
        "said_words": sorted(said),
        "empty_lead_in": lead_in,
    }


def _score(result: dict[str, Any], novelty_class: str) -> float:
    category = result["category"]
    if category in {"restatement", "filler"}:
        return 0.0
    if category in {"paraphrase", "repeated_fact"}:
        return 0.1
    ratio = 1 - float(result["coverage"])
    score = 0.35 + 0.4 * ratio
    score += 0.15 if category == "mechanism" else 0.0
    score += 0.15 if result["new_numbers"] else 0.0
    score += 0.1 if category == "contrast" else 0.0
    score += 0.15 if novelty_class in {"explanatory_gain", "comparison_gain", "distinctive"} else 0.0
    return round(min(1.0, score), 3)


def repeated_statements(sentences: list[str]) -> list[dict[str, Any]]:
    """Sentences that add nothing to the sentences before them (for review)."""
    found: list[dict[str, Any]] = []
    for index, sentence in enumerate(sentences):
        if index == 0 or not sentence.strip():
            continue
        result = classify_gain(" ".join(sentences[:index]), sentence)
        if result["category"] in {"restatement", "paraphrase"}:
            found.append({"index": index, "text": sentence, "category": result["category"]})
    return found


def _context(state: dict[str, Any]) -> dict[str, Any]:
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else {}
    plan = state.get("novelty_plan") if isinstance(state.get("novelty_plan"), dict) else {}
    facts = [fact for fact in state.get("facts") or [] if isinstance(fact, dict) and fact.get("id")]
    audit = state.get("explanation_audit") if isinstance(state.get("explanation_audit"), dict) else {}
    return {"intent": intent, "arc": arc, "plan": plan, "facts": facts, "audit": audit}


def _anchor_ids(arc: dict[str, Any]) -> set[str]:
    anchors = {str(arc.get(key)) for key in ("primary_answer_id", "final_payoff_id") if arc.get(key)}
    return anchors | {str(value) for value in (arc.get("hook") or {}).get("protected_ids") or []}


def _novelty_class(plan: dict[str, Any], arc: dict[str, Any], fact_ids: list[str]) -> str:
    for key, label in (("explanatory_gain", "explanatory_gain"), ("comparison_gain", "comparison_gain"),
                       ("distinctive_facts", "distinctive"), ("core_expected_facts", "core"),
                       ("common_context", "common"), ("redundant_candidates", "redundant")):
        if set(fact_ids) & {str(item) for item in plan.get(key) or []}:
            return label
    units = arc_units(arc)
    for fact_id in fact_ids:
        category = str(units.get(fact_id, {}).get("novelty") or "")
        if category in {"explanatory_gain", "comparison_gain", "distinctive"}:
            return category
    return ""


def _evidence(
    block: dict[str, Any], result: dict[str, Any], grounding: bool,
    facts_by_id: dict[str, dict[str, Any]], supported_words: set[str], supported_claims: list[str],
) -> dict[str, Any]:
    fact_ids = _fact_ids(block)
    sources = sorted({
        str(source.get("url") or source.get("label") or "")
        for fact_id in fact_ids for source in facts_by_id.get(fact_id, {}).get("sources") or []
        if isinstance(source, dict) and (source.get("url") or source.get("label"))
    })[:3]
    base = {"fact_ids": fact_ids, "sources": sources}
    if not grounding:
        return {**base, "status": "not_applicable", "reason": "No research evidence applies to this project."}
    text = _text(block)
    cited = [fact_id for fact_id in fact_ids if fact_id in facts_by_id and fact_is_supported(facts_by_id[fact_id])]
    cited_claims = [str(facts_by_id[fact_id].get("claim") or "") for fact_id in cited]
    allowed_numbers = set().union(*(_numbers(claim) for claim in supported_claims)) if supported_claims else set()
    unsupported_numbers = sorted(
        number for number in _numbers(text)
        if number not in allowed_numbers and not any(_rounded_from(text, claim) for claim in supported_claims)
    )
    if unsupported_numbers:
        return {**base, "status": "unsupported", "reason": f"The figure(s) {', '.join(unsupported_numbers)} appear in no supported fact."}
    said = set(result["said_words"])
    new = set(result["new_words"])
    if cited:
        cited_words = set().union(*(proposition_words(claim) for claim in cited_claims))
        if not said or _related(said, cited_words) or _numbers(text) & set().union(*(_numbers(claim) for claim in cited_claims)):
            return {**base, "status": "supported", "reason": "Cites supported research facts."}
        # A sentence split from a longer block inherits the block's fact IDs;
        # it is still grounded when the research as a whole says it.
    if not new:
        return {**base, "status": "derived", "reason": "Adds no new claim beyond what was already said."}
    grounded = _related(new, supported_words)
    if len(grounded) / len(new) >= 0.6:
        return {**base, "status": "derived", "reason": "Every new term is found in the supported research."}
    missing = sorted(new - grounded)[:5]
    prefix = "Cites a research fact but states something it does not. " if cited else ""
    return {**base, "status": "unsupported", "reason": prefix + "New terms not found in any supported fact: " + ", ".join(missing) + "."}


def _question(context: dict[str, Any]) -> str:
    intent, arc = context["intent"], context["arc"]
    return str(intent.get("question") or arc.get("primary_question") or intent.get("topic") or "")


def _payoff_result(
    units: list[dict[str, Any]], blocks: list[dict[str, Any]], context: dict[str, Any],
) -> dict[str, Any]:
    body = [unit for unit in units if unit["category"] != "hook"]
    if not body:
        return {"status": "missing", "result": "missing", "block_id": None, "reason": "The script has no body to pay off."}
    unit = next((item for item in body if item.get("is_payoff")), body[-1])
    text = unit["text"]
    question = _question(context)
    question_words = proposition_words(question)
    said = proposition_words(text)
    beyond = said - _related(said, question_words)
    new_numbers = _numbers(text) - _numbers(question)
    answers_choice = len(comparison_sides(question)) == 2 and "?" not in text
    answers_negation = bool(_NEGATED.search(text)) != bool(_NEGATED.search(question))
    base = {"block_id": unit["block_id"], "text": text, "category": unit["category"], "score": unit["information_gain_score"]}
    if unit["category"] == "filler":
        return {**base, "status": "fail", "result": "generic", "reason": "The payoff is a generic line, not a concrete answer."}
    if unit["category"] in REDUNDANT_CATEGORIES:
        return {**base, "status": "fail", "result": "repeats_earlier", "reason": "The payoff only repeats what the video already said."}
    if question_words and not beyond and not new_numbers and not answers_choice and not answers_negation:
        return {**base, "status": "fail", "result": "restates_question", "reason": "The payoff restates the question instead of answering it."}
    if unit.get("weak_resolution"):
        return {**base, "status": "fail", "result": "weak_resolution", "reason": "The payoff does not complete the explanation: " + unit["weak_resolution"]}
    if not unit["counts_as_gain"]:
        return {**base, "status": "fail", "result": "unsupported", "reason": "The payoff's information is not supported by the research."}
    strong = unit["category"] in {"mechanism", "quantitative", "contrast", "resolution"} or unit["novelty_class"] in {"explanatory_gain", "comparison_gain", "distinctive"}
    return {
        **base, "status": "pass", "result": "strong" if strong else "adequate",
        "reason": "The payoff resolves the question with specific, supported information.",
    }


def _signature(blocks: list[dict[str, Any]], state: dict[str, Any]) -> str:
    payload = {
        "blocks": [[str(block.get("id") or ""), _role(block), _text(block), _fact_ids(block)] for block in blocks],
        "facts": [[fact.get("id"), fact.get("claim"), fact.get("verification"), bool(fact.get("sources"))] for fact in state.get("facts") or [] if isinstance(fact, dict)],
        "arc": [str((state.get("story_arc") or {}).get(key) or "") for key in ("primary_answer_id", "final_payoff_id")] if isinstance(state.get("story_arc"), dict) else [],
        "audit": state.get("explanation_audit") if isinstance(state.get("explanation_audit"), dict) else None,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _shared_weight(said: set[str], unit: dict[str, Any]) -> tuple[int, int]:
    common = _related(said, proposition_words(unit["text"]))
    return sum(2 if is_salient_concept(word) else 1 for word in common), unit["index"]


# Beat classes (one per body unit): what the beat does for the viewer.
BEAT_CLASSES = ("useful_gain", "redundant", "off_chain", "unsupported", "weak_value")
# A beat that is mostly old words with one small addition: an elaboration,
# not progress (``_score`` gives 0.35 + 0.4 * the new share).
WEAK_GAIN_SCORE = 0.5
# Spoken clarity of a body sentence below this needs simpler words.
COMPLEX_LANGUAGE_SCORE = 0.45
# Grammar that explains a term right where it is used ("..., also ...",
# "das nennt man ...", "which means ..."): the term is then not a barrier.
_EXPLAINED_TERM = re.compile(
    r"(?i)\b(?:also|das heißt|d\. ?h\.|nennt man|heißt|bedeutet|sprich|that is|i\. ?e\.|which means|means|called|known as)\b|[–—:(]"
)


def _bare(words: set[str]) -> set[str]:
    return {word.lstrip("+-") for word in words}


def _core_terms(context: dict[str, Any], hook_text: str) -> tuple[set[str], set[str]]:
    """(subject, core): what the core question, its answer, its resolution and the hook are about."""
    arc = context["arc"]
    units = arc_units(arc)
    contract = arc.get("question_contract") if isinstance(arc.get("question_contract"), dict) else {}
    subject = {str(word).lstrip("+-") for word in contract.get("subject_terms") or []}
    texts = [_question(context), hook_text]
    texts += [str(units.get(str(arc.get(key) or ""), {}).get("claim") or "") for key in ("primary_answer_id", "final_payoff_id")]
    core = _bare(set().union(*(proposition_words(text) for text in texts if text)))
    return subject, core - _related(core, subject)


def _chain_role(fact_ids: list[str], arc: dict[str, Any]) -> str:
    units = arc_units(arc)
    if not fact_ids:
        return "none"
    if str(arc.get("primary_answer_id") or "") in fact_ids:
        return "answer"
    if str(arc.get("final_payoff_id") or "") in fact_ids:
        return "payoff"
    chain = set((arc.get("question_contract") or {}).get("essential_explanation_chain") or [])
    if set(fact_ids) & chain:
        return "chain"
    known = [fact_id for fact_id in fact_ids if fact_id in units]
    if known and all(units[fact_id].get("off_question") for fact_id in known):
        return "off_question"
    return "optional"


def _language(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """Spoken clarity of one body unit (the hook rubric's primitive, body thresholds)."""
    claims = {str(fact.get("id")): str(fact.get("claim") or "") for fact in context["facts"]}
    score, codes = spoken_simplicity(text, {"sides": comparison_sides(_question(context)), "claims": claims})
    if _EXPLAINED_TERM.search(text) and set(codes) & {"long_words", "unfamiliar_term"}:
        # A necessary term explained on the spot is fine for a 10-12 year old.
        codes = [code for code in codes if code not in {"long_words", "unfamiliar_term"}]
        score = min(1.0, score + 0.2)
    hard = score < COMPLEX_LANGUAGE_SCORE or "bureaucratic_wording" in codes
    return {"score": score, "codes": codes, "status": "complex" if hard else "clear"}


# Explanatory delta: what the viewer can *explain* after a beat that they
# could not before (stricter than new information).
EXPLANATORY_DELTAS = (
    "advances_explanation", "useful_evidence", "useful_example", "context_only", "restatement", "tangent", "weak_value",
)
# Deltas that never carry the explanation: removable once it is complete.
_PASSENGER_DELTAS = {"useful_example", "context_only", "weak_value", "restatement", "tangent"}
# Grammar only (no topic vocabulary).  "Es wirkt so, als ..." describes how
# something appears, not why it happens.
_APPEARANCE = re.compile(
    r"(?i)\b(?:wirkt|wirken|wirkte|sieht|sehen|aussehen|scheint|scheinen|looks?|seems?|appears?)\b[^.!?]*?"
    r"\b(?:als ob|als hätte\w*|als wäre\w*|als würde\w*|als sei|as if|as though)\b"
)
# Naming a term ("Dieser Effekt heißt ...") explains nothing by itself.
_LABEL = re.compile(r"(?i)\b(?:heißt|heisst|nennt man|nennen (?:das|wir|forscher\w*|fachleute)|is called|are called|known as)\b")
# Transfer to another case or an illustration.
_EXAMPLE = re.compile(
    r"(?i)\b(?:gilt auch für|das gleiche gilt|genauso (?:ist es|bei)|zum beispiel|beispielsweise|etwa wenn|"
    r"same (?:goes|is true) for|also applies|for example|for instance|like when)\b"
)
# A cause or reason stated (grammar).
_REASON = re.compile(
    r"(?i)\b(?:weil|denn|deshalb|darum|daher|dadurch|deswegen|sodass|so dass|grund|liegt (?:daran|an)|führt|"
    r"löst|lösen|auslös\w*|bewirk\w*|sorgt dafür|sorgen dafür|because|therefore|so that|reason|due to|leads? to|"
    r"causes?|triggers?)\b"
)
# Words that point at an explanation instead of giving one ("ein Teil der
# Erklärung", "dieser Effekt wird stärker"): a payoff made only of them
# resolves nothing.
_META = {
    "erklärung", "erklärungen", "teil", "grund", "gründe", "effekt", "effekte", "ursache", "phänomen", "stärker",
    "schwächer", "genau", "explanation", "part", "reason", "effect", "cause", "phenomenon", "stronger", "weaker",
}


def _norm_sentence(text: object) -> str:
    return " ".join(str(text or "").casefold().replace("„", "").replace("“", "").replace('"', "").split()).rstrip(".!?…")


def _audit_entries(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _norm_sentence(item.get("sentence")): item
        for item in (context.get("audit") or {}).get("sentences") or []
        if isinstance(item, dict) and item.get("delta") in EXPLANATORY_DELTAS
    }


def _deterministic_delta(unit: dict[str, Any], beat: str, chain: str, mechanism_ids: set[str]) -> str:
    text = unit["text"]
    if beat == "off_chain":
        return "tangent"
    if beat == "redundant":
        return "restatement"
    if beat == "unsupported":
        return "weak_value"
    if _APPEARANCE.search(text):
        return "context_only"
    if _LABEL.search(text) and len(unit["gain_terms"]) <= 3:
        return "context_only"
    if _EXAMPLE.search(text):
        return "useful_example"
    if beat == "weak_value":
        return "weak_value"
    if unit["category"] == "quantitative":
        return "useful_evidence"
    if _REASON.search(text) or unit["category"] == "mechanism" or set(unit["evidence"]["fact_ids"]) & mechanism_ids:
        return "advances_explanation"
    if chain in {"answer", "payoff", "chain"}:
        return "advances_explanation"
    return "context_only"


def _weak_resolution(unit: dict[str, Any], units: list[dict[str, Any]], context: dict[str, Any]) -> str:
    """Why a closing beat does not complete the explanation ("" when it does)."""
    if unit["category"] == "resolution":
        return ""
    if _APPEARANCE.search(unit["text"]):
        return "it only says how things appear, not why."
    earlier = [item["text"] for item in units if item["index"] < unit["index"]]
    gain = _bare(set(information_gain(" ".join([_question(context), *earlier]), unit["text"])))
    if not gain - _META and not _numbers(unit["text"]):
        return "it points at the explanation instead of completing it."
    if unit.get("delta_source") == "ai" and unit.get("explanatory_delta") in {"context_only", "weak_value", "tangent", "restatement"}:
        return "the review judged it adds no explanation."
    return ""


def _links_question(text: str, terms: set[str]) -> bool:
    said = _bare(proposition_words(text))
    words = set(re.findall(r"[\wäöüß]+", text.casefold()))
    return bool(_related(said, terms)) or bool(_families(words) & _families(terms))


def _asked_terms(question: str, contract: dict[str, Any], body: list[dict[str, Any]], hook: str = "") -> set[str]:
    """What the question asks about its subject, bare (polarity-free).

    The arc's predicate, minus words the hook and script repeat in most beats
    (the topic itself: "TikTok"), but never a comparative or a polar relation.
    """
    from .story_arc import _COMPARATIVE_FAMILIES, _FRAME

    asked = {word for word in proposition_words(question) if word not in _FRAME}
    subject = {str(word).lstrip("+-") for word in contract.get("subject_terms") or []}
    comparative = set().union(*_COMPARATIVE_FAMILIES.values())
    said = [_bare(proposition_words(text)) for text in [hook, *(unit["text"] for unit in body)] if text]
    for word in asked:
        bare = word.lstrip("+-")
        if is_salient_concept(word) or bare in comparative or len(said) < 4:
            continue
        hits = sum(1 for words in said if _related({bare}, words))
        if hits >= 3 and hits * 2 >= len(said):
            subject.add(bare)
    terms = {word.lstrip("+-") for word in asked}
    terms -= _related(terms, subject)
    return terms or {word.lstrip("+-") for word in asked}


def _answer_sufficiency(units: list[dict[str, Any]], context: dict[str, Any], payoff: dict[str, Any]) -> dict[str, Any]:
    """Could a viewer answer the ORIGINAL question in one simple sentence after the video?

    Deterministic structure for why/how questions: a mechanism beat (beyond
    the observation) that touches what the question asks, and a payoff that
    completes the path.  The review's semantic verdict is added on top; an
    "unanswered" verdict fails on its own.
    """
    arc = context["arc"]
    contract = arc.get("question_contract") if isinstance(arc.get("question_contract"), dict) else {}
    spine = contract.get("explanation_spine") if isinstance(contract.get("explanation_spine"), dict) else {}
    question = _question(context)
    explanatory = is_explanatory_question(question)
    ai = (context.get("audit") or {}).get("answer_sufficiency")
    ai = ai if isinstance(ai, dict) and ai.get("verdict") in {"answered", "partial", "unanswered"} else None
    primary = str(arc.get("primary_answer_id") or "")
    body = [unit for unit in units if unit["category"] != "hook"]
    hook = next((unit["text"] for unit in units if unit["category"] == "hook"), "")
    terms = _asked_terms(question, contract, body, hook)
    mechanisms = [
        unit for unit in body
        if unit.get("explanatory_delta") == "advances_explanation" and not unit.get("weak_resolution")
        # The observation itself (the answer's own fact, stated without a cause) explains nothing yet.
        and not (set(unit["evidence"]["fact_ids"]) <= {primary} and not _REASON.search(unit["text"]))
    ]
    linked = [unit for unit in mechanisms if _links_question(unit["text"], terms)]
    reasons: list[str] = []
    if explanatory and not linked:
        reasons.append("no_mechanism_linked_to_question")
    if explanatory and payoff.get("status") == "fail":
        reasons.append("payoff_does_not_resolve")
    if ai and ai["verdict"] == "unanswered":
        reasons.append("review_unanswered")
    elif ai and ai["verdict"] == "partial":
        reasons.append("review_partial")
    missing_research = spine.get("status") == "missing_mechanism"
    if explanatory and missing_research:
        reasons.append("research_has_no_mechanism")
    structural = {"no_mechanism_linked_to_question", "payoff_does_not_resolve"} & set(reasons)
    if not explanatory and not ai:
        status = "not_applicable"
    elif "review_unanswered" in reasons or len(structural) == 2 or (missing_research and structural):
        status = "fail"
    elif "review_partial" in reasons or "payoff_does_not_resolve" in reasons:
        status = "warning"
    elif reasons:
        # One lexical signal alone (a paraphrased link, thin research) is a diagnostic.
        status = "uncertain"
    else:
        status = "pass"
    return {
        "status": status,
        "explanatory_question": explanatory,
        "reasons": reasons,
        "mechanism_block_ids": [unit["block_id"] for unit in linked],
        "question_terms": sorted(terms),
        "review_verdict": ai["verdict"] if ai else None,
        "one_sentence_answer": (ai or {}).get("one_sentence_answer") or "",
        "missing": (ai or {}).get("missing") or "",
        "research_required": status == "fail" and (missing_research or "no_mechanism_linked_to_question" in reasons),
    }


def _audit_beats(units: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """Relevance, beat class, language and viewer momentum per body unit (in place)."""
    arc = context["arc"]
    hook = next((unit for unit in units if unit["category"] == "hook"), None)
    subject, core = _core_terms(context, hook["text"] if hook else "")
    question = _question(context)
    body = [unit for unit in units if unit["category"] != "hook"]
    payoff_index = next((unit["index"] for unit in body if unit.get("is_payoff")), body[-1]["index"] if body else -1)
    contract = arc.get("question_contract") if isinstance(arc.get("question_contract"), dict) else {}
    mechanism_ids = set((contract.get("explanation_spine") or {}).get("mechanism") or [])
    audit = _audit_entries(context)
    learned: list[str] = []
    for unit in body:
        fact_ids = unit["evidence"]["fact_ids"]
        chain = _chain_role(fact_ids, arc)
        said = _bare(proposition_words(unit["text"]))
        own = said - _related(said, subject)
        # Off the question: its facts are same-subject tangents and its own
        # wording does not tie it back to the question, answer or hook.
        off = chain == "off_question" and not _related(own, core)
        if unit["category"] in REDUNDANT_CATEGORIES:
            beat = "redundant"
        elif off:
            beat = "off_chain"
        elif not unit["counts_as_gain"]:
            beat = "unsupported"
        elif (
            unit["category"] == "new_fact" and not unit.get("is_payoff") and unit["role"] != "answer"
            and float(unit["information_gain_score"] or 0.0) < WEAK_GAIN_SCORE
            and unit["novelty_class"] not in {"explanatory_gain", "comparison_gain", "distinctive"}
        ):
            beat = "weak_value"
        else:
            beat = "useful_gain"
        new = list(unit["gain_terms"][:6]) if beat == "useful_gain" else []
        resolved = unit["index"] >= payoff_index
        if unit["index"] > payoff_index:
            reason = "none: the question is already resolved"
        elif resolved:
            reason = "none needed: this beat resolves the question"
        elif beat == "useful_gain":
            reason = "the question is still open and this beat moved it forward"
        else:
            reason = "weak: the question is still open, but this beat adds nothing toward it"
        unit.update(
            chain_role=chain,
            beat_class=beat,
            language=_language(unit["text"], context),
            momentum={
                "viewer_knows_before": learned[-6:],
                "new_information": new,
                "viewer_understands_after": (learned + new)[-6:],
                "unresolved_question": "" if resolved else question,
                "reason_to_continue": reason,
            },
        )
        learned += [term for term in new if term not in learned]
        delta, source, needed = _deterministic_delta(unit, beat, chain, mechanism_ids), "deterministic", None
        judged = audit.get(_norm_sentence(unit["text"]))
        if judged:
            # The review judges explanatory value; evidence stays deterministic
            # (an unsupported beat never becomes an explanation).
            source, needed = "ai", bool(judged.get("needed"))
            delta = judged["delta"] if beat != "unsupported" else delta
        unit.update(explanatory_delta=delta, delta_source=source, delta_needed=needed)
    payoff = next((unit for unit in body if unit.get("is_payoff")), None)
    if payoff is not None:
        payoff["weak_resolution"] = _weak_resolution(payoff, units, context)
    # Weak tail: once the last beat that explains has been heard, passengers
    # before the payoff only delay the ending.
    last = max((unit["index"] for unit in body if not unit.get("is_payoff") and unit["explanatory_delta"] in {"advances_explanation", "useful_evidence"}), default=None)
    for unit in body:
        unit["weak_tail"] = bool(
            last is not None and last < unit["index"] < payoff_index and not unit.get("is_payoff")
            and unit["explanatory_delta"] in _PASSENGER_DELTAS and unit.get("delta_needed") is not True
        )


def _plateaus(body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Runs of two or more consecutive beats that move the viewer nowhere."""
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for unit in body:
        if unit.get("beat_class") in {"redundant", "weak_value", "off_chain"} and not unit.get("is_payoff"):
            current.append(unit)
            continue
        if len(current) >= 2:
            runs.append(current)
        current = []
    if len(current) >= 2:
        runs.append(current)
    return runs


def assess_blocks(blocks: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-unit assessment of ``blocks`` in order (pure; never mutates)."""
    context = _context(state)
    intent, arc, plan, facts = context["intent"], context["arc"], context["plan"], context["facts"]
    facts_by_id = {str(fact["id"]): fact for fact in facts}
    supported_claims = [str(fact.get("claim") or "") for fact in facts if fact_is_supported(fact)]
    supported_words = set().union(*(proposition_words(claim) for claim in supported_claims)) if supported_claims else set()
    supported_words |= proposition_words(_question(context))
    grounding = bool(facts) and intent.get("research_required", True) is not False and intent.get("content_type") != "fictional_story"
    anchors = _anchor_ids(arc)
    units: list[dict[str, Any]] = []
    hook_text = ""
    told: set[str] = set()
    for index, block in enumerate(blocks):
        text, role = _text(block), _role(block)
        fact_ids = _fact_ids(block)
        block_id = str(block.get("id") or f"block_{index + 1:02d}")
        if role == "hook":
            hook_text = text
            units.append({
                "block_id": block_id, "index": index, "role": role, "text": text, "category": "hook",
                "redundancy": "none", "repeats_block_id": None, "new_information": "", "gain_terms": [],
                "information_gain_score": None, "counts_as_gain": False, "novelty_class": "",
                "evidence": {"status": "not_applicable", "fact_ids": fact_ids, "sources": [], "reason": "The hook is judged by the hook selection."},
                "protected": True, "empty_lead_in": False, "reason": "Opening hook (owned by hook selection).",
            })
            continue
        earlier = [unit for unit in units if unit["text"]]
        reference = " ".join(unit["text"] for unit in earlier)
        result = classify_gain(reference, text)
        category = result["category"]
        if (
            category not in REDUNDANT_CATEGORIES and fact_ids and set(fact_ids) <= told
            and not result["new_numbers"] and not result["negation"]
            and len(result["new_words"]) <= 1 and result["coverage"] >= 0.6
            and not any(is_salient_concept(word) for word in result["new_words"])
        ):
            # Its research fact was already told and the words barely differ.
            category = "repeated_fact"
        repeats = None
        if category in REDUNDANT_CATEGORIES and category != "filler":
            # A shared relation (reversed, familiar ...) identifies the
            # repeated statement better than a shared noun.
            repeats = max(earlier, key=lambda unit: _shared_weight(set(result["said_words"]), unit), default=None)
        novelty_class = _novelty_class(plan, arc, fact_ids)
        evidence = _evidence(block, result, grounding, facts_by_id, supported_words, supported_claims)
        redundancy = "none"
        if category == "filler":
            redundancy = "filler"
        elif category in REDUNDANT_CATEGORIES:
            redundancy = "restates_hook" if repeats is not None and repeats["category"] == "hook" else ("repeated_fact" if category == "repeated_fact" else "restates_earlier")
        gains = category not in REDUNDANT_CATEGORIES
        counts = gains and evidence["status"] in {"supported", "derived", "not_applicable"}
        if not gains:
            reason = {
                "filler": "Generic filler: announces or reacts instead of informing.",
                "restatement": "Says nothing that was not already said.",
                "paraphrase": "Paraphrases an earlier statement (at most one word differs).",
                "repeated_fact": "Repeats a research fact that was already told.",
            }[category]
        elif not counts:
            reason = "Would add information, but it is not grounded in the research: " + evidence["reason"]
        else:
            reason = {
                "mechanism": "Explains a cause or mechanism.",
                "quantitative": "Adds a supported figure.",
                "contrast": "Adds a contrast or distinction.",
                "new_fact": "Adds a new supported fact.",
            }[category]
        units.append({
            "block_id": block_id, "index": index, "role": role, "text": text, "category": category,
            "redundancy": redundancy, "repeats_block_id": repeats["block_id"] if repeats else None,
            "new_information": ", ".join(result["gain_terms"]) if gains else "",
            "gain_terms": result["gain_terms"],
            "information_gain_score": _score({**result, "category": category}, novelty_class),
            "counts_as_gain": counts, "novelty_class": novelty_class, "evidence": evidence,
            "protected": bool(set(fact_ids) & anchors) or role in {"answer", "payoff"},
            "empty_lead_in": result["empty_lead_in"], "reason": reason,
        })
        told |= set(fact_ids)
    if hook_text:
        for unit in units:
            if unit["category"] != "hook":
                unit["adds_to_hook"] = bool(information_gain(hook_text, unit["text"]))
    payoff = _payoff_unit(units, arc)
    if payoff is not None:
        payoff["is_payoff"] = True
        payoff["protected"] = True
        if payoff["category"] in {"restatement", "paraphrase", "repeated_fact"} and _resolves(payoff, units, context):
            # The closing synthesis: it draws on what the body established and
            # answers the question with it - the payoff, not a repetition.
            payoff.update(
                category="resolution", redundancy="none", repeats_block_id=None,
                new_information="resolves the question with: " + ", ".join(payoff["resolution_terms"][:6]),
                information_gain_score=0.7,
                counts_as_gain=payoff["evidence"]["status"] in {"supported", "derived", "not_applicable"},
                reason="Resolves the question by drawing the established mechanism together.",
            )
    _audit_beats(units, context)
    return units


def _payoff_unit(units: list[dict[str, Any]], arc: dict[str, Any]) -> dict[str, Any] | None:
    """The unit that closes the video: the end of the payoff block.

    A payoff block split into sentences ends in its resolution, so plain
    ``detail`` continuations after the last payoff-role unit belong to it.
    """
    body = [unit for unit in units if unit["category"] != "hook"]
    if not body:
        return None
    final_id = str(arc.get("final_payoff_id") or "")
    start = next((position for position in range(len(body) - 1, -1, -1) if body[position]["role"] == "payoff"), None)
    if start is None and final_id:
        start = next((position for position in range(len(body) - 1, -1, -1) if final_id in body[position]["evidence"]["fact_ids"]), None)
    if start is None:
        return body[-1]
    end = start
    while end + 1 < len(body) and body[end + 1]["role"] == "detail":
        end += 1
    return body[end]


def _resolves(unit: dict[str, Any], units: list[dict[str, Any]], context: dict[str, Any]) -> bool:
    """A closing sentence that links the question to what the body established.

    It must touch the question and draw on concepts the body introduced
    (not the hook or the question): from two different units, or from one
    with an explicit conclusion ("Darum ...").  Restating only the question or
    the hook never qualifies.
    """
    question = proposition_words(_question(context))
    hook = next((item for item in units if item["category"] == "hook"), None)
    opening = question | (proposition_words(hook["text"]) if hook else set())
    said = proposition_words(unit["text"])
    if not question or not _related(said, question | opening):
        return False
    sources: dict[str, int] = {}
    for item in units:
        if item["category"] == "hook" or item["index"] >= unit["index"] or item["redundancy"] != "none":
            continue
        for word in proposition_words(item["text"]) - _related(proposition_words(item["text"]), opening):
            sources.setdefault(word, item["index"])
    drawn = {word: sources[word] for word in said if word in sources}
    unit["resolution_terms"] = sorted(drawn)
    return len(set(drawn.values())) >= 2 or (bool(drawn) and bool(_CONCLUSION.search(unit["text"])))


def assess_information_gain(state: dict[str, Any]) -> dict[str, Any]:
    """The canonical content-level information-gain report for ``state``."""
    blocks = [block for block in (state.get("script") or {}).get("blocks") or [] if isinstance(block, dict)]
    context = _context(state)
    units = assess_blocks(blocks, state)
    body = [unit for unit in units if unit["category"] != "hook"]
    issues: list[dict[str, str]] = []

    def issue(code: str, severity: str, message: str, block_id: str | None = None) -> None:
        issues.append({"code": code, "severity": severity, "message": message, **({"block_id": block_id} if block_id else {})})

    hook = next((unit for unit in units if unit["category"] == "hook"), None)
    first = body[0] if body else None
    if hook is None or first is None:
        transition = {"status": "not_applicable", "block_id": first["block_id"] if first else None, "gain_terms": []}
    else:
        gain = information_gain(hook["text"], first["text"])
        passed = bool(gain) and first["category"] not in REDUNDANT_CATEGORIES
        transition = {"status": "pass" if passed else "fail", "block_id": first["block_id"], "gain_terms": gain[:8]}
        if not passed:
            issue("hook_body_no_information_gain", "warning", "The sentence after the hook adds no new information.", first["block_id"])
    if hook is not None:
        spent = explains_mechanism(hook["text"], _question(context), mechanism_claims(context["arc"]))
        if spent:
            issue("hook_explains_mechanism", "warning", "The hook already states the explanation (" + ", ".join(spent[:4]) + "); it should open the question, not finish it.", hook["block_id"])
    for unit in body:
        if unit.get("beat_class") == "off_chain":
            issue("off_question_segment", "warning", f"Same subject, different question: “{unit['text'][:80]}”", unit["block_id"])
        if (unit.get("language") or {}).get("status") == "complex":
            issue("complex_language", "warning", f"Hard to follow on first listen ({', '.join(unit['language']['codes'][:3]) or 'difficult wording'}): “{unit['text'][:80]}”", unit["block_id"])
    for run in _plateaus(body):
        issue("information_plateau", "warning", f"{len(run)} beats in a row add nothing new: “{run[0]['text'][:60]}” …", run[0]["block_id"])
    for unit in body:
        if unit["redundancy"] == "filler":
            issue("filler_segment", "warning", f"Generic filler: “{unit['text'][:80]}”", unit["block_id"])
        elif unit["redundancy"] != "none":
            issue("redundant_segment", "warning", f"Repeats earlier information: “{unit['text'][:80]}”", unit["block_id"])
        elif not unit["counts_as_gain"]:
            issue("unsupported_information", "warning", unit["reason"][:240], unit["block_id"])
    payoff = _payoff_result(units, blocks, context)
    if payoff["status"] == "fail":
        issue(f"payoff_{payoff['result']}", "warning", payoff["reason"], payoff["block_id"])
    tail = [unit for unit in body if unit.get("weak_tail")]
    if tail:
        issue("weak_tail", "warning", f"{len(tail)} beat(s) after the explanation is complete only delay the ending: “{tail[0]['text'][:60]}”", tail[0]["block_id"])
    for unit in body:
        if unit.get("delta_source") == "ai" and unit.get("delta_needed") is False and unit["explanatory_delta"] in _PASSENGER_DELTAS and not unit.get("weak_tail") and not unit.get("is_payoff"):
            issue("low_explanatory_value", "warning", f"Adds no explanation ({unit['explanatory_delta']}): “{unit['text'][:80]}”", unit["block_id"])
    sufficiency = _answer_sufficiency(units, context, payoff)
    if sufficiency["status"] in {"fail", "warning", "uncertain"}:
        message = "The video does not let a viewer answer the original question (" + ", ".join(sufficiency["reasons"]) + ")"
        if sufficiency["research_required"]:
            message += "; research must supply the missing mechanism - never pad or substitute advice"
        issue("answer_insufficient", {"fail": "error", "warning": "warning"}.get(sufficiency["status"], "info"), message + ".")
    counted = [unit for unit in body if unit["counts_as_gain"]]
    redundant = [unit for unit in body if unit["redundancy"] != "none"]
    unsupported = [unit for unit in body if unit["category"] not in REDUNDANT_CATEGORIES and not unit["counts_as_gain"]]
    strongest = max(counted, key=lambda unit: unit["information_gain_score"] or 0.0, default=None)
    plan = context["plan"]
    if any(unit["category"] in {"mechanism", "quantitative", "contrast"} or unit["novelty_class"] in {"explanatory_gain", "comparison_gain", "distinctive"} for unit in counted):
        audience_value = "strong"
    elif counted:
        audience_value = "basic"
    else:
        audience_value = "none"
    used = {fact_id for unit in body for fact_id in unit["evidence"]["fact_ids"]}
    available_gain = [str(item) for key in _NOVELTY_GAIN_KEYS for item in plan.get(key) or []]
    unused_gain = [fact_id for fact_id in dict.fromkeys(available_gain) if fact_id not in used]
    grounding = any(unit["evidence"]["status"] != "not_applicable" for unit in body)
    on_question = [unit for unit in counted if unit.get("beat_class") != "off_chain"]
    if body and not counted and grounding:
        issue("no_supported_information_gain", "error", "No part of the script adds supported information.")
    elif body and counted and not on_question and grounding:
        # Only same-subject tangents: tighter research is needed, never padding.
        issue("no_question_relevant_information", "error", "The script only tells facts about the subject that do not answer the question; research must be tightened.")
    elif audience_value == "basic" and unused_gain:
        issue("supported_gain_unused", "info", "Only expected basics are told although the research supports a stronger explanatory or distinctive fact.")
    scores = [float(unit["information_gain_score"] or 0.0) for unit in body]
    density = round(len(counted) / len(body), 3) if body else 0.0
    if not counted:
        status = "empty"
    elif redundant or unsupported:
        status = "diluted"
    elif len(counted) == 1 and len(body) == 1:
        status = "thin"
    else:
        status = "dense"
    return {
        "version": GAIN_VERSION,
        "status": status,
        "signature": _signature(blocks, state),
        "units": units,
        "hook_transition": transition,
        "payoff": payoff,
        "summary": {
            "body_units": len(body),
            "gain_units": len(counted),
            "redundant_units": len(redundant),
            "unsupported_units": len(unsupported),
            "density": density,
            "score": round(100 * sum(scores) / len(scores), 1) if scores else 0.0,
            "audience_value": audience_value,
            "strongest": {key: strongest[key] for key in ("block_id", "text", "category", "information_gain_score", "novelty_class")} if strongest else None,
            "evidence_thin": plan.get("status") == "low_confidence" or "sparse_research" in (plan.get("novelty_risks") or []),
            "unused_supported_gain_fact_ids": unused_gain,
            "beat_classes": {name: sum(1 for unit in body if unit.get("beat_class") == name) for name in BEAT_CLASSES},
            "plateaus": len(_plateaus(body)),
            "explanatory_deltas": {name: sum(1 for unit in body if unit.get("explanatory_delta") == name) for name in EXPLANATORY_DELTAS},
        },
        "answer_sufficiency": sufficiency,
        "question_contract": {
            **{key: value for key, value in ((context["arc"].get("question_contract") or {}) if isinstance(context["arc"].get("question_contract"), dict) else {}).items()},
            "core_question": _question(context),
            "hook_promise": hook["text"] if hook else ((context["arc"].get("question_contract") or {}).get("hook_promise") if isinstance(context["arc"].get("question_contract"), dict) else ""),
        },
        "issues": issues,
    }


def current_information_gain(state: dict[str, Any]) -> dict[str, Any]:
    """The stored report when it still describes the script, else a fresh one."""
    stored = state.get("information_gain") if isinstance(state.get("information_gain"), dict) else {}
    blocks = [block for block in (state.get("script") or {}).get("blocks") or [] if isinstance(block, dict)]
    try:
        if stored.get("units") is not None and stored.get("signature") == _signature(blocks, state):
            return stored
        return assess_information_gain(state)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block generation
        return {"version": GAIN_VERSION, "status": "fallback", "units": [], "issues": [], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def refresh_information_gain(state: dict[str, Any]) -> dict[str, Any]:
    """Recompute and persist ``state["information_gain"]`` (repairs are kept)."""
    previous = state.get("information_gain") if isinstance(state.get("information_gain"), dict) else {}
    report = {**current_information_gain(state), "repairs": list(previous.get("repairs") or [])}
    state["information_gain"] = report
    return report


def information_gain_quality_issues(state: dict[str, Any]) -> list[dict[str, str]]:
    return list(current_information_gain(state).get("issues") or [])


# ---------------------------------------------------------------------------
# Safe repair (generation time only; never adds or rewrites a claim)
# ---------------------------------------------------------------------------

def _dependencies(arc: dict[str, Any]) -> dict[str, set[str]]:
    return {fact_id: {str(dep) for dep in unit.get("depends_on") or []} for fact_id, unit in arc_units(arc).items()}


def _pre_reveal_body(blocks: list[dict[str, Any]], arc: dict[str, Any]) -> int | None:
    """Body blocks before the protected reveal (``None``: no protected reveal)."""
    if not (arc.get("curiosity_gap") or {}).get("withhold_answer"):
        return None
    primary = str(arc.get("primary_answer_id") or "")
    for index, block in enumerate(blocks):
        if (primary and primary in _fact_ids(block)) or _role(block) == "answer":
            return sum(1 for item in blocks[:index] if _role(item) != "hook")
    return None


def _removal_blocked(
    blocks: list[dict[str, Any]], index: int, arc: dict[str, Any], anchors: set[str],
    target: int | None = None, *, closing: bool = False,
) -> str | None:
    block = blocks[index]
    if _role(block) in {"hook", "answer"} or (_role(block) == "payoff" and not closing):
        return "protected"
    final = str(arc.get("final_payoff_id") or "")
    for fact_id in set(_fact_ids(block)) & anchors:
        # An anchor may only lose a repetition: the reveal keeps its first
        # telling (an earlier unit), the final payoff its closing one (a later
        # unit, or the unit that takes over the closing role).
        if closing and fact_id == final:
            continue
        others = range(index + 1, len(blocks)) if fact_id == final else range(index)
        # The earlier unit this one repeats receives its facts: an evidence
        # sentence restating the answer hands the answer fact to the answer.
        if target is not None and target < index and fact_id != final:
            continue
        if not any(fact_id in _fact_ids(blocks[position]) and _role(blocks[position]) != "hook" for position in others):
            return "protected"
    if sum(1 for item in blocks if _role(item) != "hook") <= 1:
        return "last_body_unit"
    reveal = _pre_reveal_body(blocks, arc)
    if reveal is not None and reveal <= 1:
        before = _pre_reveal_body(blocks[:index] + blocks[index + 1:], arc)
        if before is not None and before < reveal:
            return "reveal_timing"
    return None


def _merge_target(
    blocks: list[dict[str, Any]], index: int, target_id: str | None, deps: dict[str, set[str]],
    optional: set[str] | None = None,
) -> tuple[int | None, bool]:
    """Where the removed unit's own facts go (index, possible)."""
    own = set(_fact_ids(blocks[index]))
    told_before = {fact_id for block in blocks[:index] if _role(block) != "hook" for fact_id in _fact_ids(block)}
    for fact_id in own - told_before:
        # A later telling takes over only if nothing in between needs the fact.
        carrier = next((position for position in range(index + 1, len(blocks)) if fact_id in _fact_ids(blocks[position])), None)
        if carrier is not None and any(fact_id in deps.get(other, set()) for block in blocks[index + 1: carrier] for other in _fact_ids(block)):
            return None, False
    elsewhere = {fact_id for position, block in enumerate(blocks) if position != index and _role(block) != "hook" for fact_id in _fact_ids(block)}
    if own <= elsewhere:
        return None, True
    target = next((position for position, block in enumerate(blocks) if str(block.get("id") or "") == target_id), None)
    if target is None or _role(blocks[target]) == "hook":
        # Nothing can carry its facts (the hook carries none): it may only go
        # when the arc marks every fact it alone tells as optional.
        dropped = own - elsewhere
        needed = any(dropped & deps.get(fact_id, set()) for fact_id in elsewhere)
        return None, bool(optional) and dropped <= optional and not needed

    told_by_target = {fact_id for block in blocks[: target + 1] for fact_id in _fact_ids(block)}
    moving = own - elsewhere
    # Facts move only to a unit whose own text already says them, and only if
    # everything they depend on is told by then.
    if any(deps.get(fact_id, set()) - told_by_target - moving for fact_id in moving):
        return None, False
    return target, True


def _hand_over_payoff(
    blocks: list[dict[str, Any]], units: list[dict[str, Any]], arc: dict[str, Any], anchors: set[str],
    deps: dict[str, set[str]], repairs: list[dict[str, Any]],
) -> bool:
    """A closing unit that only repeats the body gives the payoff to the beat before it.

    "Und mit zunehmendem Alter wird dieser Effekt stärker" after the age
    correlation was already told is not a payoff; the last informative beat
    (the mechanism) is.  Only the last body unit hands over, only to an
    informative unit that is not the answer, and it takes its role and its
    research facts (the arc's final payoff stays on the closing unit).
    """
    payoff = next((unit for unit in units if unit.get("is_payoff")), None)
    body = [unit for unit in units if unit["category"] != "hook"]
    # Only a closing unit that adds no proposition at all: a one-word
    # paraphrase may still carry the closing fact ("der größte Inselstaat").
    if payoff is None or body[-1] is not payoff or len(body) < 2:
        return False
    weak = bool(payoff.get("weak_resolution"))
    if payoff["category"] != "restatement" and not weak:
        return False
    successor = body[-2]
    if weak and payoff["category"] != "restatement" and successor.get("explanatory_delta") not in {"advances_explanation", "useful_evidence"}:
        return False
    # Never the answer or its own continuation (a unit telling only the answer's facts).
    answer_facts = {fact_id for unit in body if unit["role"] == "answer" for fact_id in unit["evidence"]["fact_ids"]}
    answer_facts |= {str(arc.get("primary_answer_id") or "")} - {""}
    own = set(successor["evidence"]["fact_ids"])
    continues_answer = own <= answer_facts if own else any(unit["role"] == "answer" for unit in body[:-1])
    if (
        successor["role"] in {"hook", "answer"} or continues_answer
        or successor["category"] in REDUNDANT_CATEGORIES or not successor["counts_as_gain"]
    ):
        return False
    index, target = payoff["index"], successor["index"]
    if _removal_blocked(blocks, index, arc, anchors, closing=True):
        return False
    elsewhere = {fact_id for position, block in enumerate(blocks) if position != index for fact_id in _fact_ids(block)}
    moving = [fact_id for fact_id in _fact_ids(blocks[index]) if fact_id not in elsewhere]
    told = {fact_id for block in blocks[: target + 1] for fact_id in _fact_ids(block)}
    if any(deps.get(fact_id, set()) - told - set(moving) for fact_id in moving):
        return False
    removed = blocks.pop(index)
    closing = blocks[target]
    previous_role = _role(closing)
    closing["role"] = "payoff"
    closing["fact_ids"] = [*_fact_ids(closing), *moving]
    repairs.append({
        "action": "hand_over_payoff", "block_id": removed.get("id"), "text": _text(removed),
        "category": payoff["category"], "repeats_block_id": payoff["repeats_block_id"], "moved_fact_ids": moving,
        "payoff_block_id": closing.get("id"), "payoff_previous_role": previous_role,
    })
    return True


def _drop_beat(
    blocks: list[dict[str, Any]], units: list[dict[str, Any]], arc: dict[str, Any], anchors: set[str],
    deps: dict[str, set[str]], optional: set[str], repairs: list[dict[str, Any]],
) -> bool:
    """Remove one beat that does not move the viewer toward the answer.

    An off-question beat (a same-subject tangent) always qualifies; a weak
    elaboration only inside a plateau and only when every fact it alone
    tells is optional.  Never the hook, answer, payoff or an anchor, never a
    fact something else builds on, and nothing is added in its place.
    """
    body = [unit for unit in units if unit["category"] != "hook"]
    weak = {id(unit) for run in _plateaus(body) for unit in run if unit.get("beat_class") == "weak_value"}
    # The review judged these sentences unnecessary for understanding.
    weak |= {
        id(unit) for unit in body
        if unit.get("delta_source") == "ai" and unit.get("delta_needed") is False and unit["explanatory_delta"] in _PASSENGER_DELTAS
    }
    weak |= {id(unit) for unit in body if unit.get("weak_tail")}
    for unit in body:
        tangent = unit.get("beat_class") == "off_chain"
        if unit.get("is_payoff") or not (tangent or id(unit) in weak):
            continue
        index = unit["index"]
        own = set(_fact_ids(blocks[index]))
        elsewhere = {fact_id for position, block in enumerate(blocks) if position != index for fact_id in _fact_ids(block)}
        dropped = own - elsewhere
        if not tangent and not dropped <= optional:
            continue
        if any(dropped & deps.get(fact_id, set()) for fact_id in elsewhere) or _removal_blocked(blocks, index, arc, anchors):
            continue
        removed = blocks.pop(index)
        action = "remove_off_question" if tangent else ("remove_weak_tail" if unit.get("weak_tail") else (
            "remove_low_explanation" if unit.get("delta_source") == "ai" else "remove_weak_value"))
        repairs.append({
            "action": action,
            "block_id": removed.get("id"), "text": _text(removed), "category": unit["explanatory_delta"] if not tangent else unit["beat_class"],
            "repeats_block_id": None, "moved_fact_ids": [], "dropped_fact_ids": sorted(dropped),
        })
        return True
    return False


def prune_redundant_information(
    blocks: list[dict[str, Any]], state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove, merge and trim redundancy in the writer's own units.

    Safe repairs only: an empty lead-in is cut, a unit that repeats earlier
    information or is pure filler is removed (its research fact IDs move to
    the unit that already says them), and a unit fully restated by the next,
    richer unit gives way to it.  The hook, the answer/payoff units and any
    unit carrying a protected or anchor fact are never removed or moved, the
    protected reveal keeps at least one unit before it, and no text is ever
    added.  Returns the new blocks and the applied repairs.
    """
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else {}
    anchors = _anchor_ids(arc)
    deps = _dependencies(arc)
    optional = {fact_id for fact_id, unit in arc_units(arc).items() if unit.get("may_be_omitted")} - anchors
    blocks = [dict(block) for block in blocks]
    repairs: list[dict[str, Any]] = []
    for block in blocks:
        if _role(block) == "hook":
            continue
        trimmed = strip_empty_lead_in(_text(block))
        if trimmed != _text(block):
            repairs.append({"action": "trim_lead_in", "block_id": block.get("id"), "before": _text(block), "after": trimmed})
            block["text"] = trimmed
    for _round in range(len(blocks)):
        units = assess_blocks(blocks, state)
        changed = False
        for unit in units:
            index = unit["index"]
            if unit["category"] not in REDUNDANT_CATEGORIES or unit.get("is_payoff"):
                continue
            target, possible = _merge_target(blocks, index, unit["repeats_block_id"], deps, optional)
            if not possible or _removal_blocked(blocks, index, arc, anchors, target):
                continue
            removed = blocks.pop(index)
            moved: list[str] = []
            if target is not None:
                target -= 1 if target > index else 0
                elsewhere = {fact_id for block in blocks for fact_id in _fact_ids(block)}
                moved = [fact_id for fact_id in _fact_ids(removed) if fact_id not in elsewhere]
                blocks[target]["fact_ids"] = [*_fact_ids(blocks[target]), *moved]
            remaining = {fact_id for block in blocks for fact_id in _fact_ids(block)}
            repairs.append({
                "action": "remove_filler" if unit["category"] == "filler" else ("merge_redundant" if moved else "remove_redundant"),
                "block_id": removed.get("id"), "text": _text(removed), "category": unit["category"],
                "repeats_block_id": unit["repeats_block_id"], "moved_fact_ids": moved,
                "dropped_fact_ids": sorted(set(_fact_ids(removed)) - remaining),
            })
            changed = True
            break
        if changed:
            continue
        if _drop_beat(blocks, units, arc, anchors, deps, optional, repairs):
            continue
        if _hand_over_payoff(blocks, units, arc, anchors, deps, repairs):
            continue
        # A unit whose every word the next, richer unit repeats gives way to it.
        closing = {unit["index"] for unit in units if unit.get("is_payoff")}
        for index in range(len(blocks) - 1):
            current, following = blocks[index], blocks[index + 1]
            if _role(current) == "hook" or _role(following) == "hook" or set(_fact_ids(following)) & anchors or index in closing:
                continue
            said = proposition_words(_text(current))
            if len(said) < 2 or _related(said, proposition_words(_text(following))) != said or not information_gain(_text(current), _text(following)):
                continue
            if _numbers(_text(current)) - _numbers(_text(following)) or _removal_blocked(blocks, index, arc, anchors):
                continue
            removed = blocks.pop(index)
            following["fact_ids"] = list(dict.fromkeys([*_fact_ids(removed), *_fact_ids(following)]))
            repairs.append({
                "action": "replace_with_stronger", "block_id": removed.get("id"), "text": _text(removed),
                "category": "subsumed", "repeats_block_id": following.get("id"), "moved_fact_ids": _fact_ids(removed),
            })
            changed = True
            break
        if not changed:
            break
    return blocks, repairs
