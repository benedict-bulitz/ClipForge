"""Queue Overview: the current queue boundary, row states and the final-video source."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_youtube_learning_loop import connect
from youtube_support import FakeYouTube, publish_options, rendered_state, youtube_settings

from clipforge import exporter
from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.generation import create_generation_job, remove_queued_generation_job
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import GenerationJob, Project, ProjectRevision
from clipforge.queue_overview import current_queue_run, queue_overview
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.security.secrets import SecretStore
from clipforge.services import delete_project
from clipforge.youtube import connection, uploads
from clipforge.youtube.routes import get_upload_dispatcher, get_youtube_provider

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()
    app.dependency_overrides.clear()


@pytest.fixture()
def settings(tmp_path):
    return youtube_settings(tmp_path)


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def store():
    return SecretStore()


def client(db, settings, store, fake) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    app.dependency_overrides[get_upload_dispatcher] = lambda: lambda *_args: None
    return TestClient(app)


def job(db, prompt: str, status: str, created: datetime, *, ended: datetime | None = None) -> GenerationJob:
    row, _ = create_generation_job(db, ProjectCreate(prompt=prompt, options=AdvancedOptions(research="off")))
    row.status = status
    row.created_at = created
    row.updated_at = ended or created
    row.completed_at = ended
    if status == "failed":
        row.failure_message = "Research failed"
    db.commit()
    return row


def rendered_project(db, settings, project_id: str, *, title: str = "Why are airplane windows round?", music: bool = False, stale: bool = False, write: bool = True) -> Project:
    state = rendered_state(12.5)
    state["music"] = {"enabled": True, "track": {"id": "t1"}, "volume": 0.2} if music else {"enabled": False}
    render = settings.render_root / project_id / "renders" / "v1" / "clipforge.mp4"
    if write:
        render.parent.mkdir(parents=True, exist_ok=True)
        render.write_bytes(b"\x00mp4" * 6000)
    state["render"] = {"status": "complete", "url": f"/media/{project_id}/renders/v1/clipforge.mp4", "revision": 1, "stale": stale}
    state["final_quality_review"] = {"status": "passed", "revision": 1, "summary": {"label": "Passed"}}
    state["ai_review"] = {"status": "passed", "rounds": 1, "items": [], "automatic_corrections": []}
    state["thumbnails"] = {"status": "available", "selected_variant_id": "youtube-cover-1", "variants": [
        {"id": "tiktok-cover-1", "platform": "tiktok", "url": f"/media/{project_id}/thumbnails/tiktok.jpg"},
        {"id": "youtube-cover-1", "platform": "youtube", "url": f"/media/{project_id}/thumbnails/youtube.jpg"},
    ]}
    project = Project(id=project_id, original_prompt=title, title=title, status="rendered", current_revision=1)
    project.revisions.append(ProjectRevision(number=1, parent_revision=None, instruction="Original prompt", kind="initial", state=state, changed_components=["render"]))
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Current queue boundary
# ---------------------------------------------------------------------------


def test_a_job_submitted_into_an_idle_queue_starts_a_new_run(db):
    old = job(db, "Old batch question", "completed", T0, ended=T0 + timedelta(minutes=5))
    first = job(db, "First of the new batch", "completed", T0 + timedelta(hours=2), ended=T0 + timedelta(hours=2, minutes=5))
    second = job(db, "Submitted while the first ran", "running", T0 + timedelta(hours=2, minutes=1))
    third = job(db, "Waiting", "queued", T0 + timedelta(hours=2, minutes=2))

    run = current_queue_run(db.query(GenerationJob).all())

    assert [item.id for item in run] == [first.id, second.id, third.id]
    assert old.id not in {item.id for item in run}


def test_a_drained_queue_keeps_its_finished_batch_until_the_next_idle_submission(db):
    job(db, "Earlier batch", "completed", T0, ended=T0 + timedelta(minutes=3))
    a = job(db, "Question A", "completed", T0 + timedelta(hours=1), ended=T0 + timedelta(hours=1, minutes=4))
    b = job(db, "Question B", "failed", T0 + timedelta(hours=1, minutes=1), ended=T0 + timedelta(hours=1, minutes=6))
    c = job(db, "Question C", "cancelled", T0 + timedelta(hours=1, minutes=5), ended=T0 + timedelta(hours=1, minutes=7))

    assert [item.id for item in current_queue_run(db.query(GenerationJob).all())] == [a.id, b.id, c.id]

    later = job(db, "Next day", "queued", T0 + timedelta(days=1))
    assert [item.id for item in current_queue_run(db.query(GenerationJob).all())] == [later.id]


def test_a_job_submitted_while_an_older_one_still_runs_belongs_to_the_same_run(db):
    long_running = job(db, "Still generating", "running", T0)
    late = job(db, "Added hours later", "queued", T0 + timedelta(hours=3))
    assert [item.id for item in current_queue_run(db.query(GenerationJob).all())] == [long_running.id, late.id]


def test_empty_history_has_no_current_run(db):
    assert current_queue_run([]) == []


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def test_overview_rows_cover_every_queue_state_and_hide_removed_jobs(db, settings):
    done = job(db, "Rendered question", "completed", T0, ended=T0 + timedelta(minutes=4))
    rendered_project(db, settings, done.project_id)
    failed = job(db, "Failed question", "failed", T0 + timedelta(minutes=1), ended=T0 + timedelta(minutes=2))
    cancelled = job(db, "Cancelled question", "cancelled", T0 + timedelta(minutes=2), ended=T0 + timedelta(minutes=3))
    running = job(db, "Running question", "running", T0 + timedelta(minutes=3))
    running.progress = 0.42
    db.commit()
    queued = job(db, "Queued question", "queued", T0 + timedelta(minutes=4))
    removed = job(db, "Removed question", "queued", T0 + timedelta(minutes=5))
    assert remove_queued_generation_job(db, removed.id)

    overview = queue_overview(db, settings)
    rows = {item["job"]["id"]: item for item in overview["items"]}

    assert list(rows) == [done.id, failed.id, cancelled.id, running.id, queued.id]
    assert rows[running.id]["job"]["progress"] == 0.42 and rows[running.id]["project"] is None
    assert rows[queued.id]["job"]["queue_position"] == 1 and rows[queued.id]["project"] is None
    assert rows[failed.id]["job"]["failure_message"] == "Research failed"
    assert rows[cancelled.id]["job"]["status"] == "cancelled"
    project = rows[done.id]["project"]
    assert project["render"]["state"] == "ready"
    assert project["render"]["final_video_url"] == f"/api/projects/{done.project_id}/final-video?revision=1"
    assert project["duration_seconds"] == 12.5
    assert project["poster_url"] == f"/media/{done.project_id}/thumbnails/youtube.jpg"
    assert project["quality"] == {"ai_review": {"status": "passed"}, "final_review": {"status": "passed", "revision": 1, "summary": {"label": "Passed"}}}
    assert overview["youtube"] == {"status": "not_connected", "channel_title": None}
    assert project["youtube"]["upload"] is None


def test_stale_and_missing_renders_are_never_playable(db, settings):
    stale = job(db, "Stale", "completed", T0, ended=T0 + timedelta(minutes=1))
    rendered_project(db, settings, stale.project_id, stale=True)
    missing = job(db, "Missing file", "completed", T0 + timedelta(seconds=30), ended=T0 + timedelta(minutes=2))
    rendered_project(db, settings, missing.project_id, write=False)

    rows = {item["job"]["id"]: item["project"] for item in queue_overview(db, settings)["items"]}

    assert rows[stale.id]["render"]["state"] == "stale" and rows[stale.id]["render"]["final_video_url"] is None
    assert rows[missing.id]["render"]["state"] == "missing" and rows[missing.id]["render"]["final_video_url"] is None
    assert rows[stale.id]["duration_seconds"] is None


def test_a_completed_job_without_its_project_and_a_deleted_project(db, settings):
    orphan = job(db, "Project row is gone", "completed", T0, ended=T0 + timedelta(minutes=1))
    deleted = job(db, "Deleted by the user", "completed", T0 + timedelta(seconds=10), ended=T0 + timedelta(minutes=2))
    rendered_project(db, settings, deleted.project_id)
    delete_project(db, deleted.project_id, settings)

    rows = queue_overview(db, settings)["items"]

    assert [(item["job"]["id"], item["project"]) for item in rows] == [(orphan.id, None)]


def test_youtube_state_is_the_results_page_state_and_needs_no_youtube_call(db, settings, store, fake):
    connect(db, settings, store, fake)
    done = job(db, "Uploaded question", "completed", T0, ended=T0 + timedelta(minutes=1))
    project = rendered_project(db, settings, done.project_id)
    record = connection.active_connection(db)
    upload, target, should_run = uploads.request_upload(db, project, settings, channel_id=record.channel_id, options=publish_options())
    assert should_run
    uploads.run_upload(db, upload.id, target.path, settings, store, fake)

    api = client(db, settings, store, fake)
    body = api.get("/api/generation-jobs/overview").json()
    page = api.get(f"/api/youtube/projects/{project.id}").json()

    row = body["items"][0]["project"]["youtube"]
    assert body["youtube"]["status"] == "connected"
    assert row["current_render"] == page["current_render"]
    assert row["current_render"]["uploadable"] is False and row["current_render"]["code"] == "already_uploaded"
    assert row["upload"]["id"] == page["focus_upload_id"] and row["upload"]["youtube_video_id"]
    # The overview itself asked YouTube nothing (only the Results-page route may reconcile).
    fake.calls.clear()
    api.get("/api/generation-jobs/overview")
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Final video
# ---------------------------------------------------------------------------


def test_final_video_is_the_canonical_render_with_ranges(db, settings, store, fake):
    done = job(db, "Rendered", "completed", T0, ended=T0 + timedelta(minutes=1))
    project = rendered_project(db, settings, done.project_id)
    before = {path for path in settings.render_root.rglob("*") if path.is_file()}
    api = client(db, settings, store, fake)

    response = api.get(f"/api/projects/{project.id}/final-video?revision=1")
    ranged = api.get(f"/api/projects/{project.id}/final-video?revision=1", headers={"Range": "bytes=0-99"})

    render = settings.render_root / project.id / "renders" / "v1" / "clipforge.mp4"
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert response.content == render.read_bytes()
    assert ranged.status_code == 206 and len(ranged.content) == 100
    assert {path for path in settings.render_root.rglob("*") if path.is_file()} == before  # nothing copied


def test_final_video_with_music_is_the_one_mixed_master_uploads_use(db, settings, store, fake, monkeypatch):
    done = job(db, "With music", "completed", T0, ended=T0 + timedelta(minutes=1))
    project = rendered_project(db, settings, done.project_id, music=True)
    track = settings.render_root.parent / "track.mp3"
    track.write_bytes(b"music")
    mixes = []

    def fake_mix(source, destination, _state, _track):
        mixes.append(destination)
        destination.write_bytes(source.read_bytes() + b"+music")

    monkeypatch.setattr(exporter, "resolve_track_path", lambda _music: track)
    monkeypatch.setattr(exporter, "_mix_music", fake_mix)
    monkeypatch.setattr(exporter, "verify_mp4", lambda _path: None)
    api = client(db, settings, store, fake)

    first = api.get(f"/api/projects/{project.id}/final-video?revision=1")
    second = api.get(f"/api/projects/{project.id}/final-video?revision=1")
    source = uploads.resolve_upload_source(project, settings)

    assert first.status_code == second.status_code == 200
    assert first.content.endswith(b"+music") and first.content == second.content
    assert len(mixes) == 1  # mixed once, then reused
    assert source.kind == "mixed_master" and first.content == source.path.read_bytes()


def test_final_video_refuses_stale_revisions_and_missing_renders(db, settings, store, fake):
    done = job(db, "Edited", "completed", T0, ended=T0 + timedelta(minutes=1))
    project = rendered_project(db, settings, done.project_id)
    api = client(db, settings, store, fake)

    stale = api.get(f"/api/projects/{project.id}/final-video?revision=0")
    assert stale.status_code == 409 and stale.json()["detail"]["status"] == "stale_revision"

    (settings.render_root / project.id / "renders" / "v1" / "clipforge.mp4").unlink()
    missing = api.get(f"/api/projects/{project.id}/final-video?revision=1")
    assert missing.status_code == 404 and missing.json()["detail"]["status"] == "render_unavailable"

    assert api.get("/api/projects/00000000-0000-4000-8000-000000000000/final-video").status_code == 404


def test_overview_route_is_not_shadowed_by_the_job_route(db, settings, store, fake):
    api = client(db, settings, store, fake)
    response = api.get("/api/generation-jobs/overview")
    assert response.status_code == 200
    assert response.json() == {
        "run_started_at": None, "youtube": {"status": "not_connected", "channel_title": None},
        "publishing": {"connected_accounts": 0}, "items": [],
    }
