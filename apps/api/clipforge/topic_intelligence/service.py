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
from . import semantic
from .cache import CallMeter, prune_expired
from .candidate import RawTopic, Signal, TopicCandidate, TopicGroup, candidate_id_for
from .scoring import (
    RankedItem,
    diversify,
    explain,
    rank,
    rank_key,
    resolve_weights,
    score_candidate,
)
from .signals import (
    accessibility,
    assessed,
    broad_appeal,
    channel_fit,
    competition_estimate,
    merge_trend,
    question_form,
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
    content_tokens,
    de_shout,
    extract_question,
    has_universal_subject,
    prior_knowledge_flags,
    question_flags,
    question_mechanism,
    similarity,
    topic_key,
    topic_obscurity_flags,
)
from .transform import (
    MAX_BATCH,
    TRANSFORMATION_VERSION,
    Transformed,
    curated_transform,
    deterministic_transform,
)

SKIP_MEMORY = timedelta(hours=24)
RETENTION = timedelta(days=14)
PREFILTER_FLAGS = frozenset({"person", "tragedy", "disambiguation"})
# Obvious garbage never reaches the curator (calendar pages have no story of their own).
PREFILTER_TOPIC_FLAGS = frozenset({"date_page"})
OBSCURE_ENTITY_FLAGS = frozenset({"identifier", "acronym", "compound_proper_name", "foreign_proper_name", "isolated_event", "date_page"})
UNAVAILABLE_MESSAGE = "Topic discovery is temporarily unavailable."
EXHAUSTED_MESSAGE = "No further topic candidates right now. Try again later or enter your own question."
_FLIGHT = threading.Lock()
logger = logging.getLogger(__name__)
DISCOVERY_RETRY_SECONDS = 3
# A pool that is short only because of the quality floor is not re-discovered on
# every chip refill (each refresh may cost a question-rewriting call).
MIN_REFRESH_INTERVAL = timedelta(minutes=10)
BROADENING_REPORT = "broadening_pass"
EVALUATION_REPORT = "candidate_evaluation"
# Raw-pool backfill: keep evaluating already-discovered topics (no new provider
# calls) until this many candidates clear every gate, within a hard budget per
# pool (shared with its broadening pass).  With OpenAI enabled one batch = one
# request, so the budget also bounds requests: ceil(60 / MAX_BATCH) = 3.
TARGET_ACCEPTED = 9
EVALUATION_BUDGET = 60
# All AI requests of one pool (question rewriting + semantic validation), shared with its
# broadening pass.  Each request carries up to 20 topics/questions.
AI_REQUEST_BUDGET = 3
SEMANTIC_REPORT = "semantic_validation"
# Last startup warm-up (idle | running | done | failed), for diagnostics.
WARMUP: dict[str, Any] = {"state": "idle", "started_at": None, "finished_at": None, "result": None, "error": None}


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
        try:
            result = source.discover(ctx)
        except SourceSkipped as exc:
            reports.append(SourceReport(source.name, "skipped", error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - one failing source never breaks discovery
            ctx.db.rollback()
            reports.append(SourceReport(source.name, "failed", error=f"{type(exc).__name__}: {str(exc)[:160]}"))
            continue
        topics.extend(result.topics)
        if result.report is not None:
            reports.append(result.report)
    return topics, reports


def group_topics(topics: list[RawTopic]) -> list[TopicGroup]:
    """Merge sightings of the same topic (exact key, then near-identical titles)."""
    groups: list[TopicGroup] = []
    for topic in topics:
        for group in groups:
            if group.key == topic.key or similarity(group.title, topic.title) >= history_module.DUPLICATE_THRESHOLD:
                group.sightings.append(topic)
                break
        else:
            groups.append(TopicGroup(topic.key, [topic]))
    return groups


def _preliminary(group: TopicGroup) -> float:
    trend = merge_trend([item.trend for item in group.sightings if item.trend is not None])
    outliers = [item.outlier.value or 0.0 for item in group.sightings if item.outlier is not None and item.outlier.available]
    # Titles that already carry a question are cheap and likely to transform: try them earlier.
    question_like = any(extract_question(item.title)[0] for item in group.sightings)
    return round((trend.value or 0.0) + 0.5 * (max(outliers) if outliers else 0.0) + 0.1 * len(group.sources) + (0.3 if question_like else 0.0), 4)


def _best_outlier(group: TopicGroup) -> Signal:
    available = [item.outlier for item in group.sightings if item.outlier is not None and item.outlier.available]
    if not available:
        reasons = [item.outlier.evidence.get("reason") for item in group.sightings if item.outlier is not None]
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
) -> tuple[dict[str, Signal], dict[str, Any]]:
    """Mass-audience features of one question (signals only; scoring decides their worth)."""
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
    has_article = any(item.kind == "article" for item in group.sightings)
    research_value = assessment.get("researchability")
    if research_value is not None and has_article:
        research_value = max(research_value, 0.8)
    signals = {
        "trend": merge_trend([item.trend for item in group.sightings if item.trend is not None]),
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
    }
    features, feature_evidence = quality_signals(question, group.title, group.description(), niche, assessment, method, transformed.notes)
    signals.update(features)
    return TopicCandidate(
        candidate_id=candidate_id_for(topic_key(question) if question else f"raw:{group.key}"),
        topic=group.title,
        question=question,
        rationale="",
        source_signals=[item.source_signal() for item in group.sightings],
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
            **feature_evidence,
        },
    )


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


def _recent_status_keys(db: Session, now: datetime) -> tuple[set[str], set[str]]:
    """(candidate ids to keep out, group keys to keep out): used ever, skipped within 24 h."""
    ids: set[str] = set()
    groups: set[str] = set()
    for record in db.scalars(select(TopicCandidateRecord).where(TopicCandidateRecord.status.in_(("used", "skipped", "picked")))).all():
        if record.status in {"skipped", "picked"} and (_utc(record.skipped_at) or now) < now - SKIP_MEMORY:
            continue
        ids.add(record.candidate_id)
        key = (record.provenance or {}).get("group_key")
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
    """
    now = now or _now()
    _prune(db, now)
    weights, version = resolve_weights(settings)
    meter = CallMeter(quota_budget=max(0, int(settings.topic_youtube_quota_budget)))
    ctx = DiscoveryContext(db=db, settings=settings, now=now, meter=meter)
    sequence = int(db.scalar(select(func.max(TopicDiscoveryRun.sequence))) or 0) + 1
    run = TopicDiscoveryRun(
        sequence=sequence, score_version=version, weights=weights, started_at=now,
        expires_at=now + timedelta(minutes=max(1, int(settings.topic_pool_ttl_minutes))),
    )
    db.add(run)
    db.commit()  # a failing source rolls its own work back, never the run record
    raw, reports = _collect(ctx, deps.sources)
    reports.extend(deps.extra_reports)
    run.raw_topic_count = len(raw)
    usable_sources = [report for report in reports if report.status in {"ok", "cached", "partial"} and report.items > 0]
    degraded = any(report.status == "failed" for report in reports)
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
    for group in group_topics(raw):
        if group.key in excluded_groups or group.flags & PREFILTER_FLAGS:
            continue
        if topic_obscurity_flags(group.title, group.description()) & PREFILTER_TOPIC_FLAGS:
            prefiltered.append(group.title)  # cheap deterministic prefilter: never worth an AI call
            continue
        groups.append(group)
    if semantic.semantic_enabled(settings):
        # The curator can turn statements into grounded questions: discovery signals order the work.
        groups.sort(key=lambda group: (-_preliminary(group), group.key))
    else:
        # Local mode: a topic without a locally derivable question can never pass, so topics that
        # can become one are evaluated first (ordering only - the rest still follow within budget).
        groups.sort(key=lambda group: (deterministic_transform(group).method == "none", -_preliminary(group), group.key))
    budget = EVALUATION_BUDGET - (evaluated_in(broaden_from) if broaden_from is not None else 0)
    history = history_module.load_history(db)
    own_priors, own_default = history_module.own_performance_priors(db, settings)
    candidates: list[TopicCandidate] = []
    extras: dict[str, tuple[list[str], list[str]]] = {}
    methods: list[str] = []
    semantic_errors: list[str] = []
    evaluated = curation_requests = curated = cached_curations = 0
    ai_left = AI_REQUEST_BUDGET - (ai_requests_in(broaden_from) if broaden_from is not None else 0)
    semantic_on = semantic.semantic_enabled(settings)
    batch: list[TopicGroup] = []
    accepted = len(carried)
    queue = list(groups)
    # Backfill through the raw pool: the discovery pre-rank orders the work, it is not a gate.
    # One round = <= 20 raw topics -> ONE curator request (question + judgement) -> scoring.
    while queue and evaluated < budget and accepted < TARGET_ACCEPTED:
        batch = queue[: min(MAX_BATCH, budget - evaluated)]
        queue = queue[len(batch):]
        evaluated += len(batch)
        transformed: list[Transformed] = []
        if semantic_on:
            outcome = semantic.curate(db, settings, batch, requests_left=ai_left, now=now)
            curation_requests += outcome.requests
            ai_left -= outcome.requests
            cached_curations += outcome.cached
            semantic_errors.extend(outcome.errors)
            for group in batch:
                status = outcome.statuses.get(group.key, "not_curated")
                judgement = outcome.judgements.get(group.key)
                if judgement is not None:
                    transformed.append(curated_transform(group, judgement, semantic.curated_signal(judgement, status=status)))
                    curated += status == "curated"
                    methods.append("curator")
                    continue
                item = deterministic_transform(group)
                item.semantic = (
                    semantic.unavailable("semantic_curator_failed", status="failed") if status == "failed"
                    else semantic.unavailable("ai_budget_exhausted", status="not_validated")
                )
                transformed.append(item)
                methods.append("template")
        else:
            for group in batch:
                item = deterministic_transform(group)
                item.semantic = semantic.unavailable("semantic_curator_unavailable")
                transformed.append(item)
            methods.append("template")
        for group, item in zip(batch, transformed, strict=True):
            candidate = build_candidate(group, item, history=history, own_priors=own_priors, own_default=own_default, now=now, run_id=run.id)
            if candidate.candidate_id in excluded_ids:
                continue
            extras[candidate.candidate_id] = (item.issues, item.flags)
            score_candidate(candidate, weights=weights, version=version, now=now, issues=item.issues, flags=item.flags, degraded_sources=degraded)
            candidates.append(candidate)
        accepted = len(carried) + sum(1 for candidate in candidates if not candidate.rejected)
    method = "curator" if "curator" in methods else "template"
    run.transformation = f"{method}:{TRANSFORMATION_VERSION}"
    transform_error = None
    # Competition probes: only for the strongest usable candidates, bounded per refresh.
    probe_report = SourceReport(deps.probe.name, "skipped", error=None if deps.probe.available else "YouTube is not connected")
    if deps.probe.available:
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
            competition, outlier = competition_estimate(candidate.question, candidate.topic, payload.get("videos") or [])
            candidate.signals["competition"] = competition
            if not candidate.signal("outlier").available and outlier.available:
                candidate.signals["outlier"] = outlier
            candidate.source_signals.append({"source": deps.probe.name, "kind": "search", "title": payload.get("query"), "metrics": competition.evidence})
        if probe_report.status == "ok" and probe_report.items and not fetched:
            probe_report.status = "cached"
        probe_report.calls = meter.calls - calls_before
        probe_report.quota_units = meter.quota_units - units_before
    reports.append(probe_report)
    reports.append(SourceReport(
        EVALUATION_REPORT, "ok", items=evaluated, calls=curation_requests,
        error=f"raw_groups={len(groups)} budget={budget} target={TARGET_ACCEPTED} accepted={accepted} remaining={len(queue)}",
        detail={"ai_requests": curation_requests, "ai_request_budget": AI_REQUEST_BUDGET, "curation_requests": curation_requests,
                "remaining_raw_groups": len(queue), "prefiltered": len(prefiltered), "prefiltered_topics": prefiltered[:10],
                "broadened_from": broaden_from.id if broaden_from is not None else None},
    ))
    reports.append(SourceReport(
        SEMANTIC_REPORT,
        "ok" if semantic_on and not semantic_errors else "failed" if semantic_errors else "unavailable",
        error="; ".join(dict.fromkeys(semantic_errors)) or (None if semantic_on else "No OpenAI key (or disabled): strict local acceptance"),
        calls=curation_requests,
        items=curated,
        detail={"curator_version": semantic.SEMANTIC_CURATOR_VERSION, "enabled": semantic_on,
                "curated": curated, "cached": cached_curations},
    ))
    if broaden_from is not None:
        reports.append(SourceReport(BROADENING_REPORT, "ok", items=evaluated))
    for candidate in candidates:
        issues, flags = extras[candidate.candidate_id]
        score_candidate(candidate, weights=weights, version=version, now=now, issues=issues, flags=flags, degraded_sources=degraded)
    ranked = rank(candidates)
    kept: list[TopicCandidate] = []
    for candidate in ranked:
        if not candidate.rejected and any(similarity(candidate.question, other.question) >= history_module.DUPLICATE_THRESHOLD for other in kept):
            candidate.rejection_reasons.append("duplicate_in_pool")
        if not candidate.rejected:
            kept.append(candidate)
        candidate.rationale = rationale_for(candidate)
    ranked = rank(ranked)
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


def current_run(
    db: Session, now: datetime, version: str | None = None, semantic_ready: bool | None = None,
) -> TopicDiscoveryRun | None:
    """The fresh pool; a pool scored by another score version is never reused."""
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
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


def serialize_candidate(record: TopicCandidateRecord) -> dict[str, Any]:
    breakdown = record.score_breakdown or {}
    components = breakdown.get("components") or {}
    return {
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
    with _FLIGHT:  # single-flight: concurrent clicks share one refresh
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
        return {"status": "proposed", "message": None, "candidate": serialize_candidate(record), "pool": _pool_info(db, run)}


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
    taken = [record.question for record in excluded_records]
    shown = [
        (record.niche, str((record.provenance or {}).get("mechanism") or "other"))
        for record in excluded_records if record.status == "proposed"
    ]
    chosen: list[TopicCandidateRecord] = []
    for candidate_id in run.ranked_candidate_ids or []:
        if candidate_id in exclude:
            continue
        record = db.get(TopicCandidateRecord, candidate_id)
        if record is None or record.status not in {"pooled", "proposed"} or record.rejection_reasons:
            continue
        if any(similarity(record.question, other) >= history_module.DUPLICATE_THRESHOLD for other in taken):
            continue
        chosen.append(record)
        taken.append(record.question)
    by_id = {record.candidate_id: record for record in chosen}
    items = [
        RankedItem(record.candidate_id, record.final_score, record.niche, str((record.provenance or {}).get("mechanism") or "other"))
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
        return {
            "status": "discovering",
            "message": "Topic discovery is running.",
            "candidates": [],
            "retry_after_seconds": DISCOVERY_RETRY_SECONDS,
            "pool": None,
            "summary": None,
        }
    try:
        return _suggestions_locked(db, settings, deps, count=count, excluded=excluded, now=now)
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
        refreshed = discover(db, settings, deps, now=now)
        if refreshed.status != "unavailable":
            run = refreshed
            records = _available_records(db, run, excluded)
    if (
        len(records) < count and not was_broadened(run) and evaluated_in(run) < EVALUATION_BUDGET
        and _evaluation_detail(run).get("remaining_raw_groups", 1) > 0
    ):
        # Too few candidates clear the quality floor: evaluate the next raw
        # topics once per pool, rather than serving weak filler.
        broadened = discover(db, settings, deps, now=now, broaden_from=run)
        if broadened.status != "unavailable":
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
        "candidates": [serialize_candidate(record) for record in records],
        "pool": _pool_info(db, run),
        # Why fewer than requested: how many were evaluated and why they were rejected.
        "summary": pool_summary(db, run),
    }


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
        with _FLIGHT:
            if current_run(db, now, resolve_weights(settings)[1], semantic.semantic_enabled(settings)) is not None:
                return None
            return discover(db, settings, deps_factory(db), now=now).status


def run_warmup(session_factory: Any, settings: Settings, deps_factory: Any) -> None:
    """The warm-up itself, with its state recorded in ``WARMUP`` for diagnostics."""
    WARMUP.update(state="running", started_at=_now().isoformat(), finished_at=None, result=None, error=None)
    try:
        result = warm_pool(session_factory, settings, deps_factory)
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

QUALITY_FEATURE_SIGNALS = ("broad_appeal", "accessibility", "question_form")


def candidate_from_record(record: TopicCandidateRecord) -> TopicCandidate:
    """Rebuild a candidate from its persisted signals (older versions get the v2 text features)."""
    signals = {name: Signal.from_dict(payload) for name, payload in (record.signals or {}).items()}
    provenance = dict(record.provenance or {})
    missing = [name for name in QUALITY_FEATURE_SIGNALS if name not in signals]
    if missing:
        features, evidence = quality_signals(
            record.question, record.topic, "", record.niche, {}, str(provenance.get("transformation") or "template"),
            set(provenance.get("extraction_notes") or []),
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
        "broadened": parent is not None,
    }
    semantic_reports = [semantic_report(item) for item in chain if semantic_report(item)]
    semantic_summary = {
        "status": next((item["status"] for item in semantic_reports if item.get("status") != "ok"), semantic_reports[0]["status"] if semantic_reports else None),
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
    if WARMUP["state"] == "failed" and run is None:
        return "warmup_failed"
    if run is None:
        return "no_pool_yet"
    if run.status == "unavailable":
        return "provider_failure"
    if run.score_version != version:
        return "stale_pool_other_version"
    if current_run(db, now, version, semantic.semantic_enabled(settings)) is None:
        return "pool_expired_refreshes_on_next_request"
    summary = pool_summary(db, run) or {}
    if not summary.get("available"):
        reasons = summary.get("rejection_reasons") or {}
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
        "current_score_version": resolve_weights(report_settings)[1] if report_settings is not None else None,
        "discovery_running": _FLIGHT.locked(),
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
