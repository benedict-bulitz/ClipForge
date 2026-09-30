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
