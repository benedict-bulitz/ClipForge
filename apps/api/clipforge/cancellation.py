"""Cooperative cancellation of the ONE running generation job.

The durable truth is ``generation_jobs.status``: the cancel request moves a
``running`` job to ``cancelling`` (atomically, in the database), and the worker
ends it as ``cancelled``.  This module is the in-process side of that state:

* a ``CancelToken`` per running job (registered by ``run_generation_job``), set
  by the same request that writes ``cancelling`` - so checkpoints and process
  watches need no database round trip;
* ``checkpoint()`` - raise ``GenerationCancelled`` at a safe point (between
  stages, before expensive calls, before persisting a finished render);
* ``run_process()`` - a subprocess run the token can stop: terminate, a short
  grace period, kill only if needed, always reaped.  Only processes started
  here for the cancelled job are ever touched;
* ``claim_path()`` - directories the cancelled run created itself, the only
  ones cleanup may remove.

No thread is ever killed: a provider call that cannot be interrupted simply
returns (or times out) and the next checkpoint stops the job.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# How often a watched subprocess looks at the token, and how long it gets to exit cleanly.
PROCESS_POLL_SECONDS = 0.25
TERMINATE_GRACE_SECONDS = 3.0


class GenerationCancelled(Exception):
    """The user cancelled this generation; not a failure."""


@dataclass
class CancelToken:
    job_id: str
    event: threading.Event = field(default_factory=threading.Event)
    owned_paths: list[Path] = field(default_factory=list)
    processes: set[subprocess.Popen] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


_REGISTRY: dict[str, CancelToken] = {}
_REGISTRY_LOCK = threading.Lock()
_LOCAL = threading.local()


def register(job_id: str) -> CancelToken:
    """The worker owns one token per running job (replacing any stale one)."""
    token = CancelToken(job_id)
    with _REGISTRY_LOCK:
        _REGISTRY[job_id] = token
    return token


def unregister(token: CancelToken) -> None:
    with _REGISTRY_LOCK:
        if _REGISTRY.get(token.job_id) is token:
            del _REGISTRY[token.job_id]


def signal(job_id: str) -> bool:
    """Tell a live worker in this process to stop; False when no worker holds the job."""
    with _REGISTRY_LOCK:
        token = _REGISTRY.get(job_id)
    if token is None:
        return False
    token.event.set()
    return True


def has_worker(job_id: str) -> bool:
    with _REGISTRY_LOCK:
        return job_id in _REGISTRY


@contextmanager
def scope(token: CancelToken) -> Iterator[CancelToken]:
    """Make ``token`` the current one for this (worker) thread."""
    previous = getattr(_LOCAL, "token", None)
    _LOCAL.token = token
    try:
        yield token
    finally:
        _LOCAL.token = previous


def current() -> CancelToken | None:
    return getattr(_LOCAL, "token", None)


def checkpoint() -> None:
    """Stop here if the current job was cancelled (a no-op outside a generation job)."""
    token = current()
    if token is not None and token.cancelled:
        raise GenerationCancelled(f"Generation {token.job_id} was cancelled.")


def claim_path(path: Path) -> None:
    """Record a directory the current job created itself (cleanup candidate on cancel)."""
    token = current()
    if token is not None:
        with token.lock:
            token.owned_paths.append(Path(path))


def _stop(process: subprocess.Popen) -> None:
    """terminate -> grace period -> kill; always reaped, pipes drained."""
    if process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            process.kill()
    process.communicate()


def run_process(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """``subprocess.run(command, capture_output=True, text=True, timeout=...)`` that a cancel can stop.

    Outside a generation job it IS ``subprocess.run``.  Inside one, the process
    is watched: a cancel stops it (``GenerationCancelled``); the timeout behaves
    as before (``subprocess.TimeoutExpired`` after the process was stopped).
    """
    token = current()
    if token is None:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    checkpoint()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with token.lock:
        token.processes.add(process)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=PROCESS_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if token.cancelled:
                    _stop(process)
                    raise GenerationCancelled(f"Generation {token.job_id} was cancelled during {Path(command[0]).name}.") from None
                if time.monotonic() >= deadline:
                    _stop(process)
                    raise subprocess.TimeoutExpired(command, timeout) from None
    except BaseException:
        if process.poll() is None:
            _stop(process)
        raise
    finally:
        with token.lock:
            token.processes.discard(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
