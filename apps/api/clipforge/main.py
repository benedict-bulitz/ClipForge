import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import Base, engine, ensure_runtime_schema, get_db
from .models import Project
from .pipeline import UnsupportedEdit
from .renderer import RenderUnavailable, readiness
from .schemas import EditCreate, HealthRead, ProjectCreate, ProjectRead, RenderCreate
from .services import (
    RevisionConflict,
    create_project,
    edit_project,
    get_project,
    render_project,
    serialize_project,
    undo_project,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
    yield


settings = get_settings()
settings.render_root.mkdir(parents=True, exist_ok=True)
DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.render_root.resolve()), name="media")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=HealthRead)
def health(config: SettingsDep) -> HealthRead:
    return HealthRead(status="ok", service="clipforge-api", ai_mode=config.clipforge_ai_mode)


@app.get("/api/readiness")
def build_readiness(config: SettingsDep) -> dict:
    return readiness(config)


@app.get("/api/projects", response_model=list[ProjectRead])
def list_projects(db: DbSession) -> list[dict]:
    projects = db.scalars(select(Project).order_by(Project.updated_at.desc()).limit(20)).all()
    return [serialize_project(project) for project in projects]


@app.post("/api/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project_route(
    payload: ProjectCreate,
    db: DbSession,
    config: SettingsDep,
) -> dict:
    return serialize_project(create_project(db, payload, config))


@app.get("/api/projects/{project_id}", response_model=ProjectRead)
def get_project_route(project_id: str, db: DbSession) -> dict:
    project = get_project(db, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return serialize_project(project)


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
        else:
            edit_project(
                db,
                project,
                payload.instruction,
                config,
                base_revision=payload.base_revision,
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
    except RenderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.refresh(project)
    return serialize_project(project)


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
