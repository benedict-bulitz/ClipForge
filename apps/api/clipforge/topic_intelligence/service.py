"""Topic Intelligence orchestration: discovery pool, proposals and the handoff.

* ``next_topic``: reuse the fresh candidate pool (``topic_pool_ttl_minutes``)
  or run one discovery refresh (single-flight), then propose the best unused
  candidate.  Asking again returns the same proposal until it is skipped or used.
* ``skip_topic`` ("Try another"): remember the skip, propose the next
  candidate; an exhausted pool triggers one refresh.
* ``suggestions``: the Home chips - a batch of ranked candidates excluding
  what the client already shows; picked/replaced chips are remembered.
* ``warm_pool_in_background``: discovery at startup, beside the app.
* ``resolve_topic_provenance`` / ``mark_topic_used``: the only coupling to
  generation.  A confirmed topic goes through the existing
  ``POST /api/generation-jobs`` like a typed question, with provenance attached.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import TopicCandidateRecord, TopicDiscoveryRun, TopicSourceCache
from ..schemas import ProjectCreate
from ..security.secrets import SecretStore
from ..youtube.connection import access_token, active_connection
from ..youtube.provider import YouTubeProvider, has_capability
from . import history as history_module
from . import runtime, scoring, semantic
from .cache import CallMeter, prune_expired
from .candidate import RawTopic, Signal, TopicCandidate, TopicGroup, candidate_id_for
from .evergreen import EvergreenCatalogSource
from .evidence import CandidateEvidenceProvider, apply_evidence
from .scoring import (
    RankedItem,
    curation_priority,
    diversify,
    explain,
    rank,
    rank_key,
    resolve_weights,
    score_candidate,
)
from .signals import (
    LOCAL_SHORT_BASE,
    accessibility,
    assessed,
    broad_appeal,
    channel_fit,
    competition_estimate,
    curiosity,
    knowledge_value,
    merge_demand,
    merge_trend,
    opportunity,
    payoff,
    question_form,
    related_video_demand,
    short_worthiness,
    suitability,
)
from .sources import (
    BraveNewsSource,
    DiscoveryContext,
    SourceReport,
    SourceSkipped,
    TopicSource,
    WikipediaPageviewsSource,
    YouTubeCompetitionProbe,
    YouTubeTrendingSource,
)
from .text import (
    BROAD_APPEAL_PRIORS,
    NICHE_PRIORS,
    POOR_FIT_NICHES,
    classify_niche,
    compact,
    content_tokens,
    de_shout,
    extract_question,
    fold,
    has_universal_subject,
    prior_knowledge_flags,
    question_equivalence,
    question_flags,
    question_mechanism,
    short_shape_flags,
    similarity,
    subject_equivalent,
    topic_key,
    topic_obscurity_flags,
)
from .transform import (
    MAX_BATCH,
    TRANSFORMATION_VERSION,
    Transformed,
    curated_transform,
    deterministic_transform,
    evergreen_seed,
)

SKIP_MEMORY = timedelta(hours=24)
# A used question never returns (candidate id + novelty); its SUBJECT may return with a
# different question after this cool-down - one video must not block a whole domain.
USED_TOPIC_COOLDOWN = timedelta(days=21)
# Curation work order: at most this share of one curator batch may be evergreen while live
# (trend/news/YouTube) topics are still waiting - and vice versa (quality order otherwise).
SOURCE_MIX_SHARE = 0.6
# Fair bounded evaluation (real Mac: 71 raw groups, evaluation stopped at 20 the moment 9 were
# accepted - the best of the FIRST acceptable batch, not of the pool).  "9 accepted" now only
# means "enough supply"; evaluation also needs coverage and no competitive candidate left.
MIN_EVALUATION_COVERAGE = 20  # topics evaluated before "enough supply" may stop evaluation
MIN_PER_SOURCE = 2  # each discovery source's best topics are in the shortlist (and evaluated)
NICHE_REPEAT_PENALTY = 0.03  # per topic of the same niche already shortlisted (no 10 food topics first) ...
NICHE_REPEAT_MAX = 3  # ... counted up to 3 repeats: diversity reorders, it never buries a strong topic under garbage
# Representation never overrides clear evidence: a source's seed needs a viable pre-score and no hard penalty.
SEED_MIN_PRIORITY = 0.35
SEED_BLOCKING_PENALTIES = frozenset({"poor_fit_niche", "duplicate_of_previous_topic", "weak_question_shape"})
COMPETITIVE_MARGIN = 0.05  # a remaining topic within this of the current top 3's cheap pre-score is still evaluated
COMPETITIVE_TOP = 3
RETENTION = timedelta(days=14)
PREFILTER_FLAGS = frozenset({"person", "tragedy", "disambiguation"})
# Obvious garbage never reaches the curator (calendar pages have no story of their own).
PREFILTER_TOPIC_FLAGS = frozenset({"date_page"})
OBSCURE_ENTITY_FLAGS = frozenset({"identifier", "acronym", "compound_proper_name", "foreign_proper_name", "isolated_event", "date_page"})
UNAVAILABLE_MESSAGE = "Topic discovery is temporarily unavailable."
EXHAUSTED_MESSAGE = "No further topic candidates right now. Try again later or enter your own question."
# Single-flight discovery with an owner token, stage tracking and a hard limit (never held forever).
_FLIGHT = runtime.FLIGHT
logger = logging.getLogger(__name__)
DISCOVERY_RETRY_SECONDS = 3
# A pool that is short only because of the quality floor is not re-discovered on
# every chip refill (each refresh may cost a question-rewriting call).
MIN_REFRESH_INTERVAL = timedelta(minutes=10)
BROADENING_REPORT = "broadening_pass"
EVALUATION_REPORT = "candidate_evaluation"
# Raw-pool backfill: keep evaluating already-discovered topics (no new provider
# calls) until this many candidates clear every gate, within a hard budget per
# pool (shared with its broadening pass).  With the curator only JUDGED topics count:
# 3 requests x 10 topics = the best ~30 raw topics (plus cached judgements), not 60 badly.
TARGET_ACCEPTED = 9
EVALUATION_BUDGET = 60
# All AI requests of one pool (combined curation), shared with its broadening pass - including
# the one retry of a failed batch.  Each request carries ``topic_curator_batch_size`` topics.
AI_REQUEST_BUDGET = 3
# A widening pass (once per pool) always gets at least this much, even when the first pass
# spent the pool's budget: the next evergreen window and the next raw topics.  Bounded total
# per pool chain: AI_REQUEST_BUDGET + WIDEN_AI_REQUESTS requests, EVALUATION_BUDGET + WIDEN_EVALUATIONS topics.
WIDEN_AI_REQUESTS = 1
WIDEN_EVALUATIONS = 20
SEMANTIC_REPORT = "semantic_validation"
# Last startup warm-up (idle | running | done | failed), for diagnostics.
WARMUP: dict[str, Any] = {"state": "idle", "started_at": None, "finished_at": None, "result": None, "error": None}
DISCOVERY_FAILED_MESSAGE = "Themenvorschläge konnten nicht geladen werden."


def _abandoned(owner: str, message: str) -> None:
    """The flight's hard limit abandoned a discovery: a stuck warm-up is failed, not "running" forever."""
    logger.warning("Topic Intelligence %s", message)
    if owner == "warmup" and WARMUP["state"] == "running":
        WARMUP.update(state="failed", error=message, finished_at=_now().isoformat())


_FLIGHT.on_abandon = _abandoned


class TopicHandoffError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass
class DiscoveryDeps:
    sources: list[TopicSource]
    probe: YouTubeCompetitionProbe
    extra_reports: list[SourceReport] = field(default_factory=list)
    # Future analytics evidence (see ``evidence.py``); empty in cold start.
    evidence_providers: list[CandidateEvidenceProvider] = field(default_factory=list)


def default_deps(db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider) -> DiscoveryDeps:
    """Configured providers only.  The YouTube token is fetched lazily, on a cache miss."""
    connection = active_connection(db)
    token = None
    if connection is not None and has_capability(connection.granted_scopes or [], "read"):
        def token() -> str:
            return access_token(db, settings, store, provider, capability="read")[1]
    youtube_provider = provider if token is not None else None
    return DiscoveryDeps(
        sources=[
            WikipediaPageviewsSource(),
            YouTubeTrendingSource(youtube_provider, token),
            BraveNewsSource(settings.brave_search_api_key),
            # V2: evergreen subjects - works without any key, evidence from Wikipedia pageviews.
            EvergreenCatalogSource(),
        ],
        probe=YouTubeCompetitionProbe(youtube_provider, token),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _collect(ctx: DiscoveryContext, sources: list[TopicSource]) -> tuple[list[RawTopic], list[SourceReport]]:
    topics: list[RawTopic] = []
    reports: list[SourceReport] = []
    for source in sources:
        _FLIGHT.stage("provider", source.name)
        if _FLIGHT.timed_out(source.name):
            # Already waited out once in this refresh (e.g. before a broadening pass): not again.
            reports.append(SourceReport(source.name, "timeout", error="skipped: timed out earlier in this refresh"))
            continue
        try:
            result = source.discover(ctx)
        except SourceSkipped as exc:
            reports.append(SourceReport(source.name, "skipped", error=str(exc)))
            continue
        except runtime.CallTimeout as exc:
            # One hung provider never freezes discovery: mark it and continue with the others.
            ctx.db.rollback()
            reports.append(SourceReport(source.name, "timeout", error=str(exc)[:200]))
            continue
        except Exception as exc:  # noqa: BLE001 - one failing source never breaks discovery
            ctx.db.rollback()
            reports.append(SourceReport(source.name, "failed", error=f"{type(exc).__name__}: {str(exc)[:160]}"))
            continue
        topics.extend(result.topics)
        if result.report is not None:
            reports.append(result.report)
    return topics, reports


def same_subject(left: RawTopic, right: RawTopic) -> bool:
    """DISCOVERY grouping: do two sightings belong in one topic group?

    * both carry a canonical identity (Wikipedia article): only the same article;
    * otherwise strictly equivalent subject titles (same words or inflections - never one word
      contained in a longer compound: "Honig" != "Honigfrauen");
    * long titles (headlines, questions) may also group as paraphrases of one question.
    Grouping is NOT evidence attribution: only ``TopicGroup.evidence_sightings`` (the anchor's own
    entity) may supply trend, demand or outlier evidence.
    """
    if left.entity and right.entity:
        return left.entity == right.entity
    if subject_equivalent(left.title, right.title):
        return True  # (not the normalized key: it drops two-letter words, so "Apple" == "Apple TV+")
    long_titles = min(len(content_tokens(left.title)), len(content_tokens(right.title))) >= 3
    return long_titles and question_equivalence(left.title, right.title) >= history_module.DUPLICATE_THRESHOLD


def group_topics(topics: list[RawTopic]) -> list[TopicGroup]:
    """Merge sightings of the same subject (see ``same_subject``)."""
    groups: list[TopicGroup] = []
    for topic in topics:
        for group in groups:
            if any(same_subject(item, topic) for item in group.sightings):
                group.sightings.append(topic)
                break
        else:
            groups.append(TopicGroup(topic.key, [topic]))
    return groups


def _preliminary(group: TopicGroup) -> float:
    trend = merge_trend([item.trend for item in group.evidence_sightings if item.trend is not None])
    outliers = [item.outlier.value or 0.0 for item in group.evidence_sightings if item.outlier is not None and item.outlier.available]
    # Titles that already carry a question are cheap and likely to transform: try them earlier.
    question_like = any(extract_question(item.title)[0] for item in group.sightings)
    return round((trend.value or 0.0) + 0.5 * (max(outliers) if outliers else 0.0) + 0.1 * len(group.sources) + (0.3 if question_like else 0.0), 4)


def _curation_priority(group: TopicGroup, history: list[history_module.HistoryItem], now: datetime) -> tuple[float, dict[str, Any]]:
    """Which raw topics deserve the bounded AI curation first - cheap evidence only (see ``scoring``)."""
    trend = merge_trend([item.trend for item in group.evidence_sightings if item.trend is not None])
    demand = merge_demand([item.demand for item in group.evidence_sightings])
    outliers = [item.outlier.value or 0.0 for item in group.evidence_sightings if item.outlier is not None and item.outlier.available]
    description = group.description()
    niche, _strength = classify_niche(group.title, description)
    question = evergreen_seed(group, _seen_before(history, group, niche, now)) if group.evergreen else None
    question = question or next((q for q in (extract_question(item.title)[0] for item in group.sightings) if q), None)
    novelty = history_module.novelty_signal(question or group.title, group.title, niche, history, now=now)
    text = question or group.title
    features = {
        # Evidence only: momentum, sustained demand level, channel-relative outliers (never raw views).
        "demand": max(trend.value or 0.0, demand.value or 0.0) + 0.5 * (max(outliers) if outliers else 0.0),
        # A statement can still become a question - the curator decides; it just starts lower.
        "question_strength": LOCAL_SHORT_BASE.get(question_mechanism(question), 0.45) if question else 0.45,
        "universal": 1.0 if has_universal_subject(text) else 0.5,
        "evidence": 1.0 if any(item.kind in {"article", "evergreen"} or len(item.description or "") >= 60 for item in group.sightings) else 0.4,
        "mass_appeal": BROAD_APPEAL_PRIORS.get(niche, BROAD_APPEAL_PRIORS["unknown"]),
        "corroboration": 1.0 if len(group.sources) >= 2 else 0.0,
        "novelty": novelty.value if novelty.available and novelty.value is not None else 1.0,
    }
    penalties = {
        "prior_knowledge": len(prior_knowledge_flags(text)),
        "obscure_entity": len(topic_obscurity_flags(group.title, description) & OBSCURE_ENTITY_FLAGS),
        "poor_fit_niche": niche in POOR_FIT_NICHES or bool(group.flags & {"politics", "entertainment_or_sport"}),
        "weak_question_shape": bool(question and short_shape_flags(question)),
        "duplicate_of_previous_topic": history_module.is_duplicate(novelty),
    }
    value, applied = curation_priority(features, penalties)
    return value, {"features": {name: round(float(v), 3) for name, v in features.items()}, "penalties": applied, "niche": niche,
                   "origin": "evergreen" if group.evergreen else "live", "question": question}


def _seen_before(history: list[history_module.HistoryItem], group: TopicGroup, niche: str, now: datetime) -> Any:
    """Predicate: this question repeats something ClipForge already made (novelty duplicate)."""
    def seen(question: str) -> bool:
        return history_module.is_duplicate(history_module.novelty_signal(question, group.title, niche, history, now=now))
    return seen


def representative_order(
    groups: list[TopicGroup], priorities: dict[str, tuple[float, dict[str, Any]]], *, slots: int,
) -> tuple[list[TopicGroup], dict[str, Any]]:
    """The work order for the bounded (expensive) evaluation: a representative shortlist first.

    1. each discovery source's ``MIN_PER_SOURCE`` best topics (no source is never looked at);
    2. then by cheap pre-score, minus ``NICHE_REPEAT_PENALTY`` per shortlisted topic of the same
       niche, with evergreen / live each capped at ``SOURCE_MIX_SHARE`` while the other remains;
    3. a topic whose cheap question repeats an earlier one is dropped from the work order (it is
       reported as ``deferred_duplicates``) - a duplicate cluster never spends the budget twice.
    The rest follows in pre-score order.  Raw views are not a pre-score input.
    Returns (work order, report); the report also carries the dropped duplicate groups.
    """
    def priority(group: TopicGroup) -> float:
        return priorities[group.key][0]

    def question(group: TopicGroup) -> str | None:
        return priorities[group.key][1].get("question")

    by_priority = sorted(groups, key=lambda group: (-priority(group), group.key))
    chosen: list[TopicGroup] = []
    chosen_ids: set[int] = set()
    deferred: list[TopicGroup] = []

    def repeats(group: TopicGroup) -> bool:
        text = question(group)
        return bool(text) and any(
            question(other) and question_equivalence(text, str(question(other))) >= history_module.DUPLICATE_THRESHOLD for other in chosen
        )

    def take(group: TopicGroup) -> None:
        chosen.append(group)
        chosen_ids.add(id(group))

    def viable_seed(group: TopicGroup) -> bool:
        return priority(group) >= SEED_MIN_PRIORITY and not set(priorities[group.key][1].get("penalties") or {}) & SEED_BLOCKING_PENALTIES

    def niche_of(group: TopicGroup) -> str:
        return str(priorities[group.key][1].get("niche"))

    # 1. Reserve shortlist places for each source's best viable topics (representation) ...
    reserved: list[TopicGroup] = []
    for source in sorted({source for group in groups for source in group.sources}):
        taken = sum(1 for group in reserved if source in group.sources)
        for group in by_priority:
            if taken >= MIN_PER_SOURCE or len(reserved) >= slots:
                break
            if source not in group.sources or group in reserved or not viable_seed(group):
                continue
            reserved.append(group)
            taken += 1
    reserved_ids = {id(group) for group in reserved}
    # 2. ... but ORDER the shortlist by adjusted pre-score: a source's seed is guaranteed a place
    # (and, by the stopping rule, an evaluation) without pushing stronger topics out of the first batch.
    pool = list(by_priority)
    niches: Counter[str] = Counter()
    classes: Counter[bool] = Counter()
    cap = max(1, round(slots * SOURCE_MIX_SHARE))
    open_slots = slots - len(reserved)
    while pool and len(chosen) < slots:
        both_classes_left = len({group.evergreen for group in pool}) == 2

        def adjusted(group: TopicGroup, both: bool = both_classes_left) -> float:
            over_cap = classes[group.evergreen] >= cap and both
            repeats_niche = 0 if niche_of(group) == "unknown" else min(NICHE_REPEAT_MAX, niches[niche_of(group)])
            return priority(group) - NICHE_REPEAT_PENALTY * repeats_niche - (1.0 if over_cap else 0.0)

        eligible = pool if open_slots > 0 else [group for group in pool if id(group) in reserved_ids]
        if not eligible:
            break
        best = max(eligible, key=lambda group: (adjusted(group), -by_priority.index(group)))
        pool.remove(best)
        if repeats(best):
            deferred.append(best)
            continue
        take(best)
        if id(best) not in reserved_ids:
            open_slots -= 1
        niches[niche_of(best)] += 1
        classes[best.evergreen] += 1
    rest: list[TopicGroup] = []
    for group in pool:
        if repeats(group) or any(
            question(group) and question(other) and question_equivalence(str(question(group)), str(question(other))) >= history_module.DUPLICATE_THRESHOLD
            for other in rest
        ):
            deferred.append(group)
        else:
            rest.append(group)
    report = {
        "shortlist": len(chosen),
        "slots": slots,
        "deferred_duplicates": [group.title for group in deferred][:10],
        "deferred_groups": deferred,
        "shortlist_sources": dict(Counter(source for group in chosen for source in group.sources)),
        "shortlist_origin": {"evergreen": classes[True], "live": classes[False]},
    }
    return chosen + rest, report


def _best_outlier(group: TopicGroup) -> Signal:
    available = [item.outlier for item in group.evidence_sightings if item.outlier is not None and item.outlier.available]
    if not available:
        reasons = [item.outlier.evidence.get("reason") for item in group.evidence_sightings if item.outlier is not None]
        return Signal.unavailable(next((reason for reason in reasons if reason), "no_video_evidence"))
    return max(available, key=lambda signal: (signal.value or 0.0, signal.evidence.get("sample_size") or 0))


def quality_signals(
    question: str,
    topic: str,
    description: str,
    niche: str,
    assessment: dict[str, float],
    method: str,
    notes: set[str] | None = None,
    semantic_signal: Signal | None = None,
) -> tuple[dict[str, Signal], dict[str, Any]]:
    """Mass-audience and short-worthiness features of one question (signals only; scoring decides their worth)."""
    # An entity name keeps its capitals (GICON is an acronym); a headline loses its shouting (FALSCH is emphasis).
    is_entity = extract_question(topic)[0] is None and len(topic.split()) <= 6
    topic_flags = topic_obscurity_flags(topic if is_entity else de_shout(topic), description)
    flags = question_flags(question, topic)
    mechanism = question_mechanism(question)
    prior_knowledge = prior_knowledge_flags(question, notes=notes)
    # The obscure entity may appear in the video as an example, but it must not lead the question.
    # Only an entity name counts here - a headline/video title is the question's own source text.
    entity_tokens = content_tokens(topic)
    if is_entity and topic_flags & OBSCURE_ENTITY_FLAGS and entity_tokens and similarity(entity_tokens, content_tokens(question)) >= 0.5:
        prior_knowledge.add("names_obscure_entity")
    universal = has_universal_subject(question)
    signals = {
        "broad_appeal": broad_appeal(
            niche, BROAD_APPEAL_PRIORS.get(niche, BROAD_APPEAL_PRIORS["unknown"]), assessment.get("broad_appeal"),
            method=method, universal_subject=universal, mechanism=mechanism, prior_knowledge=prior_knowledge,
        ),
        "accessibility": accessibility(
            flags, topic_flags, assessment.get("accessibility"), method=method,
            prior_knowledge=prior_knowledge, universal_subject=universal,
        ),
        "question_form": question_form(mechanism, flags),
        "short_worthiness": short_worthiness(mechanism, short_shape_flags(question), semantic_signal),
    }
    return signals, {
        "mechanism": mechanism,
        "topic_flags": sorted(topic_flags),
        "question_flags": sorted(flags),
        "prior_knowledge": sorted(prior_knowledge),
        "universal_subject": universal,
        "extraction_notes": sorted(notes or []),
    }


def build_candidate(
    group: TopicGroup,
    transformed: Transformed,
    *,
    history: list[history_module.HistoryItem],
    own_priors: dict[str, Signal],
    own_default: Signal,
    now: datetime,
    run_id: str,
) -> TopicCandidate:
    question = transformed.question
    niche = transformed.niche
    assessment = transformed.assessment
    method = transformed.method
    confidence = transformed.assessment_confidence
    has_article = any(item.kind in {"article", "evergreen"} for item in group.sightings)
    research_value = assessment.get("researchability")
    if research_value is not None and has_article:
        research_value = max(research_value, 0.8)
    signals = {
        # Evidence only from sightings of the SAME entity (strict attribution, see TopicGroup.evidence_sightings).
        "trend": merge_trend([item.trend for item in group.evidence_sightings if item.trend is not None]),
        "outlier": _best_outlier(group),
        "competition": Signal.unavailable("not_probed"),
        "novelty": history_module.novelty_signal(question or group.title, group.title, niche, history, now=now),
        "channel_fit": channel_fit(niche, NICHE_PRIORS.get(niche, NICHE_PRIORS["unknown"]), assessment.get("dach_relevance"), confidence=confidence, method=method),
        "suitability": suitability(assessment, confidence=confidence, method=method),
        "visual": assessed("visual", assessment.get("visual_potential"), confidence=confidence, method=method),
        "researchability": assessed(
            "researchability", research_value, confidence="medium" if has_article else confidence, method=method, encyclopedic_article=has_article,
        ),
        "own_performance": own_priors.get(niche, own_default),
        "semantic": transformed.semantic or semantic.pending(),
        # V2 evidence: sustained interest level (never raw views); opportunity needs a search probe.
        "demand": merge_demand([item.demand for item in group.evidence_sightings]),
        "opportunity": Signal.unavailable("not_probed"),
    }
    features, feature_evidence = quality_signals(
        question, group.title, group.description(), niche, assessment, method, transformed.notes, signals["semantic"],
    )
    signals.update(features)
    mechanism = str(feature_evidence["mechanism"])
    signals["curiosity"] = curiosity(mechanism, signals["semantic"])
    signals["payoff"] = payoff(mechanism, signals["semantic"])
    signals["knowledge_value"] = knowledge_value(signals["semantic"], has_article=has_article)
    sem_evidence = signals["semantic"].evidence if signals["semantic"].available else {}
    subject = str(sem_evidence.get("subject") or "") or group.title
    return TopicCandidate(
        candidate_id=candidate_id_for(topic_key(question) if question else f"raw:{group.key}"),
        topic=group.title,
        question=question,
        rationale="",
        # Every sighting stays visible (discovery); ``attributed`` says whether it may count as evidence.
        source_signals=[{**item.source_signal(), "attributed": any(item is other for other in group.evidence_sightings)} for item in group.sightings],
        discovered_at=now,
        language="de",
        region="DE",
        niche=niche,
        signals=signals,
        freshness_at=group.newest,
        provenance={
            "group_key": group.key,
            "sources": group.sources,
            "transformation": method,
            "angle": transformed.angle,
            "flags": transformed.flags,
            "question_issues": transformed.issues,
            "run_id": run_id,
            # V2: topic != question.  The subject (family) groups related questions for diversity;
            # subject + aspect identify "the same video" for semantic dedupe.
            "origin": "evergreen" if group.evergreen else "live",
            "topic_family": topic_family(subject),
            "aspect": str(sem_evidence.get("aspect") or ""),
            "alternatives": [seed for seed in group.seed_questions if seed != question][:4],
            # Grouped (discovery) but NOT allowed to supply evidence: another entity.
            "excluded_evidence": group.excluded_evidence[:5],
            **feature_evidence,
        },
    )


def topic_family(subject: str) -> str:
    """Normalized subject key ("Mars (Planet)" / "der Mars" -> "mars")."""
    tokens = content_tokens(subject.split("(")[0]) or [fold(subject).strip()]
    return " ".join(sorted(tokens))[:60]


def same_video(left: TopicCandidateRecord | TopicCandidate, right: TopicCandidateRecord | TopicCandidate) -> float:
    """How strongly two candidates are the same video: question equivalence, or the curator's subject + aspect."""
    score = question_equivalence(left.question, right.question)
    a, b = left.provenance or {}, right.provenance or {}
    if a.get("aspect") and b.get("aspect") and a.get("topic_family") and a.get("topic_family") == b.get("topic_family"):
        score = max(score, question_equivalence(str(a["aspect"]), str(b["aspect"])))
    return score


def rationale_for(candidate: TopicCandidate) -> str:
    parts: list[str] = []
    trend = candidate.signal("trend")
    if trend.available and trend.evidence.get("ratio"):
        parts.append(f"German Wikipedia interest is {trend.evidence['ratio']}x its usual level")
    elif trend.available and trend.evidence.get("method") == "youtube_most_popular_de":
        parts.append(f"on YouTube's German {trend.evidence.get('category') or ''} chart (#{trend.evidence.get('rank')})".replace("  ", " "))
    elif trend.available and trend.evidence.get("outlets"):
        parts.append(f"covered by {trend.evidence['outlets']} German news outlets in the last day")
    outlier = candidate.signal("outlier")
    if outlier.available and outlier.evidence.get("ratio"):
        parts.append(f"a related video runs at {outlier.evidence['ratio']}x its channel's recent median")
    novelty = candidate.signal("novelty")
    if novelty.available and (novelty.value or 0) >= 0.65:
        parts.append("new for this channel")
    angle = candidate.provenance.get("angle")
    text = "; ".join(parts)
    text = text[:1].upper() + text[1:] + "." if text else "Selected from the current German topic signals."
    return f"{text} Angle: {angle}" if angle else text


def selection_reason(candidate: TopicCandidate) -> str:
    """Why this candidate sits where it does (diagnostics: "why did this question win?")."""
    if candidate.rejected:
        return "rejected: " + ", ".join(candidate.rejection_reasons[:4])
    components = candidate.score_breakdown.get("components") or {}
    top = sorted(
        ((name, float(item.get("contribution") or 0.0)) for name, item in components.items() if float(item.get("weight") or 0) > 0),
        key=lambda entry: -entry[1],
    )[:3]
    signal_class = candidate.score_breakdown.get("signal_class") or "no time signal"
    return (f"score {candidate.final_score:.3f} ({candidate.confidence} confidence, -{candidate.score_breakdown.get('confidence_penalty', 0)}); "
            f"{signal_class}; strongest: " + ", ".join(f"{name} {value:.3f}" for name, value in top))


MECHANISM_REASONS = {
    "paradox": "Frage mit eingebautem Widerspruch, der nach einer Auflösung verlangt",
    "what_if": "Gedankenexperiment mit einer konkreten Antwort",
    "why": "Fragt nach der Ursache eines bekannten Phänomens",
    "how": "Erklärt einen Mechanismus, der sich gut zeigen lässt",
    "yes_no": "Prüft eine verbreitete Annahme",
}


def times(ratio: Any, what: str, usual: str) -> str:
    """Readable multiple for the UI, no decimals: "rund 3× so viele X wie üblich" / "deutlich mehr X als üblich"."""
    value = float(ratio)
    if value >= 10:
        return f"deutlich mehr {what} als {usual}"
    if value >= 1.5:
        return f"rund {round(value)}× so viele {what} wie {usual}"
    return f"etwas mehr {what} als {usual}"


def _chart_clause(question: str, source_signals: list[dict[str, Any]]) -> str | None:
    """What the chart evidence literally shows: this very question, or a named video on the same subject."""
    charts = [
        str(item.get("title") or "") for item in source_signals
        if item.get("source") == "youtube_trending_de" and item.get("title") and item.get("attributed", True)
    ]
    if not charts:
        return None
    title = max(charts, key=lambda text: question_equivalence(question, text))
    if question_equivalence(question, title) >= history_module.DUPLICATE_THRESHOLD:
        return "ein Video mit genau dieser Frage ist gerade in den deutschen YouTube-Charts"
    return f"das Video „{compact(title, 70)}“ zum selben Thema ist gerade in den deutschen YouTube-Charts"


def user_reason(
    breakdown: dict[str, Any], signals: dict[str, Any], signal_class: str | None, *,
    question: str = "", source_signals: list[dict[str, Any]] | None = None,
) -> str:
    """One or two short German clauses for the UI ("Warum das funktionieren könnte") - no scores.

    Every evidence clause says literally what was measured (V2 calibration): a related chart
    video is named as such, never presented as a video about this question.
    """
    def value(name: str) -> float | None:
        item = signals.get(name) or {}
        return None if item.get("confidence") == "unavailable" else item.get("value")

    parts: list[str] = []
    curious, answer = value("curiosity"), value("payoff")
    judged = (signals.get("payoff") or {}).get("confidence") not in {None, "low", "unavailable"}  # a real judgement, not the form
    if judged and curious is not None and answer is not None and curious >= 0.75 and answer >= 0.75:
        parts.append("Starke Neugier-Frage mit konkreter, überraschender Antwort")
    elif judged and answer is not None and answer >= 0.7:
        parts.append("Klare Frage mit konkreter Antwort, gut in einem Short erklärbar")
    else:
        # Without a curator judgement only the question's form is known - say what that form offers.
        mechanism = ((signals.get("curiosity") or {}).get("evidence") or {}).get("mechanism")
        parts.append(MECHANISM_REASONS.get(str(mechanism), "Verständliche Wissensfrage"))
    trend = (signals.get("trend") or {}).get("evidence") or {}
    demand = (signals.get("demand") or {}).get("evidence") or {}
    outlier = (signals.get("outlier") or {}).get("evidence") or {}
    chart = _chart_clause(question, source_signals or []) if trend.get("method") == "youtube_most_popular_de" else None
    if signal_class in {"TRENDING", "EMERGING", "EVERGREEN_WITH_CURRENT_INTEREST"} and trend.get("ratio"):
        parts.append(f"gerade {times(trend['ratio'], 'Wikipedia-Aufrufe zum Thema', 'üblich')}")
    elif signal_class in {"TRENDING", "EMERGING", "EVERGREEN_WITH_CURRENT_INTEREST"} and chart:
        parts.append(chart)
    elif signal_class in {"TIMELY", "EVERGREEN_WITH_CURRENT_INTEREST"} and trend.get("outlets"):
        parts.append(f"aktuell in {trend['outlets']} deutschen Medien")
    elif outlier.get("ratio") and float(outlier["ratio"]) >= 2 and (outlier.get("video") or {}).get("title"):
        parts.append(f"das Video „{compact(outlier['video']['title'], 60)}“ zum Thema hat {times(outlier['ratio'], 'Aufrufe', 'sonst auf seinem Kanal')}")
    elif demand.get("median_views_per_day") and signal_class in {"EVERGREEN", "EVERGREEN_WITH_CURRENT_INTEREST"}:
        parts.append(f"dauerhaft gefragt (~{int(demand['median_views_per_day']):,} Wikipedia-Aufrufe pro Tag)".replace(",", "."))
    elif signal_class == "EVERGREEN":
        parts.append("zeitloses Thema")
    text = "; ".join(parts)
    return text + "."


def _recent_status_keys(db: Session, now: datetime) -> tuple[set[str], set[str]]:
    """(candidate ids to keep out, group keys to keep out): used ever, skipped within 24 h."""
    ids: set[str] = set()
    groups: set[str] = set()
    for record in db.scalars(select(TopicCandidateRecord).where(TopicCandidateRecord.status.in_(("used", "skipped", "picked")))).all():
        if record.status in {"skipped", "picked"} and (_utc(record.skipped_at) or now) < now - SKIP_MEMORY:
            continue
        ids.add(record.candidate_id)
        key = (record.provenance or {}).get("group_key")
        if record.status == "used" and (_utc(record.selected_at) or now) < now - USED_TOPIC_COOLDOWN:
            continue  # the subject may return with another question; this question never does
        if key:
            groups.add(str(key))
    return ids, groups


def _persist(db: Session, run_id: str, candidates: list[TopicCandidate], now: datetime) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen:  # two sightings became the same question: keep the better
            continue
        seen.add(candidate.candidate_id)
        record = db.get(TopicCandidateRecord, candidate.candidate_id)
        if record is None:
            record = TopicCandidateRecord(candidate_id=candidate.candidate_id, topic=candidate.topic[:300], discovered_at=now)
            db.add(record)
        keep_status = record.status == "used" or (
            record.status in {"skipped", "picked"} and (_utc(record.skipped_at) or now) >= now - SKIP_MEMORY
        )
        record.run_id = run_id
        record.topic = candidate.topic[:300]
        record.question = candidate.question[:300]
        record.rationale = candidate.rationale
        record.language = candidate.language
        record.region = candidate.region
        record.niche = candidate.niche
        record.signals = {name: signal.to_dict() for name, signal in candidate.signals.items()}
        record.source_signals = candidate.source_signals
        record.score_breakdown = candidate.score_breakdown
        record.final_score = candidate.final_score
        record.score_version = candidate.score_version
        record.confidence = candidate.confidence
        record.rejection_reasons = candidate.rejection_reasons
        record.provenance = candidate.provenance
        record.freshness_at = candidate.freshness_at
        if not keep_status:
            record.status = "rejected" if candidate.rejected else "pooled"
            record.proposed_at = None


def _prune(db: Session, now: datetime) -> None:
    prune_expired(db, now=now)
    db.execute(delete(TopicCandidateRecord).where(
        TopicCandidateRecord.status != "used", TopicCandidateRecord.updated_at < now - RETENTION,
    ))
    db.execute(delete(TopicDiscoveryRun).where(TopicDiscoveryRun.started_at < now - RETENTION))
    db.commit()


def discover(
    db: Session,
    settings: Settings,
    deps: DiscoveryDeps,
    *,
    now: datetime | None = None,
    broaden_from: TopicDiscoveryRun | None = None,
) -> TopicDiscoveryRun:
    """One bounded discovery refresh: sources -> questions -> signals -> score -> pool.

    ``broaden_from``: too few candidates of that run cleared the quality floor,
    so evaluate the NEXT raw topics instead of the same ones again, keep that
    run's usable candidates in the pool and spend no further search probes.

    Any failure (including an abandoned, timed-out discovery) ends the run as
    ``failed`` with its error - never a run that looks "in progress" forever.
    """
    started: dict[str, TopicDiscoveryRun] = {}
    try:
        return _discover(db, settings, deps, now=now or _now(), broaden_from=broaden_from, started=started)
    except Exception as exc:
        _fail_run(db, started.get("run"), exc)
        raise


def _fail_run(db: Session, run: TopicDiscoveryRun | None, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {str(exc)[:200]}"
    _FLIGHT.note("discovery_failed", error)
    logger.warning("Topic Intelligence discovery failed: %s", error)
    try:
        db.rollback()
        if run is None:
            return
        run = db.get(TopicDiscoveryRun, run.id)
        if run is None:
            return
        run.status = "failed"
        run.completed_at = _now()
        run.sources = [*(run.sources or []), {"name": "discovery", "status": "failed", "error": error, "calls": 0, "quota_units": 0, "items": 0}]
        db.commit()
    except Exception:  # recording the failure must not mask it
        db.rollback()
        logger.exception("Topic Intelligence could not record a failed discovery")


def _discover(
    db: Session,
    settings: Settings,
    deps: DiscoveryDeps,
    *,
    now: datetime,
    broaden_from: TopicDiscoveryRun | None,
    started: dict[str, TopicDiscoveryRun],
) -> TopicDiscoveryRun:
    _FLIGHT.stage("prune")
    _prune(db, now)
    ai_deadline = _FLIGHT.deadline(runtime.AI_DEADLINE_SECONDS)
    weights, version = resolve_weights(settings)
    meter = CallMeter(quota_budget=max(0, int(settings.topic_youtube_quota_budget)))
    ctx = DiscoveryContext(db=db, settings=settings, now=now, meter=meter, widen=broaden_from is not None)
    sequence = int(db.scalar(select(func.max(TopicDiscoveryRun.sequence))) or 0) + 1
    run = TopicDiscoveryRun(
        sequence=sequence, score_version=version, weights=weights, started_at=now, status="running",
        expires_at=now + timedelta(minutes=max(1, int(settings.topic_pool_ttl_minutes))),
    )
    db.add(run)
    db.commit()  # a failing source rolls its own work back, never the run record
    started["run"] = run
    raw, reports = _collect(ctx, deps.sources)
    _FLIGHT.stage("merge_raw_topics")
    reports.extend(deps.extra_reports)
    run.raw_topic_count = len(raw)
    usable_sources = [report for report in reports if report.status in {"ok", "cached", "partial"} and report.items > 0]
    degraded = any(report.status in {"failed", "timeout"} for report in reports)
    if not raw or not usable_sources:
        run.status = "unavailable"
        run.sources = [report.to_dict() for report in reports]
        run.completed_at = now
        run.youtube_quota_units = meter.quota_units
        db.commit()
        return run
    excluded_ids, excluded_groups = _recent_status_keys(db, now)
    carried: list[TopicCandidateRecord] = []
    carried_rejected: list[str] = []  # kept at the end for diagnostics; never served
    if broaden_from is not None:
        for candidate_id in broaden_from.ranked_candidate_ids or []:
            record = db.get(TopicCandidateRecord, candidate_id)
            if record is None:
                continue
            excluded_groups.add(str((record.provenance or {}).get("group_key") or ""))
            if record.status in {"pooled", "proposed"} and not record.rejection_reasons and record.score_version == version:
                carried.append(record)
            elif record.rejection_reasons:
                carried_rejected.append(record.candidate_id)
    groups: list[TopicGroup] = []
    prefiltered: list[str] = []
    raw_groups = group_topics(raw)
    _FLIGHT.stage("prefilter")
    for group in raw_groups:
        if group.key in excluded_groups or group.flags & PREFILTER_FLAGS:
            continue
        if topic_obscurity_flags(group.title, group.description()) & PREFILTER_TOPIC_FLAGS:
            prefiltered.append(group.title)  # cheap deterministic prefilter: never worth an AI call
            continue
        groups.append(group)
    budget = EVALUATION_BUDGET - (evaluated_in(broaden_from) if broaden_from is not None else 0)
    if broaden_from is not None:
        budget = max(budget, WIDEN_EVALUATIONS)
    history = history_module.load_history(db)
    semantic_on = semantic.semantic_enabled(settings)
    # Cheap deterministic pre-score for EVERY normalized topic (demand, question hint, audience,
    # grounding, novelty - never raw views), then a representative shortlist for the bounded,
    # expensive evaluation (see ``representative_order``).
    priorities: dict[str, tuple[float, dict[str, Any]]] = {group.key: _curation_priority(group, history, now) for group in groups}
    ai_capacity = AI_REQUEST_BUDGET - (ai_requests_in(broaden_from) if broaden_from is not None else 0)
    if broaden_from is not None:
        ai_capacity = max(ai_capacity, WIDEN_AI_REQUESTS)
    batch_size = max(1, min(20, int(getattr(settings, "topic_curator_batch_size", semantic.MAX_CURATION_BATCH) or semantic.MAX_CURATION_BATCH)))
    if semantic_on:
        groups, shortlist_report = representative_order(groups, priorities, slots=min(budget, ai_capacity * batch_size))
    else:
        # Local mode: a topic without a locally derivable question can never pass, so it is never shortlisted.
        transformable = [group for group in groups if group.evergreen or deterministic_transform(group).method != "none"]
        chosen_ids = {id(group) for group in transformable}
        ordered, shortlist_report = representative_order(transformable, priorities, slots=budget)
        groups = ordered + [group for group in groups if id(group) not in chosen_ids]
    shortlist_keys = [group.key for group in groups[: shortlist_report["shortlist"]]]
    cheap_duplicates: list[TopicGroup] = shortlist_report.pop("deferred_groups")

    def unused_seeds(group: TopicGroup) -> list[str]:
        seen = _seen_before(history, group, classify_niche(group.title, group.description())[0], now)
        return [seed for seed in group.seed_questions if not seen(seed)]
    own_priors, own_default = history_module.own_performance_priors(db, settings)
    candidates: list[TopicCandidate] = []
    extras: dict[str, tuple[list[str], list[str]]] = {}
    methods: list[str] = []
    semantic_errors: list[str] = []
    evaluated = curation_requests = curated = cached_curations = 0
    ai_left = AI_REQUEST_BUDGET - (ai_requests_in(broaden_from) if broaden_from is not None else 0)
    if broaden_from is not None:
        ai_left = max(ai_left, WIDEN_AI_REQUESTS)
    batch: list[TopicGroup] = []
    accepted = len(carried)
    queue = list(groups)
    evaluated_keys: set[str] = set()
    stop: dict[str, Any] = {}

    def keep_going(remaining: list[TopicGroup], done: int) -> bool:
        """Bounded stopping rule: enough supply AND fair coverage AND nothing competitive left."""
        if accepted < TARGET_ACCEPTED:
            return True
        if done < min(MIN_EVALUATION_COVERAGE, len(groups)):
            stop.update(reason="coverage_not_reached")
            return True
        shortlisted = [group for group in groups if group.key in shortlist_keys]
        for source in {source for group in shortlisted for source in group.sources}:
            needed = min(MIN_PER_SOURCE, sum(1 for group in shortlisted if source in group.sources))
            have = sum(1 for group in shortlisted if source in group.sources and group.key in evaluated_keys)
            if have < needed and any(source in group.sources for group in remaining):
                stop.update(reason=f"source_not_covered:{source}")
                return True
        top = sorted((candidate for candidate in candidates if not candidate.rejected), key=lambda item: -item.final_score)[:COMPETITIVE_TOP]
        known = [float(candidate.provenance["curation_priority"]) for candidate in top if "curation_priority" in candidate.provenance]
        if known:
            bar = min(known) - COMPETITIVE_MARGIN
            competitive = [group.title for group in remaining if priorities.get(group.key, (0.0, {}))[0] >= bar]
            if competitive:
                stop.update(reason="competitive_candidates_remain", bar=round(bar, 4), competitive=competitive[:5])
                return True
            stop.update(bar=round(bar, 4))
        stop.update(reason="enough_supply_coverage_and_no_competitive_candidate")
        return False
    curation_batches: list[dict[str, Any]] = []
    unevaluated: dict[str, tuple[str, str]] = {}  # group key -> (topic, reason): NOT judged, never "low quality"
    for group in cheap_duplicates:
        unevaluated[group.key] = (group.title, "cheap_duplicate")

    def evaluate(pairs: list[tuple[TopicGroup, Transformed]]) -> None:
        nonlocal accepted
        _FLIGHT.stage("scoring")
        for group, item in pairs:
            evaluated_keys.add(group.key)
            candidate = build_candidate(group, item, history=history, own_priors=own_priors, own_default=own_default, now=now, run_id=run.id)
            if candidate.candidate_id in excluded_ids:
                continue
            if group.key in priorities:
                candidate.provenance["curation_priority"] = priorities[group.key][0]
            extras[candidate.candidate_id] = (item.issues, item.flags)
            apply_evidence(candidate, deps.evidence_providers)
            score_candidate(candidate, weights=weights, version=version, now=now, issues=item.issues, flags=item.flags, degraded_sources=degraded)
            candidates.append(candidate)
        accepted = len(carried) + sum(1 for candidate in candidates if not candidate.rejected)

    if semantic_on:
        queue, evaluated, curation_requests, curated, cached_curations, ai_left = _curate_pool(
            db, settings, queue, now=now, budget=budget, ai_left=ai_left, ai_deadline=ai_deadline,
            evaluate=evaluate, keep_going=keep_going,
            batches=curation_batches, unevaluated=unevaluated, errors=semantic_errors, methods=methods, seeds=unused_seeds,
        )
    # Local mode: backfill through the raw pool with the deterministic question step.
    # The discovery pre-rank orders the work, it is not a gate.
    while not semantic_on and queue and evaluated < budget and keep_going(queue, evaluated):
        batch = queue[: min(MAX_BATCH, budget - evaluated)]
        queue = queue[len(batch):]
        evaluated += len(batch)
        transformed: list[Transformed] = []
        _FLIGHT.stage("curation_batch", "local_rules")
        for group in batch:
            seen = _seen_before(history, group, classify_niche(group.title, group.description())[0], now)
            item = deterministic_transform(group, avoid=seen)
            item.semantic = semantic.unavailable("semantic_curator_unavailable")
            transformed.append(item)
        methods.append("template")
        evaluate(list(zip(batch, transformed, strict=True)))
    method = "curator" if semantic_on or "curator" in methods else "template"
    run.transformation = f"{method}:{TRANSFORMATION_VERSION}"
    transform_error = None
    # Competition probes: only for the strongest usable candidates, bounded per refresh.
    probe_report = SourceReport(deps.probe.name, "skipped", error=None if deps.probe.available else "YouTube is not connected")
    if deps.probe.available:
        _FLIGHT.stage("competition_probe", deps.probe.name)
        calls_before, units_before = meter.calls, meter.quota_units
        limit = 0 if broaden_from is not None else max(0, int(settings.topic_youtube_search_probes))
        fetched = False
        probe_report.status = "ok"
        for candidate in [item for item in rank(candidates) if not item.rejected][:limit]:
            try:
                payload, cached = deps.probe.probe(ctx, candidate.question, candidate.topic)
            except Exception as exc:  # noqa: BLE001 - probe failure lowers confidence only
                db.rollback()
                probe_report.status = "failed"
                probe_report.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                degraded = True
                break
            fetched = fetched or not cached
            probe_report.items += 1
            videos = payload.get("videos") or []
            # The related-video "outlier" stays diagnostic evidence inside ``competition`` (see signals).
            competition, _related_outlier = competition_estimate(candidate.question, candidate.topic, videos)
            candidate.signals["competition"] = competition
            # Supply is evidence of demand too: related Shorts' median views/day (never one giant's raw views).
            candidate.signals["demand"] = merge_demand([candidate.signal("demand"), related_video_demand(candidate.question, candidate.topic, videos, now)])
            candidate.signals["opportunity"] = opportunity(competition, candidate.signal("demand"))
            candidate.source_signals.append({"source": deps.probe.name, "kind": "search", "title": payload.get("query"), "metrics": competition.evidence})
        if probe_report.status == "ok" and probe_report.items and not fetched:
            probe_report.status = "cached"
        probe_report.calls = meter.calls - calls_before
        probe_report.quota_units = meter.quota_units - units_before
    reports.append(probe_report)
    if not queue:
        stopped_by = "raw_pool_exhausted"
    elif evaluated >= budget:
        stopped_by = "evaluation_budget"
    elif stop.get("reason") == "enough_supply_coverage_and_no_competitive_candidate":
        stopped_by = stop["reason"]
    else:
        stopped_by = "ai_budget_or_deadline" if semantic_on else "evaluation_budget"
    evaluated_sources = Counter(source for group in groups if group.key in evaluated_keys for source in group.sources)
    reports.append(SourceReport(
        EVALUATION_REPORT, "ok", items=evaluated, calls=curation_requests,
        error=f"raw_groups={len(groups)} budget={budget} target={TARGET_ACCEPTED} accepted={accepted} remaining={len(queue)}",
        detail={"ai_requests": curation_requests, "ai_request_budget": AI_REQUEST_BUDGET, "curation_requests": curation_requests,
                "remaining_raw_groups": len(queue), "prefiltered": len(prefiltered), "prefiltered_topics": prefiltered[:10],
                "curator_batch_size": int(getattr(settings, "topic_curator_batch_size", semantic.MAX_CURATION_BATCH)) if semantic_on else None,
                # Not judged (timeout, budget, deadline) - "not evaluated", never "low quality".
                "unevaluated": dict(Counter(reason for _topic, reason in unevaluated.values())),
                "unevaluated_topics": [{"topic": topic, "reason": reason} for topic, reason in list(unevaluated.values())[:20]],
                "curation_order": [
                    {"topic": group.title, "priority": priorities[group.key][0], **priorities[group.key][1]}
                    for group in groups[:12] if group.key in priorities
                ],
                "shortlist": shortlist_report,
                "evaluated_sources": dict(evaluated_sources),
                "stopped_by": stopped_by,
                "stop_rule": {**stop, "target": TARGET_ACCEPTED, "min_coverage": MIN_EVALUATION_COVERAGE,
                              "min_per_source": MIN_PER_SOURCE, "competitive_margin": COMPETITIVE_MARGIN},
                "broadened_from": broaden_from.id if broaden_from is not None else None},
    ))
    reports.append(SourceReport(
        SEMANTIC_REPORT,
        # partial: some requests failed, but judged topics exist (their results are kept)
        "unavailable" if not semantic_on else "ok" if not semantic_errors else "partial" if curated + cached_curations else "failed",
        error="; ".join(dict.fromkeys(semantic_errors)) or (None if semantic_on else "No OpenAI key (or disabled): strict local acceptance"),
        calls=curation_requests,
        items=curated,
        detail={"curator_version": semantic.SEMANTIC_CURATOR_VERSION, "enabled": semantic_on,
                "curated": curated, "cached": cached_curations, "batches": curation_batches},
    ))
    if broaden_from is not None:
        reports.append(SourceReport(BROADENING_REPORT, "ok", items=evaluated))
    for candidate in candidates:
        issues, flags = extras[candidate.candidate_id]
        score_candidate(candidate, weights=weights, version=version, now=now, issues=issues, flags=flags, degraded_sources=degraded)
    ranked = rank(candidates)
    kept: list[TopicCandidate] = []
    for candidate in ranked:
        if not candidate.rejected:
            # Semantic dedupe (V2): the same video in other words collapses into the stronger one.
            duplicate = next((other for other in kept if same_video(candidate, other) >= history_module.DUPLICATE_THRESHOLD), None)
            if duplicate is not None:
                candidate.rejection_reasons.append("duplicate_in_pool")
                candidate.provenance["duplicate_of"] = {
                    "candidate_id": duplicate.candidate_id, "question": duplicate.question,
                    "equivalence": round(same_video(candidate, duplicate), 3),
                }
        if not candidate.rejected:
            kept.append(candidate)
        candidate.rationale = rationale_for(candidate)
    ranked = rank(ranked)
    for position, candidate in enumerate(ranked, 1):
        candidate.provenance["rank"] = position
        candidate.provenance["selection_reason"] = selection_reason(candidate)
    _FLIGHT.stage("persistence")
    _persist(db, run.id, ranked, now)
    order = [(rank_key(candidate.rejected, candidate.final_score, candidate.candidate_id), candidate.candidate_id) for candidate in ranked]
    order += [(rank_key(False, record.final_score, record.candidate_id), record.candidate_id) for record in carried]
    run.ranked_candidate_ids = list(dict.fromkeys([*(candidate_id for _key, candidate_id in sorted(order)), *carried_rejected]))
    run.status = "partial" if degraded else "ok"
    if transform_error:
        reports.append(SourceReport("question_transformation", "failed", error=transform_error))
    run.sources = [report.to_dict() for report in reports]
    run.youtube_quota_units = meter.quota_units
    run.completed_at = now
    db.commit()
    return run


# ---------------------------------------------------------------------------
# Proposals ("Generate Next Video" / "Try another")
# ---------------------------------------------------------------------------


def _curate_pool(
    db: Session,
    settings: Settings,
    queue: list[TopicGroup],
    *,
    now: datetime,
    budget: int,
    ai_left: int,
    ai_deadline: float,
    evaluate: Any,
    keep_going: Any,
    batches: list[dict[str, Any]],
    unevaluated: dict[str, tuple[str, str]],
    errors: list[str],
    methods: list[str],
    seeds: Any = None,
) -> tuple[list[TopicGroup], int, int, int, int, int]:
    """Curate the prioritized raw pool within the AI budget.

    Cached judgements first (free), then one request per ``topic_curator_batch_size``
    topics in priority order.  A failed request (e.g. a timeout) marks only its own
    topics as NOT evaluated: they are retried once - first, before lower-ranked topics,
    in a smaller batch after a timeout - and never become low-quality rejections or
    unvalidated local filler.  Returns (remaining raw groups, evaluated, requests,
    curated, cached, ai requests left).
    """
    evaluated = requests = curated = cached = 0
    _FLIGHT.stage("curation_cache", "openai_curator")
    lookup = semantic.curate(db, settings, queue, requests_left=0, now=now, seeds=seeds)
    fresh: list[TopicGroup] = []
    pairs: list[tuple[TopicGroup, Transformed]] = []
    for group in queue:
        judgement = lookup.judgements.get(group.key)
        if judgement is None or evaluated >= budget:
            fresh.append(group)
            continue
        pairs.append((group, curated_transform(group, judgement, semantic.curated_signal(judgement, status="cached"))))
        evaluated += 1
        cached += 1
    if pairs:
        methods.append("curator")
        evaluate(pairs)
    batch_size = max(1, min(20, int(getattr(settings, "topic_curator_batch_size", semantic.MAX_CURATION_BATCH) or semantic.MAX_CURATION_BATCH)))
    retried: set[str] = set()
    failed: list[TopicGroup] = []
    stop_reason = ""
    while fresh and evaluated < budget and keep_going(fresh, evaluated):
        if ai_left <= 0:
            stop_reason = "ai_budget_exhausted"
            break
        if time.monotonic() + runtime.CURATOR_TIMEOUT_SECONDS > ai_deadline:
            # A slow model must not stretch the refresh: no new request past the deadline.
            errors.append(f"ai_deadline: no new curator request after {runtime.AI_DEADLINE_SECONDS:g}s")
            stop_reason = "ai_deadline"
            break
        _FLIGHT.stage("curation_batch", "openai_curator")
        size = min(batch_size, budget - evaluated)
        batch, fresh = fresh[:size], fresh[size:]
        outcome = semantic.curate(db, settings, batch, requests_left=ai_left, now=now, batch_size=size, seeds=seeds)
        requests += outcome.requests
        ai_left -= outcome.requests
        errors.extend(outcome.errors)
        batches.extend({**item, "retry": any(group.key in retried for group in batch)} for item in outcome.batches)
        pairs, retry = [], []
        for group in batch:
            status = outcome.statuses.get(group.key, "not_curated")
            judgement = outcome.judgements.get(group.key)
            if judgement is not None:
                pairs.append((group, curated_transform(group, judgement, semantic.curated_signal(judgement, status=status))))
                evaluated += 1
                curated += status == "curated"
                cached += status == "cached"
            elif status == "failed" and group.key not in retried:
                retry.append(group)
            elif status == "failed":
                failed.append(group)
                unevaluated[group.key] = (group.title, "curator_failed")
            else:
                fresh.insert(0, group)  # not sent: still waiting, in priority order
        if pairs:
            methods.append("curator")
            evaluate(pairs)
        if retry:
            # The highest-ranked unresolved topics go first again ...
            retried.update(group.key for group in retry)
            fresh = retry + fresh
        if outcome.timed_out:
            # ... and after a timeout every following request is smaller.
            batch_size = max(semantic.MIN_CURATION_BATCH, batch_size // 2)
    for group in fresh:
        if group.key in retried:
            unevaluated[group.key] = (group.title, "curator_failed")
        elif stop_reason:
            unevaluated[group.key] = (group.title, stop_reason)
    return fresh + failed, evaluated, requests, curated, cached, ai_left


def current_run(
    db: Session, now: datetime, version: str | None = None, semantic_ready: bool | None = None,
) -> TopicDiscoveryRun | None:
    """The fresh pool; a pool scored by another score version is never reused.

    A discovery that failed or was abandoned mid-way never hides the last complete pool.
    """
    run = db.scalar(
        select(TopicDiscoveryRun).where(TopicDiscoveryRun.status.not_in(("failed", "running")))
        .order_by(TopicDiscoveryRun.sequence.desc()).limit(1)
    )
    if run is None or run.status == "unavailable" or (_utc(run.expires_at) or now) <= now:
        return None
    if version is not None and run.score_version != version:
        return None
    if version is not None and not str(run.transformation or "").endswith(f":{TRANSFORMATION_VERSION}"):
        return None  # questions built by an older question step
    if version is not None and semantic_ready is not None:
        detail = semantic_report(run).get("detail") or {}
        if detail.get("curator_version") != semantic.SEMANTIC_CURATOR_VERSION or bool(detail.get("enabled")) != semantic_ready:
            return None  # judged by another validator version, or validation availability changed
    return run


def _next_record(db: Session, run: TopicDiscoveryRun) -> TopicCandidateRecord | None:
    for candidate_id in run.ranked_candidate_ids or []:
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is not None and record.status in {"proposed", "pooled"} and not record.rejection_reasons:
            return record
    return None


def candidate_class(record: TopicCandidateRecord, now: datetime | None = None) -> tuple[str | None, dict[str, Any]]:
    """The signal class re-checked NOW: stale evidence loses its trend status even in a reused pool."""
    candidate = candidate_from_record(record)
    candidate.score_breakdown = dict(record.score_breakdown or {})
    return scoring.classify_signal(candidate, now or _now())


def serialize_candidate(record: TopicCandidateRecord, now: datetime | None = None) -> dict[str, Any]:
    breakdown = record.score_breakdown or {}
    components = breakdown.get("components") or {}
    signal_class, _basis = candidate_class(record, now)
    return {
        # V2 user-facing: concise reasoning + an evidence-backed signal label (or none).
        "reason": user_reason(breakdown, record.signals or {}, signal_class, question=record.question, source_signals=list(record.source_signals or [])),
        "signal_class": signal_class,
        "signal_label": scoring.SIGNAL_LABELS_DE.get(signal_class) if signal_class else None,
        "candidate_id": record.candidate_id,
        "question": record.question,
        "topic": record.topic,
        "rationale": record.rationale,
        "niche": record.niche,
        "language": record.language,
        "region": record.region,
        "final_score": record.final_score,
        "confidence": record.confidence,
        "score_version": record.score_version,
        "status": record.status,
        "explanation": explain(breakdown),
        "details": {
            "components": {
                name: {
                    key: item.get(key)
                    for key in ("value", "confidence", "effective", "weight", "contribution")
                }
                for name, item in components.items()
            },
            "penalties": breakdown.get("penalties") or {},
            "sources": sorted({str(item.get("source")) for item in record.source_signals or []}),
            "transformation": (record.provenance or {}).get("transformation"),
        },
        "discovered_at": _utc(record.discovered_at),
        "freshness_at": _utc(record.freshness_at),
    }


def _pool_info(db: Session, run: TopicDiscoveryRun) -> dict[str, Any]:
    remaining = 0
    for candidate_id in run.ranked_candidate_ids or []:
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is not None and record.status == "pooled" and not record.rejection_reasons:
            remaining += 1
    return {
        "run_id": run.id,
        "status": run.status,
        "discovered_at": _utc(run.completed_at or run.started_at),
        "expires_at": _utc(run.expires_at),
        "remaining": remaining,
        "total": len(run.ranked_candidate_ids or []),
        "transformation": run.transformation,
        "sources": run.sources,
    }


def _unavailable(run: TopicDiscoveryRun | None) -> dict[str, Any]:
    return {"status": "unavailable", "message": UNAVAILABLE_MESSAGE, "candidate": None, "pool": None if run is None else {"run_id": run.id, "status": run.status, "sources": run.sources}}


def next_topic(
    db: Session, settings: Settings, deps: DiscoveryDeps, *, now: datetime | None = None, refresh: bool = False,
) -> dict[str, Any]:
    now = now or _now()
    # Single-flight: concurrent clicks share one refresh - but never wait on it unboundedly.
    if not _FLIGHT.acquire(timeout=runtime.FLIGHT_WAIT_SECONDS):
        return {"status": "discovering", "message": "Topic discovery is running.", "candidate": None, "pool": None,
                "retry_after_seconds": DISCOVERY_RETRY_SECONDS}
    try:
        return _next_topic_locked(db, settings, deps, now=now, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - a failed discovery is a state, not a hang or a 500
        logger.warning("Topic Intelligence proposal failed: %s", exc)
        return {"status": "unavailable", "message": DISCOVERY_FAILED_MESSAGE, "candidate": None, "pool": None}
    finally:
        _FLIGHT.release()


def _next_topic_locked(db: Session, settings: Settings, deps: DiscoveryDeps, *, now: datetime, refresh: bool) -> dict[str, Any]:
    run = None if refresh else current_run(db, now, resolve_weights(settings)[1], semantic.semantic_enabled(settings))
    fresh = run is None
    if run is None:
        run = discover(db, settings, deps, now=now)
        if run.status == "unavailable":
            return _unavailable(run)
    record = _next_record(db, run)
    if record is None and not fresh:
        run = discover(db, settings, deps, now=now)
        if run.status == "unavailable":
            return _unavailable(run)
        record = _next_record(db, run)
    if record is None:
        return {"status": "exhausted", "message": EXHAUSTED_MESSAGE, "candidate": None, "pool": _pool_info(db, run)}
    if record.status != "proposed":
        record.status = "proposed"
        record.proposed_at = now
        db.commit()
    return {"status": "proposed", "message": None, "candidate": serialize_candidate(record, now), "pool": _pool_info(db, run)}


def skip_topic(
    db: Session, settings: Settings, deps: DiscoveryDeps, candidate_id: str, *, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    record = db.get(TopicCandidateRecord, candidate_id)
    if record is None:
        raise TopicHandoffError("unknown_candidate", "This topic suggestion no longer exists.")
    if record.status in {"pooled", "proposed"}:
        record.status = "skipped"
        record.skipped_at = now
        db.commit()
    return next_topic(db, settings, deps, now=now)


# ---------------------------------------------------------------------------
# Full Auto ("Generate automatically"): the strongest eligible question, or none
# ---------------------------------------------------------------------------

AUTO_NONE_MESSAGE = "Gerade keine ausreichend starke Frage gefunden. Gib eine eigene Frage ein oder versuche es später erneut."


def _auto_pick(db: Session, run: TopicDiscoveryRun) -> tuple[TopicCandidateRecord | None, list[dict[str, Any]]]:
    """First usable candidate in rank order that meets Full Auto's own minimum quality."""
    considered: list[dict[str, Any]] = []
    for candidate_id in run.ranked_candidate_ids or []:
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is None or record.status not in {"pooled", "proposed"} or record.rejection_reasons:
            continue
        eligible, reasons = scoring.auto_eligibility(
            record.score_breakdown or {}, rejected=False, confidence=record.confidence, score=record.final_score,
        )
        considered.append({"candidate_id": record.candidate_id, "question": record.question, "final_score": record.final_score,
                           "confidence": record.confidence, "eligible": eligible, "reasons": reasons})
        if eligible:
            return record, considered
    return None, considered


def auto_topic(db: Session, settings: Settings, deps: DiscoveryDeps, *, now: datetime | None = None) -> dict[str, Any]:
    """Full Auto: select the next question itself - never garbage just because something must win.

    Uses the fresh pool (or one discovery); if nothing meets ``scoring.auto_eligibility``,
    widens discovery ONCE (next raw topics + the next evergreen window, bounded), then
    answers ``no_strong_candidate``.  Generation is started by the caller through the
    normal generation entry point, with Topic Intelligence provenance.
    """
    now = now or _now()
    if not _FLIGHT.acquire(timeout=runtime.FLIGHT_WAIT_SECONDS):
        return {"status": "discovering", "message": "Topic discovery is running.", "candidate": None,
                "retry_after_seconds": DISCOVERY_RETRY_SECONDS}
    try:
        return _auto_locked(db, settings, deps, now=now)
    except Exception as exc:  # noqa: BLE001 - a failed discovery is a state, not a 500
        logger.warning("Topic Intelligence auto selection failed: %s", exc)
        return {"status": "unavailable", "message": DISCOVERY_FAILED_MESSAGE, "candidate": None}
    finally:
        _FLIGHT.release()


def _auto_locked(db: Session, settings: Settings, deps: DiscoveryDeps, *, now: datetime) -> dict[str, Any]:
    run = current_run(db, now, resolve_weights(settings)[1], semantic.semantic_enabled(settings))
    if run is None:
        run = discover(db, settings, deps, now=now)
        if run.status == "unavailable":
            return _unavailable(run)
    record, considered = _auto_pick(db, run)
    widened = False
    if record is None and not was_broadened(run):
        widened = True
        broadened = _discover_or_none(db, settings, deps, now=now, broaden_from=run)
        if broadened is not None and broadened.status != "unavailable":
            run = broadened
            record, considered = _auto_pick(db, run)
    if record is None:
        return {"status": "no_strong_candidate", "message": AUTO_NONE_MESSAGE, "candidate": None, "widened": widened,
                "considered": considered[:5], "pool": _pool_info(db, run)}
    record.status = "proposed"
    record.proposed_at = now
    record.provenance = {**(record.provenance or {}), "auto_selection": {
        "selected_at": now.isoformat(), "widened": widened, "considered": len(considered),
        "thresholds": {"min_score": scoring.AUTO_MIN_SCORE, "min_curiosity": scoring.AUTO_MIN_CURIOSITY,
                       "min_payoff": scoring.AUTO_MIN_PAYOFF, "fallback_min_score": scoring.AUTO_FALLBACK_MIN_SCORE},
    }}
    db.commit()
    return {"status": "selected", "message": None, "candidate": serialize_candidate(record, now),
            "selection_reason": (record.provenance or {}).get("selection_reason"), "widened": widened, "pool": _pool_info(db, run)}


# ---------------------------------------------------------------------------
# Home suggestions: 3 visible chips + a hidden reserve, served from the pool
# ---------------------------------------------------------------------------

MAX_SUGGESTIONS = 12


def _mark(db: Session, candidate_ids: list[str], status: str, now: datetime) -> None:
    """Remember chips the user picked or replaced so they are not suggested again soon."""
    for candidate_id in dict.fromkeys(candidate_ids):
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is not None and record.status in {"pooled", "proposed"}:
            record.status = status
            record.skipped_at = now
    db.commit()


def _available_records(db: Session, run: TopicDiscoveryRun, exclude: set[str]) -> list[TopicCandidateRecord]:
    """Usable, not excluded, not a near-duplicate of anything excluded or chosen; diversified.

    The order is the scoring authority's (``rank_key`` + ``diversify`` against
    what the client currently shows).
    """
    excluded_records = [record for record in (db.get(TopicCandidateRecord, item) for item in exclude) if record is not None]
    taken: list[TopicCandidateRecord] = list(excluded_records)
    shown = [
        (record.niche, str((record.provenance or {}).get("mechanism") or "other"), str((record.provenance or {}).get("topic_family") or ""))
        for record in excluded_records if record.status == "proposed"
    ]
    chosen: list[TopicCandidateRecord] = []
    for candidate_id in run.ranked_candidate_ids or []:
        if candidate_id in exclude:
            continue
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is None or record.status not in {"pooled", "proposed"} or record.rejection_reasons:
            continue
        if any(same_video(record, other) >= history_module.DUPLICATE_THRESHOLD for other in taken):
            continue
        chosen.append(record)
        taken.append(record)
    by_id = {record.candidate_id: record for record in chosen}
    items = [
        RankedItem(record.candidate_id, record.final_score, record.niche, str((record.provenance or {}).get("mechanism") or "other"),
                   str((record.provenance or {}).get("topic_family") or ""))
        for record in chosen
    ]
    return [by_id[item.candidate_id] for item in diversify(items, shown)]


def suggestions(
    db: Session,
    settings: Settings,
    deps: DiscoveryDeps,
    *,
    count: int,
    exclude: list[str] | None = None,
    picked: list[str] | None = None,
    dismissed: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Up to ``count`` ranked candidates for the Home chips, excluding what the client shows.

    Served from the fresh pool; a refresh runs only when the pool expired or
    ran out (provider caches and quota budgets still apply).  Nothing here
    starts generation.
    """
    now = now or _now()
    count = max(1, min(MAX_SUGGESTIONS, int(count)))
    excluded = set(exclude or []) | set(picked or []) | set(dismissed or [])
    _mark(db, list(picked or []), "picked", now)
    _mark(db, list(dismissed or []), "skipped", now)
    # Never make Home wait behind a discovery that is already running (e.g. the
    # startup warm-up): say so, and let the client ask again shortly.
    if not _FLIGHT.acquire(blocking=False):
        flight = _FLIGHT.snapshot()
        return {
            "status": "discovering",
            "message": "Topic discovery is running.",
            "candidates": [],
            "retry_after_seconds": DISCOVERY_RETRY_SECONDS,
            "discovery": {key: flight[key] for key in ("discovery_stage", "active_provider", "elapsed_seconds", "hard_limit_seconds")},
            "pool": None,
            "summary": None,
        }
    try:
        return _suggestions_locked(db, settings, deps, count=count, excluded=excluded, now=now)
    except Exception as exc:  # noqa: BLE001 - a failed discovery ends in a real state, and the flight is released
        logger.warning("Topic Intelligence suggestions failed: %s", exc)
        return {
            "status": "unavailable",
            "message": DISCOVERY_FAILED_MESSAGE,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "candidates": [],
            "pool": None,
            "summary": None,
        }
    finally:
        _FLIGHT.release()


def _suggestions_locked(
    db: Session, settings: Settings, deps: DiscoveryDeps, *, count: int, excluded: set[str], now: datetime,
) -> dict[str, Any]:
    version = resolve_weights(settings)[1]
    run = current_run(db, now, version, semantic.semantic_enabled(settings))
    fresh = run is None
    if run is None:
        run = discover(db, settings, deps, now=now)
        if run.status == "unavailable":
            return {**_unavailable(run), "candidates": [], "summary": pool_summary(db, run)}
    records = _available_records(db, run, excluded)
    old_enough = (_utc(run.started_at) or now) <= now - MIN_REFRESH_INTERVAL
    if len(records) < count and not fresh and old_enough:
        # Pool used up by picks/refreshes: a normal refresh first.
        refreshed = _discover_or_none(db, settings, deps, now=now)
        if refreshed is not None and refreshed.status != "unavailable":
            run = refreshed
            records = _available_records(db, run, excluded)
    if (
        len(records) < count and not was_broadened(run) and evaluated_in(run) < EVALUATION_BUDGET
        and _evaluation_detail(run).get("remaining_raw_groups", 1) > 0
        # With the curator, a pass without AI budget left could judge nothing new.
        and not (semantic.semantic_enabled(settings) and ai_requests_in(run) >= AI_REQUEST_BUDGET)
    ):
        # Too few candidates clear the quality floor: evaluate the next raw
        # topics once per pool, rather than serving weak filler.
        broadened = _discover_or_none(db, settings, deps, now=now, broaden_from=run)
        if broadened is not None and broadened.status != "unavailable":
            run = broadened
            records = _available_records(db, run, excluded)
    records = records[:count]
    for record in records:
        if record.status == "pooled":
            record.status = "proposed"  # displayed (visible chip or reserve)
            record.proposed_at = now
    db.commit()
    status = "ok" if len(records) >= count else "partial" if records else "exhausted"
    return {
        "status": status,
        "message": None if records else EXHAUSTED_MESSAGE,
        "candidates": [serialize_candidate(record, now) for record in records],
        "pool": _pool_info(db, run),
        # Why fewer than requested: how many were evaluated and why they were rejected.
        "summary": pool_summary(db, run),
    }


def _discover_or_none(db: Session, settings: Settings, deps: DiscoveryDeps, **kwargs: Any) -> TopicDiscoveryRun | None:
    """A follow-up refresh/broadening that fails keeps the pool that is already there."""
    try:
        return discover(db, settings, deps, **kwargs)
    except runtime.DiscoveryAbandoned:
        raise
    except Exception:  # noqa: BLE001 - recorded on the failed run by ``discover``
        return None


def evaluated_in(run: TopicDiscoveryRun) -> int:
    """How many raw topics this pool (and the pass it broadened) already evaluated."""
    return sum(
        int(item.get("items") or 0) for item in run.sources or []
        if isinstance(item, dict) and item.get("name") == EVALUATION_REPORT
    )


def _evaluation_detail(run: TopicDiscoveryRun) -> dict[str, Any]:
    for item in run.sources or []:
        if isinstance(item, dict) and item.get("name") == EVALUATION_REPORT:
            return dict(item.get("detail") or {})
    return {}


def ai_requests_in(run: TopicDiscoveryRun) -> int:
    for item in run.sources or []:
        if isinstance(item, dict) and item.get("name") == EVALUATION_REPORT:
            return int((item.get("detail") or {}).get("ai_requests") or 0)
    return 0


def semantic_report(run: TopicDiscoveryRun) -> dict[str, Any]:
    return next((item for item in run.sources or [] if isinstance(item, dict) and item.get("name") == SEMANTIC_REPORT), {})


def was_broadened(run: TopicDiscoveryRun) -> bool:
    return any(isinstance(item, dict) and item.get("name") == BROADENING_REPORT for item in run.sources or [])


def warm_pool(session_factory: Any, settings: Settings, deps_factory: Any, *, now: datetime | None = None) -> str | None:
    """Fill the candidate pool ahead of the first Home visit (no-op while it is fresh)."""
    with session_factory() as db:
        now = now or _now()
        # Never wait on (or deadlock with) a request that is already discovering.
        if not _FLIGHT.acquire(blocking=False, owner="warmup"):
            return "discovery_already_running"
        try:
            if current_run(db, now, resolve_weights(settings)[1], semantic.semantic_enabled(settings)) is not None:
                return None
            return discover(db, settings, deps_factory(db), now=now).status
        finally:
            _FLIGHT.release()


def run_warmup(session_factory: Any, settings: Settings, deps_factory: Any) -> None:
    """The warm-up itself, with its state recorded in ``WARMUP`` for diagnostics."""
    WARMUP.update(state="running", started_at=_now().isoformat(), finished_at=None, result=None, error=None)
    try:
        result = warm_pool(session_factory, settings, deps_factory)
        if WARMUP["state"] == "running":  # not already failed by the flight's hard limit
            WARMUP.update(state="done", result=result or "pool_already_fresh")
    except Exception as exc:  # discovery must never affect the app
        WARMUP.update(state="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        logger.exception("Topic Intelligence warm-up failed")
    finally:
        WARMUP["finished_at"] = _now().isoformat()


def warm_pool_in_background(session_factory: Any, settings: Settings, deps_factory: Any) -> threading.Thread:
    """Topic research runs beside the app; video generation never waits for it."""
    thread = threading.Thread(
        target=run_warmup, args=(session_factory, settings, deps_factory), name="topic-intelligence-warmup", daemon=True,
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Developer diagnostics (tuning only; not part of the Home UI)
# ---------------------------------------------------------------------------

QUALITY_FEATURE_SIGNALS = ("broad_appeal", "accessibility", "question_form", "short_worthiness")


def candidate_from_record(record: TopicCandidateRecord) -> TopicCandidate:
    """Rebuild a candidate from its persisted signals (older versions get the v2 text features)."""
    signals = {name: Signal.from_dict(payload) for name, payload in (record.signals or {}).items()}
    provenance = dict(record.provenance or {})
    missing = [name for name in QUALITY_FEATURE_SIGNALS if name not in signals]
    if missing:
        features, evidence = quality_signals(
            record.question, record.topic, "", record.niche, {}, str(provenance.get("transformation") or "template"),
            set(provenance.get("extraction_notes") or []), signals.get("semantic"),
        )
        signals.update({name: features[name] for name in missing})
        for key, value in evidence.items():
            provenance.setdefault(key, value)
    return TopicCandidate(
        candidate_id=record.candidate_id,
        topic=record.topic,
        question=record.question,
        rationale=record.rationale,
        source_signals=list(record.source_signals or []),
        discovered_at=_utc(record.discovered_at) or _now(),
        language=record.language,
        region=record.region,
        niche=record.niche,
        signals=signals,
        freshness_at=_utc(record.freshness_at),
        provenance=provenance,
    )


def rescore_record(record: TopicCandidateRecord, settings: Settings) -> TopicCandidate:
    """Score a persisted candidate with the CURRENT authority, at its own discovery time."""
    weights, version = resolve_weights(settings)
    candidate = candidate_from_record(record)
    provenance = candidate.provenance
    return score_candidate(
        candidate,
        weights=weights,
        version=version,
        now=candidate.freshness_at or candidate.discovered_at,
        issues=list(provenance.get("question_issues") or []),
        flags=list(provenance.get("flags") or []),
        degraded_sources=bool((record.score_breakdown or {}).get("degraded_sources")),
    )


def _score_view(breakdown: dict[str, Any], final: float, confidence: str, version: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "score_version": version,
        "final_score": final,
        "confidence": confidence,
        "components": {
            name: {key: item.get(key) for key in ("value", "confidence", "effective", "weight", "contribution", "trend_quality_factor") if key in item}
            for name, item in (breakdown.get("components") or {}).items()
        },
        "penalties": breakdown.get("penalties") or {},
        "quality": breakdown.get("quality") or {},
        "trend_quality": breakdown.get("trend_quality") or {},
        "rejection_reasons": list(reasons),
    }


def diagnostics(
    db: Session, settings: Settings, *, limit: int = 20, shown: bool = False, rescore: bool = False,
) -> dict[str, Any]:
    """Top candidates with every score component, penalties, quality gate and rejection reason.

    ``shown``: the candidates handed to Home (in the order they were served)
    instead of the latest pool.  ``rescore``: also score each persisted
    candidate with the current authority (compare v1 winners with v2).
    """
    limit = max(1, min(200, int(limit)))
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    if shown:
        served = db.scalars(
            select(TopicCandidateRecord).where(TopicCandidateRecord.proposed_at.is_not(None))
        ).all()
        records = sorted(served, key=lambda record: (_utc(record.proposed_at), -record.final_score, record.candidate_id))[:limit]
    else:
        records = [record for record in (db.get(TopicCandidateRecord, item) for item in (run.ranked_candidate_ids if run else [])) if record is not None][:limit]
    rows = []
    for record in records:
        row: dict[str, Any] = {
            "candidate_id": record.candidate_id,
            "question": record.question,
            "topic": record.topic,
            "status": record.status,
            "niche": record.niche,
            "mechanism": (record.provenance or {}).get("mechanism"),
            "sources": sorted({str(item.get("source")) for item in record.source_signals or []}),
            "transformation": (record.provenance or {}).get("transformation"),
            "served_at": _utc(record.proposed_at),
            "persisted": _score_view(record.score_breakdown or {}, record.final_score, record.confidence, record.score_version, record.rejection_reasons or []),
            # V2: why did this question win (or lose)?
            "v2": {
                "origin": (record.provenance or {}).get("origin"),
                "topic_family": (record.provenance or {}).get("topic_family"),
                "aspect": (record.provenance or {}).get("aspect"),
                "alternatives": (record.provenance or {}).get("alternatives") or [],
                "rank": (record.provenance or {}).get("rank"),
                "selection_reason": (record.provenance or {}).get("selection_reason"),
                "duplicate_of": (record.provenance or {}).get("duplicate_of"),
                "signal_class": candidate_class(record)[0],
                "signal_class_basis": candidate_class(record)[1],
                "evidence_freshness": {
                    name: {key: ((record.signals or {}).get(name) or {}).get("evidence", {}).get(key) for key in ("fetched_at", "ttl_hours")}
                    for name in ("trend", "demand", "outlier")
                    if ((record.signals or {}).get(name) or {}).get("confidence") not in {None, "unavailable"}
                },
                "auto": dict(zip(("eligible", "reasons"), scoring.auto_eligibility(
                    record.score_breakdown or {}, rejected=bool(record.rejection_reasons), confidence=record.confidence,
                    score=record.final_score,
                ), strict=True)),
            },
        }
        if rescore:
            candidate = rescore_record(record, settings)
            row["rescored"] = _score_view(candidate.score_breakdown, candidate.final_score, candidate.confidence, candidate.score_version, candidate.rejection_reasons)
        rows.append(row)
    return {
        "current_score_version": resolve_weights(settings)[1],
        "run": None if run is None else {"run_id": run.id, "score_version": run.score_version, "status": run.status, "sources": run.sources},
        "candidates": rows,
    }


def curation_calibration(db: Session, run: TopicDiscoveryRun | None) -> dict[str, Any] | None:
    """Every CURATED candidate of a pool with all v2 dimensions, short-worthiness, issues and the
    hard gates that rejected it - a reliable sample before any threshold is changed.

    ``sole_gate``: candidates rejected by exactly one gate (those a single threshold decides);
    ``gate_counts``: how often each gate fired.  Diagnostics only; nothing here changes a score.
    """
    if run is None:
        return None
    rows: list[dict[str, Any]] = []
    gate_counts: Counter[str] = Counter()
    sole_gate: Counter[str] = Counter()
    gates_per_candidate: Counter[int] = Counter()
    for candidate_id in run.ranked_candidate_ids or []:
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is None:
            continue
        quality = (record.score_breakdown or {}).get("quality") or {}
        sem = quality.get("semantic") or {}
        if sem.get("status") not in {"curated", "cached"}:
            continue
        short = quality.get("short_worthiness") or {}
        gates = list(record.rejection_reasons or [])
        gate_counts.update(gates)
        gates_per_candidate[min(len(gates), 3)] += 1
        if len(gates) == 1:
            sole_gate[gates[0]] += 1
        rows.append({
            "question": record.question,
            "topic": record.topic,
            "final_score": record.final_score,
            "dimensions": sem.get("dimensions") or {},
            "short_dimensions": short.get("dimensions") or {},
            "short_worthiness": short.get("value"),
            "short_penalties": short.get("penalties") or {},
            "issues": sem.get("issues") or [],
            "grounded": sem.get("grounded"),
            "gates": gates,
            "accepted": not gates,
        })
    return {
        "curated": len(rows),
        "accepted": sum(1 for row in rows if row["accepted"]),
        "gates_per_candidate": {("3+" if key == 3 else str(key)): value for key, value in sorted(gates_per_candidate.items())},
        "gate_counts": dict(gate_counts.most_common()),
        "sole_gate": dict(sole_gate.most_common()),
        "thresholds": {
            "semantic_dimension_min": scoring.SEMANTIC_DIMENSION_MIN,
            "short_worthiness_floor": scoring.SHORT_WORTHINESS_FLOOR,
            "single_question_focus_min": scoring.SINGLE_QUESTION_FOCUS_MIN,
            "quality_floor": scoring.QUALITY_FLOOR,
        },
        "candidates": rows,
    }


def pool_summary(db: Session, run: TopicDiscoveryRun | None) -> dict[str, Any] | None:
    """Accepted vs. rejected candidates of one pool, with rejection reasons counted."""
    if run is None:
        return None
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for candidate_id in run.ranked_candidate_ids or []:
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is None:
            continue
        statuses[record.status] += 1
        quality = (record.score_breakdown or {}).get("quality") or {}
        row = {
            "question": record.question,
            "topic": record.topic,
            "final_score": record.final_score,
            "universal_accessibility": quality.get("universal_accessibility"),
            "prior_knowledge": quality.get("prior_knowledge") or [],
            "mass_audience_quality": quality.get("mass_audience"),
            "semantic": quality.get("semantic") or {},
            "short_worthiness": quality.get("short_worthiness") or {},
            "sources": sorted({str(item.get("source")) for item in record.source_signals or []}),
            "transformation": (record.provenance or {}).get("transformation"),
        }
        if record.rejection_reasons:
            reasons.update(record.rejection_reasons)
            rejected.append({**row, "reasons": list(record.rejection_reasons)})
        else:
            accepted.append({**row, "status": record.status})
    chain = [run]
    parent_id = _evaluation_detail(run).get("broadened_from")
    parent = db.get(TopicDiscoveryRun, parent_id) if parent_id else None
    if parent is not None:
        chain.append(parent)
    evaluation = {
        "evaluated": sum(evaluated_in(item) for item in chain),
        "ai_requests": sum(ai_requests_in(item) for item in chain),
        "ai_request_budget": AI_REQUEST_BUDGET,
        "curation_requests": sum(int(_evaluation_detail(item).get("curation_requests") or 0) for item in chain),
        "remaining_raw_groups": _evaluation_detail(run).get("remaining_raw_groups"),
        "prefiltered": int(_evaluation_detail(chain[-1]).get("prefiltered") or 0),
        "prefiltered_topics": list(_evaluation_detail(chain[-1]).get("prefiltered_topics") or []),
        # Not judged in the latest pass (timeout / budget / deadline): "not evaluated", never "low quality".
        "unevaluated": dict(_evaluation_detail(run).get("unevaluated") or {}),
        "unevaluated_topics": list(_evaluation_detail(run).get("unevaluated_topics") or []),
        "curator_batch_size": _evaluation_detail(run).get("curator_batch_size"),
        # The work order the AI budget followed (first pass): cheap evidence only.
        "curation_order": list(_evaluation_detail(chain[-1]).get("curation_order") or []),
        "broadened": parent is not None,
    }
    semantic_reports = [semantic_report(item) for item in chain if semantic_report(item)]
    semantic_summary = {
        "status": next((item["status"] for item in semantic_reports if item.get("status") != "ok"), semantic_reports[0]["status"] if semantic_reports else None),
        # Every curator request of this pool, oldest first: size, seconds, ok/timeout/failed, tokens.
        "batches": [batch for item in reversed(semantic_reports) for batch in (item.get("detail") or {}).get("batches") or []],
        "curator_version": semantic.SEMANTIC_CURATOR_VERSION,
        "enabled": any((item.get("detail") or {}).get("enabled") for item in semantic_reports),
        "requests": sum(int(item.get("calls") or 0) for item in semantic_reports),
        "curated": sum(int((item.get("detail") or {}).get("curated") or 0) for item in semantic_reports),
        "cached": sum(int((item.get("detail") or {}).get("cached") or 0) for item in semantic_reports),
        "errors": [item["error"] for item in semantic_reports if item.get("error")],
    }
    return {
        "score_version": run.score_version,
        "transformation": run.transformation,
        "transformation_version": TRANSFORMATION_VERSION,
        "raw_topics": run.raw_topic_count,
        "evaluated": len(accepted) + len(rejected),
        "evaluation": evaluation,
        "evaluation_budget": EVALUATION_BUDGET,
        "semantic_validation": semantic_summary,
        "transformation_failures": reasons.get("question_no_question_transformation", 0),
        "accepted": len(accepted),
        "available": sum(1 for item in accepted if item["status"] in {"pooled", "proposed"}),
        "rejected": len(rejected),
        "rejection_reasons": dict(reasons.most_common()),
        "statuses": dict(statuses),
        "accepted_candidates": accepted[:20],
        "rejected_candidates": rejected[:20],
    }


def diagnose(db: Session, settings: Settings, *, now: datetime | None = None) -> str:
    """Which state Home suggestions are in, as one explicit code."""
    now = now or _now()
    version = resolve_weights(settings)[1]
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    if _FLIGHT.locked():
        return "discovery_running"
    if WARMUP["state"] == "failed" and (run is None or run.status in {"failed", "running"}):
        return "warmup_failed"
    if run is None:
        return "no_pool_yet"
    if run.status == "unavailable":
        return "provider_failure"
    if run.status in {"failed", "running"}:
        usable = current_run(db, now, version, semantic.semantic_enabled(settings))
        if usable is None:
            # "running" without a flight: its discovery was abandoned before it could record anything.
            return "discovery_timed_out" if run.status == "running" else "discovery_failed"
        run = usable
    if run.score_version != version:
        return "stale_pool_other_version"
    if current_run(db, now, version, semantic.semantic_enabled(settings)) is None:
        return "pool_expired_refreshes_on_next_request"
    summary = pool_summary(db, run) or {}
    if not summary.get("available"):
        reasons = summary.get("rejection_reasons") or {}
        if (summary.get("evaluation") or {}).get("unevaluated", {}).get("curator_failed") and not summary.get("rejected"):
            return "curator_failed"  # nothing was judged: not evaluated, not "no good topics"
        rejected = max(1, int(summary.get("rejected") or 0))
        if reasons.get("question_no_question_transformation", 0) * 2 >= rejected:
            return "question_transformation_failed"
        if any(reason.startswith("semantic_") for reason in reasons):
            return "semantic_rejected_all"
        if any(reason.startswith("unvalidated_") for reason in reasons):
            return "local_strict_rejected_all"
        if reasons.get("below_quality_floor"):
            return "quality_floor_rejected_all"
        if reasons.get("requires_prior_knowledge"):
            return "prior_knowledge_rejected_all"
        return "no_usable_candidates"
    if summary["available"] < 3:
        return "fewer_than_three_candidates"
    return "ok"


def budgets(settings: Settings | None) -> dict[str, Any]:
    """Every explicit request/candidate budget of one discovery (and its one widening pass)."""
    from . import evergreen
    from .sources import BraveNewsSource, WikipediaPageviewsSource, YouTubeTrendingSource

    return {
        "wikipedia_requests": f"1-2 top list + {WikipediaPageviewsSource.META_LIMIT // 20} metadata + <= {WikipediaPageviewsSource.HISTORY_LIMIT} histories",
        "evergreen_requests": evergreen.SAMPLE_SIZE,
        "evergreen_catalog": {"version": evergreen.CATALOG_VERSION, "subjects": len(evergreen.CATALOG)},
        "brave_requests": len(BraveNewsSource.QUERIES),
        "youtube_chart_categories": len(YouTubeTrendingSource.CATEGORIES),
        "youtube_quota_units": None if settings is None else int(settings.topic_youtube_quota_budget),
        "youtube_search_probes": None if settings is None else int(settings.topic_youtube_search_probes),
        "candidates_evaluated": EVALUATION_BUDGET,
        "target_accepted": TARGET_ACCEPTED,
        "ai_requests": AI_REQUEST_BUDGET,
        "widen_once": {"ai_requests": WIDEN_AI_REQUESTS, "evaluations": WIDEN_EVALUATIONS},
        "curator_batch_size": None if settings is None else int(getattr(settings, "topic_curator_batch_size", semantic.MAX_CURATION_BATCH)),
        "timeouts_seconds": {"provider": runtime.PROVIDER_TIMEOUT_SECONDS, "curator": runtime.CURATOR_TIMEOUT_SECONDS,
                             "ai_deadline": runtime.AI_DEADLINE_SECONDS, "flight": runtime.FLIGHT_MAX_SECONDS},
    }


def discovery_status(db: Session, settings: Settings | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Internal state of Topic Intelligence: diagnosis, sources, pool, rejections, caches."""
    now = now or _now()
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    caches = db.scalars(
        select(TopicSourceCache).where(TopicSourceCache.provider != semantic.CACHE_PROVIDER).order_by(TopicSourceCache.provider)
    ).all()
    semantic_cached = db.scalar(select(func.count()).select_from(TopicSourceCache).where(TopicSourceCache.provider == semantic.CACHE_PROVIDER)) or 0
    report_settings = settings
    return {
        "diagnosis": diagnose(db, report_settings, now=now) if report_settings is not None else None,
        "budgets": budgets(report_settings),
        "current_score_version": resolve_weights(report_settings)[1] if report_settings is not None else None,
        "discovery_running": _FLIGHT.locked(),
        # Live while discovery runs: stage, provider, elapsed, timeouts, lock state.
        "discovery": _FLIGHT.snapshot(),
        "warmup": dict(WARMUP),
        "config": None if report_settings is None else {
            "ai_mode": report_settings.clipforge_ai_mode,
            "question_rewriting": "curator" if semantic.semantic_enabled(report_settings) else "template",
            "topic_ai": "enabled (OpenAI key; independent of ai_mode)" if semantic.semantic_enabled(report_settings) else "unavailable (strict local acceptance)",
            "semantic_curator_version": semantic.SEMANTIC_CURATOR_VERSION,
            "brave_configured": bool(report_settings.brave_search_api_key),
            "youtube_connected": active_connection(db) is not None,
        },
        "pool": pool_summary(db, run),
        # Curated sample for threshold calibration (per-candidate rows: diagnostics --curated).
        "calibration": {key: value for key, value in (curation_calibration(db, run) or {}).items() if key != "candidates"} or None,
        "semantic_cache_entries": int(semantic_cached),
        "run": None if run is None else {
            **_pool_info(db, run),
            "score_version": run.score_version,
            "youtube_quota_units": run.youtube_quota_units,
            "fresh": current_run(db, now) is not None,
        },
        "caches": [
            {
                "provider": entry.provider,
                "fetched_at": _utc(entry.fetched_at),
                "expires_at": _utc(entry.expires_at),
                "fresh": (_utc(entry.expires_at) or now) > now,
                "quota_units": entry.quota_units,
            }
            for entry in caches
        ],
    }


# ---------------------------------------------------------------------------
# Generation handoff (provenance only; the existing pipeline does the rest)
# ---------------------------------------------------------------------------


def _normalized(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def resolve_topic_provenance(db: Session, payload: ProjectCreate, *, now: datetime | None = None) -> ProjectCreate:
    """Server-authoritative provenance; whatever a client put in ``topic_provenance`` is replaced."""
    now = now or _now()
    if payload.topic_source != "topic_intelligence":
        return payload.model_copy(update={"topic_provenance": {"topic_source": "manual"}, "topic_candidate_id": None})
    if not payload.topic_candidate_id:
        raise TopicHandoffError("missing_candidate", "A topic suggestion id is required.")
    record = db.get(TopicCandidateRecord, payload.topic_candidate_id)
    if record is None:
        raise TopicHandoffError("unknown_candidate", "This topic suggestion no longer exists. Generate a new one.")
    if record.status == "used":
        raise TopicHandoffError("already_used", "This topic was already sent to generation.")
    if record.rejection_reasons:
        raise TopicHandoffError("rejected_candidate", "This topic suggestion was rejected and cannot be generated.")
    provenance = {
        "topic_source": "topic_intelligence",
        "candidate_id": record.candidate_id,
        "score_version": record.score_version,
        "final_score": record.final_score,
        "confidence": record.confidence,
        "score_breakdown": record.score_breakdown,
        "signals_used": sorted(
            name for name, signal in (record.signals or {}).items()
            if isinstance(signal, dict) and signal.get("confidence") != "unavailable"
        ),
        "sources": sorted({str(item.get("source")) for item in record.source_signals or []}),
        "topic": record.topic,
        "niche": record.niche,
        "proposed_question": record.question,
        "edited": _normalized(payload.prompt) != _normalized(record.question),
        "run_id": record.run_id,
        "selected_at": now.isoformat(),
    }
    return payload.model_copy(update={"topic_provenance": provenance})


def mark_topic_used(db: Session, payload: ProjectCreate, project_id: str, *, now: datetime | None = None) -> None:
    if payload.topic_source != "topic_intelligence" or not payload.topic_candidate_id:
        return
    record = db.get(TopicCandidateRecord, payload.topic_candidate_id)
    if record is None:
        return
    record.status = "used"
    record.selected_at = now or _now()
    record.used_project_id = project_id
    db.commit()
