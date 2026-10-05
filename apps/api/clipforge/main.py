import re
import sys
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import SessionLocal, get_db, prepare_schema
from .editor_agent import (
    list_chat_messages,
    run_editor_turn,
    serialize_agent_turn,
    serialize_chat_message,
)
from .exporter import ExportUnavailable, exported_video_path
from .generation import (
    active_generation_job,
    clear_queued_generation_jobs,
    create_generation_job,
    finish_orphaned_cancel,
    get_generation_job,
    list_generation_jobs,
    mark_interrupted_generation_jobs,
    remove_queued_generation_job,
    request_generation_cancel,
    schedule_next_generation,
    serialize_generation_job,
)
from .integrations import get_secret_store
from .integrations import router as integrations_router
from .media_candidates import (
    CandidateError,
    apply_scene_media_candidate,
    discover_scene_media_candidates,
    generate_scene_media,
    scene_generated_alternatives,
    scene_generation_option,
)
from .models import GenerationJob, Project, YouTubeUpload
from .music import available_music_tracks, resolve_track_path
from .pipeline import UnsupportedEdit
from .renderer import RenderUnavailable, VoiceGenerationError, readiness
from .runtime_identity import label as runtime_label
from .runtime_identity import runtime_identity
from .schemas import (
    AudioSettingsUpdate,
    ChatCreate,
    ChatMessageRead,
    ChatTurnRead,
    EditCreate,
    ExportCreate,
    GenerationJobRead,
    HealthRead,
    MusicSelectionUpdate,
    ProjectCreate,
    ProjectExportRead,
    ProjectRead,
    RenderCreate,
    SceneImageGenerate,
    SceneImageGenerationResultRead,
    SceneMediaCandidateApply,
    SceneMediaCandidatesRead,
    SocialMetadataGenerate,
    SocialMetadataUpdate,
    ThumbnailSelectionUpdate,
    VoicePreviewCreate,
    VoicePreviewRead,
)
from .security.secrets import SecretStore
from .services import (
    TERMINAL_JOB_STATUSES,
    ProjectDeletionBusy,
    ProjectDeletionError,
    RevisionConflict,
    canonical_export_metadata,
    create_project,
    delete_project,
    edit_project,
    effective_revision_state,
    export_project,
    get_project,
    plan_bulk_project_deletion,
    project_music_recommendations,
    redo_project,
    regenerate_project_social_metadata,
    regenerate_project_thumbnails,
    render_project,
    select_project_thumbnail,
    serialize_project,
    undo_project,
    update_project_audio,
    update_project_music_selection,
    update_project_social_metadata,
)
from .topic_intelligence import service as topic_intelligence
from .topic_intelligence.routes import router as topic_intelligence_router
from .voice_preview import (
    PreviewRateLimited,
    enforce_preview_rate_limit,
    generate_voice_preview,
)
from .youtube import lifecycle
from .youtube.provider import YouTubeProvider
from .youtube.routes import get_youtube_provider, videos_router
from .youtube.routes import router as youtube_router
from .youtube.uploads import mark_interrupted_uploads


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Detected once per process; the launcher's backend.log keeps this line.
    identity = runtime_identity()
    print(
        f"ClipForge runtime: {runtime_label(identity)} source={identity['source']} "
        f"repo={identity['repo_path']} commit={identity['commit']} dirty={identity['dirty']} "
        f"worktree={identity['is_worktree']} package={identity['package_path']}"
        + (f" error={identity['error']}" if identity.get("error") else ""),
        file=sys.stderr,
        flush=True,
    )
    prepare_schema()
    with SessionLocal() as db:
        mark_interrupted_generation_jobs(db)
        mark_interrupted_uploads(db)
    schedule_next_generation(settings)
    # Home suggestions are researched beside the app; generation never waits for it.
    topic_intelligence.warm_pool_in_background(
        SessionLocal, settings,
        lambda db: topic_intelligence.default_deps(db, settings, get_secret_store(), get_youtube_provider()),
    )
    yield


settings = get_settings()
settings.render_root.mkdir(parents=True, exist_ok=True)
DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
YouTubeProviderDep = Annotated[YouTubeProvider, Depends(get_youtube_provider)]
# Plain defaults keep delete_project_route callable directly (tests call it so).
_STORE_DEPENDENCY = Depends(get_secret_store)
_PROVIDER_DEPENDENCY = Depends(get_youtube_provider)
app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.render_root.resolve()), name="media")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(integrations_router)
app.include_router(youtube_router)
app.include_router(videos_router)
app.include_router(topic_intelligence_router)


@app.get("/api/health", response_model=HealthRead)
def health(config: SettingsDep) -> HealthRead:
    return HealthRead(
        status="ok",
        service="clipforge-api",
        ai_mode=config.clipforge_ai_mode,
        runtime=runtime_identity(),
    )


@app.get("/api/readiness")
def build_readiness(config: SettingsDep) -> dict:
    return readiness(config)


@app.get("/api/projects", response_model=list[ProjectRead])
def list_projects(db: DbSession) -> list[dict]:
    projects = db.scalars(select(Project).order_by(Project.updated_at.desc())).all()
    return [serialize_project(project) for project in projects]


def _history_prompt(job: GenerationJob) -> str | None:
    """The job's request prompt, or ``None`` when the job has no usable request data.

    Only jobs created before ``request_payload`` existed have none (the column
    defaults to ``{}``); the worker fails those with "Generation request data is
    unavailable" before any project exists, so they never became user projects.
    Same rule as ``ProjectCreate.prompt`` (normalized, at least 3 characters).
    """
    prompt = (job.request_payload or {}).get("prompt")
    text = " ".join(prompt.split()) if isinstance(prompt, str) else ""
    return text if len(text) >= 3 else None


@app.get("/api/projects/overview")
def list_project_overview(db: DbSession) -> list[dict]:
    """Lightweight, complete history, including jobs awaiting their first revision.

    Every Project row is listed (whatever its status). A job without a Project
    row is listed with its prompt - also when it failed early - unless it has
    no usable request data (a technical placeholder, see ``_history_prompt``).
    """
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    jobs = db.scalars(select(GenerationJob).order_by(GenerationJob.created_at.desc())).all()
    latest_jobs = {}
    for job in jobs:
        latest_jobs.setdefault(job.project_id, job)
    result = [
        {
            "id": project.id,
            "title": project.title,
            "status": latest_jobs[project.id].status if project.id in latest_jobs and latest_jobs[project.id].status in ("queued", "running", "cancelling", "failed", "cancelled") else project.status,
            "current_revision": project.current_revision,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }
        for project in projects
    ]
    project_ids = {project.id for project in projects}
    for job in jobs:
        if job.project_id not in project_ids:
            project_ids.add(job.project_id)  # older jobs of the same id never override the latest
            prompt = _history_prompt(job)
            if prompt is None:
                continue
            result.append({
                "id": job.project_id,
                "title": prompt,
                "status": job.status,
                "current_revision": None,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            })
    return sorted(result, key=lambda item: (item["created_at"], item["id"]), reverse=True)


def _serialize_bulk_delete_plan(plan) -> dict:
    return {
        "project_count": len(plan.projects),
        "project_ids": [item.project_id for item in plan.projects],
        "total_bytes": plan.total_bytes,
        "total_files": plan.total_files,
        "total_directories": plan.total_directories,
        "shared_cache_excluded": True,
    }


@app.get("/api/projects/delete-plan")
def bulk_delete_plan_route(db: DbSession, config: SettingsDep) -> dict:
    try:
        plan = plan_bulk_project_deletion(db, config)
    except ProjectDeletionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    archived = sum(
        1 for item in plan.projects
        if any(lifecycle.classify_upload(db, upload) == lifecycle.SUCCEEDED for upload in db.scalars(select(YouTubeUpload).where(YouTubeUpload.project_id == item.project_id)).all())
    )
    # Visible Recent Projects entries without a project (finished requests), counted
    # with the overview's own rule; bulk deletion clears them too.
    history_entries = sum(
        1 for item in list_project_overview(db)
        if item["current_revision"] is None and item["status"] in TERMINAL_JOB_STATUSES
    )
    return {
        **_serialize_bulk_delete_plan(plan),
        "projects_keeping_learning_record": archived,
        "history_entry_count": history_entries,
    }


@app.delete("/api/projects")
def delete_all_projects_route(db: DbSession, config: SettingsDep, store: SecretStoreDep, provider: YouTubeProviderDep) -> dict:
    try:
        return lifecycle.delete_all_lifecycle(db, config, store, provider)
    except ProjectDeletionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _with_topic_provenance(db: Session, payload: ProjectCreate) -> ProjectCreate:
    """Manual and Topic Intelligence questions enter the same pipeline; attach provenance."""
    try:
        return topic_intelligence.resolve_topic_provenance(db, payload)
    except topic_intelligence.TopicHandoffError as exc:
        code = status.HTTP_409_CONFLICT if exc.code == "already_used" else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(status_code=code, detail={"status": exc.code, "message": exc.message}) from exc


@app.post("/api/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project_route(
    payload: ProjectCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    payload = _with_topic_provenance(db, payload)
    project = create_project(db, payload, config)
    topic_intelligence.mark_topic_used(db, payload, project.id)
    return serialize_project(project)


@app.post(
    "/api/generation-jobs",
    response_model=GenerationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_generation_job_route(
    payload: ProjectCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    payload = _with_topic_provenance(db, payload)
    job, created = create_generation_job(db, payload)
    topic_intelligence.mark_topic_used(db, payload, job.project_id)
    if created:
        schedule_next_generation(config)
    return serialize_generation_job(job)


@app.get("/api/generation-jobs", response_model=list[GenerationJobRead])
def list_generation_jobs_route(db: DbSession) -> list[dict]:
    return list_generation_jobs(db)


@app.delete("/api/generation-jobs/queue", status_code=status.HTTP_204_NO_CONTENT)
def clear_generation_queue_route(db: DbSession) -> None:
    clear_queued_generation_jobs(db)


@app.delete("/api/generation-jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_generation_job_route(job_id: str, db: DbSession) -> None:
    if remove_queued_generation_job(db, job_id):
        return
    job = get_generation_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    raise HTTPException(status_code=409, detail="Only queued generation jobs can be removed")


@app.post("/api/generation-jobs/{job_id}/cancel", response_model=GenerationJobRead)
def cancel_generation_job_route(job_id: str, db: DbSession, config: SettingsDep) -> dict:
    """Cancel the RUNNING generation (idempotent).  Never deletes anything.

    The job becomes ``cancelling`` at once and ``cancelled`` when the worker
    reaches its next checkpoint; the project and its data are kept.  Queued
    jobs keep their own action (remove); finished jobs are left unchanged (409).
    """
    outcome, job = request_generation_cancel(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    if outcome == "not_running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": job.status, "message": "Only a running generation can be cancelled; queued jobs are removed from the queue."},
        )
    if outcome in {"requested", "cancelling"} and finish_orphaned_cancel(db, job_id, config):
        db.refresh(job)  # no worker held it (e.g. just claimed): ended here, the queue continues
    return serialize_generation_job(job)


@app.get("/api/generation-jobs/active", response_model=GenerationJobRead | None)
def active_generation_job_route(db: DbSession) -> dict | None:
    job = active_generation_job(db)
    return serialize_generation_job(job) if job is not None else None


@app.get("/api/generation-jobs/projects/{project_id}", response_model=GenerationJobRead)
def get_project_generation_job_route(project_id: str, db: DbSession) -> dict:
    job = db.scalar(select(GenerationJob).where(GenerationJob.project_id == project_id).order_by(GenerationJob.created_at.desc()))
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    position = next(
        (item["queue_position"] for item in list_generation_jobs(db) if item["id"] == job.id),
        None,
    )
    return serialize_generation_job(job, queue_position=position)


@app.get("/api/generation-jobs/{job_id}", response_model=GenerationJobRead)
def get_generation_job_route(job_id: str, db: DbSession) -> dict:
    job = get_generation_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    return serialize_generation_job(job)


@app.get("/api/projects/{project_id}", response_model=ProjectRead)
def get_project_route(project_id: str, db: DbSession) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return serialize_project(project)


@app.get("/api/projects/{project_id}/delete-plan")
def project_delete_plan_route(project_id: str, db: DbSession, config: SettingsDep, store: SecretStoreDep, provider: YouTubeProviderDep) -> dict:
    """What deleting this project will do (verifies YouTube uploads live where possible)."""
    try:
        return lifecycle.plan_deletion(db, project_id, config, store, provider).as_dict()
    except ProjectDeletionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}")
def delete_project_route(
    project_id: str,
    db: DbSession,
    config: SettingsDep,
    store: SecretStore = _STORE_DEPENDENCY,
    provider: YouTubeProvider = _PROVIDER_DEPENDENCY,
    confirm_unverified: bool = False,
) -> dict:
    try:
        if get_project(db, project_id) is None:
            # Queued generation requests without a Project row.
            result = delete_project(db, project_id, config)
            return {"mode": "full", "freed_bytes": result.reclaimed_bytes, "retained_bytes": 0, "archive_id": None}
        return lifecycle.delete_project_lifecycle(db, project_id, config, store, provider, confirm_unverified=confirm_unverified)
    except lifecycle.ProjectDeletionUnverified as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"status": "upload_unverified", "message": "Upload status could not be verified.", "plan": jsonable_encoder(exc.plan.as_dict())},
        ) from exc
    except ProjectDeletionBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProjectDeletionError as exc:
        status_code = status.HTTP_404_NOT_FOUND if str(exc) == "Project not found." else status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/chat", response_model=list[ChatMessageRead])
def get_project_chat_route(project_id: str, db: DbSession) -> list[dict]:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return [serialize_chat_message(message) for message in list_chat_messages(db, project_id)]


@app.get("/api/projects/{project_id}/scenes/{scene_number}/media-candidates", response_model=SceneMediaCandidatesRead)
def scene_media_candidates_route(project_id: str, scene_number: int, db: DbSession, config: SettingsDep) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    state = effective_revision_state(project)
    try:
        _, candidates = discover_scene_media_candidates(
            state, project.id, scene_number, project.current_revision, config
        )
    except CandidateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scene = state["scenes"][scene_number - 1]
    preferred = str((scene.get("media") or {}).get("kind") or scene.get("preferred_media") or "video")
    return {
        "scene_number": scene_number,
        "preferred_kind": preferred if preferred in {"video", "photo"} else "video",
        # Persisted AI alternatives first, then the fetched real alternatives.
        "candidates": [*scene_generated_alternatives(state, scene_number, config), *candidates],
        "generation": scene_generation_option(state, scene_number, config),
    }


@app.post("/api/projects/{project_id}/scenes/{scene_number}/generate-image", response_model=SceneImageGenerationResultRead)
def generate_scene_image_route(
    project_id: str, scene_number: int, payload: SceneImageGenerate, db: DbSession, config: SettingsDep
) -> dict:
    """Manual, user-confirmed paid generation of a new scene alternative.

    Always answers with a status (generated | failed | rejected | unchanged),
    a UI-safe message and the current project, so the Change Media panel can
    show the new image or the reason immediately.
    """
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.base_revision != project.current_revision:
        raise HTTPException(status_code=409, detail="Project changed; reload before generating media.")
    try:
        result = generate_scene_media(db, project, scene_number, config, prompt=payload.prompt)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(project)
    return {**result, "project": serialize_project(project)}


@app.post("/api/projects/{project_id}/scenes/{scene_number}/media-candidates/apply", response_model=ProjectRead)
def apply_scene_media_candidate_route(
    project_id: str, scene_number: int, payload: SceneMediaCandidateApply, db: DbSession, config: SettingsDep
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.base_revision != project.current_revision:
        raise HTTPException(status_code=409, detail="Project changed; reload before applying media.")
    try:
        apply_scene_media_candidate(db, project, scene_number, payload.token, config, auto_render=True)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/chat", response_model=ChatTurnRead)
def project_chat_route(
    project_id: str,
    payload: ChatCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    result = run_editor_turn(db, project, payload.message, config)
    return serialize_agent_turn(result)


@app.post("/api/voice/preview", response_model=VoicePreviewRead)
def voice_preview_route(
    payload: VoicePreviewCreate,
    request: Request,
    config: SettingsDep,
) -> dict:
    client_id = request.client.host if request.client else "local"
    try:
        enforce_preview_rate_limit(client_id)
        return generate_voice_preview(payload, config)
    except PreviewRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except VoiceGenerationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"status": exc.category, "message": str(exc)},
        ) from exc
    except RenderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _is_undo_instruction(instruction: str) -> bool:
    text = instruction.casefold().strip(" .!?")
    if any(negation in text for negation in ("nicht rückgängig", "nicht rueckgaengig", "don't undo", "do not undo")):
        return False
    return bool(
        re.fullmatch(
            r"(undo(?: that| last(?: edit)?)?|(?:bitte )?(?:mach(?:e)? )?(?:das |die letzte änderung )?"
            r"(?:rückgängig|rueckgaengig))",
            text,
        )
    )


def _is_redo_instruction(instruction: str) -> bool:
    text = instruction.casefold().strip(" .!?")
    return bool(
        re.fullmatch(
            r"(?:redo(?: that| last(?: edit)?)?|(?:bitte )?(?:wiederholen|erneut anwenden))",
            text,
        )
    )


@app.post("/api/projects/{project_id}/edits", response_model=ProjectRead)
def edit_project_route(
    project_id: str,
    payload: EditCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        if _is_undo_instruction(payload.instruction):
            if undo_project(db, project, base_revision=payload.base_revision) is None:
                raise HTTPException(status_code=409, detail="There is no earlier revision")
        elif _is_redo_instruction(payload.instruction):
            if redo_project(db, project, base_revision=payload.base_revision) is None:
                raise HTTPException(status_code=409, detail="There is no edit to redo")
        else:
            edit_project(
                db,
                project,
                payload.instruction,
                config,
                base_revision=payload.base_revision,
                auto_render=True,
            )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedEdit as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/render", response_model=ProjectRead)
def render_project_route(
    project_id: str,
    payload: RenderCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        render_project(
            db,
            project,
            config,
            base_revision=payload.base_revision,
        )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VoiceGenerationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"status": exc.category, "message": str(exc)},
        ) from exc
    except RenderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/thumbnails/generate", response_model=ProjectRead)
def generate_project_thumbnails_route(
    project_id: str,
    payload: RenderCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        regenerate_project_thumbnails(db, project, config, base_revision=payload.base_revision)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.patch("/api/projects/{project_id}/thumbnails", response_model=ProjectRead)
def select_project_thumbnail_route(
    project_id: str,
    payload: ThumbnailSelectionUpdate,
    db: DbSession,
) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        select_project_thumbnail(db, project, payload.variant_id, base_revision=payload.base_revision)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.patch("/api/projects/{project_id}/audio", response_model=ProjectRead)
def update_audio_route(project_id: str, payload: AudioSettingsUpdate, db: DbSession, config: SettingsDep) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        update_project_audio(db, project, payload, config)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RenderUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


def _serialize_music_track(track) -> dict:
    return {
        "id": track.id, "title": track.title, "mood": track.mood,
        "energy": track.energy, "tags": list(track.tags), "source": track.source,
        "license": track.license, "attribution": track.attribution,
        "description": track.description, "duration_seconds": track.duration_seconds,
        "preview_url": f"/api/music/tracks/{track.id}/preview",
    }


@app.get("/api/projects/{project_id}/music/tracks")
def list_project_music_tracks_route(project_id: str, db: DbSession, config: SettingsDep, mode: str = "all_music") -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    catalog = available_music_tracks()
    if mode == "ai_matched":
        catalog = project_music_recommendations(db, project, catalog, config)
    elif mode != "all_music":
        raise HTTPException(status_code=422, detail="Unsupported music mode")
    return {"mode": mode, "tracks": [_serialize_music_track(track) for track in catalog], "revision": project.current_revision}


@app.get("/api/music/tracks/{track_id}/preview")
def music_track_preview_route(track_id: str):
    track = next((item for item in available_music_tracks() if item.id == track_id), None)
    if track is None:
        raise HTTPException(status_code=404, detail="Music track not found")
    path = resolve_track_path({"track": {"file": track.file_path}})
    if path is None:
        raise HTTPException(status_code=404, detail="Music preview is unavailable")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.patch("/api/projects/{project_id}/music", response_model=ProjectRead)
def update_music_selection_route(project_id: str, payload: MusicSelectionUpdate, db: DbSession) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        update_project_music_selection(db, project, payload)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.patch("/api/projects/{project_id}/social-metadata", response_model=ProjectRead)
def update_social_metadata_route(project_id: str, payload: SocialMetadataUpdate, db: DbSession) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        update_project_social_metadata(db, project, payload)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/social-metadata/generate", response_model=ProjectRead)
def generate_social_metadata_route(project_id: str, payload: SocialMetadataGenerate, db: DbSession, config: SettingsDep) -> dict:
    project = get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        regenerate_project_social_metadata(db, project, payload, config)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/export", response_model=ProjectExportRead)
def export_project_route(
    project_id: str,
    payload: ExportCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        _revision, result = export_project(
            db,
            project,
            config,
            base_revision=payload.base_revision,
        )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExportUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return {
        "success": True,
        "exported_filename": result.filename,
        "display_path": result.display_path,
        "file_size": result.file_size,
        "cleanup_status": result.cleanup.status,
        "cleanup_warnings": list(result.cleanup.warnings),
        "exported_at": result.exported_at,
        "media_url": result.media_url,
        "already_exported": result.already_exported,
        "project": serialize_project(project),
    }


@app.get("/api/projects/{project_id}/exported-video", response_class=FileResponse)
def exported_video_route(
    project_id: str,
    db: DbSession,
    config: SettingsDep,
) -> FileResponse:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    metadata = canonical_export_metadata(project)
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=404, detail="Exported video not found")
    try:
        path = exported_video_path(project.id, project.title, metadata, config)
    except ExportUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=None)


@app.post("/api/projects/{project_id}/undo", response_model=ProjectRead)
def undo_project_route(
    project_id: str,
    payload: RenderCreate,
    db: DbSession,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        if undo_project(db, project, base_revision=payload.base_revision) is None:
            raise HTTPException(status_code=409, detail="There is no earlier revision")
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


@app.post("/api/projects/{project_id}/redo", response_model=ProjectRead)
def redo_project_route(
    project_id: str,
    payload: RenderCreate,
    db: DbSession,
) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        if redo_project(db, project, base_revision=payload.base_revision) is None:
            raise HTTPException(status_code=409, detail="There is no edit to redo")
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)
