"""The research package: compact, structured, traceable input for generation.

Every claim in the package is either a verbatim evidence sentence or a
synthesised sentence that passed validation against the evidence it cites
(known evidence IDs, no number the evidence lacks, content words found in
the cited text, a causal claim only from causal evidence).  Anything else is
listed under ``rejected_claims`` and never reaches the script writer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .corroboration import ClaimGroup
from .evidence import KIND_PATTERNS, EvidenceUnit, numbers_in, related, words
from .quality import TIER_RANK
from .routing import RoutePlan

PACKAGE_VERSION = 1
MAX_FACTS = 8
ROLE_IMPORTANCE = {
    "core_answer": 0.95, "mechanism": 0.9, "observation": 0.75, "supporting": 0.7, "number": 0.7,
    "misconception": 0.65, "caveat": 0.6,
}
_WHY = re.compile(
    r"(?i)\b(?:because|since|due to|caused? by|in order to|so that|weil|denn|aufgrund|wegen|verursach\w*|liegt (?:da)?ran|"
    r"damit|um\b[^.,;]{1,80}\bzu\s+\w+)\b"
)


@dataclass
class PackageClaim:
    """One claim of the package with its evidence and support."""

    key: str
    role: str
    text: str
    evidence_ids: list[str]
    source_ids: list[str]
    clusters: set[str]
    basis: str
    best_tier: int
    origin: str = "evidence"  # evidence | synthesis
    kinds: list[str] = field(default_factory=list)

    @property
    def support(self) -> int:
        return len(self.clusters)

    def verification(self) -> str:
        if self.basis == "snippet":
            return "source_snippet"
        if self.support >= 2 or self.best_tier == TIER_RANK["high"]:
            return "supported"
        return "source_attributed"

    def confidence(self) -> float:
        verification = self.verification()
        if verification == "source_snippet":
            return 0.7
        if verification == "supported":
            return 0.9 if self.support >= 2 else 0.85
        return 0.78 if self.best_tier <= TIER_RANK["medium"] else 0.62

    def ref(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "role": self.role,
            "text": self.text,
            "evidence_ids": self.evidence_ids,
            "source_ids": self.source_ids,
            "independent_sources": self.support,
            "verification": self.verification(),
            "origin": self.origin,
        }


def _group_rank(group: ClaimGroup, sources: dict[str, dict[str, Any]], route: RoutePlan) -> tuple:
    """Relevance band first (authority never beats relevance), then authority, routing, support."""
    relevance = max(unit.relevance for unit in group.units)
    band = 0 if relevance >= 0.75 else (1 if relevance >= 0.5 else 2)
    tier = min(TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3) for unit in group.units)
    preference = min(route.preference(str(sources.get(unit.source_id, {}).get("source_type"))) for unit in group.units)
    snippet_only = all(unit.basis == "snippet" for unit in group.units)
    return (band, snippet_only, tier, preference, -group.support, -relevance)


def _lead(group: ClaimGroup, sources: dict[str, dict[str, Any]], route: RoutePlan) -> EvidenceUnit:
    """The wording of the best source (full text over snippet, primary over secondary)."""
    return min(
        group.units,
        key=lambda unit: (
            unit.basis == "snippet",
            TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3),
            route.preference(str(sources.get(unit.source_id, {}).get("source_type"))),
            -unit.relevance,
        ),
    )


def _claim_from_group(group: ClaimGroup, role: str, sources: dict[str, dict[str, Any]], route: RoutePlan) -> PackageClaim:
    lead = _lead(group, sources, route)
    tiers = [TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3) for unit in group.units if unit.basis == "full_text"]
    return PackageClaim(
        key=group.key,
        role=role,
        text=lead.text,
        evidence_ids=[unit.id for unit in group.units][:6],
        source_ids=sorted({unit.source_id for unit in group.units}),
        clusters=set(group.clusters),
        basis="full_text" if any(unit.basis == "full_text" for unit in group.units) else "snippet",
        best_tier=min(tiers) if tiers else 3,
        kinds=list(lead.kinds),
    )


def select_claims(
    groups: list[ClaimGroup],
    sources: dict[str, dict[str, Any]],
    route: RoutePlan,
    *,
    explanatory: bool,
) -> list[PackageClaim]:
    """Deterministic roles over the eligible claim groups (bounded to MAX_FACTS)."""
    eligible = [group for group in groups if group.status == "ok"]
    eligible.sort(key=lambda group: _group_rank(group, sources, route))
    # Low-quality / user-generated evidence needs more caution: it is used only
    # when nothing better was found (the package then reports the gap).
    if any(_group_rank(group, sources, route)[2] <= TIER_RANK["medium"] for group in eligible):
        eligible = [group for group in eligible if _group_rank(group, sources, route)[2] < TIER_RANK["low"]]
    chosen: list[PackageClaim] = []
    used: set[str] = set()

    def primary(group: ClaimGroup) -> str:
        return _lead(group, sources, route).kind

    def has(group: ClaimGroup, kind: str) -> bool:
        return kind in _lead(group, sources, route).kinds

    def take(candidates: list[ClaimGroup], role: str, limit: int) -> None:
        for group in candidates:
            if sum(1 for claim in chosen if claim.role == role) >= limit or len(chosen) >= MAX_FACTS:
                return
            if group.key in used:
                continue
            used.add(group.key)
            chosen.append(_claim_from_group(group, role, sources, route))

    relevant = [group for group in eligible if _group_rank(group, sources, route)[0] <= 1]
    # The direct answer: the most relevant claim; for why/how questions a causal one.
    core_pool = sorted(
        relevant, key=lambda group: (explanatory and primary(group) != "mechanism",) + _group_rank(group, sources, route)
    )
    take(core_pool, "core_answer", 1)
    if explanatory:
        take([group for group in eligible if primary(group) == "mechanism"], "mechanism", 3)
    take([group for group in eligible if primary(group) in {"observation", "definition"}], "observation", 1)
    take([group for group in eligible if has(group, "number") or has(group, "date")], "number", 2)
    take([group for group in eligible if has(group, "misconception")], "misconception", 1)
    take([group for group in eligible if has(group, "caveat")], "caveat", 1)
    take(relevant, "supporting", 2)
    return chosen


# ---------------------------------------------------------------------------
# Synthesised claims: accepted only when the cited evidence supports them
# ---------------------------------------------------------------------------

def validate_synthesized(text: str, evidence_ids: list[str], evidence: dict[str, EvidenceUnit]) -> str | None:
    """None when ``text`` is supported by its cited evidence, else the rejection reason."""
    if not text.strip():
        return "empty"
    if not evidence_ids:
        return "no_evidence_cited"
    unknown = [item for item in evidence_ids if item not in evidence]
    if unknown:
        return "unknown_evidence_ids"
    cited = [evidence[item] for item in evidence_ids]
    cited_text = " ".join(unit.text for unit in cited)
    missing_numbers = numbers_in(text) - numbers_in(cited_text)
    if missing_numbers:
        return "number_not_in_evidence:" + ",".join(sorted(missing_numbers))[:40]
    claim_words = {word for word in words(text) if len(word) >= 4}
    evidence_words = words(cited_text)
    if claim_words:
        supported = {word for word in claim_words if any(related(word, other) for other in evidence_words)}
        if len(supported) / len(claim_words) < 0.6:
            return "content_not_in_evidence"
    if KIND_PATTERNS["mechanism"].search(text) and not any("mechanism" in unit.kinds for unit in cited):
        return "causal_claim_without_causal_evidence"
    return None


def claims_from_synthesis(
    synthesis: dict[str, Any],
    evidence: dict[str, EvidenceUnit],
    sources: dict[str, dict[str, Any]],
    clusters: dict[str, dict[str, Any]],
) -> tuple[list[PackageClaim], list[dict[str, Any]]]:
    accepted: list[PackageClaim] = []
    rejected: list[dict[str, Any]] = []
    sections = (
        ("core_answer", "core_answer", 1), ("what_happens", "observation", 1), ("mechanism_steps", "mechanism", 4),
        ("numbers_dates", "number", 2), ("misconceptions", "misconception", 1), ("caveats", "caveat", 1),
        ("supporting", "supporting", 2),
    )
    for section, role, limit in sections:
        items = synthesis.get(section)
        items = [items] if isinstance(items, dict) else list(items or [])
        for item in items[:limit]:
            text = " ".join(str((item or {}).get("text") or "").split())
            ids = [str(value) for value in (item or {}).get("evidence_ids") or []][:6]
            reason = validate_synthesized(text, ids, evidence)
            if reason:
                rejected.append({"text": text[:200], "role": role, "reason": reason, "evidence_ids": ids})
                continue
            cited = [evidence[item_id] for item_id in ids]
            tiers = [TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3) for unit in cited if unit.basis == "full_text"]
            accepted.append(PackageClaim(
                key=f"syn_{len(accepted) + 1:02d}",
                role=role,
                text=text,
                evidence_ids=ids,
                source_ids=sorted({unit.source_id for unit in cited}),
                clusters={str(clusters.get(unit.source_id, {}).get("cluster") or unit.source_id) for unit in cited},
                basis="full_text" if tiers else "snippet",
                best_tier=min(tiers) if tiers else 3,
                origin="synthesis",
                kinds=sorted({kind for unit in cited for kind in unit.kinds}),
            ))
            if len(accepted) >= MAX_FACTS:
                return accepted, rejected
    return accepted, rejected


# ---------------------------------------------------------------------------
# Sufficiency, package and legacy facts
# ---------------------------------------------------------------------------

def sufficiency(claims: list[PackageClaim], *, explanatory: bool) -> dict[str, Any]:
    roles = {claim.role for claim in claims}
    core = next((claim for claim in claims if claim.role == "core_answer"), None)
    mechanism = [claim for claim in claims if claim.role == "mechanism"] or (
        [core] if core is not None and explanatory and "mechanism" in core.kinds else []
    )
    checks = {
        "direct_answer": core is not None,
        "mechanism": (bool(mechanism) if explanatory else None),
        "supporting_detail": bool(roles & {"supporting", "number", "observation"}) or len(mechanism) >= 2,
        "misconception_or_caveat": bool(roles & {"misconception", "caveat"}),
        "payoff": len(claims) >= 2,
    }
    gaps: list[str] = []
    if core is None:
        gaps.append("no_direct_answer")
    if explanatory and not mechanism:
        gaps.append("no_mechanism_evidence")
    if explanatory and mechanism and all(claim.support < 2 and claim.best_tier > TIER_RANK["high"] for claim in mechanism):
        gaps.append("mechanism_single_secondary_source")
    if not checks["supporting_detail"]:
        gaps.append("no_supporting_detail")
    if core is not None and core.basis == "snippet":
        gaps.append("core_answer_snippet_only")
    if core is not None and core.best_tier >= TIER_RANK["low"]:
        gaps.append("core_answer_low_authority_source")
    if core is None:
        status = "insufficient"
    elif explanatory and not mechanism:
        status = "missing_mechanism"
    elif not checks["supporting_detail"]:
        status = "partial"
    else:
        status = "sufficient"
    if core is None or core.verification() == "source_snippet":
        confidence = "low"
    elif core.verification() == "supported" and (not explanatory or any(claim.verification() == "supported" for claim in mechanism)):
        confidence = "high"
    else:
        confidence = "medium"
    if status != "sufficient" and confidence == "high":
        confidence = "medium"
    return {"status": status, "checks": checks, "gaps": gaps, "confidence": confidence}


def build_package(
    *,
    question: str,
    language: str,
    route: RoutePlan,
    sub_questions: list[dict[str, Any]],
    claims: list[PackageClaim],
    evidence: dict[str, EvidenceUnit],
    sources: dict[str, dict[str, Any]],
    contradictions: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    explanatory: bool,
    synthesis_mode: str,
    takeaway: str = "",
) -> dict[str, Any]:
    by_role: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        by_role.setdefault(claim.role, []).append(claim.ref())
    core = (by_role.get("core_answer") or [None])[0]
    mechanism = by_role.get("mechanism") or []
    core_claim = next((claim for claim in claims if claim.role == "core_answer"), None)
    if not mechanism and core is not None and core_claim is not None and explanatory and "mechanism" in core_claim.kinds:
        mechanism = [core]  # the direct answer itself states the cause
    # WHY (cause / reason / purpose) vs HOW (the process); a lone process step still answers why.
    why = [ref for ref in mechanism if _WHY.search(ref["text"])]
    how = [ref for ref in mechanism if ref not in why]
    if not why and how:
        why, how = how[:1], how[1:]
    used_evidence = {item for claim in claims for item in claim.evidence_ids}
    used_sources = {item for claim in claims for item in claim.source_ids}
    types: dict[str, int] = {}
    for source_id in used_sources:
        kind = str(sources.get(source_id, {}).get("source_type") or "unknown")
        types[kind] = types.get(kind, 0) + 1
    state = sufficiency(claims, explanatory=explanatory)
    if any(record["resolution"].startswith("unresolved") for record in contradictions):
        state["gaps"].append("unresolved_contradiction")
    return {
        "version": PACKAGE_VERSION,
        "status": state["status"],
        "question": question,
        "language": language,
        "route": route.as_dict(),
        "sub_questions": sub_questions,
        "core_answer": core,
        "explanation_spine": {
            "what_happens": (by_role.get("observation") or [None])[0],
            "why_it_happens": why,
            "how_it_works": how,
            "viewer_takeaway": takeaway or (core or {}).get("text") or "",
            "status": "not_required" if not explanatory else ("complete" if state["checks"]["mechanism"] else "missing_mechanism"),
        },
        "supporting_facts": by_role.get("supporting") or [],
        "numbers_dates": by_role.get("number") or [],
        "caveats": by_role.get("caveat") or [],
        "misconceptions": by_role.get("misconception") or [],
        "evidence": [evidence[item].as_dict() for item in sorted(used_evidence) if item in evidence],
        "source_summary": {
            "used": len(used_sources),
            "independent": len({cluster for claim in claims for cluster in claim.clusters}),
            "types": types,
            "sources": [
                {key: sources[source_id].get(key) for key in (
                    "id", "url", "title", "organization", "source_type", "authority", "published_at", "updated_at", "fetched_at",
                )}
                for source_id in sorted(used_sources) if source_id in sources
            ],
        },
        "contradictions": contradictions[:6],
        "sufficiency": state,
        "confidence": state["confidence"],
        "gaps": state["gaps"],
        "rejected_claims": rejected[:10],
        "synthesis": synthesis_mode,
    }


def legacy_facts(claims: list[PackageClaim], sources: dict[str, dict[str, Any]], clusters: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The package claims as the fact records the generation pipeline consumes.

    ``sources`` lists one source per independent cluster (copies never
    inflate the count other systems read from it); the rest stays traceable
    through ``evidence_ids``.
    """
    order = ("core_answer", "mechanism", "observation", "number", "supporting", "misconception", "caveat")
    facts: list[dict[str, Any]] = []
    for claim in sorted(claims, key=lambda item: order.index(item.role) if item.role in order else len(order)):
        per_cluster: dict[str, dict[str, Any]] = {}
        for source_id in claim.source_ids:
            source = sources.get(source_id) or {}
            cluster = str(clusters.get(source_id, {}).get("cluster") or source_id)
            current = per_cluster.get(cluster)
            if current is None or TIER_RANK.get(str(source.get("authority")), 3) < TIER_RANK.get(str(current.get("authority")), 3):
                per_cluster[cluster] = source
        fact_sources = [
            {
                "label": str(source.get("title") or source.get("organization") or source.get("domain") or source.get("url")),
                "url": source.get("url"),
                "source_id": source.get("id"),
                "source_type": source.get("source_type"),
                "authority": source.get("authority"),
                "published_at": source.get("published_at"),
                "fetched_at": source.get("fetched_at"),
            }
            for source in per_cluster.values() if source.get("url")
        ][:3]
        index = len(facts) + 1
        facts.append({
            "id": f"fact_{index:02d}",
            "claim": claim.text,
            "confidence": claim.confidence(),
            "importance": round(max(0.5, ROLE_IMPORTANCE.get(claim.role, 0.6) - 0.02 * index), 3),
            "priority": "MUST_KNOW" if claim.role in {"core_answer", "mechanism"} else "USEFUL",
            "sources": fact_sources,
            "verification": claim.verification(),
            "research_key": claim.key,
            "research_role": claim.role,
            "evidence_ids": claim.evidence_ids,
            "independent_sources": claim.support,
        })
    return facts


def link_package_facts(package: dict[str, Any] | None, facts: list[dict[str, Any]]) -> None:
    """After the pipeline re-numbers facts, write the final fact IDs into the package refs."""
    if not isinstance(package, dict):
        return
    by_key = {str(fact.get("research_key")): str(fact.get("id")) for fact in facts if fact.get("research_key")}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "key" in value and "evidence_ids" in value:
                value["fact_id"] = by_key.get(str(value["key"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for section in ("core_answer", "explanation_spine", "supporting_facts", "numbers_dates", "caveats", "misconceptions"):
        visit(package.get(section))


def research_brief(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Which research_evidence items (1-based, as sent to the planner) research found to be what.

    Guidance for the planner's story arc, never a replacement for its judgement.
    """
    roles: dict[str, list[int]] = {}
    for index, fact in enumerate(facts, 1):
        role = str(fact.get("research_role") or "")
        if role:
            roles.setdefault(role, []).append(index)
    if not roles:
        return None
    return {
        "direct_answer_index": (roles.get("core_answer") or [None])[0],
        "mechanism_indexes": roles.get("mechanism", []),
        "observation_indexes": roles.get("observation", []),
        "misconception_indexes": roles.get("misconception", []),
        "caveat_indexes": roles.get("caveat", []),
        "corroborated_indexes": [
            index for index, fact in enumerate(facts, 1) if int(fact.get("independent_sources") or 0) >= 2
        ],
    }
