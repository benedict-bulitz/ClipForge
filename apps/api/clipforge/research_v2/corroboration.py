"""Corroboration, source independence, contradictions and freshness.

* Independence: sources are grouped into provenance clusters by registrable
  domain, canonical URL, near-duplicate text (syndicated copies) and shared
  wire/press-release attribution.  Agreement counts clusters, not pages, so
  five copies of one press release are one source.
* Claim groups: evidence units stating the same proposition (content words,
  compatible numbers) are one claim; its support is the number of
  independent clusters behind it.
* Contradictions: same-topic claims from different clusters with clearly
  different numbers, or the same proposition affirmed by one and negated by
  another.  The better-supported side wins only with a clear margin;
  otherwise both are marked conflicting and stay out of the package.
* Freshness: time-sensitive claims need a recent, dated source; evergreen
  claims are never penalised for an old page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .evidence import EvidenceUnit, numbers_in, related, words
from .quality import TIER_RANK

_WIRE = re.compile(
    r"(?i)(?:\(\s*(dpa|afp|ap|reuters|kna|epd|apa|sda)\s*\)|\b(dpa|afp|reuters|kna|epd)(?:-\w+)?\s*[/|]|"
    r"\b(?:laut|according to|so) (?:einer |a )?(pressemitteilung|press release|mitteilung)\b)"
)
_NEGATION = re.compile(r"(?i)\b(?:nicht|kein\w*|nie|niemals|not|no|never|neither|weder)\b")
CURRENT_MAX_AGE_DAYS = 45
TIME_WORD_MAX_AGE_DAYS = 3 * 365


def _shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    tokens = re.findall(r"\w+", text.casefold())
    return {tuple(tokens[index:index + size]) for index in range(0, max(0, len(tokens) - size + 1))}


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: str, second: str) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def independence_clusters(sources: list[dict[str, Any]], texts: dict[str, str]) -> dict[str, dict[str, Any]]:
    """source_id -> {"cluster": id, "reason": why it is not independent}."""
    ids = [str(source["id"]) for source in sources]
    uf = _UnionFind(ids)
    reasons: dict[str, str] = {}
    by_key: dict[tuple[str, str], str] = {}
    shingles = {source_id: _shingles(texts.get(source_id, "")[:12_000]) for source_id in ids}
    for source in sources:
        source_id = str(source["id"])
        keys = [("domain", str(source.get("domain") or ""))]
        if source.get("canonical"):
            keys.append(("canonical", str(source["canonical"]).rstrip("/").casefold()))
        wire = _WIRE.search(texts.get(source_id, "")[:6000])
        if wire:
            keys.append(("wire", next(group for group in wire.groups() if group).casefold()))
        for key in keys:
            if not key[1]:
                continue
            if key in by_key:
                uf.union(source_id, by_key[key])
                reasons[source_id] = {"domain": "same site", "canonical": "same canonical page", "wire": f"same upstream ({key[1]})"}[key[0]]
            else:
                by_key[key] = source_id
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            a, b = shingles[first], shingles[second]
            if len(a) >= 20 and len(b) >= 20 and len(a & b) / min(len(a), len(b)) >= 0.3:
                uf.union(first, second)
                reasons[second] = "near-duplicate text (syndicated copy)"
    return {source_id: {"cluster": uf.find(source_id), "reason": reasons.get(source_id)} for source_id in ids}


@dataclass
class ClaimGroup:
    key: str
    units: list[EvidenceUnit]
    clusters: set[str] = field(default_factory=set)
    status: str = "ok"  # ok | disputed | conflicting | stale
    notes: list[str] = field(default_factory=list)

    @property
    def lead(self) -> EvidenceUnit:
        return self.units[0]

    @property
    def support(self) -> int:
        return len(self.clusters)


def _same_claim(first: EvidenceUnit, second: EvidenceUnit) -> bool:
    """Two sources stating the same proposition (sentences of one page never merge)."""
    if first.source_id == second.source_id:
        return False
    a, b = words(first.text), words(second.text)
    if not a or not b:
        return False
    shared = {word for word in a if any(related(word, other) for other in b)}
    overlap = len(shared) / min(len(a), len(b))
    if overlap < 0.4 or len(shared) < 4:
        return False
    numbers_a, numbers_b = set(first.numbers), set(second.numbers)
    if numbers_a and numbers_b and not numbers_a & numbers_b:
        return False  # same topic, different figures: a possible contradiction, not agreement
    return bool(_NEGATION.search(first.text)) == bool(_NEGATION.search(second.text))


def group_claims(units: list[EvidenceUnit], clusters: dict[str, dict[str, Any]]) -> list[ClaimGroup]:
    groups: list[ClaimGroup] = []
    for unit in units:
        target = next((group for group in groups if _same_claim(group.lead, unit)), None)
        if target is None:
            target = ClaimGroup(key=f"claim_{len(groups) + 1:02d}", units=[])
            groups.append(target)
        target.units.append(unit)
        target.clusters.add(str(clusters.get(unit.source_id, {}).get("cluster") or unit.source_id))
    return groups


def _values(unit: EvidenceUnit) -> list[float]:
    values: list[float] = []
    for raw in numbers_in(unit.text):
        try:
            value = float(raw)
        except ValueError:
            continue
        if not (1000 <= value <= 2100 and float(value).is_integer()):  # years are dates, not quantities
            values.append(value)
    return values


def find_contradictions(
    groups: list[ClaimGroup], sources: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark disagreeing claim groups; return compact contradiction records."""
    records: list[dict[str, Any]] = []

    def tier(group: ClaimGroup) -> int:
        return min(TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3) for unit in group.units)

    for index, first in enumerate(groups):
        for second in groups[index + 1:]:
            if first.clusters & second.clusters:
                continue
            a, b = words(first.lead.text), words(second.lead.text)
            shared = {word for word in a if any(related(word, other) for other in b)}
            if not a or not b or len(shared) < 3:
                continue
            overlap = len(shared) / min(len(a), len(b))
            kind = None
            values_a, values_b = _values(first.lead), _values(second.lead)
            if overlap >= 0.5 and values_a and values_b:
                low, high = min(values_a[0], values_b[0]), max(values_a[0], values_b[0])
                if high > 0 and (high - low) / high > 0.2:
                    kind = "numeric"
            elif overlap >= 0.7 and bool(_NEGATION.search(first.lead.text)) != bool(_NEGATION.search(second.lead.text)):
                kind = "polarity"
            if kind is None:
                continue
            score_a, score_b = (first.support, -tier(first)), (second.support, -tier(second))
            if score_a[0] >= score_b[0] + 1 and score_a >= score_b:
                winner, loser = first, second
            elif score_b[0] >= score_a[0] + 1 and score_b >= score_a:
                winner, loser = second, first
            else:
                winner = loser = None
            if winner is not None and loser is not None:
                loser.status = "disputed"
                loser.notes.append(f"contradicted by better-supported {winner.key}")
                resolution = f"consensus: {winner.key}"
            else:
                for group in (first, second):
                    group.status = "conflicting"
                    group.notes.append(f"{kind} disagreement with {(second if group is first else first).key}")
                resolution = "unresolved: both withheld"
            records.append({
                "type": kind,
                "claims": [first.key, second.key],
                "texts": [first.lead.text[:200], second.lead.text[:200]],
                "sources": [sorted({unit.source_id for unit in first.units}), sorted({unit.source_id for unit in second.units})],
                "resolution": resolution,
            })
    return records


def _parse(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def freshness(source: dict[str, Any], *, time_sensitive_question: bool, unit_time_sensitive: bool, today: date | None = None) -> dict[str, Any]:
    """Freshness of one source for one claim: evergreen | fresh | stale | undated."""
    today = today or datetime.now(UTC).date()
    dated = _parse(source.get("updated_at")) or _parse(source.get("published_at"))
    if not (time_sensitive_question or unit_time_sensitive):
        return {"status": "evergreen", "source_date": dated.isoformat() if dated else None}
    limit = CURRENT_MAX_AGE_DAYS if time_sensitive_question else TIME_WORD_MAX_AGE_DAYS
    if dated is None:
        return {"status": "undated", "max_age_days": limit}
    age = (today - dated).days
    return {"status": "fresh" if age <= limit else "stale", "age_days": age, "max_age_days": limit, "source_date": dated.isoformat()}


def filter_fresh(
    units: list[EvidenceUnit], sources: dict[str, dict[str, Any]], *, time_sensitive_question: bool, today: date | None = None
) -> tuple[list[EvidenceUnit], list[dict[str, Any]]]:
    """Units usable for their claim; stale (and, for current questions, undated) ones are rejected."""
    kept: list[EvidenceUnit] = []
    rejected: list[dict[str, Any]] = []
    for unit in units:
        state = freshness(sources.get(unit.source_id, {}), time_sensitive_question=time_sensitive_question,
                          unit_time_sensitive=unit.time_sensitive, today=today)
        if state["status"] in {"evergreen", "fresh"} or (state["status"] == "undated" and not time_sensitive_question):
            kept.append(unit)
        else:
            rejected.append({"evidence_id": unit.id, "text": unit.text[:200], "reason": f"{state['status']}_for_time_sensitive_claim", **state})
    return kept, rejected
