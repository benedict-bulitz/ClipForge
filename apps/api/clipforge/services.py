import copy
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .hashing import attach_hashes
from .models import Project, ProjectRevision
from .pipeline import _build_captions, _build_scenes, apply_edit, build_initial_state
from .renderer import render_video
from .schemas import ProjectCreate


class RevisionConflict(RuntimeError):
    pass


def create_project(db: Session, payload: ProjectCreate, settings: Settings) -> Project:
    state = build_initial_state(payload.prompt, payload.options, settings)
    script_ready = any(
        stage["id"] == "script" and stage["status"] == "complete" for stage in state["pipeline"]
    )
    project = Project(
        original_prompt=payload.prompt,
        title=state["intent"]["topic"],
        status="ready_for_production" if script_ready else "needs_attention",
        current_revision=1,
    )
    project.revisions.append(
        ProjectRevision(
            number=1,
            parent_revision=None,
            instruction="Original prompt",
            state=state,
            changed_components=[
                "intent",
                "research",
                "facts",
                "script",
                "storyboard",
                "assets",
                "voice",
                "captions",
                "timeline",
            ],
        )
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def get_project(db: Session, project_id: str, *, lock: bool = False) -> Project | None:
    statement = select(Project).where(Project.id == project_id)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def current_revision(project: Project) -> ProjectRevision:
    return next(
        revision for revision in project.revisions if revision.number == project.current_revision
    )


def _next_revision_number(db: Session, project_id: str) -> int:
    return (
        db.scalar(
            select(func.max(ProjectRevision.number)).where(ProjectRevision.project_id == project_id)
        )
        or 0
    ) + 1


def _append_revision(
    db: Session,
    project: Project,
    *,
    base_revision: int | None,
    instruction: str,
    state: dict,
    changed: list[str],
    status: str | None = None,
) -> ProjectRevision:
    expected = base_revision if base_revision is not None else project.current_revision
    locked = get_project(db, project.id, lock=True)
    if locked is None or locked.current_revision != expected:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before applying this action."
        )
    next_number = _next_revision_number(db, project.id)
    state["version"] = next_number
    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.current_revision == expected)
        .values(
            current_revision=next_number,
            status=status or project.status,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise RevisionConflict("Project changed while this action was being applied.")
    revision = ProjectRevision(
        project_id=project.id,
        number=next_number,
        parent_revision=expected,
        instruction=instruction,
        state=state,
        changed_components=changed,
    )
    db.add(revision)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise RevisionConflict("A concurrent revision won; reload and retry.") from exc
    db.refresh(project)
    db.refresh(revision)
    return revision


def edit_project(
    db: Session,
    project: Project,
    instruction: str,
    settings: Settings,
    *,
    base_revision: int | None = None,
) -> ProjectRevision:
    previous = current_revision(project)
    state, changed = apply_edit(previous.state, instruction, settings)
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction=instruction,
        state=state,
        changed=changed,
        status="ready_for_production",
    )


def render_project(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    base_revision: int | None = None,
) -> ProjectRevision:
    previous = current_revision(project)
    expected = base_revision if base_revision is not None else project.current_revision
    if expected != project.current_revision:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before rendering."
        )
    next_number = _next_revision_number(db, project.id)
    result = render_video(previous.state, project.id, next_number, settings)
    state = copy.deepcopy(previous.state)
    state["duration"]["actual_seconds"] = result.actual_seconds
    state["voice"]["provider"] = result.voice_provider
    state["voice"]["status"] = "complete"
    for block in state["voice"]["blocks"]:
        block["status"] = "complete"
    state["scenes"] = _build_scenes(
        state["script"]["blocks"], result.actual_seconds, state["scenes"]
    )
    state["captions"]["items"] = _build_captions(
        state["script"]["text"], result.actual_seconds
    )
    state["captions"]["timing"] = "measured"
    state["timeline"].update(
        {
            "duration": result.actual_seconds,
            "timing": "measured",
            "scene_ids": [scene["id"] for scene in state["scenes"]],
        }
    )
    state["render"] = {
        "status": "complete",
        "url": result.url,
        "file_size": result.file_size,
        "format": "mp4",
    }
    state["qc"].update({"status": "passed", "round": 1, "issues": []})
    for stage in state["pipeline"]:
        if stage["status"] != "blocked":
            stage["status"] = "complete"
    state = attach_hashes(state)
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction="Render video",
        state=state,
        changed=["voice", "alignment", "captions", "timeline", "render", "qc"],
        status="rendered",
    )


def undo_project(
    db: Session, project: Project, *, base_revision: int | None = None
) -> ProjectRevision | None:
    expected = base_revision if base_revision is not None else project.current_revision
    if project.current_revision != expected:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before undoing."
        )
    current = current_revision(project)
    if current.parent_revision is None:
        return None
    target = next(
        revision for revision in project.revisions if revision.number == current.parent_revision
    )
    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.current_revision == expected)
        .values(current_revision=target.number, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise RevisionConflict("Project changed while undo was being applied.")
    db.commit()
    db.refresh(project)
    return target


def serialize_project(project: Project) -> dict:
    revision = current_revision(project)
    return {
        "id": project.id,
        "original_prompt": project.original_prompt,
        "title": project.title,
        "status": project.status,
        "current_revision": project.current_revision,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "revision": revision,
        "revisions": [
            {
                "id": item.id,
                "number": item.number,
                "parent_revision": item.parent_revision,
                "instruction": item.instruction,
                "changed_components": item.changed_components,
                "created_at": item.created_at.isoformat(),
                "is_current": item.number == project.current_revision,
            }
            for item in reversed(project.revisions)
        ],
    }
