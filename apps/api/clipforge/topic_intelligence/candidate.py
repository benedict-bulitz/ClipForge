"""The canonical TopicCandidate model and its signals.

A *signal* is one independent piece of evidence normalized to 0..1 with an
explicit confidence.  Missing evidence is ``Signal.unavailable()`` - never a
zero - so a channel without analytics (or a failed source) does not make a
candidate look bad; the scoring authority treats it as neutral.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Confidence = Literal["unavailable", "low", "medium", "high"]
CONFIDENCE_ORDER: tuple[Confidence, ...] = ("unavailable", "low", "medium", "high")

# Every signal name the scoring authority understands.
SIGNAL_NAMES = (
    "trend",
    "outlier",
    "competition",
    "novelty",
    "channel_fit",
    "suitability",
    "visual",
    "researchability",
    "own_performance",
)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def lower_confidence(confidence: Confidence, steps: int = 1) -> Confidence:
    index = CONFIDENCE_ORDER.index(confidence)
    if index == 0:
        return confidence
    return CONFIDENCE_ORDER[max(1, index - steps)]


def max_confidence(*values: Confidence) -> Confidence:
    return max(values, key=CONFIDENCE_ORDER.index) if values else "unavailable"


@dataclass
class Signal:
    """One normalized piece of evidence (0..1) or an explicit absence."""

    value: float | None
    confidence: Confidence
    evidence: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    @classmethod
    def unavailable(cls, reason: str, **evidence: Any) -> Signal:
        return cls(None, "unavailable", {"reason": reason, **evidence}, [])

    @property
    def available(self) -> bool:
        return self.value is not None and self.confidence != "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else round(float(self.value), 4),
            "confidence": self.confidence,
            "evidence": self.evidence,
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Signal:
        payload = payload if isinstance(payload, dict) else {}
        value = payload.get("value")
        confidence = payload.get("confidence") if payload.get("confidence") in CONFIDENCE_ORDER else "unavailable"
        return cls(
            None if value is None else float(value),
            confidence,  # type: ignore[arg-type]
            dict(payload.get("evidence") or {}),
            list(payload.get("sources") or []),
        )


@dataclass
class RawTopic:
    """A topic as one source saw it, before any transformation or scoring."""

    key: str
    title: str
    source: str
    kind: Literal["article", "video", "news"]
    observed_at: datetime
    description: str = ""
    url: str | None = None
    language: str = "de"
    region: str = "DE"
    trend: Signal | None = None
    outlier: Signal | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)

    def source_signal(self) -> dict[str, Any]:
        """Compact provenance of this sighting (no raw payload)."""
        return {
            "source": self.source,
            "kind": self.kind,
            "title": self.title[:200],
            "url": self.url,
            "observed_at": self.observed_at.isoformat(),
            "metrics": self.metrics,
        }


@dataclass
class TopicGroup:
    """Every sighting of one topic across sources (corroboration)."""

    key: str
    sightings: list[RawTopic]

    @property
    def title(self) -> str:
        # Prefer the encyclopedic article title, then a news headline, then a video title.
        order = {"article": 0, "news": 1, "video": 2}
        return min(self.sightings, key=lambda item: (order[item.kind], item.title)).title

    @property
    def sources(self) -> list[str]:
        return sorted({item.source for item in self.sightings})

    @property
    def flags(self) -> set[str]:
        return set().union(*(item.flags for item in self.sightings)) if self.sightings else set()

    @property
    def newest(self) -> datetime:
        return max(item.observed_at for item in self.sightings)

    def description(self) -> str:
        for item in self.sightings:
            if item.description:
                return item.description
        return ""


@dataclass
class TopicCandidate:
    """The canonical candidate.  Persisted as ``models.TopicCandidateRecord``."""

    candidate_id: str
    topic: str
    question: str
    rationale: str
    source_signals: list[dict[str, Any]]
    discovered_at: datetime
    language: str
    region: str
    niche: str
    signals: dict[str, Signal]
    freshness_at: datetime | None
    provenance: dict[str, Any]
    final_score: float = 0.0
    confidence: Confidence = "low"
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    score_version: str = ""

    def signal(self, name: str) -> Signal:
        return self.signals.get(name) or Signal.unavailable("not_measured")

    @property
    def rejected(self) -> bool:
        return bool(self.rejection_reasons)


def candidate_id_for(question_key: str, language: str = "de", region: str = "DE") -> str:
    """Stable id: the same German question in the same market is the same candidate."""
    digest = hashlib.sha1(f"{language}:{region}:{question_key}".encode()).hexdigest()
    return f"tc_{digest[:24]}"
