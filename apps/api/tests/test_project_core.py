import pytest

from clipforge.config import Settings
from clipforge.dependencies import resolve_edit_scope
from clipforge.schemas import ProjectCreate
from clipforge.services import (
    _append_revision,
    create_project,
    edit_project,
    redo_project,
    revision_navigation_targets,
    serialize_project,
    undo_project,
)


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
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain lift."), settings)
    revision = edit_project(db, project, "Make the captions larger", settings)

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
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), settings)
    edit_project(db, project, "Make the music darker", settings)

    target = undo_project(db, project)

    assert target is not None
    assert target.number == 1
    assert project.current_revision == 1
    assert len(project.revisions) == 2


def test_edit_after_undo_keeps_the_actual_parent(db):
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), settings)
    edit_project(db, project, "Make the music darker", settings)
    undo_project(db, project)
    branched = edit_project(db, project, "Make the captions larger", settings)

    assert branched.number == 3
    assert branched.parent_revision == 1
    assert undo_project(db, project).number == 1


def test_multi_step_undo_and_redo_follow_active_user_lineage(db):
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), settings)
    second = edit_project(db, project, "Make the music darker", settings)
    third = edit_project(db, project, "Make the captions larger", settings)

    assert undo_project(db, project).number == second.number
    assert undo_project(db, project).number == 1
    assert redo_project(db, project).number == second.number
    assert redo_project(db, project).number == third.number
    assert redo_project(db, project) is None
    serialized = serialize_project(project)
    assert serialized["can_undo"] is True
    assert serialized["can_redo"] is False


def test_new_edit_after_undo_creates_branch_and_disables_old_redo(db):
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), settings)
    old_second = edit_project(db, project, "Make the music darker", settings)
    old_third = edit_project(db, project, "Make the captions larger", settings)
    undo_project(db, project)
    undo_project(db, project)

    branched = edit_project(db, project, "Use a warmer narrator voice", settings)
    undo_target, redo_target = revision_navigation_targets(project)

    assert branched.number == 4
    assert branched.parent_revision == 1
    assert project.active_tip_revision == 4
    assert redo_target is None
    assert undo_target is not None and undo_target.number == 1
    assert {revision.number for revision in project.revisions} == {
        1,
        old_second.number,
        old_third.number,
        branched.number,
    }
    serialized = serialize_project(project)
    inactive = {
        revision["number"]
        for revision in serialized["revisions"]
        if not revision["on_active_branch"]
    }
    assert inactive == {old_second.number, old_third.number}


def test_system_revisions_are_not_user_visible_navigation_steps(db):
    settings = local_settings()
    project = create_project(db, ProjectCreate(prompt="Explain black holes."), settings)
    user_edit = edit_project(db, project, "Make the music darker", settings)
    system_state = dict(user_edit.state)
    system = _append_revision(
        db,
        project,
        base_revision=user_edit.number,
        instruction="Render video",
        state=system_state,
        changed=["render"],
        status="rendered",
        kind="system",
    )

    assert project.current_revision == system.number
    assert undo_project(db, project).number == 1
    assert redo_project(db, project).number == user_edit.number
    assert redo_project(db, project) is None
