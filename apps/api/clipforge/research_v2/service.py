"""Research Pipeline V2 orchestration.

QUESTION -> DECOMPOSITION -> DISCOVERY -> (cheap prefilter) -> RETRIEVAL ->
EXTRACTION -> EVIDENCE UNITS -> FRESHNESS -> INDEPENDENCE / CORROBORATION /
CONTRADICTIONS -> CLAIM SELECTION (+ validated synthesis) -> RESEARCH PACKAGE

Every step is bounded by ``ResearchBudget`` and a wall-clock deadline; every
external failure becomes a diagnostic record, never an exception.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..config import Settings
from .answer_relation import question_frame
from .corroboration import filter_fresh, find_contradictions, group_claims, independence_clusters
from .discovery import Discovery
from .evidence import EvidenceUnit, matched_terms, question_terms, units_from_paragraphs, words
from .extraction import scrapling_available
from .models import Discovered, FetchOutcome, ResearchBudget, SubQuestion
from .package import build_package, claims_from_synthesis, legacy_facts, select_claims
from .quality import classify_source, host_of, registrable_domain
from .retrieval import ExtractionCache, Retriever, now_iso
from .routing import route_question
from .synthesis import (
    ResearchLLM,
    deterministic_sub_questions,
    llm_for,
    sub_questions_from_llm,
)

MAX_PER_DOMAIN = 2
MAX_CITATION_FETCHES = 2
MAX_EVIDENCE_FOR_SYNTHESIS = 28


@dataclass
class ResearchRun:
    facts: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    status: str
    error: str | None
    package: dict[str, Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _normalise_url(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(item for item in parts.query.split("&") if item and not item.casefold().startswith(("utm_", "fbclid", "gclid")))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold().removeprefix("www."), parts.path.rstrip("/") or "/", query, ""))


def _prefilter(
    hits: list[Discovered],
    core_terms: set[str],
    route: Any,
    budget: ResearchBudget,
    sub_questions: list[SubQuestion],
    question_word_set: set[str],
) -> tuple[list[Discovered], list[dict[str, Any]]]:
    """Cheap ranking before any page is fetched; returns (to fetch, decisions)."""
    unique: dict[str, Discovered] = {}
    for hit in hits:
        unique.setdefault(_normalise_url(hit.url), hit)
    rows: list[tuple[tuple, Discovered, dict[str, Any]]] = []
    for hit in unique.values():
        predicted = classify_source(hit.url, title=hit.title, text=hit.snippet, question_terms=question_word_set,
                                    first_party_allowed=route.first_party_allowed)
        hit_words = words(f"{hit.title} {hit.snippet}")
        relevance = len(matched_terms(core_terms, hit_words)) / max(1, len(core_terms)) if hit.snippet or hit.title else None
        if hit.provider == "encyclopedia_citation":
            band = 1  # unknown relevance; judged after retrieval like everything else
        elif relevance is None or relevance < 0.25:
            band = 3
        else:
            band = 0 if relevance >= 0.6 else 1
        key = (band, route.preference(predicted.source_type), hit.rank)
        rows.append((key, hit, {
            "url": hit.url, "provider": hit.provider, "sub_question": hit.sub_question, "rank": hit.rank,
            "predicted_type": predicted.source_type, "prefilter_relevance": None if relevance is None else round(relevance, 2),
        }))
    rows.sort(key=lambda row: row[0])
    chosen: list[Discovered] = []
    per_domain: dict[str, int] = {}
    citations = 0
    covered: set[str] = set()
    limit = budget.max_documents

    def pick(row: tuple[tuple, Discovered, dict[str, Any]], reason: str) -> None:
        nonlocal citations
        _key, hit, decision = row
        chosen.append(hit)
        domain = registrable_domain(hit.url)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        citations += hit.provider == "encyclopedia_citation"
        covered.add(hit.sub_question)
        decision.update(selected=True, reason=reason)

    def allowed(row: tuple[tuple, Discovered, dict[str, Any]]) -> str | None:
        key, hit, _decision = row
        if key[0] >= 3:
            return "snippet_not_relevant"
        if per_domain.get(registrable_domain(hit.url), 0) >= MAX_PER_DOMAIN:
            return "domain_quota"
        if hit.provider == "encyclopedia_citation" and citations >= MAX_CITATION_FETCHES:
            return "citation_quota"
        return None

    # Diversity first: the best hit of each sub-question, then the overall order.
    for sub in sub_questions:
        row = next((row for row in rows if row[1].sub_question == sub.id and not row[2].get("selected") and allowed(row) is None), None)
        if row is not None and len(chosen) < limit:
            pick(row, f"best for {sub.id}")
    for row in rows:
        if row[2].get("selected"):
            continue
        reason = allowed(row)
        if reason is None and len(chosen) < limit:
            pick(row, "ranked")
        else:
            row[2].update(selected=False, reason=reason or "document_budget")
    return chosen, [row[2] for row in rows][:30]


def _source_record(
    source_id: str, hit: Discovered, outcome: FetchOutcome | None, route: Any, question_word_set: set[str]
) -> dict[str, Any]:
    page = outcome.page if outcome is not None and outcome.usable else None
    url = hit.url
    classification = classify_source(
        url,
        title=(page.title if page else hit.title),
        text=" ".join(page.paragraphs[:20]) if page else hit.snippet,
        meta=page.quality_meta() if page else None,
        question_terms=question_word_set,
        first_party_allowed=route.first_party_allowed,
    )
    return {
        "id": source_id,
        "url": url,
        "domain": registrable_domain(url),
        "host": host_of(url),
        "title": ((page.title if page else "") or hit.title or host_of(url))[:200],
        "organization": ((page.site_name if page else "") or registrable_domain(url))[:120],
        "author": (page.author if page else "")[:120] or None,
        "published_at": page.published_at if page else None,
        "updated_at": page.updated_at if page else None,
        "fetched_at": (outcome.fetched_at if outcome else None) or now_iso(),
        "canonical": page.canonical if page else "",
        "basis": "full_text" if page else "snippet",
        "evidence_provenance": "full_text" if page else "search_snippet",
        "provider": hit.provider,
        "discovery_rank": hit.rank,
        "retrieval": outcome.status if outcome else "not_fetched",
        **classification.as_dict(),
    }


def run_research(
    query: str,
    language: str,
    settings: Settings,
    *,
    context: dict[str, Any] | None = None,
    transport: httpx.BaseTransport | None = None,
    llm: ResearchLLM | None | bool = True,
    dynamic_fetch: Any = None,
    today: date | None = None,
    cache_root: Path | None | bool = True,
) -> ResearchRun:
    """Run the V2 research path for one question (never raises for site/provider failures)."""
    started = time.monotonic()
    context = context or {}
    question = str(context.get("question") or query).strip()
    focus = context.get("focus")
    language = "de" if str(language).startswith("de") else "en"
    budget = ResearchBudget(
        max_searches=settings.research_max_searches,
        max_documents=settings.research_max_documents,
        max_dynamic=settings.research_max_browser_fetches if settings.research_browser_fetch else 0,
        max_llm_calls=settings.research_max_llm_calls,
    )
    provider = llm_for(settings) if llm is True else (llm or None)
    # What the question asks (entities + requested relation): the answer-grounding contract.
    frame = question_frame(question, language)

    # 1. Decomposition (bounded; deterministic fallback).
    decomposition = "deterministic"
    sub_questions = deterministic_sub_questions(question, query, language, focus=focus)
    domain_hint = None
    if provider is not None and focus is None and budget.take("llm_calls"):
        out = provider.decompose(question, language)
        if out is not None:
            sub_questions, domain_hint = sub_questions_from_llm(out, question, query, language)
            decomposition = "llm"
    route = route_question(question, content_type=context.get("content_type"), hint=domain_hint)
    core_terms = question_terms(question) or question_terms(query)
    question_word_set = words(question)

    # 2. Discovery + cheap prefilter.
    deadline = started + settings.research_deadline_seconds
    if cache_root is True:
        cache_root = Path(settings.render_root) / ".research-cache"
    cache = ExtractionCache(cache_root if isinstance(cache_root, Path) else None)
    retriever = Retriever(
        budget,
        timeout=settings.research_fetch_timeout_seconds,
        cache=cache,
        cache_max_age_hours=2.0 if route.time_sensitive else 24.0 * 7,
        transport=transport,
        dynamic_fetch=dynamic_fetch,
        dynamic_enabled=settings.research_browser_fetch,
        deadline=deadline,
    )
    try:
        discovery = Discovery(retriever.client, budget, language=language, brave_key=settings.brave_search_api_key)
        hits = discovery.discover(sub_questions)
        selected, decisions = _prefilter(hits, core_terms, route, budget, sub_questions, question_word_set)

        # 3. Retrieval (parallel, isolated).
        outcomes = retriever.fetch_many([hit.url for hit in selected]) if selected else []
    finally:
        retriever.close()

    # 4. Extraction -> sources -> evidence units.
    sources: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    units: list[EvidenceUnit] = []
    for index, (hit, outcome) in enumerate(zip(selected, outcomes), 1):
        source_id = f"src_{index:02d}"
        record = _source_record(source_id, hit, outcome, route, question_word_set)
        if outcome.usable and outcome.page is not None:
            paragraphs = outcome.page.paragraphs + outcome.page.tables[0] if outcome.page.tables else outcome.page.paragraphs
            found = units_from_paragraphs(paragraphs, source_id=source_id, sub_questions=sub_questions,
                                          core_terms=core_terms, start=len(units) + 1, title=outcome.page.title)
            texts[source_id] = " ".join(outcome.page.paragraphs)
        elif hit.snippet and outcome.status not in {"robots_disallowed"}:
            # The underlying page could not be retrieved: the snippet is weak,
            # labelled evidence (never treated like full text).
            found = units_from_paragraphs([hit.snippet], source_id=source_id, sub_questions=sub_questions,
                                          core_terms=core_terms, start=len(units) + 1, basis="snippet",
                                          provenance="search_snippet")
            texts[source_id] = hit.snippet
        else:
            found = []
        record["evidence_units"] = len(found)
        sources[source_id] = record
        units.extend(found)
    for offset, unit in enumerate(units, 1):
        unit.id = f"ev_{offset:02d}"

    # 5. Freshness, independence, claim groups, contradictions.
    fresh_units, stale = filter_fresh(units, sources, time_sensitive_question=route.time_sensitive, today=today)
    clusters = independence_clusters(list(sources.values()), texts)
    for source_id, cluster in clusters.items():
        sources[source_id]["cluster"] = cluster["cluster"]
        sources[source_id]["independence_note"] = cluster["reason"]
    groups = group_claims(fresh_units, clusters, frame.terms)
    contradictions = find_contradictions(groups, sources, frame.terms)

    # 6. Claim selection: validated synthesis when available, verbatim evidence otherwise.
    evidence_by_id = {unit.id: unit for unit in fresh_units}
    rejected: list[dict[str, Any]] = [*stale]
    deterministic = select_claims(groups, sources, route, frame=frame, rejected=rejected)
    claims = deterministic
    for group in groups:
        if group.status != "ok":
            rejected.append({"text": group.lead.text[:200], "reason": group.status, "notes": group.notes[:3]})
    synthesis_mode = "deterministic"
    takeaway = ""
    eligible_ids = [unit.id for group in groups if group.status == "ok" for unit in group.units]
    if provider is not None and eligible_ids and budget.take("llm_calls"):
        payload = {
            "question": question,
            "language": language,
            "sub_questions": [sub.as_dict() for sub in sub_questions],
            "evidence": [
                {**evidence_by_id[item].as_dict(), "source_type": sources[evidence_by_id[item].source_id]["source_type"],
                 "authority": sources[evidence_by_id[item].source_id]["authority"]}
                for item in eligible_ids[:MAX_EVIDENCE_FOR_SYNTHESIS]
            ],
        }
        out = provider.synthesize(payload)
        if out is not None:
            allowed_evidence = {item: evidence_by_id[item] for item in eligible_ids}
            synthesized, synthesis_rejected = claims_from_synthesis(out.model_dump(), allowed_evidence, sources, clusters, frame)
            rejected.extend(synthesis_rejected)
            has_core = any(claim.role == "core_answer" for claim in synthesized)
            if has_core:
                claims = synthesized
                synthesis_mode = "llm_validated"
                takeaway_item = out.viewer_takeaway
                if takeaway_item is not None and not claims_from_synthesis(
                    {"supporting": [takeaway_item.model_dump()]}, allowed_evidence, sources, clusters
                )[1]:
                    takeaway = takeaway_item.text
            else:
                synthesis_mode = "llm_rejected_fallback_deterministic"
    package = build_package(
        question=question,
        language=language,
        route=route,
        sub_questions=[sub.as_dict() for sub in sub_questions],
        claims=claims,
        evidence=evidence_by_id,
        sources=sources,
        contradictions=contradictions,
        rejected=rejected,
        frame=frame,
        synthesis_mode=synthesis_mode,
        takeaway=takeaway,
    )
    facts = legacy_facts(claims, sources, clusters)
    if package["status"] == "insufficient":
        # No direct answer: nothing is handed to the script (readiness fails
        # and the bounded retry broadens the search) - side facts never stand in.
        package["withheld_claims"] = [claim.ref() for claim in claims]
        facts = []
    cited = {item["source_id"] for fact in facts for item in fact["sources"]}
    legacy_sources = [
        {"label": source["title"], "url": source["url"], "source_type": source["source_type"], "authority": source["authority"],
         "published_at": source["published_at"], "fetched_at": source["fetched_at"]}
        for source_id, source in sources.items() if source_id in cited
    ]
    failures = [outcome.as_dict() for outcome in outcomes if not outcome.usable]
    diagnostics = {
        "version": 1,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "query": query[:200],
        "focus": focus,
        "decomposition": decomposition,
        "route": route.as_dict(),
        "sub_questions": [sub.as_dict() for sub in sub_questions],
        "discovery": {"calls": discovery.log, "candidates": decisions},
        "retrieval": [outcome.as_dict() for outcome in outcomes],
        "failures": failures,
        "sources": [
            {key: source.get(key) for key in (
                "id", "url", "source_type", "authority", "reasons", "basis", "evidence_provenance", "retrieval",
                "cluster", "independence_note",
                "published_at", "updated_at", "fetched_at", "evidence_units",
            )} | {"used": source_id in {item for claim in claims for item in claim.source_ids}}
            for source_id, source in sources.items()
        ],
        "claims": [
            {"key": group.key, "text": group.lead.text[:160], "evidence_ids": [unit.id for unit in group.units][:6],
             "independent_sources": group.support, "status": group.status, "notes": group.notes[:3]}
            for group in groups[:24]
        ],
        "selected_claims": [claim.ref() for claim in claims],
        "mechanism_origin": [
            {"claim": claim.text[:160], "evidence_ids": claim.evidence_ids,
             "sources": [{"id": item, "type": sources[item]["source_type"], "url": sources[item]["url"]} for item in claim.source_ids]}
            for claim in claims if claim.role == "mechanism" or (claim.role == "core_answer" and "mechanism" in claim.kinds)
        ],
        "contradictions": contradictions[:6],
        "rejected": rejected[:12],
        "synthesis": synthesis_mode,
        "sufficiency": package["sufficiency"],
        "budget": budget.as_dict(),
        "extractor": {"scrapling_parser": scrapling_available(), "browser_fetch_enabled": settings.research_browser_fetch},
    }
    if facts:
        status, error = "verified_sources", None
    elif not outcomes:
        status, error = "unavailable", "No source could be discovered"
    else:
        status, error = "unavailable", "No relevant evidence could be retrieved"
    return ResearchRun(facts, legacy_sources, status, error, package, diagnostics)
