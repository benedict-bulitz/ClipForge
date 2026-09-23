from pathlib import Path

from sqlalchemy import select

from clipforge.config import Settings
from clipforge.models import GenerationJob, Project, ProjectRevision
from clipforge.services import delete_project, get_project, serialize_project


def _settings(tmp_path: Path) -> Settings:
    return Settings(clipforge_ai_mode="local", openai_api_key=None, render_root=tmp_path / "projects")


def _state(project_id: str, *, title: str = "Saved project") -> dict:
    return {
        "version": 4,
        "script": {"text": "The final canonical narration.", "blocks": [{"id": "voice_01", "text": "The final canonical narration."}]},
        "render": {"status": "complete", "url": f"/media/{project_id}/renders/v4/clipforge.mp4", "file_size": 1234},
        "timeline": {"width": 1080, "height": 1920, "aspect_ratio": "9:16"},
        "captions": {"font_size": 72, "items": [], "style": "karaoke"},
        "voice": {"volume": 0.61, "voice_id": "nova"},
        "music": {"enabled": False, "volume": 0.19, "track": {"id": "track-1", "title": "Saved track"}},
        "scenes": [{"id": "scene-3", "asset_status": "photo_ready", "media": {"manually_selected": True, "provider": "pexels", "provider_id": "replacement-3", "kind": "photo", "cache_path": f"{project_id}/replacements/pexels/photo-replacement-3.jpg"}}],
        "intent": {"topic": title},
        "pipeline": [],
    }


def _saved_project(db, project_id: str, *, title: str = "Saved project") -> Project:
    project = Project(id=project_id, original_prompt=title, title=title, status="rendered", current_revision=1, active_tip_revision=1)
    project.revisions.append(ProjectRevision(number=1, parent_revision=None, instruction="Render video", kind="system", state=_state(project_id, title=title), changed_components=["render"]))
    db.add(project)
    db.commit()
    return project


def test_finished_project_reopens_from_persisted_revision_without_generation(db, tmp_path):
    project_id = "11111111-aaaa-4111-8111-111111111111"
    _saved_project(db, project_id)
    db.expunge_all()

    reopened = get_project(db, project_id)
    assert reopened is not None
    payload = serialize_project(reopened)
    state = payload["revision"]["state"]

    assert payload["status"] == "rendered"
    assert state["render"]["url"].endswith("/renders/v4/clipforge.mp4")
    assert state["script"]["text"] == "The final canonical narration."
    assert state["music"]["track"]["id"] == "track-1"
    assert state["music"]["enabled"] is False and state["music"]["volume"] == 0.19
    assert state["voice"]["volume"] == 0.61
    assert state["scenes"][0]["media"]["manually_selected"] is True
    assert db.scalars(select(GenerationJob).where(GenerationJob.project_id == project_id)).all() == []


def test_reopening_one_saved_project_never_leaks_another_projects_state(db):
    first = _saved_project(db, "22222222-aaaa-4222-8222-222222222222", title="First saved project")
    second = _saved_project(db, "33333333-aaaa-4333-8333-333333333333", title="Second saved project")
    first_state = serialize_project(get_project(db, first.id))["revision"]["state"]
    second_state = serialize_project(get_project(db, second.id))["revision"]["state"]

    assert first_state["render"]["url"] != second_state["render"]["url"]
    assert first_state["scenes"][0]["media"]["cache_path"].startswith(first.id)
    assert second_state["scenes"][0]["media"]["cache_path"].startswith(second.id)


def test_deleted_project_cannot_be_reopened_from_persistence(db, tmp_path):
    settings = _settings(tmp_path)
    project = _saved_project(db, "44444444-aaaa-4444-8444-444444444444")
    project_dir = settings.render_root / project.id / "renders"
    project_dir.mkdir(parents=True)
    (project_dir / "clipforge.mp4").write_bytes(b"owned")

    delete_project(db, project.id, settings)

    assert get_project(db, project.id) is None
