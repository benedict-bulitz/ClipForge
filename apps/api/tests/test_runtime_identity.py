from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from clipforge import runtime_identity as rid
from clipforge.config import Settings
from clipforge.database import ensure_runtime_schema
from clipforge.generation import (
    claim_next_generation_job,
    create_generation_job,
    run_generation_job,
    serialize_generation_job,
)
from clipforge.hashing import attach_hashes
from clipforge.main import health
from clipforge.models import GenerationJob, Project, ProjectRevision
from clipforge.schemas import AdvancedOptions, GenerationJobRead, HealthRead, ProjectCreate
from clipforge.services import (
    create_project,
    current_revision,
    get_project,
    render_project,
    serialize_project,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


def git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    package = root / "apps" / "api" / "clipforge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    git(root, "init", "-q", "-b", "feature/identity")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "one")
    return root


def detect(start: Path, **kwargs) -> dict:
    return rid.detect_runtime_identity(start, environ=GIT_ENV, **kwargs)


def package_of(root: Path) -> Path:
    return root / "apps" / "api" / "clipforge"


# --- detection -----------------------------------------------------------------


def test_normal_checkout_reports_commit_branch_clean_and_repo_path(repo):
    identity = detect(package_of(repo))

    assert identity["source"] == "git" and identity["method"] == "git_cli"
    assert identity["commit"] == git(repo, "rev-parse", "HEAD")
    assert identity["short_commit"] == identity["commit"][:7]
    assert identity["branch"] == "feature/identity"
    assert identity["detached"] is False
    assert identity["dirty"] is False and identity["dirty_count"] == 0
    assert identity["repo_path"] == str(repo.resolve())
    assert identity["package_path"] == str(package_of(repo).resolve())
    assert identity["is_worktree"] is False
    assert identity["error"] is None
    assert identity["label"] == f"feature/identity @ {identity['commit'][:7]}"


def test_detached_head_has_commit_but_no_branch(repo):
    first = git(repo, "rev-parse", "HEAD")
    (repo / "second.txt").write_text("2", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two")
    git(repo, "checkout", "-q", "--detach", first)

    identity = detect(package_of(repo))

    assert identity["commit"] == first
    assert identity["branch"] is None and identity["detached"] is True
    assert identity["label"].startswith("(detached) @ ")


def test_dirty_checkout_lists_modified_and_untracked_paths(repo):
    (package_of(repo) / "__init__.py").write_text("changed = True\n", encoding="utf-8")
    (package_of(repo) / "research_patch.py").write_text("x = 1\n", encoding="utf-8")

    identity = detect(package_of(repo))

    assert identity["dirty"] is True
    assert identity["dirty_count"] == 2
    assert any(path.endswith("__init__.py") for path in identity["dirty_paths"])
    assert any(path.endswith("research_patch.py") for path in identity["dirty_paths"])
    assert identity["label"].endswith("+dirty")


def test_linked_worktree_reports_its_own_path_branch_and_commit(repo, tmp_path):
    worktree = tmp_path / "other-worktree"
    git(repo, "worktree", "add", "-q", "-b", "cloud/other", str(worktree))
    (worktree / "w.txt").write_text("w", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-q", "-m", "worktree commit")

    identity = detect(package_of(worktree))
    main_identity = detect(package_of(repo))

    assert identity["repo_path"] == str(worktree.resolve())
    assert identity["branch"] == "cloud/other"
    assert identity["commit"] == git(worktree, "rev-parse", "HEAD") != main_identity["commit"]
    assert identity["is_worktree"] is True
    assert identity["git_common_dir"] == str((repo / ".git").resolve())
    assert main_identity["is_worktree"] is False


def test_metadata_fallback_without_git_binary_handles_checkout_and_worktree(repo, tmp_path):
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "cloud/meta", str(worktree))
    git(repo, "pack-refs", "--all")  # the branch ref now only lives in packed-refs

    normal = detect(package_of(repo), git_binary=None)
    linked = detect(package_of(worktree), git_binary=None)

    assert normal["source"] == "git" and normal["method"] == "git_metadata"
    assert normal["commit"] == git(repo, "rev-parse", "HEAD")
    assert normal["branch"] == "feature/identity"
    assert normal["dirty"] is None  # unknown without a git binary, never guessed
    assert "dirty state unknown" in normal["error"]
    assert linked["branch"] == "cloud/meta" and linked["is_worktree"] is True
    assert linked["repo_path"] == str(worktree.resolve())


def test_broken_git_binary_falls_back_to_metadata(repo):
    identity = detect(package_of(repo), git_binary=str(repo / "no-such-git"))

    assert identity["method"] == "git_metadata"
    assert identity["commit"] == git(repo, "rev-parse", "HEAD")


def test_missing_git_metadata_never_raises(tmp_path):
    bare = tmp_path / "installed" / "clipforge"
    bare.mkdir(parents=True)

    identity = detect(bare)

    assert identity["source"] == "unknown"
    assert identity["commit"] is None and identity["dirty"] is None
    assert identity["error"]
    assert identity["label"] == "unknown build"
    assert identity["package_path"] == str(bare.resolve())


def test_corrupt_head_never_raises(repo):
    (repo / ".git" / "HEAD").write_text("garbage", encoding="utf-8")

    identity = detect(package_of(repo), git_binary=None)

    assert identity["source"] == "unknown" and identity["error"]


def test_build_env_identity_is_used_without_git_metadata(tmp_path):
    bare = tmp_path / "image" / "clipforge"
    bare.mkdir(parents=True)
    environ = {
        rid.BUILD_ENV_COMMIT: "ABCDEF1234567890",
        rid.BUILD_ENV_BRANCH: "test",
        rid.BUILD_ENV_DIRTY: "false",
    }

    identity = rid.detect_runtime_identity(bare, environ=environ)

    assert identity["source"] == "build_env"
    assert identity["commit"] == "abcdef1234567890"
    assert identity["branch"] == "test" and identity["dirty"] is False
    assert identity["error"]  # why git was not used stays visible


def test_build_env_mismatch_with_git_is_flagged(repo):
    environ = {**GIT_ENV, rid.BUILD_ENV_COMMIT: "0" * 40}

    identity = rid.detect_runtime_identity(package_of(repo), environ=environ)

    assert identity["source"] == "git"
    assert identity["build_env"]["commit"] == "0" * 40
    assert identity["build_env_mismatch"] is True


def test_git_redirect_variables_cannot_point_at_another_repository(repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    git(other, "init", "-q", "-b", "elsewhere")
    environ = {**GIT_ENV, "GIT_DIR": str(other / ".git")}

    identity = rid.detect_runtime_identity(package_of(repo), environ=environ)

    assert identity["branch"] == "feature/identity"


def test_identity_is_detected_once_per_process(monkeypatch):
    calls: list[int] = []

    def fake_detect():
        calls.append(1)
        return {"commit": "a" * 40, "dirty_paths": []}

    monkeypatch.setattr(rid, "detect_runtime_identity", fake_detect)
    rid.reset_runtime_identity_cache()
    try:
        first = rid.runtime_identity()
        first["commit"] = "mutated"
        second = rid.runtime_identity()
    finally:
        rid.reset_runtime_identity_cache()

    assert len(calls) == 1
    assert second["commit"] == "a" * 40  # callers get copies, the cache stays intact


def test_real_package_identity_is_detected_from_this_checkout():
    identity = rid.detect_runtime_identity()

    assert identity["package_path"] == str(rid.PACKAGE_DIR)
    assert identity["source"] in {"git", "build_env", "unknown"}


# --- health --------------------------------------------------------------------


IDENTITY_A = {
    "source": "git",
    "commit": "a" * 40,
    "short_commit": "a" * 7,
    "branch": "test",
    "dirty": False,
    "dirty_count": 0,
    "dirty_paths": [],
    "repo_path": "/Users/x/ClipForge",
    "label": "test @ aaaaaaa",
}
IDENTITY_B = {
    **IDENTITY_A,
    "commit": "b" * 40,
    "short_commit": "b" * 7,
    "branch": "cloud/hook-quality-v3-local",
    "dirty": True,
    "dirty_count": 1,
    "dirty_paths": ["apps/api/clipforge/research.py"],
    "label": "cloud/hook-quality-v3-local @ bbbbbbb +dirty",
}


def use_identity(monkeypatch, identity: dict) -> None:
    monkeypatch.setattr(rid, "runtime_identity", lambda: copy.deepcopy(identity))


def test_health_exposes_runtime_identity_without_environment(monkeypatch):
    monkeypatch.setattr("clipforge.main.runtime_identity", lambda: copy.deepcopy(IDENTITY_B))

    response = health(Settings(clipforge_ai_mode="local", openai_api_key="sk-secret"))
    payload = HealthRead.model_validate(response).model_dump(mode="json")

    assert payload["status"] == "ok"
    assert payload["runtime"]["commit"] == "b" * 40
    assert payload["runtime"]["branch"] == "cloud/hook-quality-v3-local"
    assert payload["runtime"]["dirty"] is True
    assert "sk-secret" not in str(payload)


def test_health_identity_has_no_environment_values(monkeypatch):
    monkeypatch.setenv("CLIPFORGE_TEST_SECRET_VALUE", "do-not-leak-123")
    rid.reset_runtime_identity_cache()
    try:
        payload = health(Settings(clipforge_ai_mode="local", openai_api_key=None))
    finally:
        rid.reset_runtime_identity_cache()

    assert payload.runtime is not None and "commit" in payload.runtime
    assert "do-not-leak-123" not in str(payload.model_dump())


# --- persistence -----------------------------------------------------------------


def settings_for(tmp_path: Path) -> Settings:
    return Settings(clipforge_ai_mode="local", openai_api_key=None, render_root=tmp_path)


def request() -> ProjectCreate:
    return ProjectCreate(
        prompt="Explain why the sky is blue", options=AdvancedOptions(research="off")
    )


def fake_render(state, _project_id, revision, _settings, *, progress=None):
    state = copy.deepcopy(state)
    state["render"] = {
        "status": "complete",
        "url": f"/media/fake-v{revision}.mp4",
        "revision": revision,
        "stale": False,
    }
    return attach_hashes(state)


def test_generation_job_and_project_persist_the_running_identity(db, tmp_path, monkeypatch):
    use_identity(monkeypatch, IDENTITY_A)
    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    job, _ = create_generation_job(db, request())
    assert job.runtime_identity is None  # recorded when the job RUNS, by the code that runs it
    assert claim_next_generation_job(db) is not None

    run_generation_job(
        job.id,
        settings_for(tmp_path),
        session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=True),
    )
    db.expire_all()
    completed = db.get(GenerationJob, job.id)
    project = db.get(Project, job.project_id)
    initial, rendered = project.revisions

    assert completed.status == "completed"
    assert completed.runtime_identity["commit"] == "a" * 40
    assert completed.runtime_identity["recorded_at"]
    serialized = GenerationJobRead.model_validate(serialize_generation_job(completed))
    assert serialized.runtime_identity["branch"] == "test"
    assert initial.state["runtime_provenance"]["generation"]["commit"] == "a" * 40
    provenance = rendered.state["runtime_provenance"]
    assert provenance["generation"]["commit"] == "a" * 40  # carried, not replaced
    assert [item["revision"] for item in provenance["renders"]] == [rendered.number]
    assert provenance["renders"][0]["identity"]["commit"] == "a" * 40
    assert provenance["renders"][0]["matches_generation"] is True
    assert provenance["last_render"]["revision"] == rendered.number
    assert serialize_project(project)["revision"]["state"]["runtime_provenance"]


def test_later_render_under_another_commit_keeps_both_identities(db, tmp_path, monkeypatch):
    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    settings = settings_for(tmp_path)
    use_identity(monkeypatch, IDENTITY_A)
    project = create_project(db, request(), settings)
    render_project(db, project, settings)
    first_render = current_revision(project)

    use_identity(monkeypatch, IDENTITY_B)  # the backend restarted on another checkout
    project = get_project(db, project.id)
    render_project(db, project, settings)
    second_render = current_revision(project)

    provenance = second_render.state["runtime_provenance"]
    assert provenance["generation"]["commit"] == "a" * 40
    assert [item["identity"]["commit"] for item in provenance["renders"]] == ["a" * 40, "b" * 40]
    assert [item["revision"] for item in provenance["renders"]] == [
        first_render.number,
        second_render.number,
    ]
    assert provenance["renders"][-1]["matches_generation"] is False
    assert provenance["renders"][-1]["identity"]["dirty_paths"] == ["apps/api/clipforge/research.py"]
    # The earlier revision is untouched history.
    assert [item["identity"]["commit"] for item in first_render.state["runtime_provenance"]["renders"]] == ["a" * 40]


def test_old_project_without_identity_loads_and_renders(db, tmp_path, monkeypatch):
    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    settings = settings_for(tmp_path)
    use_identity(monkeypatch, IDENTITY_A)
    template = create_project(db, request(), settings)
    legacy_state = copy.deepcopy(current_revision(template).state)
    legacy_state.pop("runtime_provenance")
    legacy = Project(original_prompt="Legacy prompt", title="Legacy", status="ready_for_production")
    legacy.revisions.append(
        ProjectRevision(number=1, instruction="Original prompt", kind="initial", state=legacy_state)
    )
    db.add(legacy)
    db.commit()

    loaded = serialize_project(get_project(db, legacy.id))
    assert "runtime_provenance" not in loaded["revision"]["state"]

    render_project(db, get_project(db, legacy.id), settings)
    provenance = current_revision(get_project(db, legacy.id)).state["runtime_provenance"]
    assert provenance["generation"] is None  # never fabricated after the fact
    assert provenance["renders"][0]["identity"]["commit"] == "a" * 40
    assert provenance["renders"][0]["matches_generation"] is None


def test_old_generation_job_without_identity_serializes(db):
    job, _ = create_generation_job(db, request())

    assert GenerationJobRead.model_validate(serialize_generation_job(job)).runtime_identity is None


def test_runtime_schema_adds_identity_column_to_existing_job_table(db):
    bind = db.get_bind()
    with bind.begin() as connection:
        connection.execute(text("ALTER TABLE generation_jobs DROP COLUMN runtime_identity"))
    assert "runtime_identity" not in {c["name"] for c in sa_inspect(bind).get_columns("generation_jobs")}

    ensure_runtime_schema(bind)

    assert "runtime_identity" in {c["name"] for c in sa_inspect(bind).get_columns("generation_jobs")}


# --- hashes ---------------------------------------------------------------------


def test_identity_metadata_never_changes_content_hashes(db, tmp_path, monkeypatch):
    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    use_identity(monkeypatch, IDENTITY_A)
    project = create_project(db, request(), settings_for(tmp_path))
    state = copy.deepcopy(current_revision(project).state)
    state["render"] = {"status": "complete", "url": "/media/x.mp4", "revision": 2}

    plain = copy.deepcopy(state)
    plain.pop("runtime_provenance")
    stamped_a = copy.deepcopy(state)
    rid.stamp_render(stamped_a, 2, snapshot=copy.deepcopy(IDENTITY_A))
    stamped_b = copy.deepcopy(state)
    rid.stamp_generation(stamped_b, copy.deepcopy(IDENTITY_B))
    rid.stamp_render(stamped_b, 2, snapshot=copy.deepcopy(IDENTITY_B))

    hashes = [attach_hashes(item)["content_hashes"] for item in (plain, stamped_a, stamped_b)]
    assert hashes[0] == hashes[1] == hashes[2]
    assert "runtime_provenance" not in hashes[0]
    assert "runtime_provenance" not in stamped_b["render"]


def test_renders_under_different_identities_store_identical_content_hashes(db, tmp_path, monkeypatch):
    monkeypatch.setattr("clipforge.services._render_state", fake_render)
    settings = settings_for(tmp_path)
    use_identity(monkeypatch, IDENTITY_A)
    project = create_project(db, request(), settings)
    render_project(db, project, settings)
    first = copy.deepcopy(current_revision(project).state)

    use_identity(monkeypatch, IDENTITY_B)
    render_project(db, get_project(db, project.id), settings)
    second = copy.deepcopy(current_revision(get_project(db, project.id)).state)

    # Only the render's own revision number differs; identity changes nothing.
    assert {k: v for k, v in first["content_hashes"].items() if k != "render"} == {
        k: v for k, v in second["content_hashes"].items() if k != "render"
    }
    for state in (first, second):
        stripped = copy.deepcopy(state)
        stripped.pop("runtime_provenance")
        assert attach_hashes(stripped)["content_hashes"] == state["content_hashes"]
    assert first["runtime_provenance"]["last_render"]["identity"]["commit"] == "a" * 40
    assert second["runtime_provenance"]["last_render"]["identity"]["commit"] == "b" * 40


def test_render_history_is_bounded():
    state: dict = {}
    rid.stamp_generation(state, copy.deepcopy(IDENTITY_A))
    for number in range(rid.MAX_RENDER_HISTORY + 5):
        rid.stamp_render(state, number, snapshot=copy.deepcopy(IDENTITY_A))

    renders = state["runtime_provenance"]["renders"]
    assert len(renders) == rid.MAX_RENDER_HISTORY
    assert renders[-1]["revision"] == rid.MAX_RENDER_HISTORY + 4


def test_same_code_comparison():
    assert rid.same_code(IDENTITY_A, copy.deepcopy(IDENTITY_A)) is True
    assert rid.same_code(IDENTITY_A, IDENTITY_B) is False
    assert rid.same_code(IDENTITY_B, copy.deepcopy(IDENTITY_B)) is True
    dirtier = {**IDENTITY_B, "dirty_paths": ["other.py"]}
    assert rid.same_code(IDENTITY_B, dirtier) is False
    assert rid.same_code(None, IDENTITY_A) is None
    assert rid.same_code({"commit": None}, IDENTITY_A) is None


def test_language_rebuild_edit_keeps_runtime_history(tmp_path, monkeypatch):
    from clipforge.pipeline import apply_edit, build_initial_state

    settings = settings_for(tmp_path)
    state = build_initial_state("Explain why the sky is blue", AdvancedOptions(research="off", language="en"), settings)
    rid.stamp_generation(state, copy.deepcopy(IDENTITY_A))
    rid.stamp_render(state, 2, snapshot=copy.deepcopy(IDENTITY_A))

    edited, _changed = apply_edit(state, "Translate this video to German", settings)

    assert edited["intent"]["language"] == "de"
    assert edited["runtime_provenance"] == state["runtime_provenance"]


def test_job_with_unusable_request_still_records_its_runtime(db, tmp_path, monkeypatch):
    use_identity(monkeypatch, IDENTITY_B)
    job, _ = create_generation_job(db, request())
    job.request_payload = {}
    db.commit()
    assert claim_next_generation_job(db) is not None

    run_generation_job(
        job.id,
        settings_for(tmp_path),
        session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=True),
    )
    db.expire_all()
    failed = db.get(GenerationJob, job.id)

    assert failed.status == "failed"
    assert failed.runtime_identity["commit"] == "b" * 40
