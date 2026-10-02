"""Provider pooling and bounded widening using the existing scene authorities."""

from __future__ import annotations

from typing import Any

from .source_router import route_sources
from .visual_providers import AcquisitionBudget, CandidateLedger, ProviderRegistry, provider_call
from .visual_rights import evaluate_rights


def run_routed_scene_search(
    queries: list[str],
    scene: dict[str, Any],
    state: dict[str, Any],
    query_plan: dict[str, Any],
    *,
    registry: ProviderRegistry,
    preferred_kind: str,
    portrait: bool,
    scene_duration: float,
    used: set[str],
    verifier: Any | None,
    query_budget: int = 3,
    acquisition_budget: AcquisitionBudget | None = None,
    skip_searches: set[tuple[str, str, str]] | None = None,
    widen_fully: bool = False,
) -> Any:
    from .media import (
        _METADATA_ONLY_VERIFIER,
        COVERAGE_STRONG,
        MAX_SCENE_QUERY_BUDGET,
        MediaProviderError,
        StagedSearchResult,
        _candidate_coverage_score,
        _safe_verify,
        _scene_query_order,
        _usable_quality,
        media_relevance,
        real_media_quality_gate,
        scene_coverage_targets,
        select_fallback_query,
        summarize_coverage,
    )

    budget = acquisition_budget or AcquisitionBudget()
    query_budget = max(1, min(query_budget, MAX_SCENE_QUERY_BUDGET))
    targets_info = scene_coverage_targets(scene, state, query_plan)
    targets = targets_info["targets"]
    planned = _scene_query_order(list(dict.fromkeys(q for q in queries if q)), targets, query_plan)[
        :query_budget
    ]
    remaining = planned.copy()
    ledger = CandidateLedger(used)
    candidates, rows = {}, {}
    stages = []
    coverage = summarize_coverage([], targets, scene_duration)
    before_widening = None
    failure = None
    verification_ok = True
    active_verifier = verifier
    stop_reason = "no_queries"
    while remaining:
        query = (
            remaining.pop(0) if not stages else select_fallback_query(remaining, coverage, targets)
        )
        if query is None:
            stop_reason = "no_targeted_query"
            break
        if query in remaining:
            remaining.remove(query)
        stage = {
            "query": query,
            "requests": 0,
            "new": 0,
            "duplicates": 0,
            "errors": [],
            "providers": [],
            "widening_reasons": [],
        }
        groups = route_sources(registry, scene, state, query, preferred_kind)
        stage["routed_providers"] = [
            {"provider": s.adapter.provider, "kind": s.kind, "reason": s.reason, "tier": i}
            for i, group in enumerate(groups)
            for s in group
        ]
        for tier, sources in enumerate(groups):
            if tier:
                if coverage["overall"] == COVERAGE_STRONG and not widen_fully:
                    break
                stage["widening_reasons"].append(f"insufficient_coverage:{coverage['overall']}")
                before_widening = before_widening or coverage
            pool = []
            for source in sources:
                if (query, source.adapter.provider, source.kind) in (skip_searches or set()):
                    continue
                stats = {
                    "provider": source.adapter.provider,
                    "kind": source.kind,
                    "requests": 0,
                    "returned": 0,
                    "rights_rejects": 0,
                    "relevance_rejects": 0,
                    "dedupe_rejects": 0,
                    "technical_rejects": 0,
                }
                before = budget.search_requests
                try:
                    results = provider_call(
                        lambda source=source, query=query: source.adapter.search(
                            query,
                            source.kind,
                            portrait=portrait,
                            scene_duration=scene_duration,
                            budget=budget,
                        )
                    )
                    stats["returned"] = len(results)
                except MediaProviderError as exc:
                    failure = exc
                    stats["failure"] = exc.category
                    stage["errors"].append(exc.category)
                    results = []
                stats["requests"] = budget.search_requests - before
                stats["cache_hit"] = bool(results and not stats["requests"])
                stage["requests"] += stats["requests"]
                for candidate in results:
                    if evaluate_rights(candidate.rights).status != "usable":
                        stats["rights_rejects"] += 1
                    elif not _usable_quality(candidate, scene_duration):
                        stats["technical_rejects"] += 1
                    elif not ledger.admit(candidate):
                        stats["dedupe_rejects"] += 1
                        stage["duplicates"] += 1
                    else:
                        candidates[candidate.identity] = candidate
                        if media_relevance(candidate, scene, state)["confidence"] == "rejected":
                            stats["relevance_rejects"] += 1
                        else:
                            pool.append(candidate)
                stage["providers"].append(stats)
            # Both primary providers contribute BEFORE shortlist/ranking. No
            # provider's first attractive result can terminate the primary pool.
            stage["new"] += len(pool)
            before = budget.verifications
            verified, ok = (
                _safe_verify(pool, scene, state, active_verifier, budget) if pool else ([], True)
            )
            stage["verification_admissions"] = (
                stage.get("verification_admissions", 0) + budget.verifications - before
            )
            if not ok:
                verification_ok = False
                active_verifier = _METADATA_ONLY_VERIFIER
            for candidate, relevance in verified:
                rows[candidate.identity] = (candidate, relevance)
                if not real_media_quality_gate(candidate, relevance)[0]:
                    for stats in stage["providers"]:
                        if (
                            stats["provider"] == candidate.provider
                            and stats["kind"] == candidate.kind
                        ):
                            stats["relevance_rejects"] += 1
                            break
            accepted = [row for row in rows.values() if real_media_quality_gate(*row)[0]]
            coverage = summarize_coverage(accepted, targets, scene_duration)
            if budget.search_requests >= budget.max_search_requests:
                break
        stage["coverage"] = coverage["overall"]
        stages.append(stage)
        if coverage["overall"] == COVERAGE_STRONG and not widen_fully:
            stop_reason = "strong_coverage"
            break
        if budget.search_requests >= budget.max_search_requests:
            stop_reason = "acquisition_budget_exhausted"
            break
        stop_reason = "budget_exhausted" if len(stages) == query_budget else "plan_exhausted"
    ranked = sorted(
        (row for row in rows.values() if real_media_quality_gate(*row)[0]),
        key=lambda row: (
            int(row[1].get("selection_tier") or 0),
            _candidate_coverage_score(*row, targets, scene_duration),
            float((row[1].get("visual") or {}).get("scene_score") or -1),
            float(row[1]["score"]),
            int(row[0].kind == preferred_kind),
            row[0].rank,
        ),
        reverse=True,
    )
    reasons = [reason for stage in stages for reason in stage["widening_reasons"]]
    provenance = {
        "version": 2,
        "routed": True,
        "query_budget": query_budget,
        "planned_queries": planned,
        "executed_queries": [stage["query"] for stage in stages],
        "planned_query_count": len(planned),
        "logical_queries_executed": len(stages),
        "executed_query_count": len(stages),
        "provider_requests_executed": sum(stage["requests"] for stage in stages),
        "provider_requests_by_source": {
            "staged_search": sum(stage["requests"] for stage in stages)
        },
        "early_stop": stop_reason == "strong_coverage" and len(stages) < len(planned),
        "stop_reason": stop_reason,
        "fallback_count": len(reasons),
        "fallback_reason": reasons[0] if reasons else None,
        "fallback_reasons": reasons,
        "coverage_mode": targets_info["mode"],
        "coverage_targets": targets,
        "coverage_before_fallback": before_widening,
        "coverage_after_fallback": coverage if reasons else None,
        "final_coverage": coverage["overall"],
        "stages": stages,
        "duplicate_count": sum(s["duplicates"] for s in stages),
        "visual_verification": "ok" if verification_ok else "failed_metadata_fallback",
        "acquisition_budget": budget.snapshot(),
    }
    return StagedSearchResult(
        ranked,
        list(candidates.values()),
        provenance,
        failure,
        set(rows),
        {key: row[1] for key, row in rows.items()},
        not verification_ok,
    )
