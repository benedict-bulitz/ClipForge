"""One QAC research retry owner and provenance-preserving evidence combination."""
from __future__ import annotations

import copy
from dataclasses import fields
from typing import Any
from urllib.parse import urlsplit

from .narration import clean_research_claim
from .novelty import fact_is_supported
from .question_answer_contract import answer_obligations, checked_coverage
from .research import ResearchResult
from .research_v2.answer_relation import question_frame
from .research_v2.corroboration import find_contradictions, group_claims, independence_clusters
from .research_v2.evidence import EvidenceUnit
from .research_v2.package import (
    build_package,
    legacy_facts,
    link_package_facts,
    select_claims,
    weak_core,
)
from .research_v2.routing import route_question


def normalize_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    by_claim: dict[str, dict[str, Any]] = {}
    for original in facts:
        fact = copy.deepcopy(original)
        # Once normalized, never clean a different semantic version or renumber it.
        claim = str(fact.get("claim") or "") if fact.get("qac_normalized") else clean_research_claim(fact.get("claim"))
        if not claim:
            continue
        fact.setdefault("raw_claim", str(fact.get("claim") or ""))
        fact.update(claim=claim, qac_normalized=True)
        fact["confidence"] = float(fact.get("confidence") if fact.get("confidence") is not None else 0.5)
        if "sources" not in fact:
            label, url = fact.pop("source_label", None), fact.pop("source_url", None)
            fact["sources"] = [{"label": label, "url": url}] if label and url else []
        fact["verification"] = fact.get("verification") or (
            "source_attributed" if fact["sources"] else "unverified_model_synthesis"
        )
        fact["raw_claims"] = list(dict.fromkeys([*fact.get("raw_claims", []), fact["raw_claim"]]))
        fact["research_keys"] = list(dict.fromkeys([*fact.get("research_keys", []), *([fact["research_key"]] if fact.get("research_key") else [])]))
        key = " ".join(claim.casefold().split())
        if key in by_claim:
            existing = by_claim[key]
            for source in fact["sources"]:
                if source not in existing["sources"]:
                    existing["sources"].append(source)
            if (
                fact_is_supported(fact) and not fact_is_supported(existing)
                or fact_is_supported(fact) == fact_is_supported(existing)
                and fact["confidence"] > existing["confidence"]
            ):
                existing["confidence"] = fact["confidence"]
                existing["verification"] = fact["verification"]
            for name in ("evidence_ids", "research_keys", "raw_claims"):
                existing[name] = list(dict.fromkeys([*existing.get(name, []), *fact.get(name, [])]))
            continue
        # Allocate IDs once after deduplication. Subsequent passes preserve them.
        fact["id"] = f"fact_{len(normalized) + 1:02d}"
        fact["raw_claims"] = list(dict.fromkeys([*fact.get("raw_claims", []), fact["raw_claim"]]))
        fact["research_keys"] = list(dict.fromkeys([*fact.get("research_keys", []), *([fact["research_key"]] if fact.get("research_key") else [])]))
        normalized.append(fact)
        by_claim[key] = fact
    return normalized


def merge_results(first: ResearchResult, retry: ResearchResult, question: str, language: str) -> ResearchResult:
    """Enrich the complete fact union from native evidence in isolated namespaces."""
    units: list[EvidenceUnit] = []
    sources: dict[str, dict[str, Any]] = {}
    facts: list[dict[str, Any]] = []
    legacy_sources = []
    runs = []
    allowed = {field.name for field in fields(EvidenceUnit)}
    for index, result in enumerate((first, retry), 1):
        prefix = f"run{index}_"
        bundle = getattr(result, "evidence_bundle", None) or {}
        package = getattr(result, "package", None) or {}
        run_sources = bundle.get("sources") or (package.get("source_summary") or {}).get("sources") or []
        for source in run_sources:
            record = copy.deepcopy(source)
            record["id"] = prefix + str(source["id"])
            record.setdefault("domain", urlsplit(str(record.get("url") or "")).hostname or "")
            sources[record["id"]] = record
        for evidence in bundle.get("evidence") or package.get("evidence") or []:
            record = {key: value for key, value in evidence.items() if key in allowed}
            record.update(id=prefix + str(evidence["id"]), source_id=prefix + str(evidence["source_id"]))
            record.setdefault("matched", [])
            if record["source_id"] in sources:
                units.append(EvidenceUnit(**record))
        for original in result.facts:
            fact = copy.deepcopy(original)
            fact["research_key"] = prefix + str(fact.get("research_key") or fact.get("id") or len(facts))
            fact["research_keys"] = list(dict.fromkeys([
                fact["research_key"], *[prefix + str(key) for key in original.get("research_keys", [])],
            ]))
            fact["evidence_ids"] = [prefix + identifier for identifier in fact.get("evidence_ids", [])]
            for source in fact.get("sources", []):
                if source.get("source_id"):
                    source["source_id"] = prefix + str(source["source_id"])
            facts.append(fact)
        for original in result.sources:
            source = copy.deepcopy(original)
            for name in ("id", "source_id"):
                if source.get(name):
                    source[name] = prefix + str(source[name])
            legacy_sources.append(source)
        runs.append({"run": index, "status": result.status, "provider": result.provider,
                     "error": result.error, "diagnostics": copy.deepcopy(getattr(result, "diagnostics", None)),
                     "package": copy.deepcopy(package)})
    package = None
    bundle = None
    if units:
        frame = question_frame(question, language)
        route = route_question(question)
        texts = {identifier: " ".join(unit.text for unit in units if unit.source_id == identifier) for identifier in sources}
        clusters = independence_clusters(list(sources.values()), texts)
        groups = group_claims(units, clusters, frame.terms)
        rejected: list[dict[str, Any]] = []
        claims = select_claims(groups, sources, route, frame=frame, rejected=rejected)
        package = build_package(
            question=question, language=language, route=route, sub_questions=[], claims=claims,
            evidence={unit.id: unit for unit in units}, sources=sources,
            contradictions=find_contradictions(groups, sources, frame.terms), rejected=rejected,
            frame=frame, synthesis_mode="combined_evidence",
        )
        # Native claims lead so package keys survive duplicate normalization.
        # Package reconstruction must never erase usable V1 or validated summary facts.
        facts = [*legacy_facts(claims, sources, clusters), *facts]
        bundle = {"sources": list(sources.values()), "evidence": [
            {field.name: getattr(unit, field.name) for field in fields(EvidenceUnit)} for unit in units
        ]}
    facts = normalize_facts(facts)
    link_package_facts(package, facts)
    unique_sources = {str(source.get("url")): source for source in legacy_sources}
    return ResearchResult(
        facts, list(unique_sources.values()), "verified_sources" if facts else "unavailable",
        "combined_research", None, package, {"runs": runs, "combined": True}, bundle,
    )


def run_contract_research(query, language, settings, *, context, contract, research, evaluate, request):
    """Initial call plus at most one evidence retry. Coverage outages fail closed."""
    calls = []
    retry_report = None
    result = research(query, language, settings, context=context)

    def assess(current):
        current.facts[:] = normalize_facts(current.facts)
        link_package_facts(getattr(current, "package", None), current.facts)
        try:
            return checked_coverage(contract, evaluate(contract, current.facts, settings), current.facts), None
        except Exception as exc:  # noqa: BLE001 - explicit unavailable state, never missing evidence
            return None, f"{type(exc).__name__}: {exc}"

    calls.append({"query": query, "focus": context.get("focus")})
    coverage, error = assess(result)
    first_sufficient = bool(coverage and coverage.is_sufficient)
    reason = None
    if coverage is not None:
        package = getattr(result, "package", None) or {}
        if not coverage.is_sufficient:
            reason = "contract_insufficient"
        elif package.get("status") in {"insufficient", "missing_mechanism"}:
            reason = "missing_mechanism" if package.get("status") == "missing_mechanism" else "insufficient_package"
        elif weak_core(package):
            reason = "weak_core_source"
    if reason:
        missing = set(coverage.missing_obligations)
        obligations = [item for item in answer_obligations(contract) if item.is_required and (not missing or item.id in missing)]
        focus = " | ".join(f"Missing required answer: {item.description}" for item in obligations)
        query2, context2 = request(query, context, focus, [item.description for item in obligations], reason)
        retry = research(query2, language, settings, context=context2)
        calls.append({"query": query2, "focus": focus})
        result = merge_results(result, retry, str(context["question"]), language)
        coverage, error = assess(result)
        retry_report = {"attempted": True, "reason": reason, "focus": focus,
                        "failure_type": "RESEARCH_MISSING" if reason == "contract_insufficient" else "RESEARCH_QUALITY"}
    diagnostic = {
        **(getattr(result, "diagnostics", None) or {}), "research_calls": calls,
        "retry_attempted": len(calls) == 2, "retry_budget": {"maximum": 1, "used": len(calls) - 1},
        "coverage_status": "COVERAGE_UNAVAILABLE" if coverage is None else "SUFFICIENT" if coverage.is_sufficient else "RESEARCH_MISSING",
    }
    merge_regression = first_sufficient and coverage is not None and not coverage.is_sufficient
    if error:
        diagnostic.update(failure_type="COVERAGE_UNAVAILABLE", contract_failure_reason=error)
    elif coverage and not coverage.is_sufficient:
        failure = "MERGE_REGRESSION" if merge_regression else "RESEARCH_MISSING"
        diagnostic.update(coverage_status=failure, failure_type=failure, contract_failure_reason="Required research obligations unsupported after merge: " + ", ".join(coverage.missing_obligations))
    return result, coverage, retry_report, diagnostic
