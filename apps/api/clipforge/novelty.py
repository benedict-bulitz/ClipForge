"""Conservative research-only novelty planning.

This module measures useful information relative to the evidence already
collected for the current project.  It deliberately makes no claim about
global internet-wide originality and never performs additional research.
"""
from __future__ import annotations

import re
from typing import Any

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
            if _similarity(record["words"], previous["words"]) >= 0.72:
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
