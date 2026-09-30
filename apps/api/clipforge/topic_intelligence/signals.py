"""Pure signal math: trend, outlier-relative-to-channel and competition.

Every function returns a ``Signal`` with its sample size and method in the
evidence, so the rationale can always be traced back.  No causal claims: an
outlier says a video beat its own channel's normal level, not why.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .candidate import Signal, clamp
from .text import similarity

WIKIPEDIA_MIN_BASELINE = 50.0  # views/day floor so tiny articles cannot fake a spike
STRONG_VIEWS = 100_000


def _log_scale(ratio: float, full_at: float) -> float:
    """0 at ratio <= 1, 1 at ratio >= full_at, logarithmic in between."""
    if ratio <= 1:
        return 0.0
    return clamp(math.log(ratio) / math.log(full_at))


def wikipedia_trend(history: Sequence[float], *, rank: int | None = None, top_size: int = 60) -> Signal:
    """Recent interest vs. the article's own median of the preceding weeks."""
    values = [float(item) for item in history if item is not None]
    if len(values) >= 10:
        recent_values = values[-2:]
        baseline_values = values[:-3]
        recent = statistics.fmean(recent_values)
        baseline = max(WIKIPEDIA_MIN_BASELINE, statistics.median(baseline_values))
        ratio = recent / baseline
        n = len(baseline_values)
        if n >= 21 and recent >= 3000:
            confidence = "high"
        elif n >= 14:
            confidence = "medium"
        else:
            confidence = "low"
        return Signal(
            round(_log_scale(ratio, 8.0), 4),
            confidence,
            {
                "method": "wikipedia_pageviews_recent_vs_median",
                "recent_views_per_day": round(recent),
                "baseline_median_views_per_day": round(baseline),
                "ratio": round(ratio, 2),
                "baseline_days": n,
            },
            ["wikipedia_pageviews"],
        )
    if rank is not None:
        return Signal(
            round(0.5 * clamp(1 - rank / max(1, top_size)), 4),
            "low",
            {"method": "wikipedia_top_rank_only", "rank": rank},
            ["wikipedia_pageviews"],
        )
    return Signal.unavailable("no_pageview_history")


def trending_chart_trend(rank: int, chart_size: int, *, category: str) -> Signal:
    """Being on YouTube's German most-popular chart is direct current demand."""
    value = 0.55 + 0.35 * clamp(1 - (rank - 1) / max(1, chart_size))
    return Signal(
        round(value, 4),
        "medium",
        {"method": "youtube_most_popular_de", "rank": rank, "chart_size": chart_size, "category": category},
        ["youtube_trending_de"],
    )


def news_trend(outlets: int, articles: int) -> Signal:
    """Several independent German outlets covering a story in the last day."""
    if articles <= 0:
        return Signal.unavailable("no_recent_news")
    value = clamp(0.25 + 0.15 * outlets + 0.05 * articles, 0.0, 0.9)
    confidence = "medium" if outlets >= 3 else "low"
    return Signal(
        round(value, 4),
        confidence,
        {"method": "brave_news_de_last_day", "outlets": outlets, "articles": articles},
        ["brave_news_de"],
    )


def views_per_day(views: float, published_at: datetime, now: datetime) -> float:
    age_days = max(0.5, (now - published_at).total_seconds() / 86_400)
    return float(views) / age_days


def outlier_vs_channel(video_vpd: float, channel_recent_vpd: Sequence[float]) -> Signal:
    """Video views/day relative to the median views/day of the channel's recent uploads.

    Robust: median and MAD on log values; sample size decides confidence.
    """
    sample = [float(value) for value in channel_recent_vpd if value is not None and value > 0]
    n = len(sample)
    if n < 3 or video_vpd <= 0:
        return Signal.unavailable("channel_sample_too_small", sample_size=n)
    median = statistics.median(sample)
    ratio = video_vpd / max(1.0, median)
    logs = [math.log(value) for value in sample]
    log_median = statistics.median(logs)
    mad = statistics.median(abs(value - log_median) for value in logs)
    robust_z = (math.log(video_vpd) - log_median) / (1.4826 * mad) if mad > 0 else None
    confidence = "high" if n >= 10 else "medium" if n >= 5 else "low"
    return Signal(
        round(_log_scale(ratio, 10.0), 4),
        confidence,
        {
            "method": "views_per_day_vs_channel_recent_median",
            "ratio": round(ratio, 2),
            "robust_z": None if robust_z is None else round(robust_z, 2),
            "sample_size": n,
            "channel_median_views_per_day": round(median, 1),
        },
        ["youtube_trending_de"],
    )


def outlier_vs_channel_mean(video_views: float, channel_views: float, channel_videos: float) -> float | None:
    """Fallback ratio against the channel's lifetime mean views per video."""
    if channel_videos <= 0 or channel_views <= 0:
        return None
    return float(video_views) / (channel_views / channel_videos)


def competition_estimate(question: str, topic: str, results: Sequence[dict[str, Any]]) -> tuple[Signal, Signal]:
    """(competition saturation, outlier evidence) from one bounded YouTube search.

    Saturation counts recent German Shorts that are strongly related to the
    candidate and how many of them are already established hits.  It is an
    estimate of supply, not of market demand.
    """
    items = [item for item in results if isinstance(item, dict)]
    if not items:
        return (
            Signal(0.0, "low", {"method": "youtube_search_estimate", "sample": 0, "related": 0, "strong": 0}, ["youtube_search_competition"]),
            Signal.unavailable("no_related_videos"),
        )
    related = [
        item for item in items
        if max(similarity(question, str(item.get("title") or "")), similarity(topic, str(item.get("title") or ""))) >= 0.5
    ]
    strong = [item for item in related if float(item.get("views") or 0) >= STRONG_VIEWS]
    near_identical = [item for item in related if similarity(question, str(item.get("title") or "")) >= 0.8]
    saturation = clamp(
        0.5 * min(1.0, len(related) / 10) + 0.3 * min(1.0, len(strong) / 4) + 0.2 * min(1.0, len(near_identical) / 3)
    )
    competition = Signal(
        round(saturation, 4),
        "medium" if len(items) >= 10 else "low",
        {
            "method": "youtube_search_estimate",
            "estimate": True,
            "sample": len(items),
            "related": len(related),
            "strong": len(strong),
            "near_identical": len(near_identical),
        },
        ["youtube_search_competition"],
    )
    ratios = []
    for item in related:
        ratio = outlier_vs_channel_mean(
            float(item.get("views") or 0), float(item.get("channel_views") or 0), float(item.get("channel_videos") or 0)
        )
        if ratio is not None and float(item.get("channel_videos") or 0) >= 10:
            ratios.append(ratio)
    if not ratios:
        return competition, Signal.unavailable("no_channel_baseline_for_related_videos")
    best = max(ratios)
    outlier = Signal(
        round(_log_scale(best, 10.0), 4),
        "low",
        {
            "method": "related_video_views_vs_channel_lifetime_mean",
            "best_ratio": round(best, 2),
            "sample_size": len(ratios),
        },
        ["youtube_search_competition"],
    )
    return competition, outlier


def merge_trend(signals: Sequence[Signal]) -> Signal:
    """Strongest trend evidence; independent sources agreeing raise confidence."""
    available = [signal for signal in signals if signal.available]
    if not available:
        return Signal.unavailable("no_trend_source")
    order = ("unavailable", "low", "medium", "high")
    best = max(available, key=lambda item: (item.value or 0.0, order.index(item.confidence)))
    sources = sorted({source for item in available for source in item.sources})
    confidence = best.confidence
    if len(sources) >= 2 and confidence != "high":
        confidence = "medium" if confidence == "low" else "high"
    return Signal(
        best.value,
        confidence,  # type: ignore[arg-type]
        {**best.evidence, "corroborating_sources": len(sources)},
        sources,
    )


# ---------------------------------------------------------------------------
# Fit and knowledge-short quality (from the question assessment)
# ---------------------------------------------------------------------------

SUITABILITY_KEYS = ("curiosity_gap", "clear_payoff", "substance", "premise_clarity", "information_gain")


def channel_fit(niche: str, niche_prior: float, dach_relevance: float | None, *, confidence: str, method: str) -> Signal:
    """German short-form knowledge channel fit: the niche's documented prior + DACH relevance."""
    dach = 0.75 if dach_relevance is None else dach_relevance
    return Signal(
        round(clamp(0.75 * niche_prior + 0.25 * dach), 4),
        confidence,  # type: ignore[arg-type]
        {"niche": niche, "niche_prior": niche_prior, "dach_relevance": round(dach, 3), "method": method},
        ["topic_assessment"],
    )


def suitability(assessment: dict[str, float], *, confidence: str, method: str) -> Signal:
    values = [assessment[key] for key in SUITABILITY_KEYS if key in assessment]
    if not values:
        return Signal.unavailable("not_assessed")
    return Signal(
        round(statistics.fmean(values), 4),
        confidence,  # type: ignore[arg-type]
        {key: assessment[key] for key in SUITABILITY_KEYS if key in assessment} | {"method": method},
        ["topic_assessment"],
    )


def assessed(name: str, value: float | None, *, confidence: str, method: str, **evidence: Any) -> Signal:
    if value is None:
        return Signal.unavailable("not_assessed")
    return Signal(round(clamp(value), 4), confidence, {"method": method, **evidence}, ["topic_assessment"])  # type: ignore[arg-type]



# ---------------------------------------------------------------------------
# Mass-audience quality features (ti-score-v2)
# ---------------------------------------------------------------------------

# How much each obscurity carried into the question costs accessibility.
ACCESSIBILITY_DEDUCTIONS = {
    # Prior knowledge the viewer needs before curiosity can start (content cues).
    "needs_title_context": 0.45,
    "names_obscure_entity": 0.35,
    "named_brand": 0.3,
    "multiword_name": 0.3,
    "product_news": 0.3,
    "institutional": 0.25,
    "specific_event_reference": 0.3,
    # Obscurity carried into the question.
    "question_date": 0.45,
    "question_identifier": 0.3,
    "question_acronym": 0.2,
    "question_compound_proper_name": 0.2,
    "question_foreign_proper_name": 0.15,
}
# Curiosity strength of a question's structure (paradox and what-if open the widest gap).
QUESTION_FORM_VALUES = {
    "paradox": 0.9,
    "what_if": 0.85,
    "why": 0.8,
    "how": 0.72,
    "yes_no": 0.6,
    "other": 0.5,
    "what_is": 0.35,
}
GENERIC_WRAPPER_CAP = 0.25


UNIVERSAL_SUBJECT_BONUS = 0.05


def broad_appeal(
    niche: str,
    prior: float,
    assessed_value: float | None,
    *,
    method: str,
    universal_subject: bool = False,
    mechanism: str = "other",
    prior_knowledge: set[str] | None = None,
) -> Signal:
    """Would an average German viewer (12+) without special interest want the answer?

    Content-based: the question's own subject and mechanism count as much as the
    niche prior, so a niche-origin question about a universal phenomenon can
    score well and a "good" niche cannot carry a question that needs prior knowledge.
    """
    if assessed_value is not None:
        return Signal(round(clamp(assessed_value), 4), "medium", {"method": method, "niche": niche, "niche_prior": prior}, ["topic_assessment"])
    content = 0.55 + (0.2 if universal_subject else 0.0) + (0.1 if mechanism in {"why", "how", "what_if", "paradox"} else 0.0)
    content -= 0.25 if prior_knowledge else 0.0
    value = clamp(0.5 * prior + 0.5 * content)
    return Signal(
        round(value, 4),
        "low",
        {"method": "niche_prior_and_question_content", "niche": niche, "niche_prior": prior, "universal_subject": universal_subject,
         "mechanism": mechanism, "prior_knowledge": sorted(prior_knowledge or [])},
        ["topic_assessment"],
    )


def accessibility(
    question_flags: set[str],
    topic_flags: set[str],
    assessed_value: float | None,
    *,
    method: str,
    prior_knowledge: set[str] | None = None,
    universal_subject: bool = False,
) -> Signal:
    """Universal 12+ accessibility: the premise is clear without context or specialist knowledge."""
    flags = set(question_flags) | set(prior_knowledge or [])
    deductions = {flag: ACCESSIBILITY_DEDUCTIONS[flag] for flag in sorted(flags) if flag in ACCESSIBILITY_DEDUCTIONS}
    feature_value = clamp(0.9 - sum(deductions.values()) + (UNIVERSAL_SUBJECT_BONUS if universal_subject and not deductions else 0.0))
    value = feature_value if assessed_value is None else min(feature_value, clamp(assessed_value))
    return Signal(
        round(value, 4),
        "medium",
        {
            "method": method if assessed_value is not None else "text_features",
            "question_flags": sorted(question_flags),
            "topic_flags": sorted(topic_flags),
            "prior_knowledge": sorted(prior_knowledge or []),
            "universal_subject": universal_subject,
            "deductions": deductions,
        },
        ["topic_features"],
    )


def question_form(mechanism: str, flags: set[str]) -> Signal:
    """Strength of the question's curiosity structure; a bare generic wrapper is weak."""
    value = QUESTION_FORM_VALUES.get(mechanism, QUESTION_FORM_VALUES["other"])
    if "generic_wrapper" in flags:
        value = min(value, GENERIC_WRAPPER_CAP)
    return Signal(round(value, 4), "high", {"mechanism": mechanism, "generic_wrapper": "generic_wrapper" in flags}, ["topic_features"])


# ---------------------------------------------------------------------------
# Short-worthiness (ti-score-v6): would this make a STRONG short, not merely a clear one?
# ---------------------------------------------------------------------------

# The curator's short dimensions (0-1) and their share of the value.
SHORT_WEIGHTS = {
    "curiosity_strength": 0.25,
    "payoff_specificity": 0.20,
    "reveal_potential": 0.20,
    "concreteness": 0.15,
    "knowledge_short_fit": 0.10,
    "visual_potential": 0.10,
}
# Without a curator judgement only the question's structure is known (low confidence).
LOCAL_SHORT_BASE = {"paradox": 0.8, "what_if": 0.75, "why": 0.7, "how": 0.62, "yes_no": 0.58, "other": 0.45, "what_is": 0.35}
# Deterministic form of a weak default short (see ``text.short_shape_flags``).
SHAPE_PENALTIES = {"multi_part": 0.25, "list_answer": 0.25, "abstract_measure": 0.25, "advice": 0.12}
# Curator issues that lower the rank without rejecting (useful but dry is not wrong).
SOFT_ISSUE_PENALTIES = {"generic_advice": 0.12, "broad_overview": 0.1, "no_clear_reveal": 0.15}
MAX_SHORT_PENALTY = 0.4


def short_worthiness(mechanism: str, shape_flags: set[str], semantic: Signal | None = None) -> Signal:
    """Immediate curiosity, one specific reveal, concrete premise, substance and footage - minus weak shapes."""
    short = (semantic.evidence.get("short_dimensions") if semantic is not None and semantic.available else None) or None
    if short:
        base = sum(weight * float(short.get(name, 0.5)) for name, weight in SHORT_WEIGHTS.items())
        confidence, basis = "medium", "curator"
        issues = set(semantic.evidence.get("issues") or []) if semantic is not None else set()
        focus: float | None = float(short.get("single_question_focus", 0.5))
    else:
        base = LOCAL_SHORT_BASE.get(mechanism, LOCAL_SHORT_BASE["other"])
        confidence, basis, issues, focus = "low", "question_shape", set(), None
    penalties = {f"shape_{flag}": SHAPE_PENALTIES[flag] for flag in sorted(shape_flags) if flag in SHAPE_PENALTIES}
    penalties.update({f"issue_{issue}": SOFT_ISSUE_PENALTIES[issue] for issue in sorted(issues) if issue in SOFT_ISSUE_PENALTIES})
    if "shape_advice" in penalties and "issue_generic_advice" in penalties:
        del penalties["shape_advice"]  # the same weakness, counted once
    value = clamp(base - min(MAX_SHORT_PENALTY, sum(penalties.values())))
    return Signal(
        round(value, 4),
        confidence,  # type: ignore[arg-type]
        {"basis": basis, "base": round(base, 4), "penalties": penalties, "shape_flags": sorted(shape_flags),
         "single_question_focus": focus, "dimensions": short},
        ["short_worthiness"],
    )
