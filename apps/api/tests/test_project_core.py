import pytest

from clipforge.config import Settings
from clipforge.dependencies import resolve_edit_scope
from clipforge.schemas import ProjectCreate
from clipforge.services import create_project, edit_project, undo_project


def local_settings() -> Settings:
    return Settings(clipforge_ai_mode="local", openai_api_key=None)


def test_project_creation_preserves_prompt_and_uses_auto_duration(db):
    prompt = "Why did Concorde disappear?"
    project = create_project(db, ProjectCreate(prompt=prompt), local_settings())

    assert project.original_prompt == prompt
    assert project.current_revision == 1
    assert project.revisions[0].state["duration"]["mode"] == "AUTO"
    assert project.revisions[0].state["duration"]["estimated_seconds"] <= 180
    assert project.revisions[0].state["scenes"]


def test_original_prompt_is_immutable(db):
    project = create_project(db, ProjectCreate(prompt="Why is the sky blue?"), local_settings())
    project.original_prompt = "Replace the prompt"

    with pytest.raises(ValueError, match="immutable"):
        db.commit()


def test_caption_edit_invalidates_only_downstream_components(db):
    project = create_project(db, ProjectCreate(prompt="Explain lift."), local_settings())
    revision = edit_project(db, project, "Make the captions larger")

    assert revision.number == 2
    assert revision.changed_components == ["captions", "timeline", "render", "qc"]
    assert revision.state["captions"]["font_size"] == 82
    assert revision.state["research"] == project.revisions[0].state["research"]
    assert project.original_prompt == "Explain lift."


def test_research_edit_cascades_through_dependency_graph():
    affected = resolve_edit_scope("Füge noch den damaligen Ticketpreis hinzu")

    assert affected[0] == "research"
    assert "facts" in affected
    assert "script" in affected
    assert affected[-1] == "qc"


def test_undo_moves_pointer_without_deleting_history(db):
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), local_settings())
    edit_project(db, project, "Make the music darker")

    target = undo_project(db, project)

    assert target is not None
    assert target.number == 1
    assert project.current_revision == 1
    assert len(project.revisions) == 2


def test_edit_after_undo_keeps_the_actual_parent(db):
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), local_settings())
    edit_project(db, project, "Make the music darker")
    undo_project(db, project)
    branched = edit_project(db, project, "Make the captions larger")

    assert branched.number == 3
    assert branched.parent_revision == 1
    assert undo_project(db, project).number == 1
