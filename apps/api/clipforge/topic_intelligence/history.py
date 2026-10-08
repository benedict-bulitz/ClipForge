from __future__ import annotations

"""What ClipForge already made: novelty memory and the optional own-performance prior.

Reads the existing stores only (projects, queued requests, YouTube uploads,
the retained Learning Archive and analytics snapshots) - it never keeps a
second copy of analytics.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    GenerationJob,
    Project,
    ProjectRevision,
    YouTubeLearningArchive,
    YouTubeUpload,
)
from ..youtube.connection import active_connection
from ..youtube.learning import api_snapshots, eligible_uploads, latest_with_data, metric_value
from .candidate import Signal, clamp
from .text import classify_niche, content_tokens, question_equivalence, similarity

DUPLICATE_THRESHOLD = 0.72  # same question in other words -> rejected
ANSWER_REPEAT_THRESHOLD = 0.8
RECENT_WINDOW = timedelta(days=60)
OLDER_WEIGHT = 0.85  # older topics still count, slightly less
HISTORY_LIMIT = 200
ANSWER_LIMIT = 40  # answers are read from revision state: only the most recent projects


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class HistoryItem:
    text: str
    kind: str  # project | queued_request | upload | archive | answer
    ref_id: str
    at: datetime | None
    niche: str = "unknown"


def _answer_claim(state: dict[str, Any]) -> str | None:
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else {}
    answer_id = arc.get("primary_answer_id")
    for fact in state.get("facts") or []:
        if isinstance(fact, dict) and answer_id and fact.get("id") == answer_id:
            return str(fact.get("claim") or "") or None
    return None


def load_history(db: Session) -> list[HistoryItem]:
    items: list[HistoryItem] = []
    projects = db.scalars(select(Project).order_by(Project.created_at.desc()).limit(HISTORY_LIMIT)).all()
    for index, project in enumerate(projects):
        text = project.original_prompt or project.title
        items.append(HistoryItem(text, "project", project.id, _utc(project.created_at), classify_niche(text)[0]))
        if project.title and project.title != project.original_prompt:
            items.append(HistoryItem(project.title, "project", project.id, _utc(project.created_at), classify_niche(project.title)[0]))
        if index >= ANSWER_LIMIT:
            continue
        revision = db.scalar(
            select(ProjectRevision).where(
                ProjectRevision.project_id == project.id, ProjectRevision.number == project.current_revision
            )
        )
        claim = _answer_claim(revision.state) if revision is not None and isinstance(revision.state, dict) else None
        if claim:
            items.append(HistoryItem(claim, "answer", project.id, _utc(project.created_at)))
    project_ids = {project.id for project in projects}
    for job in db.scalars(select(GenerationJob).where(GenerationJob.status.in_(("queued", "running")))).all():
        prompt = str((job.request_payload or {}).get("prompt") or "")
        if prompt and job.project_id not in project_ids:
            items.append(HistoryItem(prompt, "queued_request", job.id, _utc(job.created_at), classify_niche(prompt)[0]))
    for upload in db.scalars(select(YouTubeUpload).order_by(YouTubeUpload.created_at.desc()).limit(HISTORY_LIMIT)).all():
        if upload.title:
            items.append(HistoryItem(upload.title, "upload", upload.id, _utc(upload.uploaded_at or upload.created_at), classify_niche(upload.title)[0]))
    for archive in db.scalars(select(YouTubeLearningArchive).limit(HISTORY_LIMIT)).all():
        for text in {archive.prompt, archive.topic or "", archive.title}:
            if text:
                items.append(HistoryItem(text, "archive", archive.project_id, _utc(archive.archived_at), classify_niche(text)[0]))
    return items


def novelty_signal(question: str, topic: str, niche: str, history: list[HistoryItem], *, now: datetime) -> Signal:
    """1 = nothing like it before; the closest previous item is kept as evidence."""
    best: tuple[float, HistoryItem | None] = (0.0, None)
    answer_repeat: tuple[float, HistoryItem | None] = (0.0, None)
    question_tokens = content_tokens(question)
    topic_tokens = content_tokens(topic)
    for item in history:
        item_tokens = content_tokens(item.text)
        if item.kind == "answer":
            if len(question_tokens) >= 2:
                score = similarity(question_tokens, item_tokens)
                if score > answer_repeat[0]:
                    answer_repeat = (score, item)
            continue
        # V2: question vs question by semantic equivalence (both must share their concepts), so
        # "Warum ist der Mars rot?" repeats "Was macht den Mars rot?" but not "... Staubstürme?".
        score = max(question_equivalence(question, item.text), similarity(topic_tokens, item_tokens) if len(topic_tokens) >= 2 else 0.0)
        at = item.at
        if at is not None and now - at > RECENT_WINDOW:
            score *= OLDER_WEIGHT
        if score > best[0]:
            best = (score, item)
    value = 1.0 - best[0]
    evidence: dict[str, Any] = {"method": "soft_token_overlap_vs_history", "history_items": len(history)}
    if best[1] is not None:
        evidence["closest"] = {"text": best[1].text[:160], "kind": best[1].kind, "similarity": round(best[0], 3)}
    if best[0] >= DUPLICATE_THRESHOLD:
        evidence["duplicate_of"] = best[1].kind if best[1] else None
    if answer_repeat[0] >= ANSWER_REPEAT_THRESHOLD and answer_repeat[1] is not None:
        value -= 0.25
        evidence["repeats_previous_answer"] = {"text": answer_repeat[1].text[:160], "similarity": round(answer_repeat[0], 3)}
    recent_niches = [item.niche for item in sorted(
        (entry for entry in history if entry.kind == "project" and entry.at is not None),
        key=lambda entry: entry.at, reverse=True,  # type: ignore[arg-type,return-value]
    )[:5]]
    if niche != "unknown" and recent_niches.count(niche) >= 3:
        value -= 0.15
        evidence["niche_repetition"] = recent_niches.count(niche)
    return Signal(round(clamp(value), 4), "high", evidence, ["clipforge_history"])


def is_duplicate(signal: Signal) -> bool:
    return "duplicate_of" in signal.evidence


# ---------------------------------------------------------------------------
# Own performance: optional prior from the channel's real published videos.
# ---------------------------------------------------------------------------


def _upload_views(db: Session, upload: YouTubeUpload) -> float | None:
    snapshot = latest_with_data(api_snapshots(db, upload.id))
    views = metric_value(snapshot, "views")
    if views is None and upload.remote_view_count is not None:
        views = float(upload.remote_view_count)
    return views


def own_performance_priors(db: Session, settings: Settings) -> tuple[dict[str, Signal], Signal]:
    """(niche -> prior, default for other niches).  Unavailable, never zero, when data is thin."""
    connection = active_connection(db)
    if connection is None:
        return {}, Signal.unavailable("no_connected_channel")
    rows: list[tuple[str, float]] = []
    archives = {item.project_id: item for item in db.scalars(select(YouTubeLearningArchive)).all()}
    for upload in eligible_uploads(db, connection.channel_id):
        views = _upload_views(db, upload)
        if views is None:
            continue
        project = db.get(Project, upload.project_id)
        archive = archives.get(upload.project_id)
        text = " ".join(filter(None, [upload.title, project.original_prompt if project else None, archive.prompt if archive else None]))
        rows.append((classify_niche(text)[0], views))
    minimum = max(1, int(settings.youtube_baseline_min_sample))
    if len(rows) < minimum:
        return {}, Signal.unavailable("insufficient_own_analytics", sample_size=len(rows), min_sample=minimum)
    overall = statistics.median(views for _niche, views in rows) or 1.0
    priors: dict[str, Signal] = {}
    for niche in {niche for niche, _views in rows}:
        values = [views for item_niche, views in rows if item_niche == niche]
        if len(values) < 3:
            priors[niche] = Signal.unavailable("insufficient_niche_sample", sample_size=len(values))
            continue
        ratio = statistics.median(values) / max(1.0, overall)
        confidence = "high" if len(values) >= 12 else "medium" if len(values) >= 5 else "low"
        priors[niche] = Signal(
            round(clamp(0.5 + math.log2(max(ratio, 1e-6)) / 4), 4),
            confidence,
            {
                "method": "niche_median_views_vs_channel_median",
                "ratio": round(ratio, 2),
                "niche_sample": len(values),
                "channel_sample": len(rows),
                "caveat": "association across own uploads only, not causal",
            },
            ["own_analytics"],
        )
    return priors, Signal.unavailable("no_own_videos_in_niche", channel_sample=len(rows))
