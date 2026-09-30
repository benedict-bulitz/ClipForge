"""Bounded discovery runtime: single-flight ownership, the current stage, hard time limits.

Real Mac (d86d9b3): Home showed "Themenvorschläge werden gesucht…" for good while the
startup warm-up sat inside the OpenAI curator request, which had no timeout of its
own (client default: 600 s per attempt, 2 retries).  Nothing here may wait forever:

* every external call (provider fetch, curator request) runs under a wall-clock
  guard (``call_with_timeout``) - a hung socket is abandoned, not awaited;
* the single-flight ``FLIGHT`` is owned by a token; a discovery holding it longer
  than ``FLIGHT_MAX_SECONDS`` is abandoned (its thread stops at the next stage) and
  the flight is free again, so "Neue Vorschläge" can retry;
* the current stage, its provider and elapsed time are visible in the status endpoint.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

T = TypeVar("T")

# Per external call (each provider fetch, each competition probe): a hung socket or a
# trickling response never holds discovery longer than this.
PROVIDER_TIMEOUT_SECONDS = 30.0
# One curator request (<= 20 topics, question + judgement).
CURATOR_TIMEOUT_SECONDS = 60.0
# The wall-clock guard around it allows the client this much longer to report its own timeout.
CURATOR_GRACE_SECONDS = 5.0
# No new curator request starts after this much time in one flight (the remaining
# topics fall back to strict local rules), so a slow model cannot stretch a refresh.
AI_DEADLINE_SECONDS = 150.0
# Hard limit for one flight (warm-up or request).  Above every bounded stage combined;
# only reached if something unforeseen blocks.  The owner is then abandoned.
FLIGHT_MAX_SECONDS = 300.0
# How long "Generate Next Video" may wait for a running discovery before answering "discovering".
FLIGHT_WAIT_SECONDS = 10.0
STAGE_LOG_LIMIT = 40


class CallTimeout(RuntimeError):
    """An external call exceeded its wall-clock bound; its thread was abandoned."""


class DiscoveryAbandoned(RuntimeError):
    """This thread's discovery exceeded the flight's hard limit and was abandoned."""


def _iso(value: float | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, UTC).isoformat()


def call_with_timeout(fn: Callable[[], T], seconds: float, *, name: str) -> T:
    """Run ``fn`` with a wall-clock bound.  On timeout the worker thread is abandoned.

    The worker must not use the caller's database session: callers commit before
    and write results after, in their own thread.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=target, name=f"topic-{name}", daemon=True)
    worker.start()
    if not done.wait(max(0.0, seconds)):
        FLIGHT.note(f"timeout:{name}", f"{name} exceeded {seconds:g}s")
        raise CallTimeout(f"{name} timed out after {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


class Flight:
    """Single-flight discovery lock with an owner token, stage tracking and a hard limit.

    Compatible with the ``threading.Lock`` calls used before (``acquire``/``release``/
    ``locked``).  Unlike a plain lock it can never stay held forever.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._local = threading.local()
        self._token: object | None = None
        self._owner = ""
        self._acquired = 0.0
        self._acquired_wall = 0.0
        self._stage = "idle"
        self._stage_started = 0.0
        self._stage_wall = 0.0
        self._provider: str | None = None
        self._log: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self.last_finished_at: str | None = None
        self.on_abandon: Callable[[str, str], None] | None = None

    # -- lock -------------------------------------------------------------------------------

    def acquire(self, blocking: bool = True, timeout: float = -1, *, owner: str = "request") -> bool:
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        with self._cond:
            while self._token is not None:
                if self._expired_locked():
                    self._abandon_locked()
                    break
                if not blocking:
                    return False
                wait = FLIGHT_MAX_SECONDS if deadline is None else deadline - time.monotonic()
                if wait <= 0:
                    return False
                self._cond.wait(min(wait, 1.0))
            token = object()
            self._token = token
            self._local.token = token
            self._owner = owner
            self._acquired = self._stage_started = time.monotonic()
            self._acquired_wall = self._stage_wall = time.time()
            self._stage, self._provider = "lock_acquired", None
            self._log = [{"stage": "lock_acquired", "provider": None, "started_at": _iso(self._acquired_wall), "seconds": 0.0}]
            self._events = []
            self.last_error = None
            return True

    def release(self) -> None:
        with self._cond:
            token = getattr(self._local, "token", None)
            self._local.token = None
            if token is None or token is not self._token:
                return  # abandoned earlier: the flight already belongs to someone else (or no one)
            self._close_stage_locked("released")
            self._token = None
            self._stage, self._provider = "idle", None
            self.last_finished_at = _iso(time.time())
            self._cond.notify_all()

    def locked(self) -> bool:
        with self._cond:
            if self._token is not None and self._expired_locked():
                self._abandon_locked()
            return self._token is not None

    def deadline(self, seconds: float) -> float:
        """Monotonic deadline ``seconds`` after this flight started (or from now without one)."""
        with self._cond:
            start = self._acquired if self._token is not None and getattr(self._local, "token", None) is self._token else time.monotonic()
        return start + seconds

    # -- stages -----------------------------------------------------------------------------

    def ensure_owner(self) -> None:
        token = getattr(self._local, "token", None)
        if token is not None and token is not self._token:
            raise DiscoveryAbandoned("discovery exceeded its hard time limit and was abandoned")

    def stage(self, name: str, provider: str | None = None) -> None:
        """Record the current discovery stage (and stop an abandoned discovery right here)."""
        self.ensure_owner()
        with self._cond:
            token = getattr(self._local, "token", None)
            if token is None or token is not self._token:
                return  # direct calls without the flight (tests, scripts) are not tracked
            self._close_stage_locked("ok")
            self._stage, self._provider = name, provider
            self._stage_started, self._stage_wall = time.monotonic(), time.time()
            self._log.append({"stage": name, "provider": provider, "started_at": _iso(self._stage_wall), "seconds": None})
            del self._log[:-STAGE_LOG_LIMIT]

    def timed_out(self, name: str) -> bool:
        """Whether ``name`` already timed out in this thread's flight (a broadening pass skips it)."""
        with self._cond:
            if self._token is None or getattr(self._local, "token", None) is not self._token:
                return False
            return any(event["code"] == f"timeout:{name}" for event in self._events)

    def note(self, code: str, message: str) -> None:
        with self._cond:
            self._events.append({"code": code, "message": message, "stage": self._stage, "provider": self._provider, "at": _iso(time.time())})
            del self._events[:-STAGE_LOG_LIMIT]

    def snapshot(self) -> dict[str, Any]:
        running = self.locked()
        with self._cond:
            now = time.monotonic()
            return {
                "running": running,
                "lock_held": running,
                "owner": self._owner if running else None,
                "discovery_stage": self._stage if running else "idle",
                "active_provider": self._provider if running else None,
                "started_at": _iso(self._acquired_wall) if running else None,
                "stage_started_at": _iso(self._stage_wall) if running else None,
                "elapsed_seconds": round(now - self._acquired, 1) if running else None,
                "stage_elapsed_seconds": round(now - self._stage_started, 1) if running else None,
                "hard_limit_seconds": FLIGHT_MAX_SECONDS,
                "timeouts_seconds": {"provider": PROVIDER_TIMEOUT_SECONDS, "curator": CURATOR_TIMEOUT_SECONDS, "ai_deadline": AI_DEADLINE_SECONDS},
                "stages": [dict(item) for item in self._log],
                "events": [dict(item) for item in self._events],
                "last_error": self.last_error,
                "last_finished_at": self.last_finished_at,
            }

    # -- internals (hold self._cond) ----------------------------------------------------------

    def _close_stage_locked(self, status: str) -> None:
        if self._log and self._log[-1]["seconds"] is None:
            self._log[-1]["seconds"] = round(time.monotonic() - self._stage_started, 2)
            self._log[-1]["status"] = status

    def _expired_locked(self) -> bool:
        return self._token is not None and time.monotonic() - self._acquired > FLIGHT_MAX_SECONDS

    def _abandon_locked(self) -> None:
        elapsed = time.monotonic() - self._acquired
        message = f"discovery_timed_out: {self._owner} held discovery {elapsed:.0f}s (limit {FLIGHT_MAX_SECONDS:g}s) in stage {self._stage}" + (
            f" ({self._provider})" if self._provider else ""
        )
        self._close_stage_locked("abandoned")
        self._events.append({"code": "discovery_timed_out", "message": message, "stage": self._stage, "provider": self._provider, "at": _iso(time.time())})
        owner = self._owner
        self._token = None
        self._stage, self._provider = "idle", None
        self.last_error = message
        self.last_finished_at = _iso(time.time())
        self._cond.notify_all()
        if self.on_abandon is not None:
            self.on_abandon(owner, message)


FLIGHT = Flight()
