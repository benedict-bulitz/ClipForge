from .config import get_settings
from .database import SessionLocal
from .services import get_project, render_project
from .worker import celery_app


@celery_app.task(
    bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 4}
)
def execute_pipeline_stage(self, project_id: str, revision: int, stage: str) -> dict:
    """Execute supported production work through the same revision-safe service as the API."""
    if stage != "render":
        return {"project_id": project_id, "revision": revision, "stage": stage, "status": "unsupported"}
    with SessionLocal() as db:
        project = get_project(db, project_id)
        if project is None:
            return {"project_id": project_id, "revision": revision, "stage": stage, "status": "missing"}
        result = render_project(
            db,
            project,
            get_settings(),
            base_revision=revision,
        )
        return {
            "project_id": project_id,
            "revision": result.number,
            "stage": stage,
            "status": "complete",
        }
