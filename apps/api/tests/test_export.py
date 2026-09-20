import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.config import Settings, get_settings
from clipforge.database import get_db
from clipforge.exporter import (
    CleanupResult,
    ExportUnavailable,
    UnsafeExportPath,
    cleanup_project_files,
    commit_finalized_export,
    finalize_export,
    safe_export_filename,
)
from clipforge.main import app
from clipforge.models import Project, ProjectRevision
from clipforge.services import (
    _append_revision,
    create_project,
    current_revision,
    edit_project,
    export_project,
    redo_project,
    serialize_project,
    undo_project,
)


def export_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        clipforge_ai_mode="local",
        openai_api_key=None,
        render_root=tmp_path / "projects",
        downloads_root=tmp_path / "Downloads",
    )


def test_default_downloads_location_is_repository_local():
    root = Path(__file__).resolve().parents[3]
    assert Settings(_env_file=None).resolved_downloads_root == root / "Downloads"


def state_for(project_id: str, settings: Settings, content: bytes = b"v" * 20_000) -> dict:
    source = settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return {
        "version": 2,
        "render": {
            "status": "complete",
            "url": f"/media/{project_id}/renders/v2/clipforge.mp4",
            "file_size": len(content),
            "format": "mp4",
        },
        "assets": {"status": "complete", "license_manifest": []},
        "scenes": [],
        "edit_history": [],
    }


def accept_mp4(path: Path) -> None:
    assert path.is_file()
    assert path.stat().st_size >= 10_000


def seed_project(db, project_id: str, state: dict, title: str = "Why is the sky blue?") -> Project:
    project = Project(
        id=project_id,
        original_prompt=title,
        title=title,
        status="rendered",
        current_revision=1,
    )
    project.revisions.append(
        ProjectRevision(
            number=1,
            parent_revision=None,
            instruction="Original prompt",
            state=state,
            changed_components=["render"],
        )
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_export_creates_downloads_and_uses_safe_useful_direct_filename(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "11111111-1111-4111-8111-111111111111"
    state = state_for(project_id, settings)

    result = finalize_export(project_id, "What if / Yellowstone ../ erupted?", state, settings, verifier=accept_mp4)
    commit_finalized_export(result)

    destination = settings.resolved_downloads_root / result.filename
    assert settings.resolved_downloads_root.is_dir()
    assert destination.parent == settings.resolved_downloads_root
    assert destination.is_file()
    assert result.filename.startswith("what-if-yellowstone-erupted-")
    assert len(result.filename) <= 87
    assert ".." not in result.filename and "/" not in result.filename
    assert result.display_path == f"Downloads / {result.filename}"


def test_same_title_projects_have_distinct_canonical_exports(tmp_path):
    settings = export_settings(tmp_path)
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    first = finalize_export(first_id, "Same title", state_for(first_id, settings), settings, verifier=accept_mp4)
    second = finalize_export(second_id, "Same title", state_for(second_id, settings), settings, verifier=accept_mp4)
    commit_finalized_export(first)
    commit_finalized_export(second)

    assert first.filename != second.filename
    assert (settings.resolved_downloads_root / first.filename).is_file()
    assert (settings.resolved_downloads_root / second.filename).is_file()


@pytest.mark.parametrize("project_id", ["../outside", "project/other", "/absolute"])
def test_project_storage_identity_cannot_escape_root(tmp_path, project_id):
    with pytest.raises(UnsafeExportPath):
        cleanup_project_files(project_id, export_settings(tmp_path))


def test_missing_source_render_keeps_all_project_files(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "missing-project"
    project_dir = settings.render_root / project_id
    asset = project_dir / "assets" / "pexels" / "video.mp4"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"asset")
    state = {"render": {"status": "complete", "url": f"/media/{project_id}/renders/v2/clipforge.mp4"}}

    with pytest.raises(ExportUnavailable):
        finalize_export(project_id, "Missing", state, settings, verifier=accept_mp4)

    assert asset.is_file()


def test_empty_render_fails_validation_without_cleanup(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "empty-project"
    state = state_for(project_id, settings, b"")
    asset = settings.render_root / project_id / "assets" / "keep.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"keep")

    with pytest.raises(ExportUnavailable, match="too small"):
        finalize_export(project_id, "Empty", state, settings)

    assert asset.is_file()
    assert (settings.render_root / project_id / "renders").is_dir()


def test_invalid_mp4_never_starts_cleanup(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "broken-container-project"
    state = state_for(project_id, settings, b"not-an-mp4" * 2_000)
    asset = settings.render_root / project_id / "assets" / "keep.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"keep")

    with pytest.raises(ExportUnavailable, match="valid narrated MP4"):
        finalize_export(project_id, "Broken container", state, settings)

    assert asset.is_file()
    assert (settings.render_root / project_id / "renders").is_dir()


def test_ffprobe_verification_failure_never_starts_cleanup(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "invalid-project"
    state = state_for(project_id, settings)
    asset = settings.render_root / project_id / "assets" / "keep.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"keep")

    def reject(_path: Path) -> None:
        raise ExportUnavailable("FFprobe rejected this file")

    with pytest.raises(ExportUnavailable, match="FFprobe"):
        finalize_export(project_id, "Invalid", state, settings, verifier=reject)

    assert asset.is_file()
    assert (settings.render_root / project_id / "renders").is_dir()


def test_previous_valid_export_survives_failed_replacement(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "replacement-project"
    state = state_for(project_id, settings, b"n" * 20_000)
    downloads = settings.resolved_downloads_root
    downloads.mkdir(parents=True)
    destination = downloads / safe_export_filename("Replace me", project_id)
    destination.write_bytes(b"o" * 20_000)
    calls = 0

    def fail_after_atomic_replace(_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExportUnavailable("destination verification failed")

    with pytest.raises(ExportUnavailable, match="destination verification"):
        finalize_export(project_id, "Replace me", state, settings, verifier=fail_after_atomic_replace)

    assert destination.read_bytes() == b"o" * 20_000
    assert (settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4").is_file()


def test_persistence_failure_rolls_back_canonical_and_never_cleans(
    db, tmp_path, monkeypatch
):
    settings = export_settings(tmp_path)
    project_id = "66666666-6666-4666-8666-666666666666"
    title = "Persistence safety"
    state = state_for(project_id, settings, b"n" * 24_000)
    filename = safe_export_filename(title, project_id)
    destination = settings.resolved_downloads_root / filename
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"o" * 20_000)
    state["export"] = {
        "status": "exported",
        "filename": filename,
        "exported_at": "2026-01-01T00:00:00+00:00",
    }
    project = seed_project(db, project_id, state, title)
    cleanup_called = False
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    def forbidden_cleanup(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        raise AssertionError("cleanup ran before persistence")

    monkeypatch.setattr("clipforge.services.cleanup_project_files", forbidden_cleanup)
    monkeypatch.setattr(
        "clipforge.services._append_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        export_project(db, project, settings, base_revision=1)

    assert cleanup_called is False
    assert destination.read_bytes() == b"o" * 20_000
    assert (settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4").is_file()
    assert project.current_revision == 1
    assert current_revision(project).state["render"]["url"].startswith("/media/")


def test_cleanup_begins_only_after_export_revision_is_durable(db, tmp_path, monkeypatch):
    from clipforge import services

    settings = export_settings(tmp_path)
    project_id = "77777777-7777-4777-8777-777777777777"
    project = seed_project(db, project_id, state_for(project_id, settings), "Ordering")
    events: list[str] = []
    original_append = services._append_revision
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    def tracked_append(*args, **kwargs):
        revision = original_append(*args, **kwargs)
        events.append(f"persist:{revision.number}")
        return revision

    def tracked_cleanup(*_args, **_kwargs):
        assert events == ["persist:2"]
        events.append("cleanup")
        return CleanupResult(status="complete", removed=("renders",), warnings=())

    monkeypatch.setattr(services, "_append_revision", tracked_append)
    monkeypatch.setattr(services, "cleanup_project_files", tracked_cleanup)

    revision, _result = export_project(db, project, settings, base_revision=1)

    assert revision.number == 3
    assert events == ["persist:2", "cleanup", "persist:3"]


def test_cleanup_metadata_failure_does_not_invalidate_durable_export(
    db, tmp_path, monkeypatch
):
    from clipforge import services

    settings = export_settings(tmp_path)
    project_id = "88888888-8888-4888-8888-888888888888"
    project = seed_project(
        db, project_id, state_for(project_id, settings), "Cleanup metadata safety"
    )
    original_append = services._append_revision
    calls = 0
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    def fail_second_append(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cleanup metadata database failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(services, "_append_revision", fail_second_append)

    revision, result = export_project(db, project, settings, base_revision=1)

    assert revision.number == 2
    assert current_revision(project).state["render"]["url"] == result.media_url
    assert (settings.resolved_downloads_root / result.filename).is_file()
    assert not (settings.render_root / project_id / "renders").exists()
    assert result.cleanup.status == "warning"
    assert "cleanup status could not be saved" in result.cleanup.warnings[-1]


def test_verified_export_cleans_only_known_directories_for_that_project(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "clean-project"
    other_id = "other-project"
    state = state_for(project_id, settings)
    project_dir = settings.render_root / project_id
    for relative in ("assets/pexels/video.mp4", "audio/narration.wav", "tmp/work.bin"):
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")
    metadata = project_dir / "keep-metadata.json"
    metadata.write_text("keep")
    other = settings.render_root / other_id / "assets" / "untouched.mp4"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    unrelated_download = settings.resolved_downloads_root / "keep-me.mp4"
    unrelated_download.parent.mkdir(parents=True)
    unrelated_download.write_bytes(b"download")

    result = finalize_export(project_id, "Clean project", state, settings, verifier=accept_mp4)
    commit_finalized_export(result)
    cleanup = cleanup_project_files(project_id, settings)

    assert cleanup.status == "complete"
    assert set(cleanup.removed) >= {"renders", "assets", "audio", "tmp"}
    assert not (project_dir / "renders").exists()
    assert not (project_dir / "assets").exists()
    assert not (project_dir / "audio").exists()
    assert metadata.is_file()
    assert other.is_file()
    assert unrelated_download.is_file()


def test_cleanup_is_idempotent(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "idempotent-project"
    state = state_for(project_id, settings)
    finalized = finalize_export(project_id, "Idempotent", state, settings, verifier=accept_mp4)
    commit_finalized_export(finalized)
    cleanup_project_files(project_id, settings)

    second = cleanup_project_files(project_id, settings)

    assert second.status == "complete"
    assert second.removed == ()
    assert second.warnings == ()


def test_cleanup_failure_is_warning_and_valid_export_remains(tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    project_id = "warning-project"
    state = state_for(project_id, settings)
    asset = settings.render_root / project_id / "assets" / "keep.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"keep")
    original_rmtree = __import__("shutil").rmtree

    def selective_failure(path):
        if Path(path).name == "assets":
            raise OSError("locked")
        return original_rmtree(path)

    monkeypatch.setattr("clipforge.exporter.shutil.rmtree", selective_failure)
    result = finalize_export(project_id, "Warning", state, settings, verifier=accept_mp4)
    commit_finalized_export(result)
    cleanup = cleanup_project_files(project_id, settings)

    assert cleanup.status == "warning"
    assert cleanup.warnings
    assert (settings.resolved_downloads_root / result.filename).is_file()
    assert asset.is_file()


def test_service_retains_metadata_and_preview_uses_canonical_export(db, tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    project_id = "33333333-3333-4333-8333-333333333333"
    state = state_for(project_id, settings)
    state.update(
        script={"text": "Blue light scatters.", "blocks": [], "word_count": 3},
        options={},
        captions={},
        research={},
        facts=[],
        voice={},
        storyboard={},
        music={},
        timeline={},
        qc={},
        intent={},
        pipeline=[],
        integrations={},
    )
    project = seed_project(db, project_id, state)
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    revision, result = export_project(db, project, settings, base_revision=1)

    assert revision.number == 3
    assert revision.instruction == "Clean exported project media"
    assert revision.state["render"]["url"] == result.media_url
    assert revision.state["export"]["filename"] == result.filename
    assert revision.state["script"]["text"] == "Blue light scatters."
    assert project.original_prompt == "Why is the sky blue?"
    assert serialize_project(project)["revision"]["state"]["export"]["status"] == "exported"
    assert not (settings.render_root / project_id / "renders").exists()


def test_later_edit_keeps_previous_canonical_export_safe(db, tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    project_id = "44444444-4444-4444-8444-444444444444"
    from clipforge.pipeline import build_initial_state
    from clipforge.schemas import AdvancedOptions

    state = build_initial_state(
        "Tell a story about an astronaut",
        AdvancedOptions(language="en", research="off"),
        settings,
    )
    source_state = state_for(project_id, settings)
    state["render"] = source_state["render"]
    project = seed_project(db, project_id, state, "Astronaut story")
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)
    _revision, result = export_project(db, project, settings, base_revision=1)
    canonical = settings.resolved_downloads_root / result.filename

    edited = edit_project(
        db,
        project,
        "Make the captions larger",
        settings,
        base_revision=3,
        auto_render=False,
    )

    assert canonical.is_file()
    assert edited.state["export"]["filename"] == result.filename
    assert edited.state["render"]["url"] == result.media_url
    assert edited.state["render"]["status"] == "regeneration_required"


def test_later_successful_export_atomically_replaces_same_project_file(tmp_path):
    settings = export_settings(tmp_path)
    project_id = "replace-success-project"
    first_state = state_for(project_id, settings, b"a" * 20_000)
    first = finalize_export(project_id, "Stable title", first_state, settings, verifier=accept_mp4)
    commit_finalized_export(first)
    cleanup_project_files(project_id, settings)
    destination = settings.resolved_downloads_root / first.filename
    assert destination.read_bytes() == b"a" * 20_000

    second_state = state_for(project_id, settings, b"b" * 24_000)
    second = finalize_export(project_id, "Stable title", second_state, settings, verifier=accept_mp4)
    commit_finalized_export(second)
    cleanup_project_files(project_id, settings)

    assert second.filename == first.filename
    assert destination.read_bytes() == b"b" * 24_000
    assert list(settings.resolved_downloads_root.glob(f"{first.filename}")) == [destination]


def test_undo_redo_skip_export_maintenance_and_preserve_canonical_file(
    db, tmp_path, monkeypatch
):
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    settings = export_settings(tmp_path)
    project = create_project(
        db,
        ProjectCreate(
            prompt="Explain why the sky is blue",
            options=AdvancedOptions(research="off"),
        ),
        settings,
    )
    user_edit = edit_project(
        db,
        project,
        "Make the captions larger",
        settings,
        base_revision=1,
        auto_render=False,
    )
    render_state = copy.deepcopy(user_edit.state)
    working = state_for(project.id, settings, b"v" * 24_000)
    render_state["render"] = working["render"]
    render_revision = _append_revision(
        db,
        project,
        base_revision=user_edit.number,
        instruction="Render video",
        state=render_state,
        changed=["render"],
        status="rendered",
        kind="system",
    )
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    cleanup_revision, result = export_project(
        db, project, settings, base_revision=render_revision.number
    )
    canonical = settings.resolved_downloads_root / result.filename
    revision_count = len(project.revisions)

    assert cleanup_revision.kind == "system"
    assert current_revision(project).instruction == "Clean exported project media"
    assert undo_project(db, project, base_revision=cleanup_revision.number).number == 1
    assert project.status == "ready_for_production"
    assert canonical.is_file()
    undone = serialize_project(project)
    assert undone["revision"]["state"]["render"]["url"] == result.media_url
    assert undone["revision"]["state"]["render"]["stale"] is True

    assert redo_project(db, project, base_revision=1).number == user_edit.number
    assert project.status == "exported"
    assert canonical.is_file()
    redone = serialize_project(project)
    assert redone["revision"]["state"]["export"]["filename"] == result.filename
    assert redone["can_redo"] is False

    undo_project(db, project, base_revision=user_edit.number)
    branched = edit_project(
        db,
        project,
        "Use a warmer narrator voice",
        settings,
        base_revision=1,
        auto_render=False,
    )

    assert branched.parent_revision == 1
    assert serialize_project(project)["can_redo"] is False
    assert len(project.revisions) == revision_count + 1
    assert canonical.is_file()
    assert branched.state["export"]["filename"] == result.filename


def test_export_endpoint_returns_structure_and_rejects_client_cleanup_path(
    db, tmp_path, monkeypatch
):
    settings = export_settings(tmp_path)
    project_id = "55555555-5555-4555-8555-555555555555"
    project = seed_project(db, project_id, state_for(project_id, settings))
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/projects/{project.id}/export", json={"base_revision": 1}
            )
            malicious = client.post(
                f"/api/projects/{project.id}/export",
                json={"base_revision": 2, "cleanup_path": "/tmp"},
            )
            preview = client.get(f"/api/projects/{project.id}/exported-video")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["display_path"].startswith("Downloads / ")
    assert body["cleanup_status"] == "complete"
    assert body["project"]["revision"]["state"]["render"]["url"].endswith(
        "/exported-video"
    )
    assert malicious.status_code == 422
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("video/mp4")
