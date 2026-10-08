from __future__ import annotations

"""Queue Overview: the current generation queue, one compact row per job.

ClipForge persists no batch identifier, so the *current queue* is derived from
the job timestamps alone (``current_queue_run``):

* jobs are taken in submission order (``created_at``);
* a job belongs to the run before it when it was submitted while that run was
  still busy - some earlier job of the run was waiting or generating, i.e. the
  job's ``created_at`` is not later than the run's latest end (``completed_at``
  of a finished job, "still running" for an active one);
* a job submitted while the queue was idle (every earlier job had finished)
  starts a new run.

The current queue is the latest run: while anything is queued or generating it
holds those jobs plus every job that finished since the queue last went idle;
once it drains it keeps showing that finished batch until the next submission
into an idle queue starts a new run.  Removed jobs ("Remove" / "Clear Queue")
count for the boundary but are never shown, and deleting a project deletes its
jobs, so it leaves the overview as well.

Everything here is read-only and local: no YouTube call, no render, no mix.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .exporter import final_master_available
from .generation import ACTIVE_JOB_STATUSES, serialize_generation_job
from .models import GenerationJob, Project, YouTubeUpload
from .publishing.accounts import list_accounts
from .publishing.read_model import project_publications
from .services import effective_revision_state
from .youtube import connection
from .youtube.routes import focus_upload, render_upload_status
from .youtube.uploads import serialize_upload

HIDDEN_JOB_STATUSES = ("removed",)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _job_end(job: GenerationJob) -> datetime | None:
    """When the job stopped occupying the queue; ``None`` while it is still active."""
    if job.status in ACTIVE_JOB_STATUSES:
        return None
    return _utc(job.completed_at or job.updated_at or job.created_at)


def current_queue_run(jobs: list[GenerationJob]) -> list[GenerationJob]:
    """The latest queue run (see the module docstring), in submission order."""
    ordered = sorted(jobs, key=lambda job: (_utc(job.created_at), job.id))
    run: list[GenerationJob] = []
    busy_until: datetime | None = None
    active = False
    for job in ordered:
        created = _utc(job.created_at)
        if not run or (not active and busy_until is not None and created > busy_until):
            run, busy_until, active = [], None, False
        run.append(job)
        end = _job_end(job)
        if end is None:
            active = True
        elif busy_until is None or end > busy_until:
            busy_until = end
    return run


def _poster(state: dict[str, Any]) -> str | None:
    """The project's selected cover, else any cover, else a still scene image."""
    thumbnails = state.get("thumbnails") if isinstance(state.get("thumbnails"), dict) else {}
    variants = [item for item in thumbnails.get("variants") or [] if isinstance(item, dict) and item.get("url")]
    selected = next((item for item in variants if item.get("id") == thumbnails.get("selected_variant_id")), None)
    choice = selected or next((item for item in variants if item.get("platform") == "youtube"), None) or (variants[0] if variants else None)
    if choice is not None:
        return str(choice["url"])
    for scene in state.get("scenes") or []:
        media = scene.get("media") if isinstance(scene, dict) else None
        if isinstance(media, dict) and media.get("kind") == "photo" and media.get("cache_path"):
            return f"/media/{media['cache_path']}"
    return None


def _render_summary(project: Project, state: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """``ready`` only for the current, complete render whose final MP4 exists."""
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    complete = render.get("status") == "complete" and not render.get("stale")
    if complete:
        playable = final_master_available(project.id, project.title, state, settings)
        condition = "ready" if playable else "missing"
    elif render.get("stale") and render.get("url"):
        condition = "stale"
    else:
        condition = "not_rendered"
    return {
        "state": condition,
        "status": render.get("status"),
        "revision": render.get("revision"),
        # The one canonical final video (narration + mixed music), pinned to the
        # revision shown, so a newer revision never plays under an older row.
        "final_video_url": f"/api/projects/{project.id}/final-video?revision={project.current_revision}" if condition == "ready" else None,
    }


def _quality_summary(state: dict[str, Any]) -> dict[str, Any]:
    """The existing review results as stored - never re-judged here."""
    ai_review = state.get("ai_review") if isinstance(state.get("ai_review"), dict) else None
    final = state.get("final_quality_review") if isinstance(state.get("final_quality_review"), dict) else None
    summary = final.get("summary") if final and isinstance(final.get("summary"), dict) else None
    return {
        "ai_review": {"status": ai_review.get("status")} if ai_review else None,
        "final_review": {
            "status": final.get("status"),
            "revision": final.get("revision"),
            "summary": {"label": summary.get("label")} if summary and summary.get("label") else None,
        } if final else None,
    }


def _duration(state: dict[str, Any]) -> float | None:
    for value in ((state.get("duration") or {}).get("actual_seconds"), (state.get("timeline") or {}).get("duration")):
        if isinstance(value, int | float) and value > 0:
            return round(float(value), 2)
    return None


def _project_summary(
    db: Session,
    project: Project,
    uploads: list[YouTubeUpload],
    settings: Settings,
    channel_id: str | None,
    publications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = effective_revision_state(project)
    render = _render_summary(project, state, settings)
    timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    current = render_upload_status(db, project, settings, channel_id)
    focus = focus_upload(uploads, current)
    return {
        "id": project.id,
        "title": project.title,
        "status": project.status,
        "current_revision": project.current_revision,
        "render": render,
        "duration_seconds": _duration(state) if render["state"] == "ready" else None,
        "width": timeline.get("width"),
        "height": timeline.get("height"),
        "poster_url": _poster(state),
        "quality": _quality_summary(state),
        # The same render-level answer the Results page's YouTube card uses.
        "youtube": {"current_render": current, "upload": serialize_upload(focus) if focus else None},
        # Every platform/account publication of this project (compact).
        "publications": publications or [],
    }


def queue_overview(db: Session, settings: Settings) -> dict[str, Any]:
    jobs = list(db.scalars(select(GenerationJob).order_by(GenerationJob.created_at.asc(), GenerationJob.id.asc())).all())
    # FIFO positions over every waiting job, exactly as ``list_generation_jobs`` numbers them.
    waiting = [job.id for job in jobs if job.status == "queued"]
    positions = {job_id: index + 1 for index, job_id in enumerate(waiting)}
    run = current_queue_run(jobs)
    visible = [job for job in run if job.status not in HIDDEN_JOB_STATUSES]
    project_ids = {job.project_id for job in visible}
    projects = {
        project.id: project
        for project in db.scalars(select(Project).where(Project.id.in_(project_ids))).all()
    } if project_ids else {}
    uploads: dict[str, list[YouTubeUpload]] = {}
    if projects:
        rows = db.scalars(
            select(YouTubeUpload).where(YouTubeUpload.project_id.in_(list(projects))).order_by(YouTubeUpload.created_at.desc())
        ).all()
        for row in rows:
            uploads.setdefault(row.project_id, []).append(row)
    record = connection.active_connection(db)
    channel_id = record.channel_id if record else None
    now = datetime.now(UTC)
    items = []
    for job in visible:
        project = projects.get(job.project_id)
        items.append({
            "job": serialize_generation_job(job, now=now, queue_position=positions.get(job.id)),
            "project": _project_summary(
                db, project, uploads.get(project.id, []), settings, channel_id, project_publications(db, project.id),
            ) if project else None,
        })
    return {
        "run_started_at": _utc(run[0].created_at) if run else None,
        "youtube": {
            "status": record.status if record else "not_connected",
            "channel_title": record.channel_title if record else None,
        },
        # Any connected publishing account enables the unified Upload action.
        "publishing": {"connected_accounts": len(list_accounts(db))},
        "items": items,
    }
