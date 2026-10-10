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

from .evidence import EvidenceUnit, related, words
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
    return {tuple(tokens[index:index + size]) for index in range(max(0, len(tokens) - size + 1))}


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


_GROUPING_NOISE = {
    "weil", "denn", "deshalb", "daher", "darum", "deswegen", "dadurch", "sodass", "damit", "wodurch", "because", "therefore",
    "since", "thus", "hence", "dass", "that", "which", "wird", "werden", "wurde", "nur", "only",
}


def _proposition(unit: EvidenceUnit, topic: frozenset[str]) -> set[str]:
    """What a sentence says beyond the question's own topic words and connectives."""
    return {
        word for word in words(unit.text)
        if word not in _GROUPING_NOISE and not any(related(word, term) for term in topic)
    }


def _same_claim(first: EvidenceUnit, second: EvidenceUnit, topic: frozenset[str] = frozenset()) -> bool:
    """Two sources stating the same proposition (sentences of one page never merge).

    Agreement is measured on what the sentences say *beyond* the question's
    topic words: every sentence about microwaves shares "Mikrowelle" and
    "Essen", which is no evidence that two sources state the same cause.
    """
    if first.source_id == second.source_id:
        return False
    a, b = _proposition(first, topic), _proposition(second, topic)
    if not a or not b:
        return False
    shared = {word for word in a if any(related(word, other) for other in b)}
    overlap = len(shared) / min(len(a), len(b))
    if overlap < 0.4 or len(shared) < 3:
        return False
    # Different figures (years aside - a shared date is no shared figure) are a
    # possible contradiction, never agreement.
    values_a = {value for value, _counted in quantities(first.text)}
    values_b = {value for value, _counted in quantities(second.text)}
    if values_a and values_b and not values_a & values_b:
        return False
    return bool(_NEGATION.search(first.text)) == bool(_NEGATION.search(second.text))


def group_claims(
    units: list[EvidenceUnit], clusters: dict[str, dict[str, Any]], topic: frozenset[str] | set[str] = frozenset()
) -> list[ClaimGroup]:
    groups: list[ClaimGroup] = []
    topic = frozenset(topic)
    for unit in units:
        target = next((group for group in groups if _same_claim(group.lead, unit, topic)), None)
        if target is None:
            target = ClaimGroup(key=f"claim_{len(groups) + 1:02d}", units=[])
            groups.append(target)
        target.units.append(unit)
        target.clusters.add(str(clusters.get(unit.source_id, {}).get("cluster") or unit.source_id))
    return groups


_SCALE = {
    "tausend": 1e3, "thousand": 1e3, "million": 1e6, "millionen": 1e6, "mio": 1e6, "milliarde": 1e9, "milliarden": 1e9,
    "mrd": 1e9, "billion": 1e9,
}
_QUANTITY = re.compile(
    r"(?i)(\d+(?:[.,]\d{3})*(?:[.,]\d+)?)\s*(tausend|thousand|millionen|million|mio\.?|milliarden|milliarde|mrd\.?|billion)?"
    r"\s*(%|°c|[A-Za-zÄÖÜäöüß]{2,})?"
)


def quantities(text: str) -> list[tuple[float, str]]:
    """(value, what is counted) per number: "rund 3,5 Millionen Menschen" -> (3500000.0, "menschen")."""
    found: list[tuple[float, str]] = []
    for raw, scale, counted in _QUANTITY.findall(str(text or "")):
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            number = re.sub(r"[.,]", "", raw)  # 1.000 / 1,000
        elif "," in raw:
            number = raw.replace(".", "").replace(",", ".")  # German decimal comma
        else:
            number = raw
        try:
            value = float(number)
        except ValueError:
            continue
        if not scale and re.fullmatch(r"1\d{3}|20\d{2}", raw):
            continue  # a year is a date, not a quantity
        value *= _SCALE.get(scale.casefold().rstrip("."), 1.0) if scale else 1.0
        found.append((value, (counted or "").casefold()))
    return found


def _statement(unit: EvidenceUnit, topic: frozenset[str]) -> set[str]:
    """What a numeric sentence says besides its numbers, scales and the question's topic words."""
    return {word for word in _proposition(unit, topic) if not any(char.isdigit() for char in word) and word not in _SCALE}


def comparable_quantities(first: EvidenceUnit, second: EvidenceUnit, topic: frozenset[str]) -> tuple[float, float] | None:
    """Two values that measure the same thing, or None.

    Values are comparable only when they count the same thing ("Menschen",
    "Zentimeter") *and* the sentences say the same thing about it besides the
    number ("flohen" vs "getötet" are different metrics of the same history).
    """
    a_statement, b_statement = _statement(first, topic), _statement(second, topic)
    for value_a, counted_a in quantities(first.text):
        for value_b, counted_b in quantities(second.text):
            if not counted_a or not counted_b or not related(counted_a, counted_b):
                continue
            a_rest = {word for word in a_statement if not related(word, counted_a)}
            b_rest = {word for word in b_statement if not related(word, counted_b)}
            if not a_rest or not b_rest:
                return value_a, value_b
            shared = {word for word in a_rest if any(related(word, other) for other in b_rest)}
            if len(shared) / min(len(a_rest), len(b_rest)) >= 0.7:
                return value_a, value_b
    return None


def find_contradictions(
    groups: list[ClaimGroup], sources: dict[str, dict[str, Any]], topic: frozenset[str] | set[str] = frozenset()
) -> list[dict[str, Any]]:
    """Mark disagreeing claim groups; return compact contradiction records."""
    records: list[dict[str, Any]] = []
    topic = frozenset(topic)

    def tier(group: ClaimGroup) -> int:
        return min(TIER_RANK.get(str(sources.get(unit.source_id, {}).get("authority")), 3) for unit in group.units)

    for index, first in enumerate(groups):
        for second in groups[index + 1:]:
            if first.clusters & second.clusters:
                continue
            kind = None
            pair = comparable_quantities(first.lead, second.lead, topic)
            if pair is not None:
                low, high = sorted(pair)
                if high > 0 and (high - low) / high > 0.2:
                    kind = "numeric"
            else:
                # The same statement affirmed by one source and negated by another
                # (measured beyond the topic words every sentence shares).
                a, b = _proposition(first.lead, topic), _proposition(second.lead, topic)
                shared = {word for word in a if any(related(word, other) for other in b)}
                if a and b and len(shared) >= 3 and len(shared) / min(len(a), len(b)) >= 0.7 and (
                    bool(_NEGATION.search(first.lead.text)) != bool(_NEGATION.search(second.lead.text))
                ):
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
