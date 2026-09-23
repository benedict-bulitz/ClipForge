from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from clipforge.config import Settings
from clipforge.main import delete_project_route
from clipforge.models import GenerationJob, Project, ProjectChatMessage, ProjectRevision
from clipforge.services import (
    ProjectDeletionError,
    delete_all_projects,
    delete_project,
    plan_bulk_project_deletion,
    plan_project_deletion,
    project_local_storage_bytes,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        clipforge_ai_mode="local",
        openai_api_key=None,
        render_root=tmp_path / "project-storage",
        downloads_root=tmp_path / "Downloads",
    )


def _project(db, project_id: str, title: str) -> Project:
    project = Project(
        id=project_id,
        original_prompt=title,
        title=title,
        status="rendered",
        current_revision=1,
        active_tip_revision=1,
    )
    project.revisions.append(
        ProjectRevision(
            number=1,
            parent_revision=None,
            instruction="Original prompt",
            kind="initial",
            state={"render": {"url": f"/media/{project_id}/renders/v1/clipforge.mp4"}},
            changed_components=["render"],
        )
    )
    project.chat_messages.append(ProjectChatMessage(role="user", content="Keep this project."))
    db.add(project)
    db.commit()
    return project


def _project_files(root: Path, project_id: str) -> Path:
    project_dir = root / project_id
    for relative in (
        "renders/v1/clipforge.mp4",
        "audio/narration.wav",
        "assets/scene.jpg",
        "audio-layers/v1/narration.wav",
        "replacements/picture-v2.mp4",
        "tmp/captions.ass",
    ):
        target = project_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"project-owned")
    return project_dir


def test_delete_project_removes_only_its_database_rows_and_project_local_files(db, tmp_path):
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    settings = _settings(tmp_path)
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    first = _project(db, first_id, "First")
    second = _project(db, second_id, "Second")
    db.add_all(
        [
            GenerationJob(id="job-first", project_id=first.id, request_hash="first", status="completed"),
            GenerationJob(id="job-second", project_id=second.id, request_hash="second", status="completed"),
        ]
    )
    db.commit()
    first_dir = _project_files(settings.render_root, first.id)
    second_dir = _project_files(settings.render_root, second.id)
    shared_cache = settings.render_root / "shared-cache" / "provider-file.bin"
    shared_cache.parent.mkdir(parents=True)
    shared_cache.write_bytes(b"shared")

    result = delete_project(db, first.id, settings)

    assert result.reclaimed_bytes == project_local_storage_bytes(second.id, settings)
    assert not first_dir.exists()
    assert second_dir.is_dir()
    assert shared_cache.read_bytes() == b"shared"
    assert db.get(Project, first.id) is None
    assert db.scalars(select(ProjectRevision).where(ProjectRevision.project_id == first.id)).all() == []
    assert db.scalars(select(ProjectChatMessage).where(ProjectChatMessage.project_id == first.id)).all() == []
    assert db.scalars(select(GenerationJob).where(GenerationJob.project_id == first.id)).all() == []
    assert db.get(Project, second.id) is not None
    assert db.scalars(select(ProjectRevision).where(ProjectRevision.project_id == second.id)).one()
    assert db.scalars(select(GenerationJob).where(GenerationJob.project_id == second.id)).one()


def test_delete_plan_reports_owned_resources_without_deleting_them(db, tmp_path):
    settings = _settings(tmp_path)
    project = _project(db, "99999999-9999-4999-8999-999999999999", "Plan")
    project_dir = _project_files(settings.render_root, project.id)
    shared_preview = settings.render_root / "voice-previews" / "shared.wav"
    shared_preview.parent.mkdir(parents=True)
    shared_preview.write_bytes(b"shared")

    plan = plan_project_deletion(db, project.id, settings)

    assert plan.project_directory == project_dir
    assert plan.reclaimed_bytes == project_local_storage_bytes(project.id, settings)
    assert plan.database_records == {
        "project": 1,
        "revisions": 1,
        "chat_messages": 1,
        "generation_jobs": 0,
    }
    assert shared_preview.parent in plan.excluded_shared_locations
    assert project_dir.exists()
    assert db.get(Project, project.id) is not None


def test_delete_project_rejects_unsafe_storage_path_without_touching_outside_file(db, tmp_path):
    settings = _settings(tmp_path)
    project = _project(db, "..", "Unsafe")
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete")

    with pytest.raises(ProjectDeletionError, match="storage identity"):
        delete_project(db, project.id, settings)

    assert outside.read_text() == "do not delete"
    assert db.get(Project, project.id) is not None


def test_delete_project_surfaces_filesystem_failure_without_deleting_database_rows(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    project = _project(db, "33333333-3333-4333-8333-333333333333", "Failure")
    _project_files(settings.render_root, project.id)

    monkeypatch.setattr("clipforge.services.shutil.rmtree", lambda _path: (_ for _ in ()).throw(OSError("disk busy")))
    with pytest.raises(ProjectDeletionError, match="files could not be removed"):
        delete_project(db, project.id, settings)

    assert db.get(Project, project.id) is not None


def test_delete_project_api_reports_unknown_project_as_not_found(db, tmp_path):
    with pytest.raises(HTTPException) as error:
        delete_project_route("missing-project", db, _settings(tmp_path))

    assert error.value.status_code == 404


def test_delete_project_refuses_active_generation(db, tmp_path):
    settings = _settings(tmp_path)
    project = _project(db, "44444444-4444-4444-8444-444444444444", "Active")
    db.add(GenerationJob(id="active-job", project_id=project.id, request_hash="active", status="running"))
    db.commit()

    with pytest.raises(ProjectDeletionError, match="still being generated"):
        delete_project(db, project.id, settings)
    assert db.get(Project, project.id) is not None


def test_delete_queued_project_removes_job_before_it_can_start(db, tmp_path):
    settings = _settings(tmp_path)
    queued_id = "55555555-5555-4555-8555-555555555555"
    db.add(GenerationJob(id="queued-job", project_id=queued_id, request_hash="queued", status="queued"))
    db.commit()

    result = delete_project(db, queued_id, settings)

    assert result.reclaimed_bytes == 0
    assert db.get(GenerationJob, "queued-job") is None

def test_bulk_delete_plan_is_read_only_and_bulk_delete_removes_all_owned_resources(db, tmp_path):
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    settings = _settings(tmp_path)
    first = _project(db, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "First")
    second = _project(db, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "Second")
    first_dir = _project_files(settings.render_root, first.id)
    second_dir = _project_files(settings.render_root, second.id)
    shared_cache = settings.render_root / "voice-previews" / "shared.wav"
    shared_cache.parent.mkdir(parents=True)
    shared_cache.write_bytes(b"shared")

    plan = plan_bulk_project_deletion(db, settings)

    assert [item.project_id for item in plan.projects] == [first.id, second.id]
    assert plan.total_bytes == project_local_storage_bytes(first.id, settings) + project_local_storage_bytes(second.id, settings)
    assert plan.total_files == 12
    assert first_dir.exists() and second_dir.exists()
    assert db.scalars(select(Project)).all()

    result = delete_all_projects(db, settings)

    assert result.deleted_projects == 2
    assert result.freed_bytes == plan.total_bytes
    assert result.failed_projects == {}
    assert result.remaining_projects == 0
    assert not first_dir.exists() and not second_dir.exists()
    assert shared_cache.read_bytes() == b"shared"
    assert db.scalars(select(Project)).all() == []


def test_bulk_delete_removes_queued_jobs_only_when_no_generation_is_running(db, tmp_path):
    settings = _settings(tmp_path)
    queued_id = "66666666-6666-4666-8666-666666666666"
    db.add(GenerationJob(id="queued-bulk-job", project_id=queued_id, request_hash="bulk", status="queued"))
    db.commit()

    result = delete_all_projects(db, settings)

    assert result.remaining_projects == 0
    assert db.get(GenerationJob, "queued-bulk-job") is None


def test_bulk_delete_rejects_unsafe_plan_before_any_project_is_deleted(db, tmp_path):
    settings = _settings(tmp_path)
    safe = _project(db, "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "Safe")
    unsafe = _project(db, "..", "Unsafe")
    safe_dir = _project_files(settings.render_root, safe.id)

    with pytest.raises(ProjectDeletionError, match="storage identity"):
        delete_all_projects(db, settings)

    assert safe_dir.exists()
    assert db.get(Project, safe.id) is not None
    assert db.get(Project, unsafe.id) is not None


def test_bulk_delete_stops_and_reports_partial_filesystem_failure(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    first = _project(db, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "First")
    second = _project(db, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "Second")
    _project_files(settings.render_root, first.id)
    second_dir = _project_files(settings.render_root, second.id)
    original_rmtree = __import__("shutil").rmtree

    def fail_second(path):
        if Path(path).name == second.id:
            raise OSError("disk busy")
        return original_rmtree(path)

    monkeypatch.setattr("clipforge.services.shutil.rmtree", fail_second)
    result = delete_all_projects(db, settings)

    assert result.deleted_projects == 1
    assert result.failed_projects == {second.id: "Project-local files could not be removed; the project was not deleted."}
    assert result.remaining_projects == 1
    assert db.get(Project, first.id) is None
    assert db.get(Project, second.id) is not None
    assert second_dir.exists()
