from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .database import SessionLocal
from .models import GenerationJob, GenerationTimingStat
from .progress import ProgressEvent
from .renderer import RenderUnavailable, VoiceGenerationError
from .schemas import ProjectCreate

ACTIVE_JOB_STATUSES = ("queued", "running")
EMA_ALPHA = 0.3

BASELINE_SECONDS = {
    "preparing": 0.5,
    "research": 7.0,
    "script": 6.0,
    "storyboard": 0.8,
    "media": 2.5,
    "review": 4.0,
    "voice": 8.0,
    "alignment": 5.0,
    "rendering": 0.9,
    "music": 2.0,
    "finalizing": 12.0,
}

STAGE_LABELS = {
    "preparing": "Preparing your project",
    "research": "Researching the topic",
    "script": "Writing the narration",
    "storyboard": "Building the storyboard",
    "media": "Finding visuals",
    "review": "Reviewing content quality",
    "voice": "Generating narration",
    "alignment": "Timing captions to speech",
    "rendering": "Preparing scene video",
    "music": "Preparing the audio mix",
    "finalizing": "Finalizing and checking the video",
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _request_hash(payload: ProjectCreate) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _estimated_target_duration(payload: ProjectCreate) -> float:
    return float(
        min(payload.options.max_duration, max(payload.options.min_duration or 10, 24))
    )


def _estimated_scene_count(payload: ProjectCreate) -> int:
    target = _estimated_target_duration(payload)
    seconds_per_scene = {"slow": 5.0, "balanced": 3.8, "fast": 2.6}[payload.options.pacing]
    return max(2, min(16, math.ceil(target / seconds_per_scene)))


def _historical_estimate(
    stats: dict[str, GenerationTimingStat], stage: str, units: int | None = None
) -> float:
    stat = stats.get(stage)
    if stat is not None:
        if units and stat.ema_seconds_per_unit:
            return max(0.1, stat.ema_seconds_per_unit * units)
        return max(0.1, stat.ema_seconds)
    baseline = BASELINE_SECONDS[stage]
    return baseline * units if units and stage in {"media", "rendering"} else baseline


def build_stage_plan(db: Session, payload: ProjectCreate) -> list[dict[str, Any]]:
    stats = {
        item.stage: item for item in db.scalars(select(GenerationTimingStat)).all()
    }
    scenes = _estimated_scene_count(payload)
    target_duration = _estimated_target_duration(payload)
    stages: list[tuple[str, int | None]] = [("preparing", None)]
    if payload.options.research != "off":
        stages.append(("research", None))
    stages.extend(
        [
            ("script", None),
            ("storyboard", None),
            ("media", scenes),
            ("review", None),
            ("voice", None),
        ]
    )
    if payload.options.captions_enabled:
        stages.append(("alignment", None))
    stages.append(("rendering", scenes))
    if payload.options.music_enabled:
        stages.append(("music", None))
    stages.append(("finalizing", None))
    plan = []
    for stage, units in stages:
        estimate = _historical_estimate(stats, stage, units)
        if stage not in stats:
            if stage == "voice":
                estimate = 3.0 + target_duration * 0.2
            elif stage == "alignment":
                estimate = 2.0 + target_duration * 0.12
            elif stage == "finalizing":
                estimate = 5.0 + target_duration * 0.35
        plan.append(
            {
                "id": stage,
                "label": STAGE_LABELS[stage],
                "estimate_seconds": round(estimate, 2),
                "total_units": units,
            }
        )
    return plan


def create_generation_job(
    db: Session, payload: ProjectCreate
) -> tuple[GenerationJob, bool]:
    request_hash = _request_hash(payload)
    active = db.scalar(
        select(GenerationJob)
        .where(
            GenerationJob.request_hash == request_hash,
            GenerationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(GenerationJob.created_at.desc())
    )
    if active is not None:
        return active, False
    project_id = str(uuid.uuid4())
    plan = build_stage_plan(db, payload)
    job = GenerationJob(
        project_id=project_id,
        request_hash=request_hash,
        active_key=f"request:{request_hash}",
        status="queued",
        current_stage="preparing",
        stage_label=STAGE_LABELS["preparing"],
        progress=0.0,
        stage_plan=plan,
        completed_stages=[],
        stage_timings={},
        estimated_remaining_seconds=round(
            sum(float(item["estimate_seconds"]) for item in plan), 1
        ),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(
            select(GenerationJob).where(
                GenerationJob.active_key == f"request:{request_hash}"
            )
        )
        if active is None:
            raise
        return active, False
    db.refresh(job)
    return job, True


def active_generation_job(db: Session) -> GenerationJob | None:
    return db.scalar(
        select(GenerationJob)
        .where(GenerationJob.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(GenerationJob.updated_at.desc())
    )


def get_generation_job(db: Session, job_id: str) -> GenerationJob | None:
    return db.get(GenerationJob, job_id)


def serialize_generation_job(job: GenerationJob, *, now: datetime | None = None) -> dict:
    current = _utc(now or datetime.now(UTC))
    origin = job.started_at or job.created_at
    elapsed = max(0.0, (current - _utc(origin)).total_seconds())
    if job.completed_at is not None:
        elapsed = max(0.0, (_utc(job.completed_at) - _utc(origin)).total_seconds())
    eta = job.estimated_remaining_seconds
    if job.status == "running" and eta is not None and job.stage_started_at is not None:
        stage_elapsed = max(
            0.0, (current - _utc(job.stage_started_at)).total_seconds()
        )
        stage = next(
            (item for item in job.stage_plan if item["id"] == job.current_stage), None
        )
        if stage is not None:
            expected = max(0.1, float(stage["estimate_seconds"]))
            if stage_elapsed <= expected:
                eta = max(0.0, eta - min(stage_elapsed, expected * 0.8))
            else:
                eta = max(
                    0.0,
                    eta - expected * 0.8 + (stage_elapsed - expected) * 0.5,
                )
    return {
        "id": job.id,
        "project_id": job.project_id,
        "base_revision": job.base_revision,
        "status": job.status,
        "current_stage": job.current_stage,
        "stage_label": job.stage_label,
        "progress": round(job.progress, 4),
        "completed_units": job.completed_units,
        "total_units": job.total_units,
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
        "elapsed_seconds": round(elapsed, 1),
        "estimated_remaining_seconds": round(eta, 1) if eta is not None else None,
        "failure_category": job.failure_category,
        "failure_message": job.failure_message,
    }


class ProgressTracker:
    def __init__(
        self,
        job_id: str,
        *,
        session: Session,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.job_id = job_id
        self.session = session
        self.now = now or (lambda: datetime.now(UTC))

    def __call__(self, event: ProgressEvent) -> None:
        now = self.now()
        job = self.session.get(GenerationJob, self.job_id)
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return
        plan = [dict(item) for item in job.stage_plan]
        item = next((entry for entry in plan if entry["id"] == event.stage), None)
        if event.phase == "skipped":
            plan = [entry for entry in plan if entry["id"] != event.stage]
            job.stage_plan = plan
            self._update_progress(job, plan, 0.0)
            job.updated_at = now
            self.session.commit()
            return
        if item is None:
            item = {
                "id": event.stage,
                "label": event.label,
                "estimate_seconds": BASELINE_SECONDS.get(event.stage, 1.0),
                "total_units": event.total_units,
            }
            plan.append(item)
        if event.total_units:
            previous_units = int(item.get("total_units") or event.total_units)
            item["total_units"] = event.total_units
            if event.stage in {"media", "rendering"} and previous_units != event.total_units:
                per_unit = float(item["estimate_seconds"]) / max(1, previous_units)
                item["estimate_seconds"] = round(per_unit * event.total_units, 2)
        job.stage_plan = plan
        if job.current_stage != event.stage or job.stage_started_at is None:
            job.current_stage = event.stage
            job.stage_label = event.label
            job.stage_started_at = now
        job.status = "running"
        if job.started_at is None:
            job.started_at = now
        job.completed_units = event.completed_units
        job.total_units = event.total_units
        fraction = 0.0
        if event.completed_units is not None and event.total_units:
            fraction = min(1.0, max(0.0, event.completed_units / event.total_units))
        if event.phase == "complete":
            fraction = 1.0
            completed = list(dict.fromkeys([*job.completed_stages, event.stage]))
            job.completed_stages = completed
            self._record_timing(job, event, now)
        self._update_progress(job, plan, fraction)
        job.updated_at = now
        self.session.commit()

    def _update_progress(
        self, job: GenerationJob, plan: list[dict[str, Any]], current_fraction: float
    ) -> None:
        total = max(0.1, sum(float(item["estimate_seconds"]) for item in plan))
        completed = set(job.completed_stages)
        completed_weight = sum(
            float(item["estimate_seconds"]) for item in plan if item["id"] in completed
        )
        current = next((item for item in plan if item["id"] == job.current_stage), None)
        if current is not None and job.current_stage not in completed:
            completed_weight += float(current["estimate_seconds"]) * current_fraction
        calculated = min(0.99, completed_weight / total)
        job.progress = max(job.progress, calculated)
        raw_eta = max(0.0, total - completed_weight)
        previous = job.estimated_remaining_seconds
        job.estimated_remaining_seconds = (
            raw_eta if previous is None else max(0.0, previous * 0.55 + raw_eta * 0.45)
        )

    def _record_timing(
        self, job: GenerationJob, event: ProgressEvent, now: datetime
    ) -> None:
        if job.stage_started_at is None:
            return
        elapsed = max(0.01, (_utc(now) - _utc(job.stage_started_at)).total_seconds())
        timings = dict(job.stage_timings)
        timings[event.stage] = round(elapsed, 3)
        job.stage_timings = timings
        if event.cached:
            return
        stat = self.session.get(GenerationTimingStat, event.stage)
        per_unit = elapsed / event.total_units if event.total_units else None
        if stat is None:
            self.session.add(
                GenerationTimingStat(
                    stage=event.stage,
                    ema_seconds=elapsed,
                    ema_seconds_per_unit=per_unit,
                    sample_count=1,
                )
            )
            return
        stat.ema_seconds = stat.ema_seconds * (1 - EMA_ALPHA) + elapsed * EMA_ALPHA
        if per_unit is not None:
            stat.ema_seconds_per_unit = (
                per_unit
                if stat.ema_seconds_per_unit is None
                else stat.ema_seconds_per_unit * (1 - EMA_ALPHA) + per_unit * EMA_ALPHA
            )
        stat.sample_count += 1

    def complete(self) -> None:
        now = self.now()
        job = self.session.get(GenerationJob, self.job_id)
        if job is None:
            return
        job.status = "completed"
        job.current_stage = "complete"
        job.stage_label = "Complete"
        job.progress = 1.0
        job.completed_units = None
        job.total_units = None
        job.estimated_remaining_seconds = 0.0
        job.completed_at = now
        job.updated_at = now
        job.active_key = None
        self.session.commit()

    def fail(self, message: str, *, category: str = "generation_failed") -> None:
        now = self.now()
        job = self.session.get(GenerationJob, self.job_id)
        if job is None:
            return
        job.status = "failed"
        job.progress = min(job.progress, 0.99)
        job.failure_category = category
        job.failure_message = message[:500]
        job.estimated_remaining_seconds = None
        job.completed_at = now
        job.updated_at = now
        job.active_key = None
        self.session.commit()


def run_generation_job(
    job_id: str,
    payload: ProjectCreate,
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> None:
    from .services import create_project, render_project

    with session_factory() as db:
        tracker = ProgressTracker(job_id, session=db)
        tracker(
            ProgressEvent(
                stage="preparing",
                label=STAGE_LABELS["preparing"],
                phase="start",
            )
        )
        tracker(
            ProgressEvent(
                stage="preparing",
                label=STAGE_LABELS["preparing"],
                phase="complete",
            )
        )
        try:
            job = db.get(GenerationJob, job_id)
            if job is None:
                return
            project = create_project(
                db,
                payload,
                settings,
                project_id=job.project_id,
                progress=tracker,
            )
            render_project(
                db,
                project,
                settings,
                base_revision=project.current_revision,
                progress=tracker,
            )
            tracker.complete()
        except Exception as exc:  # noqa: BLE001 - durable job boundary
            db.rollback()
            if isinstance(exc, VoiceGenerationError):
                tracker.fail(str(exc), category=exc.category)
            elif isinstance(exc, RenderUnavailable):
                tracker.fail(str(exc), category="render_unavailable")
            else:
                job = db.get(GenerationJob, job_id)
                label = job.stage_label if job is not None else "generation"
                tracker.fail(
                    f"Generation stopped while {label.casefold()}. You can retry safely.",
                    category="generation_failed",
                )


def mark_interrupted_generation_jobs(db: Session) -> int:
    jobs = db.scalars(
        select(GenerationJob).where(GenerationJob.status.in_(ACTIVE_JOB_STATUSES))
    ).all()
    now = datetime.now(UTC)
    for job in jobs:
        job.status = "failed"
        job.failure_category = "interrupted"
        job.failure_message = (
            "Generation was interrupted when ClipForge restarted. Retry to continue safely."
        )
        job.estimated_remaining_seconds = None
        job.completed_at = now
        job.updated_at = now
        job.active_key = None
    if jobs:
        db.commit()
    return len(jobs)
