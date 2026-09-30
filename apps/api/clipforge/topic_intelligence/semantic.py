"""Semantic question validation (semantic-validator-v1).

Deterministic rules verify a question's FORM; they cannot judge its meaning
(an unexplained metaphor, a rhetorical question, a niche term that looks like
ordinary German, an unclear payoff).  This step asks the existing worker
model - batched, cached, within the pool's AI budget - to rate each final
QUESTION on its own, without the source headline:

    self_contained_clarity, clear_factual_payoff, universal_12plus_relevance,
    prior_knowledge_free, natural_spoken_german, knowledge_short_fit  (0-10)

plus explicit issue codes.  It only supplies the ``semantic`` signal; accept /
reject stays in ``scoring``.  Validation never runs during video generation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import TopicSourceCache
from .candidate import Signal

SEMANTIC_VALIDATOR_VERSION = "semantic-validator-v1"
SEMANTIC_CLIENT_FACTORY: Any = OpenAI
MAX_VALIDATION_BATCH = 20
CACHE_PROVIDER = "semantic_validator"
CACHE_TTL = timedelta(days=14)

DIMENSIONS = (
    "self_contained_clarity",
    "clear_factual_payoff",
    "universal_12plus_relevance",
    "prior_knowledge_free",
    "natural_spoken_german",
    "knowledge_short_fit",
)
ISSUES = (
    "unexplained_metaphor",
    "unclear_payoff",
    "rhetorical_or_opinion",
    "niche_context_required",
    "too_narrow_audience",
    "unnatural_or_headline_german",
)

VALIDATOR_INSTRUCTIONS = (
    "You are the strict quality gate for ClipForge's default topic suggestions: German knowledge shorts (15-40 s) "
    "for a broad German-speaking audience aged about 12 and older - general curiosity, not children's content. You see "
    "ONLY the final question, exactly as a viewer would; judge it without any source headline or article. Rate 0-10: "
    "self_contained_clarity (fully understandable on its own; no missing context, no word whose meaning depends on an "
    "article), clear_factual_payoff (it is obvious which concrete, verifiable knowledge the video answers), "
    "universal_12plus_relevance (an average viewer without a special hobby, product, brand or community would plausibly "
    "want the answer), prior_knowledge_free (understanding the premise needs no niche product, company, event, community, "
    "specialist term or trend first), natural_spoken_german (a real person would ask it like this; not a headline, teaser "
    "or translation), knowledge_short_fit (a factual, satisfying explanation is possible; not opinion, rhetoric, advice for "
    "one product or a survey result only). List issues with these codes only: unexplained_metaphor (a figurative phrase "
    "carries the question and its literal meaning is unclear), unclear_payoff, rhetorical_or_opinion, "
    "niche_context_required, too_narrow_audience, unnatural_or_headline_german. Be strict: when in doubt, score low. "
    "A question about a specific product, brand setup or niche trend is not a broad default suggestion even if it is "
    "grammatical. A question about a universal phenomenon is fine even if it came from a niche source. reason: at most 12 "
    "words. Return the supplied id unchanged. Structured output only."
)


class AISemanticJudgement(BaseModel):
    id: str
    self_contained_clarity: int = Field(ge=0, le=10)
    clear_factual_payoff: int = Field(ge=0, le=10)
    universal_12plus_relevance: int = Field(ge=0, le=10)
    prior_knowledge_free: int = Field(ge=0, le=10)
    natural_spoken_german: int = Field(ge=0, le=10)
    knowledge_short_fit: int = Field(ge=0, le=10)
    issues: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=200)


class AISemanticBatch(BaseModel):
    items: list[AISemanticJudgement]


def semantic_enabled(settings: Settings) -> bool:
    """Validation needs the OpenAI key; it is independent of the video director's AI mode."""
    return bool(getattr(settings, "topic_semantic_validation", True)) and bool(settings.openai_api_key)


def _cache_key(question: str) -> str:
    normalized = " ".join(str(question or "").split()).casefold()
    return f"{CACHE_PROVIDER}:{SEMANTIC_VALIDATOR_VERSION}:{hashlib.sha1(normalized.encode()).hexdigest()}"


def _signal(judgement: dict[str, Any], *, status: str) -> Signal:
    dims = {name: round(max(0, min(10, int(judgement.get(name, 0)))) / 10, 2) for name in DIMENSIONS}
    issues = sorted({str(issue) for issue in judgement.get("issues") or [] if str(issue) in ISSUES})
    return Signal(
        round(sum(dims.values()) / len(dims), 4),
        "medium",
        {"status": status, "dimensions": dims, "issues": issues, "reason": str(judgement.get("reason") or "")[:200],
         "validator_version": SEMANTIC_VALIDATOR_VERSION},
        ["semantic_validator"],
    )


def pending() -> Signal:
    """Not validated yet: deterministic gates only; never served in this state."""
    return Signal.unavailable("pending_validation", status="pending", validator_version=SEMANTIC_VALIDATOR_VERSION)


def unavailable(reason: str, *, status: str = "unavailable") -> Signal:
    """No semantic judgement: scoring applies the strict local acceptance rules."""
    return Signal.unavailable(reason, status=status, validator_version=SEMANTIC_VALIDATOR_VERSION)


@dataclass
class ValidationOutcome:
    signals: dict[str, Signal]
    requests: int
    cached: int
    error: str | None = None


def validate(
    db: Session, settings: Settings, questions: dict[str, str], *, requests_left: int, now: datetime,
) -> ValidationOutcome:
    """Validate ``{candidate_id: question}``: cache first, then ONE batched request of <= 20 if allowed."""
    signals: dict[str, Signal] = {}
    cached = 0
    missing: dict[str, str] = {}
    for candidate_id, question in questions.items():
        entry = db.get(TopicSourceCache, _cache_key(question))
        expires = entry.expires_at if entry is not None else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if entry is not None and expires is not None and expires > now:
            signals[candidate_id] = _signal(entry.payload, status="cached")
            cached += 1
        else:
            missing[candidate_id] = question
    if not missing:
        return ValidationOutcome(signals, 0, cached)
    if not semantic_enabled(settings):
        signals.update({candidate_id: unavailable("semantic_validator_unavailable") for candidate_id in missing})
        return ValidationOutcome(signals, 0, cached)
    if requests_left <= 0:
        signals.update({candidate_id: unavailable("ai_budget_exhausted", status="not_validated") for candidate_id in missing})
        return ValidationOutcome(signals, 0, cached)
    batch = list(missing.items())[:MAX_VALIDATION_BATCH]
    for candidate_id, _question in list(missing.items())[MAX_VALIDATION_BATCH:]:
        signals[candidate_id] = unavailable("batch_limit", status="not_validated")
    db.commit()  # no open transaction while waiting on OpenAI
    try:
        response = SEMANTIC_CLIENT_FACTORY(api_key=settings.openai_api_key).responses.parse(
            model=settings.openai_worker_model,
            instructions=VALIDATOR_INSTRUCTIONS,
            input=json.dumps({"questions": [{"id": f"q{index}", "question": question} for index, (_cid, question) in enumerate(batch)]}, ensure_ascii=False),
            text_format=AISemanticBatch,
            max_output_tokens=4000,
            store=False,
        )
        parsed = response.output_parsed
        if not isinstance(parsed, AISemanticBatch):
            raise TypeError("no parsed semantic batch")
    except (OpenAIError, ValidationError, ValueError, TypeError) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        signals.update({candidate_id: unavailable("semantic_validator_failed", status="failed") for candidate_id, _q in batch})
        return ValidationOutcome(signals, 1, cached, error)
    by_id = {item.id: item.model_dump() for item in parsed.items}
    for index, (candidate_id, question) in enumerate(batch):
        judgement = by_id.get(f"q{index}")
        if judgement is None:
            signals[candidate_id] = unavailable("not_returned", status="failed")
            continue
        signals[candidate_id] = _signal(judgement, status="validated")
        key = _cache_key(question)
        entry = db.get(TopicSourceCache, key) or TopicSourceCache(key=key, provider=CACHE_PROVIDER)
        entry.payload = {name: judgement[name] for name in (*DIMENSIONS, "issues", "reason")}
        entry.fetched_at = now
        entry.expires_at = now + CACHE_TTL
        db.add(entry)
    db.commit()
    return ValidationOutcome(signals, 1, cached)
