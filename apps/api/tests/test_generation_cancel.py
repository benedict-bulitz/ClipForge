"""Active generation cancel: running -> cancelling -> cancelled, cooperatively.

The status column is the one canonical state; the worker stops at its next
checkpoint (every progress event, before rendering, before saving a render,
at completion) and only processes ClipForge started for that job are stopped.
Nothing is deleted except directories the cancelled run created and never saved.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from clipforge import cancellation, generation, renderer
from clipforge.cancellation import GenerationCancelled
from clipforge.config import Settings
from clipforge.generation import (
    ProgressTracker,
    active_generation_job,
    claim_next_generation_job,
    create_generation_job,
    finalize_cancelled_job,
    mark_interrupted_generation_jobs,
    request_generation_cancel,
    run_generation_job,
)
from clipforge.main import cancel_generation_job_route, list_project_overview
from clipforge.models import GenerationJob, Project, ProjectRevision
from clipforge.progress import ProgressEvent, report_progress
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.services import ProjectDeletionBusy, delete_project

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]
STUBBORN = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"]


def request(topic: str = "Warum ist der Himmel blau?") -> ProjectCreate:
    return ProjectCreate(prompt=topic, options=AdvancedOptions(research="off"))


def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, clipforge_ai_mode="local", openai_api_key=None, render_root=tmp_path / "renders")


@pytest.fixture()
def factory(db):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=True)


@pytest.fixture()
def launched(monkeypatch):
    """The next job is claimed for real, but not started in a thread."""
    started: list[str] = []
    original = generation.schedule_next_generation

    def schedule(config, *, session_factory=generation.SessionLocal, launch=None):
        return original(config, session_factory=session_factory, launch=started.append)

    monkeypatch.setattr(generation, "schedule_next_generation", schedule)
    return started


def running_job(db, topic: str = "Warum ist der Himmel blau?") -> GenerationJob:
    job, _ = create_generation_job(db, request(topic))
    assert claim_next_generation_job(db).id == job.id
    return job


def fake_pipeline(monkeypatch, *, during_render=None, reached: list[str] | None = None, scenes: int = 4):
    """create_project / render_project stand-ins that report real progress events."""
    reached = reached if reached is not None else []

    def create_project(db, payload, config, *, project_id, progress):
        report_progress(progress, "script", "Writing the narration", phase="start")
        project = Project(id=project_id, original_prompt=payload.prompt, title=payload.prompt[:160], status="ready")
        db.add(project)
        db.add(ProjectRevision(project_id=project_id, number=1, instruction=payload.prompt, kind="system", state={"prompt": payload.prompt}))
        db.commit()
        reached.append("created")
        report_progress(progress, "script", "Writing the narration", phase="complete")
        return project

    def render_project(db, project, config, *, base_revision, progress):
        cancellation.checkpoint()
        out = config.render_root.resolve() / project.id / "renders" / "v2"
        cancellation.claim_path(out)
        out.mkdir(parents=True)
        (out / "clipforge.mp4").write_bytes(b"partial")
        for index in range(scenes):
            report_progress(progress, "rendering", "Preparing scene video", completed_units=index + 1, total_units=scenes)
            reached.append(f"scene{index + 1}")
            if during_render is not None:
                during_render(index)
        cancellation.checkpoint()
        db.add(ProjectRevision(project_id=project.id, number=2, parent_revision=1, instruction="Render video", kind="system", state={}))
        db.commit()
        reached.append("saved")
        return project

    monkeypatch.setattr("clipforge.services.create_project", create_project)
    monkeypatch.setattr("clipforge.services.render_project", render_project)
    return reached


# --- The cancel API -----------------------------------------------------------------------------


def test_cancel_is_idempotent_and_goes_through_cancelling(db, tmp_path, launched):
    job = running_job(db)
    token = cancellation.register(job.id)  # a live worker holds the job
    try:
        first = cancel_generation_job_route(job.id, db, settings(tmp_path))
        again = cancel_generation_job_route(job.id, db, settings(tmp_path))
        assert first["status"] == again["status"] == "cancelling"
        assert token.cancelled
        assert active_generation_job(db).id == job.id  # still holds the worker slot
    finally:
        cancellation.unregister(token)
    # The worker is gone (e.g. it just stopped): the next request finishes the cancel.
    done = cancel_generation_job_route(job.id, db, settings(tmp_path))
    assert done["status"] == "cancelled"
    assert cancel_generation_job_route(job.id, db, settings(tmp_path))["status"] == "cancelled"


def test_cancel_refuses_other_states_without_changing_them(db, tmp_path, launched):
    done = running_job(db, "Fertiges Video")
    ProgressTracker(done.id, session=db).complete()
    queued, _ = create_generation_job(db, request("Warteschlange bleibt"))
    with pytest.raises(HTTPException) as error:
        cancel_generation_job_route(queued.id, db, settings(tmp_path))
    assert error.value.status_code == 409 and db.get(GenerationJob, queued.id).status == "queued"
    with pytest.raises(HTTPException) as error:
        cancel_generation_job_route(done.id, db, settings(tmp_path))
    assert error.value.status_code == 409
    finished = db.get(GenerationJob, done.id)
    assert finished.status == "completed" and finished.progress == 1.0 and finished.failure_category is None
    with pytest.raises(HTTPException) as error:
        cancel_generation_job_route("missing", db, settings(tmp_path))
    assert error.value.status_code == 404


def test_cancel_route_is_wired_and_never_deletes(db, tmp_path, launched):
    from fastapi.testclient import TestClient

    from clipforge.config import get_settings
    from clipforge.database import get_db
    from clipforge.main import app

    job = running_job(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings(tmp_path)
    try:
        client = TestClient(app)
        response = client.post(f"/api/generation-jobs/{job.id}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert client.post("/api/generation-jobs/nope/cancel").status_code == 404
        assert client.get("/api/generation-jobs/active").json() is None
    finally:
        app.dependency_overrides.clear()
    assert db.get(GenerationJob, job.id) is not None


# --- Cooperative cancellation of the running pipeline ------------------------------------------------


def test_running_pipeline_stops_at_the_next_checkpoint_and_keeps_the_project(db, factory, tmp_path, monkeypatch, launched):
    job = running_job(db)
    waiting, _ = create_generation_job(db, request("Nächste Frage"))
    other = factory()

    def cancel_after_first_scene(index):
        if index == 0:
            assert request_generation_cancel(other, job.id)[0] == "requested"

    reached = fake_pipeline(monkeypatch, during_render=cancel_after_first_scene)
    run_generation_job(job.id, settings(tmp_path), session_factory=factory)

    db.expire_all()
    stopped = db.get(GenerationJob, job.id)
    assert reached == ["created", "scene1"]  # scene 2 never started, nothing was saved
    assert stopped.status == "cancelled" and stopped.status not in {"failed", "completed"}
    assert stopped.failure_category == "user_cancelled" and "Preparing scene video" in stopped.failure_message
    assert stopped.completed_at is not None and stopped.progress < 1
    # The project, its question and its first revision survive; the unsaved render does not.
    project = db.get(Project, job.project_id)
    assert project is not None and project.original_prompt == "Warum ist der Himmel blau?"
    assert [revision.number for revision in project.revisions] == [1]
    assert not (tmp_path / "renders" / job.project_id / "renders" / "v2").exists()
    # The one worker slot is free: the next queued job was claimed.
    assert launched == [waiting.id] and db.get(GenerationJob, waiting.id).status == "running"
    assert cancellation.has_worker(job.id) is False


def test_cleanup_keeps_saved_revisions_earlier_exports_and_caches(db, factory, tmp_path, monkeypatch, launched):
    job = running_job(db)
    config = settings(tmp_path)
    root = config.render_root.resolve() / job.project_id
    earlier = root / "renders" / "v1"
    earlier.mkdir(parents=True)
    (earlier / "clipforge.mp4").write_bytes(b"earlier export")
    cache = root / "segments"
    cache.mkdir()
    (cache / "scene.mp4").write_bytes(b"cache")
    stranger = tmp_path / "renders" / "other-project" / "renders" / "v2"
    stranger.mkdir(parents=True)

    def cancel_now(index):
        cancellation.claim_path(earlier)  # even a wrongly claimed saved revision is kept
        cancellation.claim_path(stranger)  # and nothing outside this project
        request_generation_cancel(factory(), job.id)

    fake_pipeline(monkeypatch, during_render=cancel_now)
    run_generation_job(job.id, config, session_factory=factory)
    assert (earlier / "clipforge.mp4").read_bytes() == b"earlier export"
    assert (cache / "scene.mp4").exists() and stranger.exists()
    assert not (root / "renders" / "v2").exists()


def test_cancel_before_the_worker_starts_never_runs_the_pipeline(db, factory, tmp_path, monkeypatch, launched):
    job = running_job(db)  # claimed, thread not started yet
    assert cancel_generation_job_route(job.id, db, settings(tmp_path))["status"] == "cancelled"
    reached = fake_pipeline(monkeypatch)
    run_generation_job(job.id, settings(tmp_path), session_factory=factory)
    assert reached == []
    db.expire_all()
    assert db.get(GenerationJob, job.id).status == "cancelled"


def test_completion_and_cancel_race_is_decided_by_the_first_commit(db, factory, tmp_path, monkeypatch, launched):
    # Cancel commits first: the finished render stays saved, the job ends cancelled.
    first = running_job(db, "Erst abgebrochen")

    def cancel_at_the_end(index):
        if index == 3:
            request_generation_cancel(factory(), first.id)

    monkeypatch.setattr(cancellation, "checkpoint", lambda: None)  # the cancel lands between the last checkpoint and complete()
    reached = fake_pipeline(monkeypatch, during_render=cancel_at_the_end, scenes=4)
    original_call = ProgressTracker.__call__
    monkeypatch.setattr(ProgressTracker, "__call__", lambda self, event: None if event.stage == "rendering" else original_call(self, event))
    run_generation_job(first.id, settings(tmp_path), session_factory=factory)
    db.expire_all()
    assert "saved" in reached
    assert db.get(GenerationJob, first.id).status == "cancelled"
    assert [revision.number for revision in db.get(Project, first.project_id).revisions] == [1, 2]
    assert (tmp_path / "renders" / first.project_id / "renders" / "v2" / "clipforge.mp4").exists()  # saved -> kept
    # Completion commits first: the job is completed and a late cancel changes nothing.
    second = running_job(db, "Erst fertig")
    ProgressTracker(second.id, session=db).complete()
    assert request_generation_cancel(db, second.id)[0] == "not_running"
    assert db.get(GenerationJob, second.id).status == "completed"


def test_a_failure_caused_by_stopping_the_work_is_a_cancellation(db, factory, tmp_path, monkeypatch, launched):
    job = running_job(db)

    def stop_then_fail(index):
        request_generation_cancel(factory(), job.id)
        raise renderer.RenderUnavailable("FFmpeg exited")

    fake_pipeline(monkeypatch, during_render=stop_then_fail)
    run_generation_job(job.id, settings(tmp_path), session_factory=factory)
    db.expire_all()
    assert db.get(GenerationJob, job.id).status == "cancelled"


def test_double_cancel_and_finalize_are_safe(db, tmp_path, launched):
    job = running_job(db)
    assert request_generation_cancel(db, job.id)[0] == "requested"
    assert request_generation_cancel(db, job.id)[0] == "cancelling"
    assert finalize_cancelled_job(db, job.id) is True
    assert finalize_cancelled_job(db, job.id) is True
    assert request_generation_cancel(db, job.id)[0] == "cancelled"
    assert db.get(GenerationJob, job.id).status == "cancelled"


# --- Terminal state everywhere -------------------------------------------------------------------------


def test_cancelled_is_terminal_restart_safe_and_never_claimed_again(db, tmp_path, launched):
    cancelling = running_job(db, "Beim Neustart abgebrochen")
    request_generation_cancel(db, cancelling.id)
    waiting, _ = create_generation_job(db, request("Wartet"))
    # Never two active jobs: a cancelling job still holds the worker.
    assert claim_next_generation_job(db) is None
    # Restart: the requested cancel is completed, never resumed or failed.
    assert mark_interrupted_generation_jobs(db) == 0
    db.expire_all()
    assert db.get(GenerationJob, cancelling.id).status == "cancelled"
    assert claim_next_generation_job(db).id == waiting.id
    assert mark_interrupted_generation_jobs(db) == 1  # the new running one is the only interrupted job
    assert db.get(GenerationJob, cancelling.id).status == "cancelled"


def test_overview_and_deletion_understand_cancelling_and_cancelled(db, tmp_path, launched):
    config = settings(tmp_path)
    job = running_job(db, "Abgebrochene Frage")
    db.add(Project(id=job.project_id, original_prompt="Abgebrochene Frage", title="Abgebrochene Frage", status="ready"))
    db.add(ProjectRevision(project_id=job.project_id, number=1, instruction="x", kind="system", state={}))
    db.commit()
    request_generation_cancel(db, job.id)
    with pytest.raises(ProjectDeletionBusy):
        delete_project(db, job.project_id, config)  # the worker is still alive
    from clipforge.youtube import lifecycle

    assert lifecycle.plan_deletion(db, job.project_id, config, None, None, verify=False).mode == "busy"
    assert {item["id"]: item["status"] for item in list_project_overview(db)}[job.project_id] == "cancelling"
    finalize_cancelled_job(db, job.id)
    assert {item["id"]: item["status"] for item in list_project_overview(db)}[job.project_id] == "cancelled"
    assert active_generation_job(db) is None
    # A cancelled request without a project is history, not active work.
    early = running_job(db, "Früh abgebrochen")
    request_generation_cancel(db, early.id)
    finalize_cancelled_job(db, early.id)
    overview = {item["id"]: item for item in list_project_overview(db)}
    assert overview[early.project_id]["status"] == "cancelled" and overview[early.project_id]["title"] == "Früh abgebrochen"


def test_progress_after_cancel_never_revives_the_job(db, launched):
    job = running_job(db)
    tracker = ProgressTracker(job.id, session=db)
    tracker(ProgressEvent("script", "Writing", phase="start"))
    request_generation_cancel(db, job.id)
    with pytest.raises(GenerationCancelled):
        tracker(ProgressEvent("script", "Writing", phase="complete"))
    finalize_cancelled_job(db, job.id)
    tracker(ProgressEvent("voice", "Voice", phase="start"))  # a late event is ignored
    tracker.fail("late failure")
    db.expire_all()
    assert db.get(GenerationJob, job.id).status == "cancelled"


# --- Controlled subprocesses -------------------------------------------------------------------------


def _cancel_after(token, seconds):
    timer = threading.Timer(seconds, token.event.set)
    timer.start()
    return timer


def test_a_cancel_terminates_the_jobs_ffmpeg_process_and_reaps_it(monkeypatch):
    token = cancellation.CancelToken("job-ffmpeg")
    unrelated = subprocess.Popen(SLEEPER)
    started = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    try:
        timer = _cancel_after(token, 0.3)
        began = time.monotonic()
        with cancellation.scope(token), pytest.raises(GenerationCancelled):
            renderer._run_process(SLEEPER, timeout=60, failure="render")  # the render path, not RenderUnavailable
        assert time.monotonic() - began < 5
        timer.join()
        assert len(started) == 1 and started[0].returncode is not None  # stopped and reaped
        assert not token.processes
        assert unrelated.poll() is None  # an unrelated process is never touched
    finally:
        unrelated.kill()
        unrelated.wait()


def test_a_process_ignoring_terminate_is_killed_after_the_grace_period(monkeypatch):
    monkeypatch.setattr(cancellation, "TERMINATE_GRACE_SECONDS", 0.5)
    token = cancellation.CancelToken("job-stubborn")
    started = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    timer = _cancel_after(token, 0.5)
    with cancellation.scope(token), pytest.raises(GenerationCancelled):
        cancellation.run_process(STUBBORN, timeout=60)
    timer.join()
    assert started[0].returncode is not None and started[0].returncode != 0


def test_process_runner_keeps_timeout_and_plain_behaviour():
    token = cancellation.CancelToken("job-timeout")
    with cancellation.scope(token), pytest.raises(subprocess.TimeoutExpired):
        cancellation.run_process(SLEEPER, timeout=0.3)
    done = cancellation.run_process([sys.executable, "-c", "print('ok')"], timeout=10)  # outside a job: subprocess.run
    assert done.returncode == 0 and done.stdout.strip() == "ok"
    with cancellation.scope(token):
        inside = cancellation.run_process([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], timeout=10)
    assert (inside.returncode, inside.stdout.strip(), inside.stderr.strip()) == (0, "out", "err")


def test_render_video_removes_its_half_written_output_on_cancel(tmp_path, monkeypatch):
    config = settings(tmp_path)
    monkeypatch.setattr(renderer, "ffmpeg_path", lambda: "ffmpeg")

    def partial(state, project_id, revision_number, settings, ffmpeg, output, *, progress):
        output.write_bytes(b"half")
        raise GenerationCancelled("stop")

    monkeypatch.setattr(renderer, "_render_video", partial)
    state = {"render": {"status": "ready"}, "script": {"text": "Der Himmel ist blau."}}
    token = cancellation.CancelToken("job-render")
    with cancellation.scope(token), pytest.raises(GenerationCancelled):
        renderer.render_video(state, "project-1", 3, config)
    target = config.render_root.resolve() / "project-1" / "renders" / "v3"
    assert not (target / "clipforge.mp4").exists()
    assert token.owned_paths == [target]  # created by this run: a cleanup candidate
