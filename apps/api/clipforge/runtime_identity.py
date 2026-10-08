from __future__ import annotations

"""Which code is this backend process actually running?

The identity is detected ONCE per process (``runtime_identity`` is cached) from
the checkout that contains the loaded ``clipforge`` package - not from the
current working directory - so a backend started from another clone or git
worktree reports that clone.  Detection is local only (no network), bounded by
short timeouts and never raises: an unreadable repository yields
``source="unknown"`` with an ``error`` instead of a crash.

Precedence: the ``git`` CLI, then reading ``.git`` metadata directly (no git
binary needed; dirty state is then unknown), then a build-time identity from
``CLIPFORGE_BUILD_COMMIT`` / ``CLIPFORGE_BUILD_BRANCH`` / ``CLIPFORGE_BUILD_DIRTY``
(images without ``.git``), else ``unknown``.  The identity is audit metadata
only: it is never part of creative content or content hashes.
"""


import copy
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

IDENTITY_SCHEMA = 1
PACKAGE_DIR = Path(__file__).resolve().parent
BUILD_ENV_COMMIT = "CLIPFORGE_BUILD_COMMIT"
BUILD_ENV_BRANCH = "CLIPFORGE_BUILD_BRANCH"
BUILD_ENV_DIRTY = "CLIPFORGE_BUILD_DIRTY"
GIT_TIMEOUT_SECONDS = 3.0
STATUS_TIMEOUT_SECONDS = 8.0
MAX_DIRTY_PATHS = 20
MAX_RENDER_HISTORY = 20
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# Variables that would point git at a different repository than the package's.
_GIT_REDIRECT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git_env(environ: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in environ.items() if key not in _GIT_REDIRECT_ENV}
    # Read-only probing: never take the index lock, never prompt.
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def _run_git(
    git: str, args: list[str], cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _resolve(path: str, cwd: Path) -> str:
    candidate = Path(path)
    return str((candidate if candidate.is_absolute() else cwd / candidate).resolve())


def _from_git_cli(start: Path, git: str, environ: dict[str, str]) -> dict[str, Any]:
    env = _git_env(environ)
    parsed = _run_git(
        git,
        ["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir", "HEAD"],
        start,
        env,
        GIT_TIMEOUT_SECONDS,
    )
    lines = parsed.stdout.strip().splitlines()
    if parsed.returncode != 0 or len(lines) != 4 or not _SHA.fullmatch(lines[3]):
        raise RuntimeError((parsed.stderr.strip() or "git rev-parse failed")[:200])
    toplevel, git_dir, common_dir, commit = lines
    git_dir, common_dir = _resolve(git_dir, start), _resolve(common_dir, start)
    branch_run = _run_git(git, ["symbolic-ref", "-q", "--short", "HEAD"], start, env, GIT_TIMEOUT_SECONDS)
    branch = branch_run.stdout.strip() if branch_run.returncode == 0 else None
    identity: dict[str, Any] = {
        "source": "git",
        "method": "git_cli",
        "commit": commit,
        "branch": branch or None,
        "detached": not branch,
        "repo_path": str(Path(toplevel).resolve()),
        "git_dir": git_dir,
        "git_common_dir": common_dir,
        "is_worktree": git_dir != common_dir,
        "dirty": None,
        "dirty_count": None,
        "dirty_paths": [],
    }
    try:
        status = _run_git(
            git,
            ["status", "--porcelain=v1", "--untracked-files=normal"],
            start,
            env,
            STATUS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        identity["error"] = "git status timed out; dirty state unknown"
        return identity
    if status.returncode != 0:
        identity["error"] = ("git status failed: " + status.stderr.strip())[:200]
        return identity
    changed = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    identity.update(dirty=bool(changed), dirty_count=len(changed), dirty_paths=changed[:MAX_DIRTY_PATHS])
    return identity


def _find_dot_git(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".git"
        if candidate.exists():
            return candidate
    return None


def _read_ref(common_dir: Path, ref: str) -> str | None:
    loose = common_dir / ref
    if loose.is_file():
        value = loose.read_text(encoding="utf-8").strip()
        return value if _SHA.fullmatch(value) else None
    packed = common_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref and _SHA.fullmatch(parts[0]):
                return parts[0]
    return None


def _from_git_metadata(start: Path) -> dict[str, Any]:
    """Read HEAD without a git binary: normal checkouts and linked worktrees."""
    dot_git = _find_dot_git(start)
    if dot_git is None:
        raise RuntimeError("no .git metadata above the clipforge package")
    if dot_git.is_file():  # linked worktree / submodule: "gitdir: <path>"
        content = dot_git.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            raise RuntimeError(".git file has no gitdir")
        git_dir = Path(_resolve(content.split(":", 1)[1].strip(), dot_git.parent))
    else:
        git_dir = dot_git.resolve()
    commondir_file = git_dir / "commondir"
    common_dir = (
        Path(_resolve(commondir_file.read_text(encoding="utf-8").strip(), git_dir))
        if commondir_file.is_file()
        else git_dir
    )
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    branch: str | None = None
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/")
        commit = _read_ref(git_dir, ref) or _read_ref(common_dir, ref)
    else:
        commit = head if _SHA.fullmatch(head) else None
    if commit is None:
        raise RuntimeError("HEAD could not be resolved from .git metadata")
    return {
        "source": "git",
        "method": "git_metadata",
        "commit": commit,
        "branch": branch,
        "detached": branch is None,
        "repo_path": str(dot_git.parent.resolve()),
        "git_dir": str(git_dir),
        "git_common_dir": str(common_dir),
        "is_worktree": git_dir != common_dir,
        "dirty": None,
        "dirty_count": None,
        "dirty_paths": [],
        "error": "git CLI unavailable; dirty state unknown",
    }


def _build_env(environ: dict[str, str]) -> dict[str, Any] | None:
    commit = (environ.get(BUILD_ENV_COMMIT) or "").strip().lower()
    if not commit:
        return None
    dirty_raw = (environ.get(BUILD_ENV_DIRTY) or "").strip().lower()
    dirty = {"1": True, "true": True, "yes": True, "0": False, "false": False, "no": False}.get(
        dirty_raw
    )
    return {
        "commit": commit[:64],
        "branch": (environ.get(BUILD_ENV_BRANCH) or "").strip()[:200] or None,
        "dirty": dirty,
    }


# Fields of an identity that no checkout could be located for.
_UNLOCATED: dict[str, Any] = {
    "source": "unknown",
    "method": None,
    "commit": None,
    "branch": None,
    "detached": None,
    "repo_path": None,
    "git_dir": None,
    "git_common_dir": None,
    "is_worktree": None,
    "dirty": None,
    "dirty_count": None,
    "dirty_paths": [],
}


def label(identity: dict[str, Any] | None) -> str:
    """Short human label, e.g. ``test @ 200e3f2`` or ``(detached) @ 9a88b86 +dirty``."""
    if not identity or not identity.get("commit"):
        return "unknown build"
    branch = identity.get("branch") or "(detached)"
    dirty = identity.get("dirty")
    suffix = " +dirty" if dirty else " (dirty unknown)" if dirty is None else ""
    return f"{branch} @ {str(identity['commit'])[:7]}{suffix}"


def detect_runtime_identity(
    start: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    git_binary: str | None = "auto",
) -> dict[str, Any]:
    """Detect the identity of the code under ``start`` (default: this package). Never raises."""
    start = (start or PACKAGE_DIR).resolve()
    environ = dict(os.environ if environ is None else environ)
    git = shutil.which("git") if git_binary == "auto" else git_binary
    errors: list[str] = []
    identity: dict[str, Any] | None = None
    if git:
        try:
            identity = _from_git_cli(start, git, environ)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            errors.append(f"git: {exc}"[:240])
    if identity is None:
        try:
            identity = _from_git_metadata(start)
        except (OSError, RuntimeError, ValueError, UnicodeDecodeError) as exc:
            errors.append(f"git metadata: {exc}"[:240])
    build_env = _build_env(environ)
    if identity is None and build_env is not None:
        identity = {**_UNLOCATED, "source": "build_env", "method": "build_env", **build_env}
    if identity is None:
        identity = dict(_UNLOCATED)
    if errors and identity.get("source") != "git":
        identity["error"] = "; ".join(errors)
    identity.setdefault("error", None)
    identity.update(
        schema=IDENTITY_SCHEMA,
        short_commit=str(identity["commit"])[:7] if identity.get("commit") else None,
        package_path=str(start),
        build_env=build_env,
        build_env_mismatch=bool(
            build_env
            and identity.get("source") == "git"
            and identity.get("commit")
            and identity["commit"] != build_env["commit"]
        ),
        python=sys.executable,
        pid=os.getpid(),
        detected_at=_now(),
    )
    identity["label"] = label(identity)
    return identity


@lru_cache(maxsize=1)
def _cached_identity() -> dict[str, Any]:
    return detect_runtime_identity()


def runtime_identity() -> dict[str, Any]:
    """This process' identity, detected once (a copy: callers may not mutate the cache)."""
    return copy.deepcopy(_cached_identity())


def reset_runtime_identity_cache() -> None:
    _cached_identity.cache_clear()


def identity_snapshot() -> dict[str, Any]:
    """A persisted copy of the process identity, stamped with when it was recorded."""
    snapshot = runtime_identity()
    snapshot["recorded_at"] = _now()
    return snapshot


def same_code(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool | None:
    """Whether two identities describe the same code; ``None`` when either is unknown."""
    if not first or not second or not first.get("commit") or not second.get("commit"):
        return None
    if first.get("dirty") or second.get("dirty"):
        # A dirty tree's content is not pinned by its commit: equal only if both
        # are the same tree in the same state (still a best effort).
        return (
            first["commit"] == second["commit"]
            and first.get("repo_path") == second.get("repo_path")
            and first.get("dirty_paths") == second.get("dirty_paths")
            and first.get("dirty_count") == second.get("dirty_count")
        )
    return first["commit"] == second["commit"]


def stamp_generation(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> None:
    """Record which code generated this project (audit metadata, never hashed)."""
    provenance = state.get("runtime_provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    provenance["generation"] = snapshot or identity_snapshot()
    provenance.setdefault("renders", [])
    state["runtime_provenance"] = provenance


def stamp_render(state: dict[str, Any], revision: int, snapshot: dict[str, Any] | None = None) -> None:
    """Append (never overwrite) which code produced the render of ``revision``."""
    provenance = state.get("runtime_provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    snapshot = snapshot or identity_snapshot()
    generation = provenance.get("generation") if isinstance(provenance.get("generation"), dict) else None
    renders = [item for item in provenance.get("renders") or [] if isinstance(item, dict)]
    renders.append(
        {
            "revision": revision,
            "identity": snapshot,
            "matches_generation": same_code(generation, snapshot),
        }
    )
    provenance.setdefault("generation", None)
    provenance["renders"] = renders[-MAX_RENDER_HISTORY:]
    provenance["last_render"] = renders[-1]
    state["runtime_provenance"] = provenance
