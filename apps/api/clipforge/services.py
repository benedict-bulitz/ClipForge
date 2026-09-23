import copy
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .dependencies import expand_dependencies
from .exporter import (
    CleanupResult,
    ExportResult,
    cleanup_project_files,
    commit_finalized_export,
    finalize_export,
    rollback_finalized_export,
)
from .hashing import attach_hashes
from .media import prepare_project_media
from .models import GenerationJob, Project, ProjectChatMessage, ProjectRevision
from .music import available_music_tracks, music_track_state
from .pipeline import (
    _apply_selected_hook,
    _authoritative_hook_blocks,
    _build_scenes,
    _is_hook_block,
    _refresh_script_derivatives,
    apply_edit,
    build_initial_state,
)
from .progress import ProgressCallback, report_progress
from .renderer import RenderUnavailable, VoiceGenerationError, render_video
from .review import run_ai_review
from .schemas import (
    AudioSettingsUpdate,
    MusicSelectionUpdate,
    ProjectCreate,
    SocialMetadataGenerate,
    SocialMetadataUpdate,
)
from .social_metadata import generate_social_metadata, normalize_hashtags
from .thumbnails import build_project_thumbnails


class RevisionConflict(RuntimeError):
    pass


class ProjectDeletionError(RuntimeError):
    pass


class ProjectDeletionBusy(ProjectDeletionError):
    pass


@dataclass(frozen=True)
class ProjectDeletionResult:
    reclaimed_bytes: int


@dataclass(frozen=True)
class ProjectDeletionPlan:
    project_id: str
    project_directory: Path
    reclaimed_bytes: int
    file_count: int
    directory_count: int
    database_records: dict[str, int]
    excluded_shared_locations: tuple[Path, ...]


_PROJECT_STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")


@dataclass(frozen=True)
class BulkProjectDeletionPlan:
    projects: tuple[ProjectDeletionPlan, ...]
    total_bytes: int
    total_files: int
    total_directories: int


@dataclass(frozen=True)
class BulkProjectDeletionResult:
    deleted_projects: int
    freed_bytes: int
    failed_projects: dict[str, str]
    remaining_projects: int


def _project_storage_directory(project_id: str, settings: Settings) -> Path:
    """Return only a direct, non-symlink child of the configured project root."""
    if not _PROJECT_STORAGE_ID.fullmatch(project_id):
        raise ProjectDeletionError("The project storage identity is invalid.")
    storage_root = settings.render_root.resolve()
    project_dir = (storage_root / project_id).resolve()
    if project_dir.parent != storage_root or project_dir.is_symlink():
        raise ProjectDeletionError("Refused to remove an unsafe project storage path.")
    return project_dir


def _project_local_storage_stats(project_id: str, settings: Settings) -> tuple[int, int, int]:
    """Report the size of files owned by this project's isolated storage directory."""
    project_dir = _project_storage_directory(project_id, settings)
    if not project_dir.exists():
        return 0, 0, 0
    total = 0
    file_count = 0
    directory_count = 1
    for root, child_directories, child_files in os.walk(project_dir, followlinks=False):
        directory_count += len(child_directories)
        for name in [*child_directories, *child_files]:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            try:
                total += candidate.stat().st_size
                if candidate.is_file():
                    file_count += 1
            except OSError as exc:
                raise ProjectDeletionError("Could not inspect project-local storage.") from exc
    return total, file_count, directory_count


def project_local_storage_bytes(project_id: str, settings: Settings) -> int:
    return _project_local_storage_stats(project_id, settings)[0]


def plan_project_deletion(db: Session, project_id: str, settings: Settings) -> ProjectDeletionPlan:
    """Classify one project's owned records/files without changing database or storage."""
    if get_project(db, project_id) is None:
        raise ProjectDeletionError("Project not found.")
    project_dir = _project_storage_directory(project_id, settings)
    reclaimed_bytes, file_count, directory_count = _project_local_storage_stats(project_id, settings)
    record_counts = {
        "project": 1,
        "revisions": int(
            db.scalar(select(func.count()).where(ProjectRevision.project_id == project_id)) or 0
        ),
        "chat_messages": int(
            db.scalar(select(func.count()).where(ProjectChatMessage.project_id == project_id)) or 0
        ),
        "generation_jobs": int(
            db.scalar(select(func.count()).where(GenerationJob.project_id == project_id)) or 0
        ),
    }
    return ProjectDeletionPlan(
        project_id=project_id,
        project_directory=project_dir,
        reclaimed_bytes=reclaimed_bytes,
        file_count=file_count,
        directory_count=directory_count,
        database_records=record_counts,
        excluded_shared_locations=(
            settings.render_root.resolve() / "voice-previews",
            settings.resolved_downloads_root,
            Path(__file__).resolve().parents[1] / "music-library" / "cache",
        ),
    )


def plan_bulk_project_deletion(db: Session, settings: Settings) -> BulkProjectDeletionPlan:
    """Validate every current project before any bulk deletion can begin."""
    projects = db.scalars(select(Project).order_by(Project.created_at.asc())).all()
    plans = tuple(plan_project_deletion(db, project.id, settings) for project in projects)
    running_job = db.scalar(
        select(GenerationJob.id).where(GenerationJob.status == "running").limit(1)
    )
    if running_job is not None:
        raise ProjectDeletionBusy("A project is still being generated; bulk deletion cannot start.")
    return BulkProjectDeletionPlan(
        projects=plans,
        total_bytes=sum(plan.reclaimed_bytes for plan in plans),
        total_files=sum(plan.file_count for plan in plans),
        total_directories=sum(plan.directory_count for plan in plans),
    )


def delete_project(db: Session, project_id: str, settings: Settings) -> ProjectDeletionResult:
    """Delete one project and its isolated local storage, never shared caches/downloads."""
    project = get_project(db, project_id, lock=True)
    active_job = db.scalar(
        select(GenerationJob.id).where(
            GenerationJob.project_id == project_id,
            GenerationJob.status == "running",
        )
    )
    if active_job is not None:
        raise ProjectDeletionBusy("This project is still being generated. Try again when it finishes.")
    if project is None:
        # A queued request has a durable project id before expensive generation
        # creates its first Project revision. Removing that job is sufficient:
        # it owns no project-local render directory and can never be claimed.
        queued_job = db.scalar(
            select(GenerationJob.id).where(
                GenerationJob.project_id == project_id,
                GenerationJob.status == "queued",
            )
        )
        if queued_job is None:
            raise ProjectDeletionError("Project not found.")
        db.execute(delete(GenerationJob).where(GenerationJob.id == queued_job))
        db.commit()
        return ProjectDeletionResult(reclaimed_bytes=0)
    plan = plan_project_deletion(db, project_id, settings)

    project_dir = plan.project_directory
    if project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except OSError as exc:
            raise ProjectDeletionError(
                "Project-local files could not be removed; the project was not deleted."
            ) from exc

    try:
        # Generation jobs are project-keyed but intentionally not FK-linked.
        # Revisions and chat messages are removed by the project's DB cascades.
        db.execute(delete(GenerationJob).where(GenerationJob.project_id == project_id))
        db.execute(delete(Project).where(Project.id == project_id))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise ProjectDeletionError(
            "Project files were removed, but database cleanup failed. The project was not fully deleted."
        ) from exc
    return ProjectDeletionResult(reclaimed_bytes=plan.reclaimed_bytes)


def delete_all_projects(db: Session, settings: Settings) -> BulkProjectDeletionResult:
    """Delete validated projects sequentially; stop and report accurately on first failure."""
    plan = plan_bulk_project_deletion(db, settings)
    # Queued work has no Project row yet. It is safe to remove before project
    # cleanup, and no running job reaches this point (the plan blocks those).
    db.execute(delete(GenerationJob).where(GenerationJob.status == "queued"))
    db.commit()
    deleted_projects = 0
    freed_bytes = 0
    failed_projects: dict[str, str] = {}
    for project in plan.projects:
        try:
            result = delete_project(db, project.project_id, settings)
        except ProjectDeletionError as exc:
            failed_projects[project.project_id] = str(exc)
            break
        deleted_projects += 1
        freed_bytes += result.reclaimed_bytes
    remaining_projects = int(db.scalar(select(func.count()).select_from(Project)) or 0)
    return BulkProjectDeletionResult(
        deleted_projects=deleted_projects,
        freed_bytes=freed_bytes,
        failed_projects=failed_projects,
        remaining_projects=remaining_projects,
    )


def update_project_audio(db: Session, project: Project, payload: AudioSettingsUpdate, settings: Settings) -> ProjectRevision:
    if payload.base_revision != project.current_revision:
        raise RevisionConflict("Project changed; reload before saving audio settings.")
    state = effective_revision_state(project)
    music = state.setdefault("music", {})
    music.update(volume=payload.music_volume, enabled=payload.music_enabled, requested_enabled=payload.music_enabled)
    state.setdefault("voice", {})["volume"] = payload.voice_volume
    state.pop("export", None)
    return _append_revision(db, project, base_revision=payload.base_revision, instruction="Adjust export audio", state=attach_hashes(state), changed=["music", "voice"], status=project.status)


def update_project_music_selection(
    db: Session, project: Project, payload: MusicSelectionUpdate
) -> ProjectRevision:
    if payload.base_revision != project.current_revision:
        raise RevisionConflict("Project changed; reload before changing music.")
    state = effective_revision_state(project)
    music = state.setdefault("music", {})
    if payload.track_id is None:
        music.update(enabled=False, requested_enabled=False, status="disabled", selection={"mode": payload.mode})
    else:
        tracks = {track.id: track for track in available_music_tracks()}
        track = tracks.get(payload.track_id)
        if track is None:
            raise ValueError("The selected music track is unavailable.")
        music.update(enabled=True, requested_enabled=True, status="planned", track=music_track_state(track), selection={"mode": payload.mode, "basis": "catalog_selection"})
    state.pop("export", None)
    return _append_revision(db, project, base_revision=payload.base_revision, instruction="Change export music", state=attach_hashes(state), changed=["music"], status=project.status)


def update_project_social_metadata(
    db: Session, project: Project, payload: SocialMetadataUpdate
) -> ProjectRevision:
    if payload.base_revision != project.current_revision:
        raise RevisionConflict("Project changed; reload before saving hashtags.")
    state = effective_revision_state(project)
    metadata = state.setdefault("social_metadata", {"status": "available", "platforms": {}})
    platforms = metadata.setdefault("platforms", {})
    for platform, values in payload.hashtags.items():
        previous = platforms.get(platform) or {}
        fields = (payload.metadata or {}).get(platform) or {}
        platforms[platform] = {
            "title": str(fields.get("title", previous.get("title", ""))).strip(),
            "description": str(fields.get("description", previous.get("description", ""))).strip(),
            "hashtags": normalize_hashtags(values),
            "manual": True,
        }
    metadata["status"] = "available"
    return _append_revision(db, project, base_revision=payload.base_revision, instruction="Edit social hashtags", state=attach_hashes(state), changed=["social_metadata"], status=project.status)


def regenerate_project_social_metadata(
    db: Session, project: Project, payload: SocialMetadataGenerate, settings: Settings
) -> ProjectRevision:
    if payload.base_revision != project.current_revision:
        raise RevisionConflict("Project changed; reload before generating hashtags.")
    state = effective_revision_state(project)
    generated = generate_social_metadata(state, settings)
    if payload.platform and generated.get("status") == "available":
        previous = (state.get("social_metadata") or {}).get("platforms") or {}
        generated["platforms"] = {**previous, payload.platform: generated["platforms"][payload.platform]}
    state["social_metadata"] = generated
    return _append_revision(db, project, base_revision=payload.base_revision, instruction="Generate social hashtags", state=attach_hashes(state), changed=["social_metadata"], status=project.status)


def create_project(
    db: Session,
    payload: ProjectCreate,
    settings: Settings,
    *,
    project_id: str | None = None,
    progress: ProgressCallback | None = None,
) -> Project:
    state = build_initial_state(
        payload.prompt, payload.options, settings, progress=progress
    )
    script_ready = any(
        stage["id"] == "script" and stage["status"] == "complete" for stage in state["pipeline"]
    )
    project = Project(
        **({"id": project_id} if project_id is not None else {}),
        original_prompt=payload.prompt,
        title=state["intent"]["topic"],
        status="ready_for_production" if script_ready else "needs_attention",
        current_revision=1,
        active_tip_revision=1,
    )
    project.revisions.append(
        ProjectRevision(
            number=1,
            parent_revision=None,
            instruction="Original prompt",
            kind="initial",
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
    kind: str = "user",
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
            active_tip_revision=next_number,
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
        kind=kind,
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
    auto_render: bool = False,
) -> ProjectRevision:
    state, changed = apply_edit(effective_revision_state(project), instruction, settings)
    return _persist_edited_state(
        db,
        project,
        state=state,
        changed=changed,
        instruction=instruction,
        settings=settings,
        base_revision=base_revision,
        auto_render=auto_render,
    )


def mutate_project_state(
    db: Session,
    project: Project,
    *,
    instruction: str,
    changed_roots: set[str],
    mutate: Callable[[dict], str],
    settings: Settings,
    base_revision: int | None = None,
    auto_render: bool = True,
) -> ProjectRevision:
    """Apply a validated tool mutation through the normal append-only revision lifecycle."""
    previous = current_revision(project)
    state = effective_revision_state(project)
    state["version"] = int(state.get("version", previous.number)) + 1
    summary = mutate(state)
    changed = expand_dependencies(changed_roots)
    if "render" in changed:
        previous_render = copy.deepcopy(state.get("render", {}))
        previous_url = previous_render.get("url")
        state["render"] = {
            **previous_render,
            "status": "regeneration_required",
            "url": previous_url,
            "stale": bool(previous_url),
        }
        state.setdefault("ai_review", {}).update(
            status="pending", items=[], automatic_corrections=[]
        )
    if "qc" in changed:
        state.setdefault("qc", {}).update(status="not_started", issues=[])
    state.setdefault("edit_history", []).append(
        {
            "revision": state["version"],
            "instruction": instruction,
            "changed_components": changed,
            "summary": summary,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    state = attach_hashes(state)
    return _persist_edited_state(
        db,
        project,
        state=state,
        changed=changed,
        instruction=instruction,
        settings=settings,
        base_revision=base_revision,
        auto_render=auto_render,
    )


def _persist_edited_state(
    db: Session,
    project: Project,
    *,
    state: dict,
    changed: list[str],
    instruction: str,
    settings: Settings,
    base_revision: int | None,
    auto_render: bool,
) -> ProjectRevision:
    status = "ready_for_production"
    if auto_render and "render" in changed:
        next_number = _next_revision_number(db, project.id)
        try:
            state = _render_state(state, project.id, next_number, settings)
            status = "rendered"
        except RenderUnavailable as exc:
            state["render"].update(
                status="regeneration_failed",
                stale=bool(state["render"].get("url")),
                error=str(exc),
            )
            if isinstance(exc, VoiceGenerationError):
                state["render"]["error_code"] = exc.category
            state = attach_hashes(state)
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction=instruction,
        state=state,
        changed=changed,
        status=status,
    )


def render_project(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    base_revision: int | None = None,
    progress: ProgressCallback | None = None,
) -> ProjectRevision:
    expected = base_revision if base_revision is not None else project.current_revision
    if expected != project.current_revision:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before rendering."
        )
    next_number = _next_revision_number(db, project.id)
    render_kwargs = {"progress": progress} if progress is not None else {}
    state = _render_state(
        effective_revision_state(project),
        project.id,
        next_number,
        settings,
        **render_kwargs,
    )
    # Secondary metadata never changes the outcome of a completed render.
    state["social_metadata"] = generate_social_metadata(state, settings)
    # Covers are secondary too: unavailable project imagery must never fail a
    # finished video render.
    try:
        state["thumbnails"] = build_project_thumbnails(state, project.id, settings)
    except Exception as exc:  # noqa: BLE001 - thumbnail failure is non-fatal
        state["thumbnails"] = {
            "status": "unavailable",
            "error": str(exc),
            "selected_variant_id": None,
            "variants": [],
        }
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction="Render video",
        state=state,
        changed=["voice", "alignment", "captions", "timeline", "render", "qc", "social_metadata", "thumbnails"],
        status="rendered",
        kind="system",
    )


def regenerate_project_thumbnails(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    base_revision: int | None = None,
) -> ProjectRevision:
    state = copy.deepcopy(effective_revision_state(project))
    state["thumbnails"] = build_project_thumbnails(state, project.id, settings)
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction="Build cover thumbnails",
        state=attach_hashes(state),
        changed=["thumbnails"],
        status=project.status,
        kind="system",
    )


def select_project_thumbnail(
    db: Session,
    project: Project,
    variant_id: str,
    *,
    base_revision: int | None = None,
) -> ProjectRevision:
    state = copy.deepcopy(effective_revision_state(project))
    thumbnails = state.get("thumbnails") or {}
    variants = thumbnails.get("variants") or []
    if not any(str(item.get("id")) == variant_id for item in variants if isinstance(item, dict)):
        raise ValueError("Thumbnail variant not found.")
    thumbnails["selected_variant_id"] = variant_id
    state["thumbnails"] = thumbnails
    return _append_revision(
        db,
        project,
        base_revision=base_revision,
        instruction="Select cover thumbnail",
        state=attach_hashes(state),
        changed=["thumbnails"],
        status=project.status,
        kind="user",
    )


def export_project(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    base_revision: int | None = None,
) -> tuple[ProjectRevision, ExportResult]:
    expected = base_revision if base_revision is not None else project.current_revision
    locked = get_project(db, project.id, lock=True)
    if locked is None or expected != locked.current_revision:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before exporting."
        )
    project = locked
    previous = current_revision(project)
    finalized = finalize_export(project.id, project.title, previous.state, settings)
    if finalized.already_exported:
        cleanup = cleanup_project_files(project.id, settings)
        return previous, ExportResult(
            filename=finalized.filename,
            display_path=finalized.display_path,
            media_url=finalized.media_url,
            file_size=finalized.file_size,
            exported_at=finalized.exported_at,
            cleanup=cleanup,
            already_exported=True,
        )

    state = copy.deepcopy(previous.state)
    state["export"] = {
        "status": "exported",
        "filename": finalized.filename,
        "display_path": finalized.display_path,
        "file_size": finalized.file_size,
        "exported_at": finalized.exported_at,
        "media_url": finalized.media_url,
        "source_revision": previous.number,
        "cleanup_status": "pending",
        "cleanup_warnings": [],
        "cleaned_directories": [],
    }
    state.setdefault("render", {}).update(
        status="complete",
        url=finalized.media_url,
        file_size=finalized.file_size,
        format="mp4",
        stale=False,
        exported=True,
    )
    state = attach_hashes(state)
    try:
        export_revision = _append_revision(
            db,
            project,
            base_revision=base_revision,
            instruction="Export MP4",
            state=state,
            changed=["export", "render"],
            status="exported",
            kind="system",
        )
    except Exception:
        rollback_finalized_export(finalized)
        raise

    finalization_warnings = commit_finalized_export(finalized)
    cleanup = cleanup_project_files(project.id, settings)
    warnings = (*finalization_warnings, *cleanup.warnings)
    cleanup = CleanupResult(
        status="warning" if warnings else "complete",
        removed=cleanup.removed,
        warnings=warnings,
    )

    cleanup_state = copy.deepcopy(export_revision.state)
    cleanup_state["export"].update(
        cleanup_status=cleanup.status,
        cleanup_warnings=list(cleanup.warnings),
        cleaned_directories=list(cleanup.removed),
    )
    asset_status = "cleaned_after_export" if cleanup.status == "complete" else "cleanup_warning"
    cleanup_state.setdefault("assets", {})["status"] = asset_status
    for scene in cleanup_state.get("scenes", []):
        if (scene.get("media") or {}).get("manually_selected"):
            continue
        scene["asset_status"] = asset_status
        media = scene.get("media")
        if isinstance(media, dict):
            media.pop("cache_path", None)
    cleanup_state = attach_hashes(cleanup_state)
    try:
        revision = _append_revision(
            db,
            project,
            base_revision=export_revision.number,
            instruction="Clean exported project media",
            state=cleanup_state,
            changed=["export", "assets"],
            status="exported",
            kind="system",
        )
    except Exception:  # noqa: BLE001 - export remains valid after optional cleanup metadata fails
        db.rollback()
        refreshed = get_project(db, project.id)
        revision = current_revision(refreshed) if refreshed is not None else export_revision
        cleanup = CleanupResult(
            status="warning",
            removed=cleanup.removed,
            warnings=(
                *cleanup.warnings,
                "Export succeeded and project media was cleaned, but cleanup status could not be saved.",
            ),
        )
    return revision, ExportResult(
        filename=finalized.filename,
        display_path=finalized.display_path,
        media_url=finalized.media_url,
        file_size=finalized.file_size,
        exported_at=finalized.exported_at,
        cleanup=cleanup,
    )


def _render_state(
    previous_state: dict,
    project_id: str,
    revision_number: int,
    settings: Settings,
    *,
    progress: ProgressCallback | None = None,
) -> dict:
    state = copy.deepcopy(previous_state)
    _refresh_script_derivatives(state, old_scenes=state.get("scenes", []))
    prepare_project_media(state, project_id, settings, progress=progress)
    script_before_review = state["script"]["text"]
    report_progress(progress, "review", "Reviewing content quality", phase="start")
    run_ai_review(state, settings)
    # Review may rewrite the opening. Keep exactly one hook for TTS/captions:
    # sync selected_hook to a single surviving hook, or insert/collapse via the
    # authoritative selector when review left zero or many hook blocks.
    blocks_before_hook = copy.deepcopy(state.get("script", {}).get("blocks", []))
    hooks = [block for block in state["script"]["blocks"] if _is_hook_block(block)]
    if len(hooks) == 1:
        state["script"]["selected_hook"] = str(hooks[0].get("text") or "").strip() or state[
            "script"
        ].get("selected_hook")
    elif len(hooks) > 1 or not hooks:
        selected = state.get("script", {}).get("selected_hook")
        if not hooks and selected:
            state["script"]["blocks"] = _apply_selected_hook(
                state["script"]["blocks"], str(selected)
            )
        else:
            hooked_blocks, candidate = _authoritative_hook_blocks(
                state["script"]["blocks"],
                state.get("intent", {}),
                state.get("facts", []),
                state.get("script", {}).get("hook_candidates") or [],
            )
            state["script"]["blocks"] = hooked_blocks
            if candidate:
                state["script"]["selected_hook"] = candidate.text
                state["script"]["selected_hook_strategy"] = candidate.strategy
    if state["script"]["blocks"] != blocks_before_hook:
        _refresh_script_derivatives(state, old_scenes=state.get("scenes", []))
    if state["script"]["text"] != script_before_review:
        prepare_project_media(state, project_id, settings)
    report_progress(progress, "review", "Reviewing content quality", phase="complete")
    result = render_video(
        state, project_id, revision_number, settings, progress=progress
    )
    state["duration"]["actual_seconds"] = result.actual_seconds
    state["voice"]["provider"] = result.voice_provider
    state["voice"]["status"] = "complete"
    for block in state["voice"]["blocks"]:
        block["status"] = "complete"
    state["scenes"] = _build_scenes(
        state["script"]["blocks"],
        result.actual_seconds,
        state["scenes"],
        str(state.get("timeline", {}).get("cut_pace") or "fast"),
    )
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
        "revision": revision_number,
        "stale": False,
    }
    state["qc"].update({"status": "passed", "round": 1, "issues": []})
    for stage in state["pipeline"]:
        if stage["status"] != "blocked":
            stage["status"] = "complete"
    return attach_hashes(state)


def _revision_map(project: Project) -> dict[int, ProjectRevision]:
    return {revision.number: revision for revision in project.revisions}


def _active_revision_chain(project: Project) -> list[ProjectRevision]:
    revisions = _revision_map(project)
    cursor = revisions.get(project.active_tip_revision) or revisions.get(project.current_revision)
    chain: list[ProjectRevision] = []
    seen: set[int] = set()
    while cursor is not None and cursor.number not in seen:
        chain.append(cursor)
        seen.add(cursor.number)
        cursor = revisions.get(cursor.parent_revision) if cursor.parent_revision is not None else None
    return list(reversed(chain))


def _is_user_visible_revision(revision: ProjectRevision) -> bool:
    return revision.kind in {"initial", "user"}


def revision_navigation_targets(
    project: Project,
) -> tuple[ProjectRevision | None, ProjectRevision | None]:
    chain = _active_revision_chain(project)
    positions = {revision.number: index for index, revision in enumerate(chain)}
    current_position = positions.get(project.current_revision)
    if current_position is None:
        return None, None
    visible = [
        (index, revision)
        for index, revision in enumerate(chain)
        if _is_user_visible_revision(revision)
    ]
    anchor = next(
        (
            visible_index
            for visible_index in range(len(visible) - 1, -1, -1)
            if visible[visible_index][0] <= current_position
        ),
        None,
    )
    if anchor is None:
        return None, visible[0][1] if visible else None
    undo_target = visible[anchor - 1][1] if anchor > 0 else None
    redo_target = visible[anchor + 1][1] if anchor + 1 < len(visible) else None
    if visible[anchor][1].number != project.current_revision and redo_target is not None:
        # A system revision after the latest visible edit is already at that creative state.
        redo_target = None
    return undo_target, redo_target


def _move_revision_pointer(
    db: Session,
    project: Project,
    target: ProjectRevision,
    *,
    expected: int,
    action: str,
) -> ProjectRevision:
    target_state = effective_revision_state(project, target)
    target_render = target_state.get("render", {})
    if target_state.get("export") and target_render.get("status") == "complete" and not target_render.get("stale"):
        target_status = "exported"
    elif target_render.get("status") == "complete":
        target_status = "rendered"
    else:
        target_status = "ready_for_production"
    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.current_revision == expected)
        .values(
            current_revision=target.number,
            status=target_status,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise RevisionConflict(f"Project changed while {action} was being applied.")
    db.commit()
    db.refresh(project)
    return target


def undo_project(
    db: Session, project: Project, *, base_revision: int | None = None
) -> ProjectRevision | None:
    expected = base_revision if base_revision is not None else project.current_revision
    if project.current_revision != expected:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before undoing."
        )
    target, _redo = revision_navigation_targets(project)
    if target is None:
        return None
    return _move_revision_pointer(db, project, target, expected=expected, action="undo")


def redo_project(
    db: Session, project: Project, *, base_revision: int | None = None
) -> ProjectRevision | None:
    expected = base_revision if base_revision is not None else project.current_revision
    if project.current_revision != expected:
        raise RevisionConflict(
            f"Project changed since revision {expected}; reload before redoing."
        )
    _undo, target = revision_navigation_targets(project)
    if target is None:
        return None
    return _move_revision_pointer(db, project, target, expected=expected, action="redo")


def canonical_export_metadata(project: Project) -> dict | None:
    for revision in sorted(project.revisions, key=lambda item: item.number, reverse=True):
        export = revision.state.get("export")
        if isinstance(export, dict) and export.get("status") == "exported":
            return copy.deepcopy(export)
    return None


def effective_revision_state(
    project: Project, revision: ProjectRevision | None = None
) -> dict:
    revision = revision or current_revision(project)
    state = copy.deepcopy(revision.state)
    if revision.number == project.active_tip_revision:
        return state
    export = canonical_export_metadata(project)
    if export is None:
        return state
    state["export"] = export
    source_number = int(export.get("source_revision") or 0)
    revisions = _revision_map(project)
    source = revisions.get(source_number)
    while source is not None and not _is_user_visible_revision(source):
        source = revisions.get(source.parent_revision) if source.parent_revision is not None else None
    matches_export = source is not None and source.number == revision.number
    render = state.setdefault("render", {})
    render.update(
        url=export.get("media_url"),
        file_size=export.get("file_size"),
        format="mp4",
        exported=True,
        status="complete" if matches_export else "regeneration_required",
        stale=not matches_export,
    )
    if export.get("cleanup_status") in {"complete", "warning"}:
        state.setdefault("assets", {})["status"] = "regeneration_required"
        for scene in state.get("scenes", []):
            if (scene.get("media") or {}).get("manually_selected"):
                continue
            scene["asset_status"] = "regeneration_required"
            media = scene.get("media")
            if isinstance(media, dict):
                media.pop("cache_path", None)
    return state


def serialize_project(project: Project) -> dict:
    revision = current_revision(project)
    undo_target, redo_target = revision_navigation_targets(project)
    active_numbers = {item.number for item in _active_revision_chain(project)}
    return {
        "id": project.id,
        "original_prompt": project.original_prompt,
        "title": project.title,
        "status": project.status,
        "current_revision": project.current_revision,
        "active_tip_revision": project.active_tip_revision,
        "can_undo": undo_target is not None,
        "can_redo": redo_target is not None,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "revision": {
            "id": revision.id,
            "number": revision.number,
            "parent_revision": revision.parent_revision,
            "instruction": revision.instruction,
            "kind": revision.kind,
            "changed_components": revision.changed_components,
            "state": effective_revision_state(project, revision),
            "created_at": revision.created_at,
        },
        "revisions": [
            {
                "id": item.id,
                "number": item.number,
                "parent_revision": item.parent_revision,
                "instruction": item.instruction,
                "kind": item.kind,
                "changed_components": item.changed_components,
                "created_at": item.created_at.isoformat(),
                "is_current": item.number == project.current_revision,
                "is_user_visible": _is_user_visible_revision(item),
                "on_active_branch": item.number in active_numbers,
            }
            for item in reversed(project.revisions)
        ],
    }
