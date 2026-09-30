"""THE Topic Intelligence scoring authority.

Nothing else ranks candidates: routes, the UI and the future learning loop
only read ``score_breakdown`` / ``explanation`` produced here.  Providers and
the question step only supply signals; every weight, penalty, gate and the
diversity rule live in this module.

Formula (``ti-score-v2``)::

    effective_i = 0.5 + (value_i - 0.5) * CONFIDENCE_WEIGHT[confidence_i]
    effective_i = 0.5                                  if signal i is unavailable
    final       = sum_i weight_i * effective_i - penalties

v2 changes after real validation surfaced encyclopedic winners ("Was steckt
eigentlich hinter 29. September?"):

* Mass-audience quality carries the score: suitability, broad appeal,
  accessibility (no prior niche knowledge needed) and question form together
  weigh 0.45; trend drops to 0.13.
* Trend quality: "pageviews increased" is not "strong video topic".  Trend is
  scaled by source corroboration (a lone Wikipedia spike counts 0.65, an
  uncorroborated obscure topic less) and by the candidate's mass-audience
  quality.
* Obscurity penalties (date pages, identifiers, isolated events, acronyms,
  proper-noun compounds, generic wrappers) - reduced to a quarter when there
  is exceptional evidence, so obscure != automatically rejected.
* Quality floor: a candidate below it is rejected (``below_quality_floor``)
  instead of being served as the least-bad filler.
* Diversity: among near-equal scores, prefer a different subject/mechanism.

v3 (real Mac validation: 0 of 16 candidates usable in local mode):

* Universal 12+ accessibility is a gate, not a bonus: a question whose premise
  needs prior knowledge (title context, a brand or multi-word name, product
  generation news, an institution, a date, an identifier) is rejected as
  ``requires_prior_knowledge`` - exceptional evidence does not lift this
  gate; only reframing the question around a universal phenomenon does.
* Accessibility weighs 0.14 (was 0.10), taken from trend and channel fit.
* The quality floor and obscurity penalties are unchanged.

Unchanged from v1: missing data is neutral (0.5), never zero, and only lowers
``confidence``; low-confidence evidence is shrunk towards neutral;
competition enters as openness; trend decays with evidence age; hard
rejections are separate from the score.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import Settings
from .candidate import (
    SIGNAL_NAMES,
    Confidence,
    Signal,
    TopicCandidate,
    clamp,
    lower_confidence,
)
from .history import is_duplicate
from .transform import REJECT_FLAGS

SCORE_VERSION = "ti-score-v3"
NEUTRAL_PRIOR = 0.5
CONFIDENCE_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.8, "low": 0.55, "unavailable": 0.0}
TREND_HALF_LIFE_HOURS = 48.0
EXCEPTIONAL_DEMAND = 0.85
FLAG_PENALTY = 0.06
MAX_FLAG_PENALTY = 0.18
MIN_CHANNEL_FIT = 0.3
MIN_SUITABILITY = 0.35

# Weights sum to 1.0.  Mass-audience quality (suitability + broad appeal +
# accessibility + question form = 0.45) outweighs demand; demand (trend,
# outlier) still decides between good topics; novelty and fit keep the channel
# coherent; own performance stays a small optional prior.
DEFAULT_WEIGHTS: dict[str, float] = {
    "suitability": 0.15,
    "broad_appeal": 0.14,
    "accessibility": 0.14,
    "trend": 0.11,
    "outlier": 0.10,
    "novelty": 0.09,
    "channel_fit": 0.05,
    "question_form": 0.06,
    "visual": 0.06,
    "researchability": 0.04,
    "competition": 0.04,
    "own_performance": 0.02,
}
WEIGHT_RATIONALE: dict[str, str] = {
    "suitability": "Curiosity gap, clear payoff and substance decide whether a short can work at all.",
    "broad_appeal": "General German knowledge shorts need topics an average viewer wants answered.",
    "trend": "Current German demand - but a lone page spike is not a video topic by itself.",
    "accessibility": "Universal 12+: the premise is clear without context, a specific name, product, date or specialist knowledge.",
    "outlier": "Related videos beating their own channel's normal level signal topic pull, not channel size.",
    "novelty": "Repeating recent ClipForge topics wastes a slot.",
    "channel_fit": "German short-form knowledge content, relevant to DACH viewers.",
    "question_form": "A concrete curiosity mechanism beats a generic 'Was steckt hinter X?' wrapper.",
    "visual": "Real footage must be able to show it.",
    "researchability": "Claims must be verifiable by the existing research step.",
    "competition": "Saturated topics are harder to win; an estimate, so a modest weight.",
    "own_performance": "Optional prior from the channel's own published videos; small until data is rich.",
}

# Mass-audience quality = mean of these (available ones only).
QUALITY_SIGNALS = ("suitability", "broad_appeal", "accessibility", "question_form")
QUALITY_FLOOR = 0.55
# Universal accessibility below this = the premise needs prior knowledge.
PRIOR_KNOWLEDGE_GATE = 0.5
QUALITY_FLOOR_EXCEPTIONAL = 0.45
# Trend quality: how much a spike counts by corroboration.
TREND_SINGLE_WIKIPEDIA = 0.65
TREND_SINGLE_OTHER = 0.85
TREND_OBSCURE_UNCORROBORATED = 0.6
# Obscurity penalties (topic flags count only while the question still depends on them).
OBSCURITY_PENALTIES: dict[str, float] = {
    "date_page": 0.15,
    "identifier": 0.10,
    "isolated_event": 0.08,
    "acronym": 0.06,
    "compound_proper_name": 0.06,
    "foreign_proper_name": 0.04,
    "generic_wrapper": 0.08,
}
MAX_OBSCURITY_PENALTY = 0.3
EXCEPTIONAL_PENALTY_FACTOR = 0.25
# Diversity: only candidates within this score distance of the best may be preferred.
DIVERSITY_TOLERANCE = 0.04

LABELS = {
    "trend": ("Recent interest", "Little recent interest"),
    "outlier": ("Related videos outperform their channels", "No outlier evidence"),
    "novelty": ("Strong novelty", "Close to a previous topic"),
    "channel_fit": ("Good channel fit", "Weak channel fit"),
    "suitability": ("Strong knowledge-short question", "Weak knowledge-short question"),
    "broad_appeal": ("Broad audience appeal", "Niche audience"),
    "accessibility": ("Instantly understandable premise", "Needs prior knowledge"),
    "question_form": ("Strong curiosity question", "Generic question"),
    "visual": ("Good visual potential", "Limited visual potential"),
    "researchability": ("Well researchable", "Hard to verify"),
    "own_performance": ("Similar videos did well on your channel", "Similar videos were weaker on your channel"),
}
NEGATIVE_LABEL_SIGNALS = {"novelty", "channel_fit", "suitability", "own_performance", "broad_appeal", "accessibility", "question_form"}


def resolve_weights(settings: Settings | None = None) -> tuple[dict[str, float], str]:
    """(normalized weights, score version).  Overrides are validated and versioned."""
    weights = dict(DEFAULT_WEIGHTS)
    raw = getattr(settings, "topic_score_weights", None) if settings is not None else None
    if raw:
        try:
            overrides = json.loads(raw)
        except ValueError as exc:
            raise ValueError("TOPIC_SCORE_WEIGHTS must be a JSON object") from exc
        if not isinstance(overrides, dict):
            raise ValueError("TOPIC_SCORE_WEIGHTS must be a JSON object")
        for name, value in overrides.items():
            if name not in DEFAULT_WEIGHTS or not isinstance(value, int | float) or value < 0:
                raise ValueError(f"Invalid topic score weight: {name}")
            weights[name] = float(value)
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("Topic score weights must not all be zero")
    normalized = {name: round(value / total, 6) for name, value in weights.items()}
    if normalized == {name: round(value, 6) for name, value in DEFAULT_WEIGHTS.items()}:
        return normalized, SCORE_VERSION
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:8]
    return normalized, f"{SCORE_VERSION}+w{digest}"


# ---------------------------------------------------------------------------
# Mass-audience quality, trend quality, exceptional evidence
# ---------------------------------------------------------------------------


def mass_audience_quality(candidate: TopicCandidate) -> float | None:
    """Mean of the available quality signals, or None when none was assessed."""
    values = [float(candidate.signal(name).value or 0.0) for name in QUALITY_SIGNALS if candidate.signal(name).available]
    return round(statistics.fmean(values), 4) if values else None


def _trend_sources(candidate: TopicCandidate) -> list[str]:
    return sorted(set(candidate.signal("trend").sources))


def _topic_flags(candidate: TopicCandidate) -> set[str]:
    return set(candidate.signal("accessibility").evidence.get("topic_flags") or [])


def _question_flags(candidate: TopicCandidate) -> set[str]:
    return set(candidate.signal("accessibility").evidence.get("question_flags") or [])


def exceptional_evidence(candidate: TopicCandidate) -> list[str]:
    """Strong reasons an otherwise obscure topic may still be worth a video."""
    reasons: list[str] = []
    trend = candidate.signal("trend")
    if trend.available and (trend.value or 0) >= EXCEPTIONAL_DEMAND and len(_trend_sources(candidate)) >= 2:
        reasons.append("corroborated_strong_trend")
    outlier = candidate.signal("outlier")
    if outlier.available and (outlier.value or 0) >= 0.8 and outlier.confidence in {"medium", "high"}:
        reasons.append("strong_outlier")
    suitable, appeal = candidate.signal("suitability"), candidate.signal("broad_appeal")
    if (
        suitable.available and appeal.available
        and (suitable.value or 0) >= 0.85 and (appeal.value or 0) >= 0.8
        and suitable.confidence in {"medium", "high"} and appeal.confidence in {"medium", "high"}
    ):
        reasons.append("compelling_payoff")
    return reasons


def trend_quality_factor(candidate: TopicCandidate, quality: float | None) -> tuple[float, dict[str, Any]]:
    """How much a trend spike is worth as evidence for a *video* topic."""
    sources = _trend_sources(candidate)
    if len(sources) >= 2:
        source_factor = 1.0
    elif sources == ["wikipedia_pageviews"]:
        source_factor = TREND_SINGLE_WIKIPEDIA
    else:
        source_factor = TREND_SINGLE_OTHER
    obscure = bool(_topic_flags(candidate)) and len(sources) < 2
    if obscure:
        source_factor *= TREND_OBSCURE_UNCORROBORATED
    q = NEUTRAL_PRIOR if quality is None else quality
    quality_gate = 0.4 + 0.6 * clamp((q - 0.35) / 0.35)
    factor = round(source_factor * quality_gate, 4)
    return factor, {
        "corroborating_sources": len(sources),
        "source_factor": round(source_factor, 4),
        "quality_gate": round(quality_gate, 4),
        "obscure_uncorroborated": obscure,
    }


def obscurity_penalty(candidate: TopicCandidate, exceptional: list[str]) -> tuple[float, dict[str, float]]:
    topic_flags = _topic_flags(candidate)
    question_flags = _question_flags(candidate)
    still_obscure = bool(question_flags - {"generic_wrapper"})
    items: dict[str, float] = {}
    for flag in sorted(topic_flags):
        if flag in OBSCURITY_PENALTIES and (flag == "date_page" or still_obscure):
            items[flag] = OBSCURITY_PENALTIES[flag]
    if candidate.signal("question_form").evidence.get("generic_wrapper") or "generic_wrapper" in question_flags:
        items["generic_wrapper"] = OBSCURITY_PENALTIES["generic_wrapper"]
    total = min(MAX_OBSCURITY_PENALTY, sum(items.values()))
    if exceptional:
        total *= EXCEPTIONAL_PENALTY_FACTOR
    return round(total, 4), items


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _trend_decay(candidate: TopicCandidate, now: datetime) -> float:
    if candidate.freshness_at is None:
        return 1.0
    age = max(0.0, (now - candidate.freshness_at).total_seconds() / 3600)
    return 0.5 ** (age / TREND_HALF_LIFE_HOURS)


def _effective(
    name: str, signal: Signal, candidate: TopicCandidate, now: datetime, trend_factor: float,
) -> tuple[float, float | None, dict[str, Any]]:
    """(effective value used in the sum, raw directional value, notes)."""
    notes: dict[str, Any] = {}
    if not signal.available:
        return NEUTRAL_PRIOR, None, {"missing": True}
    raw = float(signal.value or 0.0)
    if name == "trend":
        decay = _trend_decay(candidate, now)
        if decay < 0.999:
            notes["freshness_decay"] = round(decay, 3)
        notes["trend_quality_factor"] = trend_factor
        raw *= decay * trend_factor
    if name == "competition":
        trend = candidate.signal("trend")
        exceptional = trend.available and (trend.value or 0) >= EXCEPTIONAL_DEMAND and trend.confidence in {"medium", "high"}
        saturation = raw * (0.5 if exceptional else 1.0)
        if exceptional:
            notes["exceptional_demand"] = True
        raw = 1.0 - saturation  # openness
    effective = NEUTRAL_PRIOR + (raw - NEUTRAL_PRIOR) * CONFIDENCE_WEIGHT[signal.confidence]
    return clamp(effective), raw, notes


def rejection_reasons(
    candidate: TopicCandidate, *, issues: list[str], flags: list[str], quality: float | None, floor: float,
) -> list[str]:
    reasons: list[str] = []
    if is_duplicate(candidate.signal("novelty")):
        reasons.append("duplicate_of_previous_topic")
    reasons.extend(f"question_{issue}" for issue in issues)
    reasons.extend(f"flag_{flag}" for flag in flags if flag in REJECT_FLAGS)
    fit = candidate.signal("channel_fit")
    if fit.available and (fit.value or 0) < MIN_CHANNEL_FIT:
        reasons.append("poor_channel_fit")
    suitability = candidate.signal("suitability")
    if suitability.available and (suitability.value or 0) < MIN_SUITABILITY:
        reasons.append("weak_knowledge_short")
    access = candidate.signal("accessibility")
    if access.available and (access.value or 0) < PRIOR_KNOWLEDGE_GATE:
        reasons.append("requires_prior_knowledge")
    if quality is not None and quality < floor:
        reasons.append("below_quality_floor")
    return list(dict.fromkeys(reasons))


def overall_confidence(candidate: TopicCandidate, weights: dict[str, float], *, degraded_sources: bool) -> Confidence:
    covered = sum(weights[name] * CONFIDENCE_WEIGHT[candidate.signal(name).confidence] for name in weights)
    confidence: Confidence = "high" if covered >= 0.75 else "medium" if covered >= 0.5 else "low"
    return lower_confidence(confidence) if degraded_sources else confidence


def score_candidate(
    candidate: TopicCandidate,
    *,
    weights: dict[str, float],
    version: str,
    now: datetime,
    issues: list[str] | None = None,
    flags: list[str] | None = None,
    degraded_sources: bool = False,
) -> TopicCandidate:
    """Fill final_score, score_breakdown, confidence and rejection_reasons in place."""
    flags = list(flags or [])
    quality = mass_audience_quality(candidate)
    exceptional = exceptional_evidence(candidate)
    trend_factor, trend_notes = trend_quality_factor(candidate, quality)
    components: dict[str, Any] = {}
    total = 0.0
    for name in SIGNAL_NAMES:
        weight = float(weights.get(name, 0.0))
        signal = candidate.signal(name)
        effective, raw, notes = _effective(name, signal, candidate, now, trend_factor)
        contribution = weight * effective
        total += contribution
        components[name] = {
            "value": None if signal.value is None else round(float(signal.value), 4),
            "directional_value": None if raw is None else round(raw, 4),
            "confidence": signal.confidence,
            "effective": round(effective, 4),
            "weight": weight,
            "contribution": round(contribution, 4),
            **notes,
        }
    soft_flags = [flag for flag in flags if flag not in REJECT_FLAGS]
    flag_penalty = min(MAX_FLAG_PENALTY, FLAG_PENALTY * len(soft_flags))
    obscurity, obscurity_items = obscurity_penalty(candidate, exceptional)
    penalty = round(flag_penalty + obscurity, 4)
    final = round(clamp(total - penalty), 4)
    floor = QUALITY_FLOOR_EXCEPTIONAL if exceptional else QUALITY_FLOOR
    candidate.final_score = final
    candidate.score_version = version
    candidate.rejection_reasons = rejection_reasons(candidate, issues=list(issues or []), flags=flags, quality=quality, floor=floor)
    candidate.confidence = overall_confidence(candidate, weights, degraded_sources=degraded_sources)
    candidate.score_breakdown = {
        "version": version,
        "components": components,
        "penalties": {
            "flags": soft_flags,
            "flag_value": round(flag_penalty, 4),
            "obscurity": obscurity_items,
            "obscurity_value": obscurity,
            "value": penalty,
        },
        "quality": {
            "mass_audience": quality,
            "floor": floor,
            "passed": quality is None or quality >= floor,
            "exceptional_evidence": exceptional,
            "prior_knowledge_gate": PRIOR_KNOWLEDGE_GATE,
            "universal_accessibility": candidate.signal("accessibility").value,
            "prior_knowledge": candidate.signal("accessibility").evidence.get("prior_knowledge") or [],
        },
        "trend_quality": trend_notes,
        "final": final,
        "neutral_prior": NEUTRAL_PRIOR,
        "confidence": candidate.confidence,
        "degraded_sources": degraded_sources,
    }
    return candidate


def rank_key(rejected: bool, final_score: float, candidate_id: str) -> tuple[bool, float, str]:
    """The one ordering: usable first, then score, then the stable candidate id."""
    return (rejected, -final_score, candidate_id)


def rank(candidates: list[TopicCandidate]) -> list[TopicCandidate]:
    return sorted(candidates, key=lambda item: rank_key(item.rejected, item.final_score, item.candidate_id))


@dataclass(frozen=True)
class RankedItem:
    candidate_id: str
    final_score: float
    niche: str
    mechanism: str


def diversify(items: Sequence[RankedItem], shown: Sequence[tuple[str, str]] = (), *, tolerance: float = DIVERSITY_TOLERANCE) -> list[RankedItem]:
    """Reorder ranked items so near-equal candidates vary in subject and question structure.

    Quality outranks diversity: a candidate is only moved ahead of a better one
    that is at most ``tolerance`` points stronger.  ``shown`` = (niche,
    mechanism) of suggestions that are already visible.
    """
    remaining = sorted(items, key=lambda item: (-item.final_score, item.candidate_id))
    niches = Counter(niche for niche, _mechanism in shown)
    mechanisms = Counter(mechanism for _niche, mechanism in shown)
    result: list[RankedItem] = []
    while remaining:
        best = remaining[0]
        window = [item for item in remaining if best.final_score - item.final_score <= tolerance]
        pick = min(
            window,
            key=lambda item: (
                (niches[item.niche] > 0 and item.niche != "unknown") + (mechanisms[item.mechanism] > 0),
                -item.final_score,
                item.candidate_id,
            ),
        )
        result.append(pick)
        remaining.remove(pick)
        niches[pick.niche] += 1
        mechanisms[pick.mechanism] += 1
    return result


def explain(breakdown: dict[str, Any], *, limit: int = 4) -> list[dict[str, str]]:
    """Compact 'why this topic' lines (↑ / → / ↓) derived only from the breakdown."""
    components = breakdown.get("components") or {}
    lines: list[tuple[float, dict[str, str]]] = []
    for name, (positive, negative) in LABELS.items():
        item = components.get(name) or {}
        value = item.get("directional_value")
        if value is None or item.get("confidence") == "unavailable":
            continue
        weight = float(item.get("weight") or 0)
        if value >= 0.65:
            lines.append((weight * value, {"direction": "up", "label": positive, "signal": name, "confidence": item["confidence"]}))
        elif value < 0.35 and name in NEGATIVE_LABEL_SIGNALS:
            lines.append((weight * (1 - value) * 0.5, {"direction": "down", "label": negative, "signal": name, "confidence": item["confidence"]}))
    lines.sort(key=lambda entry: -entry[0])
    result = [line for _weight, line in lines[: limit - 1]]
    competition = components.get("competition") or {}
    saturation = competition.get("value")
    if saturation is not None and competition.get("confidence") != "unavailable":
        if saturation < 0.35:
            result.append({"direction": "up", "label": "Low competition", "signal": "competition", "confidence": competition["confidence"]})
        elif saturation <= 0.65:
            result.append({"direction": "neutral", "label": "Moderate competition", "signal": "competition", "confidence": competition["confidence"]})
        else:
            result.append({"direction": "down", "label": "Crowded topic", "signal": "competition", "confidence": competition["confidence"]})
    elif len(lines) >= limit:
        result.append(lines[limit - 1][1])
    return result
