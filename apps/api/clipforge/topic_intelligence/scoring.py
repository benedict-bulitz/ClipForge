"""THE Topic Intelligence scoring authority.

Nothing else ranks candidates: routes, the UI and the future learning loop
only read ``score_breakdown`` / ``explanation`` produced here.

Formula (``ti-score-v1``)::

    effective_i = 0.5 + (value_i - 0.5) * CONFIDENCE_WEIGHT[confidence_i]
    effective_i = 0.5                                  if signal i is unavailable
    final       = sum_i weight_i * effective_i - flag_penalty

* Missing data is neutral (0.5), never zero: a channel without analytics, or a
  source that failed, cannot make a candidate look bad - it only lowers the
  candidate's overall ``confidence``.
* Low-confidence evidence is shrunk towards neutral instead of trusted fully.
* ``competition`` is a saturation estimate; it enters as openness
  (1 - saturation), halved as a penalty when current demand is exceptional.
* ``trend`` decays with the age of its evidence (half-life 48 h).
* Hard rejections (duplicates, unusable questions, poor fit) are separate from
  the score and always listed in ``rejection_reasons``.
"""
from __future__ import annotations

import hashlib
import json
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

SCORE_VERSION = "ti-score-v1"
NEUTRAL_PRIOR = 0.5
CONFIDENCE_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.8, "low": 0.55, "unavailable": 0.0}
TREND_HALF_LIFE_HOURS = 48.0
EXCEPTIONAL_DEMAND = 0.85
FLAG_PENALTY = 0.06
MAX_FLAG_PENALTY = 0.18
MIN_CHANNEL_FIT = 0.3
MIN_SUITABILITY = 0.35

# Weights sum to 1.0.  Demand and knowledge-short quality dominate; novelty and
# fit keep the channel coherent; outlier and competition refine; own
# performance is a small optional prior until real analytics accumulate.
DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.20,
    "suitability": 0.15,
    "novelty": 0.13,
    "channel_fit": 0.13,
    "outlier": 0.12,
    "visual": 0.08,
    "researchability": 0.08,
    "competition": 0.07,
    "own_performance": 0.04,
}
WEIGHT_RATIONALE: dict[str, str] = {
    "trend": "Current German-language demand is the main reason to make a video now.",
    "suitability": "Curiosity gap, clear payoff and substance decide whether a short can work at all.",
    "novelty": "Repeating recent ClipForge topics wastes a slot.",
    "channel_fit": "German short-form knowledge content, relevant to DACH viewers.",
    "outlier": "Related videos beating their own channel's normal level signal topic pull, not channel size.",
    "visual": "Real footage must be able to show it.",
    "researchability": "Claims must be verifiable by the existing research step.",
    "competition": "Saturated topics are harder to win; an estimate, so a modest weight.",
    "own_performance": "Optional prior from the channel's own published videos; small until data is rich.",
}

LABELS = {
    "trend": ("Recent interest", "Little recent interest"),
    "outlier": ("Related videos outperform their channels", "No outlier evidence"),
    "novelty": ("Strong novelty", "Close to a previous topic"),
    "channel_fit": ("Good channel fit", "Weak channel fit"),
    "suitability": ("Strong knowledge-short question", "Weak knowledge-short question"),
    "visual": ("Good visual potential", "Limited visual potential"),
    "researchability": ("Well researchable", "Hard to verify"),
    "own_performance": ("Similar videos did well on your channel", "Similar videos were weaker on your channel"),
}


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


def _trend_decay(candidate: TopicCandidate, now: datetime) -> float:
    if candidate.freshness_at is None:
        return 1.0
    age = max(0.0, (now - candidate.freshness_at).total_seconds() / 3600)
    return 0.5 ** (age / TREND_HALF_LIFE_HOURS)


def _effective(name: str, signal: Signal, candidate: TopicCandidate, now: datetime) -> tuple[float, float | None, dict[str, Any]]:
    """(effective value used in the sum, raw directional value, notes)."""
    notes: dict[str, Any] = {}
    if not signal.available:
        return NEUTRAL_PRIOR, None, {"missing": True}
    raw = float(signal.value or 0.0)
    if name == "trend":
        decay = _trend_decay(candidate, now)
        if decay < 0.999:
            notes["freshness_decay"] = round(decay, 3)
        raw *= decay
    if name == "competition":
        trend = candidate.signal("trend")
        exceptional = trend.available and (trend.value or 0) >= EXCEPTIONAL_DEMAND and trend.confidence in {"medium", "high"}
        saturation = raw * (0.5 if exceptional else 1.0)
        if exceptional:
            notes["exceptional_demand"] = True
        raw = 1.0 - saturation  # openness
    effective = NEUTRAL_PRIOR + (raw - NEUTRAL_PRIOR) * CONFIDENCE_WEIGHT[signal.confidence]
    return clamp(effective), raw, notes


def rejection_reasons(candidate: TopicCandidate, *, issues: list[str], flags: list[str]) -> list[str]:
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
    components: dict[str, Any] = {}
    total = 0.0
    for name in SIGNAL_NAMES:
        weight = float(weights.get(name, 0.0))
        signal = candidate.signal(name)
        effective, raw, notes = _effective(name, signal, candidate, now)
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
    penalty = min(MAX_FLAG_PENALTY, FLAG_PENALTY * len(soft_flags))
    final = round(clamp(total - penalty), 4)
    candidate.final_score = final
    candidate.score_version = version
    candidate.rejection_reasons = rejection_reasons(candidate, issues=list(issues or []), flags=flags)
    candidate.confidence = overall_confidence(candidate, weights, degraded_sources=degraded_sources)
    candidate.score_breakdown = {
        "version": version,
        "components": components,
        "penalties": {"flags": soft_flags, "value": round(penalty, 4)},
        "final": final,
        "neutral_prior": NEUTRAL_PRIOR,
        "confidence": candidate.confidence,
        "degraded_sources": degraded_sources,
    }
    return candidate


def rank(candidates: list[TopicCandidate]) -> list[TopicCandidate]:
    """Deterministic: usable first, then score, then the stable candidate id."""
    return sorted(candidates, key=lambda item: (item.rejected, -item.final_score, item.candidate_id))


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
        elif value < 0.35 and name in {"novelty", "channel_fit", "suitability", "own_performance"}:
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
