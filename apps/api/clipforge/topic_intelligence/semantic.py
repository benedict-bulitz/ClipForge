"""Semantic topic curation (semantic-curator-v2): question creation + validation in ONE call.

Deterministic rules verify a question's FORM; they cannot judge meaning, and
they cannot honestly turn a statement headline into a question.  With an
OpenAI key configured - independent of ClipForge's director AI mode - one
batched worker-model request per <= 20 raw topics:

    raw topic + evidence -> usable? -> grounded German question -> judged on
    self_contained_clarity, clear_factual_payoff, universal_12plus_relevance,
    prior_knowledge_free, natural_spoken_german, knowledge_short_fit (0-10)
    + short-worthiness: curiosity_strength, payoff_specificity, reveal_potential,
    concreteness, single_question_focus (0-10)   [v2]
    + issue codes

It only supplies signals (the question and the ``semantic`` signal); accept /
reject stays in ``scoring``.  Results are cached per topic evidence + curator
version + model, so the same topic is never paid for twice.  Nothing here runs
during video generation.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from openai import APITimeoutError, OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from ..ai import _reasoning_options
from ..config import Settings
from ..models import TopicSourceCache
from . import runtime
from .candidate import Signal, TopicGroup
from .text import compact, extract_question

SEMANTIC_CURATOR_VERSION = "semantic-curator-v3"
SEMANTIC_CLIENT_FACTORY: Any = OpenAI
# Topics per curator request.  semantic-curator-v2 asks 14 ratings + 14 issue codes per topic
# (v1: 9 + 8): ~3,000 visible output tokens per 20 topics plus hidden reasoning ran past the
# 60 s request timeout on the real Mac.  10 topics keep a request well inside it; after a
# timeout the next requests halve again (never below MIN_CURATION_BATCH).
MAX_CURATION_BATCH = 10
MIN_CURATION_BATCH = 4
CACHE_PROVIDER = "semantic_curator"
CACHE_TTL = timedelta(days=14)

DIMENSIONS = (
    "self_contained_clarity",
    "clear_factual_payoff",
    "universal_12plus_relevance",
    "prior_knowledge_free",
    "natural_spoken_german",
    "knowledge_short_fit",
)
# v2: is it a STRONG short, not merely a clear, factual, broad one?
SHORT_DIMENSIONS = (
    "curiosity_strength",
    "payoff_specificity",
    "reveal_potential",
    "concreteness",
    "single_question_focus",
)
ISSUES = (
    "unexplained_metaphor",
    "unclear_payoff",
    "rhetorical_or_opinion",
    "niche_context_required",
    "too_narrow_audience",
    "demographic_subgroup_only",
    "unnatural_or_headline_german",
    "unsupported_premise",
    # v2 short-worthiness (scoring decides which reject and which only lower the rank)
    "multi_part_question",
    "list_answer",
    "abstract_or_survey",
    "generic_advice",
    "broad_overview",
    "no_clear_reveal",
    # v3 (Topic Intelligence V2): hard eligibility the deterministic rules cannot see
    "trivial_answer",
    "speculation_dependent",
    "misleading_premise",
    # Only meaningful while the news is fresh: scoring rejects it without fresh timely evidence.
    "current_event_only",
)

CURATOR_INSTRUCTIONS = (
    "You curate default topic suggestions for ClipForge: German knowledge shorts (15-40 s) for a broad German-speaking "
    "audience aged about 12 and older - general curiosity, NOT children's content. For every raw topic you get its source "
    "evidence (headlines, article snippets) and sometimes a local_question already extracted from a title. STEP 1: decide "
    "whether an honest, broadly interesting knowledge question exists. STEP 2: if so, write ONE natural spoken German "
    "question (at most 18 words, ending with '?'). You may reframe a headline or statement into a stronger question ONLY "
    "when the evidence supports its premise - e.g. a headline about a blood test for breast-cancer screening may become "
    "'Kann Brustkrebs künftig mit einem einfachen Bluttest erkannt werden?'. Never invent mechanisms, numbers, causal "
    "claims or contradictions the evidence does not contain. Lead with the universal phenomenon, not with a niche name, "
    "product, brand, event or study; the specific case may appear later in the video. The question must make complete "
    "sense WITHOUT the headline: no metaphor or teaser wording, no 'das/dahinter' without a referent, no rhetorical or "
    "opinion question, no headline fragment, no embedded answer. STEP 3: rate the FINAL question 0-10: "
    "self_contained_clarity, clear_factual_payoff (it is obvious which concrete, verifiable knowledge the video delivers), "
    "universal_12plus_relevance (would an average viewer care - NOT the same as understandable: a question limited to one "
    "product, brand, demographic subgroup, niche hobby or specialist field scores low even if clear), prior_knowledge_free, "
    "natural_spoken_german, knowledge_short_fit (a factual, satisfying short; not opinion, advice for one product, or one "
    "survey's result), curiosity_gap, visual_potential (real footage can show it), dach_relevance. SHORT-WORTHINESS: clear, "
    "factual and broad is not enough - it must make a STRONG short. Ask ONE core question (never 'X, und Y?' or two things "
    "at once; if the evidence holds two, pick the stronger single one). Prefer a concrete mechanism, a surprising limit, an "
    "apparent contradiction, a hidden cause or a concrete consequence over broad overviews, generic advice or guidance, "
    "survey results, abstract social measurements, and 'Welche Faktoren/Gründe/Tipps ...' questions whose answer is a "
    "list. No domain is excluded: a health, fitness or social topic wins when it has a concrete, surprising reveal (weak: "
    "'Welche Faktoren beeinflussen den Schlaf, und was hilft am meisten?', 'Wie zufrieden sind die Deutschen mit ihrer "
    "Arbeit?'; strong: 'Warum kann man sich nicht selbst kitzeln?'). Rate the FINAL question 0-10 also on: "
    "curiosity_strength (would a scrolling viewer stop to hear the answer - an immediate hook), payoff_specificity (one "
    "concrete, specific answer - not 'it depends', not a list of factors), reveal_potential (the answer is surprising or "
    "non-obvious), concreteness (a concrete premise, not an abstract concept or measurement), single_question_focus (10 = "
    "exactly one core question). Also set grounded (true only if the evidence supports every premise of the question). "
    "Issue codes, allowed values only: unexplained_metaphor, unclear_payoff, rhetorical_or_opinion, niche_context_required, "
    "too_narrow_audience, demographic_subgroup_only, unnatural_or_headline_german, unsupported_premise, "
    "multi_part_question, list_answer, abstract_or_survey, generic_advice, broad_overview, no_clear_reveal, "
    "trivial_answer (the answer is obvious or one boring fact), speculation_dependent (no settled answer exists yet), "
    "misleading_premise (the premise is a myth or false - a myth may only be asked as 'Stimmt es, dass ...?'), "
    "current_event_only (the question only makes sense while a specific news story is current). "
    "TOPIC VS QUESTION (v3): a topic is a subject; the question is what the video answers. Some topics come with "
    "candidate_questions (editorial evergreen seeds): choose the strongest one, improve its wording if needed, or write a "
    "better single question about the same subject - never keep a weak question just because it was supplied. For such "
    "evergreen topics the evidence is the subject itself: set grounded=true only if the premise is established, "
    "textbook-level knowledge (not a myth, not an open research question). Fit the question form to the actual mechanism - "
    "do not force 'Warum ...?': 'Wie funktioniert ...?', 'Was würde passieren, wenn ...?', 'Stimmt es, dass ...?', "
    "'Woher wissen wir ...?', 'Was macht ... anders?' are all fine. Curiosity without a concrete payoff is clickbait; a "
    "payoff without curiosity is a boring short - both must be high for a strong suggestion. Also return subject (1-3 "
    "German words: the thing the video is about, e.g. 'Mars') and aspect (1-4 words: what is asked about it, e.g. 'rote "
    "Farbe'); two questions with the same subject and aspect are the same video. Optionally rate knowledge_value 0-10 (will "
    "the viewer genuinely learn a mechanism or cause, not just hear one fact). Be strict; when in doubt, score low or set "
    "usable=false. reason: at most 12 words. Return the supplied id unchanged. Structured output only."
)


class AICuratedTopic(BaseModel):
    id: str
    usable: bool
    question: str = Field(default="", max_length=240)
    niche: str = "unknown"
    self_contained_clarity: int = Field(default=0, ge=0, le=10)
    clear_factual_payoff: int = Field(default=0, ge=0, le=10)
    universal_12plus_relevance: int = Field(default=0, ge=0, le=10)
    prior_knowledge_free: int = Field(default=0, ge=0, le=10)
    natural_spoken_german: int = Field(default=0, ge=0, le=10)
    knowledge_short_fit: int = Field(default=0, ge=0, le=10)
    curiosity_strength: int = Field(default=5, ge=0, le=10)
    payoff_specificity: int = Field(default=5, ge=0, le=10)
    reveal_potential: int = Field(default=5, ge=0, le=10)
    concreteness: int = Field(default=5, ge=0, le=10)
    single_question_focus: int = Field(default=5, ge=0, le=10)
    curiosity_gap: int = Field(default=5, ge=0, le=10)
    visual_potential: int = Field(default=5, ge=0, le=10)
    dach_relevance: int = Field(default=5, ge=0, le=10)
    grounded: bool = False
    issues: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=200)
    # v3: LLM-assisted semantic dedupe and knowledge value (optional; derived when absent)
    subject: str = Field(default="", max_length=60)
    aspect: str = Field(default="", max_length=60)
    knowledge_value: int | None = Field(default=None, ge=0, le=10)


class AICuratedBatch(BaseModel):
    items: list[AICuratedTopic]


def semantic_enabled(settings: Settings) -> bool:
    """Topic curation needs the OpenAI key only - never CLIPFORGE_AI_MODE=openai."""
    return bool(getattr(settings, "topic_semantic_validation", True)) and bool(settings.openai_api_key)


def pending() -> Signal:
    """Not curated yet: deterministic gates only; never served in this state."""
    return Signal.unavailable("pending_curation", status="pending", curator_version=SEMANTIC_CURATOR_VERSION)


def unavailable(reason: str, *, status: str = "unavailable") -> Signal:
    """No semantic judgement: scoring applies the strict local acceptance rules."""
    return Signal.unavailable(reason, status=status, curator_version=SEMANTIC_CURATOR_VERSION)


def curated_signal(judgement: dict[str, Any], *, status: str) -> Signal:
    dims = {name: round(max(0, min(10, int(judgement.get(name, 0)))) / 10, 2) for name in DIMENSIONS}
    short = {
        name: round(max(0, min(10, int(judgement.get(name, 5)))) / 10, 2)
        for name in (*SHORT_DIMENSIONS, "knowledge_short_fit", "visual_potential")
    }
    issues = {str(issue) for issue in judgement.get("issues") or [] if str(issue) in ISSUES}
    if not judgement.get("grounded", False) and judgement.get("usable"):
        issues.add("unsupported_premise")
    extra = {name: round(max(0, min(10, int(judgement.get(name, 5)))) / 10, 2) for name in ("curiosity_gap", "visual_potential", "dach_relevance")}
    if judgement.get("knowledge_value") is not None:
        extra["knowledge_value"] = round(max(0, min(10, int(judgement["knowledge_value"]))) / 10, 2)
    return Signal(
        round(sum(dims.values()) / len(dims), 4),
        "medium",
        {"status": status, "dimensions": dims, "short_dimensions": short, "extra_dimensions": extra, "issues": sorted(issues),
         "reason": str(judgement.get("reason") or "")[:200], "grounded": bool(judgement.get("grounded", False)),
         "subject": str(judgement.get("subject") or "")[:60], "aspect": str(judgement.get("aspect") or "")[:60],
         "curator_version": SEMANTIC_CURATOR_VERSION},
        ["semantic_curator"],
    )


def evidence(group: TopicGroup) -> list[dict[str, str]]:
    return [
        {"source": item.source, "kind": item.kind, "title": compact(item.title, 180), "text": compact(item.description, 320)}
        for item in group.evidence_sightings[:3]  # the subject's own sightings, not loosely related ones
    ]


def local_question(group: TopicGroup, seeds: list[str] | None = None) -> str | None:
    if seeds:
        return seeds[0]
    for item in group.sightings:
        question, _notes = extract_question(item.title)
        if question:
            return question
    return None


def _cache_key(group: TopicGroup, model: str, seeds: list[str] | None = None) -> str:
    identity = json.dumps([group.key, sorted(item.title for item in group.sightings), list(seeds or [])], ensure_ascii=False)
    digest = hashlib.sha1(f"{model}|{identity}".encode()).hexdigest()
    return f"{CACHE_PROVIDER}:{SEMANTIC_CURATOR_VERSION}:{digest}"


@dataclass
class CurationOutcome:
    judgements: dict[str, dict[str, Any]]  # group key -> raw judgement (usable, question, dims, ...)
    statuses: dict[str, str]  # group key -> curated | cached | failed | not_curated
    requests: int = 0
    cached: int = 0
    errors: list[str] = field(default_factory=list)
    timed_out: bool = False
    # One entry per request: size, seconds, status, token usage, error (diagnostics only).
    batches: list[dict[str, Any]] = field(default_factory=list)


def _usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }


def curate(
    db: Session, settings: Settings, groups: list[TopicGroup], *, requests_left: int, now: datetime,
    batch_size: int = MAX_CURATION_BATCH, seeds: Any = None,
) -> CurationOutcome:
    """Cache first, then ONE request for the first ``batch_size`` uncached topics if the budget allows.

    A failed request (timeout, API or parse error) marks only its own topics ``failed`` -
    not evaluated, never judged low quality - and nothing is cached for them.
    ``seeds``: group -> the evergreen candidate questions still worth asking (not used before).
    """
    model = settings.openai_worker_model
    outcome = CurationOutcome({}, {})
    missing: list[TopicGroup] = []

    def seeds_for(group: TopicGroup) -> list[str]:
        return list(seeds(group)) if seeds is not None else list(group.seed_questions)

    for group in groups:
        entry = db.get(TopicSourceCache, _cache_key(group, model, seeds_for(group)))
        expires = entry.expires_at if entry is not None else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if entry is not None and expires is not None and expires > now:
            outcome.judgements[group.key] = dict(entry.payload)
            outcome.statuses[group.key] = "cached"
            outcome.cached += 1
        else:
            missing.append(group)
    if not missing:
        return outcome
    if requests_left <= 0 or not semantic_enabled(settings):
        outcome.statuses.update({group.key: "not_curated" for group in missing})
        return outcome
    size = max(1, int(batch_size))
    batch, rest = missing[:size], missing[size:]
    outcome.statuses.update({group.key: "not_curated" for group in rest})
    request = []
    for index, group in enumerate(batch):
        candidates = seeds_for(group)
        item: dict[str, Any] = {"id": f"t{index}", "topic": group.title, "evidence": evidence(group)}
        if (question := local_question(group, candidates)):
            item["local_question"] = question
        if candidates:
            item["candidate_questions"] = candidates[:3]
        request.append(item)
    db.commit()  # no open transaction while waiting on OpenAI
    outcome.requests = 1
    runtime.FLIGHT.stage("curator_request", "openai_curator")

    def request_batch() -> Any:
        # Bounded twice: the client's own timeout (no retries - the budget is 3 requests),
        # and a wall-clock guard in case the connection hangs anyway.
        client = SEMANTIC_CLIENT_FACTORY(api_key=settings.openai_api_key, timeout=runtime.CURATOR_TIMEOUT_SECONDS, max_retries=0)
        return client.responses.parse(
            model=model,
            instructions=CURATOR_INSTRUCTIONS,
            input=json.dumps({"market": {"language": "de", "region": "DE", "broader": "DACH"}, "topics": request}, ensure_ascii=False),
            text_format=AICuratedBatch,
            max_output_tokens=8000,
            store=False,
            # Like every other ClipForge structured call: low reasoning effort for a reasoning model
            # (default effort spent the whole request budget thinking about 20 x 14 ratings).
            **_reasoning_options(model),
        )

    started = time.monotonic()
    record: dict[str, Any] = {"size": len(batch), "topics": [group.title for group in batch][:10]}
    outcome.batches.append(record)
    try:
        response = runtime.call_with_timeout(request_batch, runtime.CURATOR_TIMEOUT_SECONDS + runtime.CURATOR_GRACE_SECONDS, name="openai_curator")
        record.update(_usage(response))
        parsed = response.output_parsed
        if not isinstance(parsed, AICuratedBatch):
            raise TypeError("no parsed curation batch")
    except (OpenAIError, ValidationError, ValueError, TypeError, runtime.CallTimeout) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        outcome.errors.append(error)
        outcome.timed_out = isinstance(exc, APITimeoutError | runtime.CallTimeout)
        record.update(status="timeout" if outcome.timed_out else "failed", error=error, seconds=round(time.monotonic() - started, 2))
        outcome.statuses.update({group.key: "failed" for group in batch})
        return outcome
    record.update(status="ok", seconds=round(time.monotonic() - started, 2))
    by_id = {item.id: item.model_dump() for item in parsed.items}
    for index, group in enumerate(batch):
        judgement = by_id.get(f"t{index}")
        if judgement is None:
            outcome.statuses[group.key] = "failed"
            continue
        outcome.judgements[group.key] = judgement
        outcome.statuses[group.key] = "curated"
        key = _cache_key(group, model, seeds_for(group))
        entry = db.get(TopicSourceCache, key) or TopicSourceCache(key=key, provider=CACHE_PROVIDER)
        entry.payload = judgement
        entry.fetched_at = now
        entry.expires_at = now + CACHE_TTL
        db.add(entry)
    db.commit()
    return outcome
