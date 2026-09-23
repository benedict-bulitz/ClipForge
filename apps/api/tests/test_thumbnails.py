from pathlib import Path

from PIL import Image

from clipforge.config import Settings
from clipforge.models import Project, ProjectRevision
from clipforge.services import (
    get_project,
    regenerate_project_thumbnails,
    select_project_thumbnail,
    serialize_project,
)
from clipforge.thumbnails import build_project_thumbnails


def _settings(tmp_path: Path) -> Settings:
    return Settings(render_root=tmp_path / "projects", clipforge_ai_mode="local", openai_api_key=None)


def _state(project_id: str) -> dict:
    return {
        "prompt": "Why do fireflies glow?",
        "intent": {"topic": "Why do fireflies glow?"},
        "render": {"status": "complete", "url": f"/media/{project_id}/renders/v1/clipforge.mp4"},
        "scenes": [
            {
                "id": "scene-1",
                "media": {
                    "provider": "wikimedia",
                    "provider_id": "photo-1",
                    "kind": "photo",
                    "cache_path": f"{project_id}/assets/wikimedia/photo-1.jpg",
                },
            }
        ],
    }


def test_thumbnail_generation_uses_project_media_and_builds_variants(tmp_path):
    settings = _settings(tmp_path)
    project_id = "project-one"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)

    result = build_project_thumbnails(_state(project_id), project_id, settings)

    assert result["status"] == "available"
    assert result["selected_variant_id"] == result["variants"][0]["id"]
    assert len(result["variants"]) == 3
    assert all((settings.render_root / variant["url"].removeprefix("/media/")).is_file() for variant in result["variants"])
    assert all(variant["source_scene_id"] == "scene-1" for variant in result["variants"])
    assert "FIREFLIES" in result["variants"][0]["text"]


def test_missing_project_media_is_non_fatal(tmp_path):
    result = build_project_thumbnails(_state("missing"), "missing", _settings(tmp_path))

    assert result["status"] == "unavailable"
    assert result["variants"] == []
    assert result["selected_variant_id"] is None


def test_thumbnail_selection_persists_as_a_revision(db, tmp_path):
    settings = _settings(tmp_path)
    project_id = "persisted-project"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)
    project = Project(id=project_id, original_prompt="Why do fireflies glow?", title="Fireflies", status="rendered")
    project.revisions.append(ProjectRevision(number=1, instruction="Render video", kind="system", state=_state(project_id), changed_components=["render"]))
    db.add(project)
    db.commit()

    regenerate_project_thumbnails(db, project, settings, base_revision=1)
    db.refresh(project)
    selected = serialize_project(get_project(db, project_id))["revision"]["state"]["thumbnails"]["variants"][1]["id"]
    select_project_thumbnail(db, project, selected, base_revision=2)

    reopened = serialize_project(get_project(db, project_id))
    assert reopened["revision"]["state"]["thumbnails"]["selected_variant_id"] == selected
    assert reopened["revision"]["state"]["render"] == _state(project_id).get("render", {})
