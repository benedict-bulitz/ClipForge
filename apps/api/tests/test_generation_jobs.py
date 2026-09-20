from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from clipforge.config import Settings
from clipforge.generation import (
    ProgressTracker,
    active_generation_job,
    create_generation_job,
    mark_interrupted_generation_jobs,
    run_generation_job,
    serialize_generation_job,
)
from clipforge.main import start_generation_job_route
from clipforge.media import prepare_project_media
from clipforge.models import GenerationJob, GenerationTimingStat, Project, ProjectRevision
from clipforge.progress import ProgressEvent
from clipforge.schemas import AdvancedOptions, ProjectCreate


def payload(**options) -> ProjectCreate:
    return ProjectCreate(
        prompt="Explain why the sky is blue",
        options=AdvancedOptions(research="off", **options),
    )


def test_generate_starts_trackable_job_with_early_eta(db):
    job, created = create_generation_job(db, payload())
    serialized = serialize_generation_job(job)

    assert created is True
    assert job.id and job.project_id
    assert serialized["status"] == "queued"
    assert serialized["progress"] == 0
    assert serialized["estimated_remaining_seconds"] > 0


def test_duplicate_generate_reuses_active_job_and_schedules_once(db):
    first_tasks = BackgroundTasks()
    second_tasks = BackgroundTasks()
    settings = Settings(clipforge_ai_mode="local", openai_api_key=None)

    first = start_generation_job_route(payload(), first_tasks, db, settings)
    second = start_generation_job_route(payload(), second_tasks, db, settings)

    assert first["id"] == second["id"]
    assert len(first_tasks.tasks) == 1
    assert len(second_tasks.tasks) == 0
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1


def test_stage_plan_omits_disabled_work(db):
    job, _ = create_generation_job(
        db,
        payload(captions_enabled=False, music_enabled=False),
    )
    stages = {item["id"] for item in job.stage_plan}

    assert "research" not in stages
    assert "alignment" not in stages
    assert "music" not in stages


def test_progress_is_monotonic_and_reaches_100_only_when_completed(db):
    job, _ = create_generation_job(db, payload())
    tracker = ProgressTracker(job.id, session=db)
    tracker(ProgressEvent("script", "Writing", phase="complete"))
    first = db.get(GenerationJob, job.id).progress
    tracker(ProgressEvent("preparing", "Preparing", phase="start"))
    second = db.get(GenerationJob, job.id).progress
    tracker(ProgressEvent("finalizing", "Finalizing", phase="complete"))

    assert 0 < first <= second < 1
    assert db.get(GenerationJob, job.id).progress < 1
    tracker.complete()
    assert db.get(GenerationJob, job.id).progress == 1


def test_failed_job_keeps_stage_and_never_reports_100(db):
    job, _ = create_generation_job(db, payload())
    tracker = ProgressTracker(job.id, session=db)
    tracker(ProgressEvent("voice", "Generating narration", phase="start"))
    tracker.fail("Voice provider rejected the request.", category="voice_authentication")
    failed = db.get(GenerationJob, job.id)

    assert failed.status == "failed"
    assert failed.current_stage == "voice"
    assert failed.progress < 1
    assert failed.failure_category == "voice_authentication"
    assert "rejected" in failed.failure_message


def test_observed_stage_timing_updates_future_eta_without_content(db):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    job, _ = create_generation_job(db, payload())
    tracker = ProgressTracker(job.id, session=db, now=lambda: now[0])
    tracker(ProgressEvent("script", "Writing", phase="start"))
    now[0] += timedelta(seconds=18)
    tracker(ProgressEvent("script", "Writing", phase="complete"))
    tracker.complete()

    stat = db.get(GenerationTimingStat, "script")
    second, _ = create_generation_job(db, payload())
    estimate = next(item for item in second.stage_plan if item["id"] == "script")

    assert stat.ema_seconds == 18
    assert estimate["estimate_seconds"] == 18
    assert set(GenerationTimingStat.__table__.columns.keys()) == {
        "stage",
        "ema_seconds",
        "ema_seconds_per_unit",
        "sample_count",
        "updated_at",
    }


def test_eta_decreases_as_real_and_cached_work_completes(db):
    job, _ = create_generation_job(db, payload())
    tracker = ProgressTracker(job.id, session=db)
    starting_eta = job.estimated_remaining_seconds
    tracker(
        ProgressEvent(
            "media",
            "Finding visuals",
            phase="start",
            completed_units=0,
            total_units=4,
        )
    )
    tracker(
        ProgressEvent(
            "media",
            "Finding visuals",
            completed_units=2,
            total_units=4,
            cached=True,
        )
    )

    assert db.get(GenerationJob, job.id).estimated_remaining_seconds < starting_eta


def test_media_reports_actual_scene_work_units_and_cache_hits(tmp_path):
    events: list[ProgressEvent] = []
    settings = Settings(render_root=tmp_path, openai_api_key=None)
    scenes = []
    for index in range(2):
        relative = f"project/assets/cached-{index}.jpg"
        cached = tmp_path / relative
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"image")
        scenes.append(
            {
                "id": f"scene-{index}",
                "start": index * 2,
                "end": index * 2 + 2,
                "visual_goal": "blue daylight sky",
                "asset_status": "photo_ready",
                "media": {"identity": f"cached-{index}", "cache_path": relative},
            }
        )
    state = {
        "timeline": {"width": 1080, "height": 1920},
        "assets": {"license_manifest": []},
        "scenes": scenes,
    }

    prepare_project_media(state, "project", settings, progress=events.append)

    updates = [event for event in events if event.stage == "media"]
    assert [event.completed_units for event in updates if event.phase == "update"] == [1, 2]
    assert all(event.cached for event in updates if event.phase == "update")
    assert updates[-1].phase == "complete"


def test_active_job_can_be_rediscovered_after_refresh(db):
    job, _ = create_generation_job(db, payload())
    recovered = active_generation_job(db)

    assert recovered is not None
    assert recovered.id == job.id


def test_restart_marks_active_jobs_retryable_without_touching_history(db):
    job, _ = create_generation_job(db, payload())
    revisions_before = db.scalar(select(func.count()).select_from(ProjectRevision))

    assert mark_interrupted_generation_jobs(db) == 1
    interrupted = db.get(GenerationJob, job.id)

    assert interrupted.status == "failed"
    assert interrupted.failure_category == "interrupted"
    assert "Retry" in interrupted.failure_message
    assert interrupted.active_key is None
    assert db.scalar(select(func.count()).select_from(ProjectRevision)) == revisions_before


def test_progress_updates_do_not_create_project_revisions(db):
    job, _ = create_generation_job(db, payload())
    tracker = ProgressTracker(job.id, session=db)
    before = db.scalar(select(func.count()).select_from(ProjectRevision))
    tracker(ProgressEvent("script", "Writing", phase="start"))
    tracker(ProgressEvent("script", "Writing", phase="complete"))

    assert db.scalar(select(func.count()).select_from(ProjectRevision)) == before


def test_worker_completes_only_after_project_render_revision_is_persisted(
    db, tmp_path, monkeypatch
):
    request = payload()
    job, _ = create_generation_job(db, request)

    def fake_render(state, _project_id, revision, _settings, *, progress=None):
        if progress:
            progress(ProgressEvent("media", "Finding visuals", phase="complete"))
            progress(ProgressEvent("finalizing", "Finalizing", phase="complete"))
        state["render"] = {
            "status": "complete",
            "url": f"/media/fake-v{revision}.mp4",
            "revision": revision,
            "stale": False,
        }
        return state

    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=True)
    run_generation_job(
        job.id,
        request,
        Settings(
            clipforge_ai_mode="local",
            openai_api_key=None,
            render_root=tmp_path,
        ),
        session_factory=factory,
    )
    db.expire_all()
    completed = db.get(GenerationJob, job.id)
    project = db.get(Project, job.project_id)

    assert completed.status == "completed"
    assert completed.progress == 1
    assert project is not None
    assert project.current_revision == 2
    assert [revision.kind for revision in project.revisions] == ["initial", "system"]
