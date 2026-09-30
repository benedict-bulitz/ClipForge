"""Topic Intelligence orchestration: discovery pool, proposals and the handoff.

* ``next_topic``: reuse the fresh candidate pool (``topic_pool_ttl_minutes``)
  or run one discovery refresh (single-flight), then propose the best unused
  candidate.  Asking again returns the same proposal until it is skipped or used.
* ``skip_topic`` ("Try another"): remember the skip, propose the next
  candidate; an exhausted pool triggers one refresh.
* ``resolve_topic_provenance`` / ``mark_topic_used``: the only coupling to
  generation.  A confirmed topic goes through the existing
  ``POST /api/generation-jobs`` like a typed question, with provenance attached.
"""
from __future__ import annotations

import threading
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
from .cache import CallMeter, prune_expired
from .candidate import RawTopic, Signal, TopicCandidate, TopicGroup, candidate_id_for
from .scoring import explain, rank, resolve_weights, score_candidate
from .signals import assessed, channel_fit, competition_estimate, merge_trend, suitability
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
from .text import NICHE_PRIORS, similarity, topic_key
from .transform import MAX_BATCH, Transformed, transform_topics

SKIP_MEMORY = timedelta(hours=24)
RETENTION = timedelta(days=14)
PREFILTER_FLAGS = frozenset({"person", "tragedy", "disambiguation"})
UNAVAILABLE_MESSAGE = "Topic discovery is temporarily unavailable."
EXHAUSTED_MESSAGE = "No further topic candidates right now. Try again later or enter your own question."
_FLIGHT = threading.Lock()


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
    return round((trend.value or 0.0) + 0.5 * (max(outliers) if outliers else 0.0) + 0.1 * len(group.sources), 4)


def _best_outlier(group: TopicGroup) -> Signal:
    available = [item.outlier for item in group.sightings if item.outlier is not None and item.outlier.available]
    if not available:
        reasons = [item.outlier.evidence.get("reason") for item in group.sightings if item.outlier is not None]
        return Signal.unavailable(next((reason for reason in reasons if reason), "no_video_evidence"))
    return max(available, key=lambda signal: (signal.value or 0.0, signal.evidence.get("sample_size") or 0))


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
    }
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
    for record in db.scalars(select(TopicCandidateRecord).where(TopicCandidateRecord.status.in_(("used", "skipped")))).all():
        if record.status == "skipped" and (_utc(record.skipped_at) or now) < now - SKIP_MEMORY:
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
            record.status == "skipped" and (_utc(record.skipped_at) or now) >= now - SKIP_MEMORY
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


def discover(db: Session, settings: Settings, deps: DiscoveryDeps, *, now: datetime | None = None) -> TopicDiscoveryRun:
    """One bounded discovery refresh: sources -> questions -> signals -> score -> pool."""
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
    usable_sources = [report for report in reports if report.status in {"ok", "cached"} and report.items > 0]
    degraded = any(report.status == "failed" for report in reports)
    if not raw or not usable_sources:
        run.status = "unavailable"
        run.sources = [report.to_dict() for report in reports]
        run.completed_at = now
        run.youtube_quota_units = meter.quota_units
        db.commit()
        return run
    excluded_ids, excluded_groups = _recent_status_keys(db, now)
    groups = [
        group for group in group_topics(raw)
        if group.key not in excluded_groups and not group.flags & PREFILTER_FLAGS
    ]
    groups.sort(key=lambda group: (-_preliminary(group), group.key))
    batch = groups[:MAX_BATCH]
    transformed, method, transform_error = transform_topics(batch, settings)
    run.transformation = method
    history = history_module.load_history(db)
    own_priors, own_default = history_module.own_performance_priors(db, settings)
    candidates: list[TopicCandidate] = []
    extras: dict[str, tuple[list[str], list[str]]] = {}
    for group, item in zip(batch, transformed, strict=True):
        candidate = build_candidate(group, item, history=history, own_priors=own_priors, own_default=own_default, now=now, run_id=run.id)
        if candidate.candidate_id in excluded_ids:
            continue
        extras[candidate.candidate_id] = (item.issues, item.flags)
        score_candidate(candidate, weights=weights, version=version, now=now, issues=item.issues, flags=item.flags, degraded_sources=degraded)
        candidates.append(candidate)
    # Competition probes: only for the strongest usable candidates, bounded per refresh.
    probe_report = SourceReport(deps.probe.name, "skipped", error=None if deps.probe.available else "YouTube is not connected")
    if deps.probe.available:
        calls_before, units_before = meter.calls, meter.quota_units
        limit = max(0, int(settings.topic_youtube_search_probes))
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
    run.ranked_candidate_ids = list(dict.fromkeys(candidate.candidate_id for candidate in ranked))
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


def current_run(db: Session, now: datetime) -> TopicDiscoveryRun | None:
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    if run is None or run.status == "unavailable" or (_utc(run.expires_at) or now) <= now:
        return None
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
        run = None if refresh else current_run(db, now)
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


def discovery_status(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Internal freshness view: latest run and cache entries per provider."""
    now = now or _now()
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    caches = db.scalars(select(TopicSourceCache).order_by(TopicSourceCache.provider)).all()
    return {
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
