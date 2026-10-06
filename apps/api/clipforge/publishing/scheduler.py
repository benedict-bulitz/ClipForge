"""The persistent publication scheduler (ClipForge-owned schedules).

Instagram and TikTok offer no future-publish timestamp, so ClipForge owns the
schedule: every scheduled post is a ``social_publications`` row (nothing lives
only in memory) and this loop executes due rows, polls processing rows and
recovers after a restart.  The loop is stateless between ticks, uses DB
leases for exclusivity and can therefore move to an always-on worker later
without a schema change.

Recovery policy (documented in the UI):

* A post whose due time (schedule, or the next retry) passed more than
  ``publishing_missed_grace_minutes`` ago while ClipForge was not running is
  never published late on its own: it becomes **missed** and waits for the
  user (Publish now / Reschedule / Cancel).
* A post interrupted mid-upload restarts from the beginning when it is still
  within the grace window (no partial post exists on either platform), else
  it becomes missed as well.
* A post whose bytes fully reached the provider is never uploaded again: it
  goes back to **processing** and the provider's status decides.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import SocialPublication
from ..security.secrets import SecretStore
from . import publications
from .publications import Apis, _event, aware

logger = logging.getLogger(__name__)
MAX_PER_TICK = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _due_at(row: SocialPublication) -> datetime:
    due = aware(row.scheduled_at) or aware(row.created_at) or _now()
    retry = aware(row.next_attempt_at)
    return max(due, retry) if retry else due


def mark_missed(db: Session, settings: Settings, *, now: datetime) -> list[str]:
    """Overdue beyond the grace window = ClipForge was not running at the time."""
    grace = timedelta(minutes=max(0, settings.publishing_missed_grace_minutes))
    missed: list[str] = []
    rows = db.scalars(select(SocialPublication).where(SocialPublication.state.in_(publications.RUNNABLE))).all()
    for row in rows:
        due = _due_at(row)
        if due < now - grace:
            row.state = "missed"
            row.lease_until = None
            row.last_error_code = "missed_schedule"
            row.last_error_message = (
                f"ClipForge was not running at {due.strftime('%Y-%m-%d %H:%M')} UTC, so this post was not published. "
                "Publish it now, reschedule it or cancel it."
            )
            _event(row, "missed", "due while ClipForge was not running", now=now)
            missed.append(row.id)
    if missed:
        db.commit()
    return missed


def recover_interrupted(db: Session, *, now: datetime, force: bool = False) -> list[str]:
    """Rows left ``uploading`` by a stopped process (lease expired, or ``force``
    at startup of the single local process)."""
    condition = SocialPublication.state == "uploading"
    if not force:
        condition = and_(condition, or_(SocialPublication.lease_until.is_(None), SocialPublication.lease_until < now))
    recovered: list[str] = []
    for row in db.scalars(select(SocialPublication).where(condition)).all():
        row.lease_until = None
        if row.upload_complete and row.remote_container_id:
            row.state = "processing"
            row.next_attempt_at = now
            _event(row, "processing", "recovered after restart: the provider received the video; checking its status", now=now)
        else:
            # Nothing was posted: the next attempt starts over (or the post is
            # marked missed by ``mark_missed`` if it is too late now).
            row.state = "pending" if row.mode == "now" else "scheduled"
            row.remote_container_id = None
            row.bytes_uploaded = 0
            row.upload_complete = False
            row.next_attempt_at = None
            row.last_error_code = "interrupted"
            row.last_error_message = "The upload was interrupted (ClipForge stopped); it starts over."
            _event(row, row.state, "recovered after restart: upload restarts", now=now)
        recovered.append(row.id)
    if recovered:
        db.commit()
    return recovered


def due_ids(db: Session, *, now: datetime) -> list[str]:
    return list(db.scalars(
        select(SocialPublication.id)
        .where(
            SocialPublication.state.in_(publications.RUNNABLE),
            or_(SocialPublication.scheduled_at.is_(None), SocialPublication.scheduled_at <= now),
            or_(SocialPublication.next_attempt_at.is_(None), SocialPublication.next_attempt_at <= now),
            or_(SocialPublication.lease_until.is_(None), SocialPublication.lease_until < now),
        )
        .order_by(SocialPublication.scheduled_at.asc(), SocialPublication.created_at.asc())
        .limit(MAX_PER_TICK)
    ).all())


def poll_ids(db: Session, *, now: datetime) -> list[str]:
    return list(db.scalars(
        select(SocialPublication.id)
        .where(
            SocialPublication.state == "processing",
            or_(SocialPublication.next_attempt_at.is_(None), SocialPublication.next_attempt_at <= now),
            or_(SocialPublication.lease_until.is_(None), SocialPublication.lease_until < now),
        )
        .limit(MAX_PER_TICK)
    ).all())


def tick(db: Session, settings: Settings, store: SecretStore, apis: Apis, *, now: datetime | None = None) -> dict[str, Any]:
    """One scheduler pass.  Safe to run concurrently (leases) and repeatedly."""
    now = now or _now()
    recovered = recover_interrupted(db, now=now)
    missed = mark_missed(db, settings, now=now)
    ran = []
    for publication_id in due_ids(db, now=now):
        row = publications.run(db, publication_id, settings, store, apis, now=now)
        ran.append({"id": publication_id, "state": row.state if row else None})
    polled = []
    for publication_id in poll_ids(db, now=now):
        row = publications.poll(db, publication_id, settings, store, apis, now=now)
        polled.append({"id": publication_id, "state": row.state if row else None})
    return {"recovered": recovered, "missed": missed, "ran": ran, "polled": polled}


def startup(db: Session, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """After a restart no upload is alive in this (single, local) process."""
    now = now or _now()
    recovered = recover_interrupted(db, now=now, force=True)
    missed = mark_missed(db, settings, now=now)
    return {"recovered": recovered, "missed": missed}


def drive(
    session_factory: Callable[[], Session],
    publication_id: str,
    settings: Settings,
    store: SecretStore,
    apis: Apis,
    *,
    max_polls: int = 40,
    sleep: Callable[[float], Any] | None = None,
    stop: threading.Event | None = None,
) -> str | None:
    """"Publish now": run one publication and follow it for a while; the
    scheduler loop picks it up later if the provider is still processing."""
    waiter = stop or threading.Event()
    pause = sleep or (lambda seconds: waiter.wait(seconds))
    with session_factory() as db:
        row = publications.run(db, publication_id, settings, store, apis)
        state = row.state if row else None
        for _ in range(max_polls):
            if row is None or row.state != "processing":
                break
            wait = max(1.0, ((aware(row.next_attempt_at) or _now()) - _now()).total_seconds())
            pause(min(wait, 60.0))
            row = publications.poll(db, publication_id, settings, store, apis)
            state = row.state if row else None
        return state


class SchedulerThread:
    """The background loop of the local backend (one per process)."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings_getter: Callable[[], Settings],
        store_factory: Callable[[], SecretStore],
        apis_factory: Callable[[], Apis],
    ) -> None:
        self._session_factory = session_factory
        self._settings_getter = settings_getter
        self._store_factory = store_factory
        self._apis_factory = apis_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="publishing-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _loop(self) -> None:
        settings = self._settings_getter()
        try:
            with self._session_factory() as db:
                startup(db, settings)
        except Exception:
            logger.exception("Publishing scheduler startup recovery failed")
        while not self._stop.is_set():
            settings = self._settings_getter()
            try:
                with self._session_factory() as db:
                    tick(db, settings, self._store_factory(), self._apis_factory())
                self.last_tick_at = _now()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.exception("Publishing scheduler tick failed")
            self._stop.wait(max(5.0, float(settings.publishing_scheduler_interval_seconds)))


_SCHEDULER: SchedulerThread | None = None


def get_scheduler() -> SchedulerThread | None:
    return _SCHEDULER


def start_scheduler(thread: SchedulerThread) -> SchedulerThread:
    global _SCHEDULER
    if _SCHEDULER is not None:
        _SCHEDULER.stop()
    _SCHEDULER = thread
    thread.start()
    return thread


def status() -> dict[str, Any]:
    scheduler = _SCHEDULER
    return {
        "running": bool(scheduler and scheduler.running),
        "last_tick_at": scheduler.last_tick_at if scheduler else None,
        "last_error": scheduler.last_error if scheduler else None,
        "notice": publications.OFFLINE_NOTICE,
    }
