"""Topic Intelligence: handoff into the ONE existing generation pipeline, with provenance."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from topic_support import NOW, deps, settings

from clipforge import generation
from clipforge.main import create_project_route, start_generation_job_route
from clipforge.models import GenerationJob, Project, TopicCandidateRecord
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.topic_intelligence import service
from clipforge.topic_intelligence.routes import next_topic_route, skip_topic_route
from clipforge.youtube.fingerprint import build_fingerprint

PACKAGE = Path(__file__).resolve().parents[1] / "clipforge" / "topic_intelligence"


@pytest.fixture()
def launched(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: calls.append("start"))
    return calls


def proposal(db) -> dict:
    result = service.next_topic(db, settings(), deps(), now=NOW)
    assert result["status"] == "proposed"
    return result["candidate"]


def request(prompt: str, **topic) -> ProjectCreate:
    return ProjectCreate(prompt=prompt, options=AdvancedOptions(research="off", language="de"), **topic)


def test_confirmed_topic_enters_the_existing_generation_entry_point_with_provenance(db, launched):
    candidate = proposal(db)

    job = start_generation_job_route(
        request(candidate["question"], topic_source="topic_intelligence", topic_candidate_id=candidate["candidate_id"]),
        db, settings(),
    )

    assert launched == ["start"]  # the same FIFO worker as manual questions
    stored = db.get(GenerationJob, job["id"])
    provenance = stored.request_payload["topic_provenance"]
    assert stored.request_payload["prompt"] == candidate["question"]
    assert provenance["topic_source"] == "topic_intelligence"
    assert provenance["candidate_id"] == candidate["candidate_id"]
    assert provenance["score_version"] == "ti-score-v1"
    assert provenance["score_breakdown"]["components"]["trend"]["weight"] == 0.2
    assert "trend" in provenance["signals_used"] and "own_performance" not in provenance["signals_used"]
    assert provenance["selected_at"].startswith("20") and provenance["edited"] is False
    record = db.get(TopicCandidateRecord, candidate["candidate_id"])
    assert record.status == "used" and record.used_project_id == job["project_id"]


def test_edited_topic_keeps_provenance_and_marks_the_edit(db, launched):
    candidate = proposal(db)
    job = start_generation_job_route(
        request("Warum leuchten Polarlichter manchmal rot?", topic_source="topic_intelligence", topic_candidate_id=candidate["candidate_id"]),
        db, settings(),
    )
    provenance = db.get(GenerationJob, job["id"]).request_payload["topic_provenance"]
    assert provenance["edited"] is True and provenance["proposed_question"] == candidate["question"]


def test_manual_generation_is_unchanged_and_records_manual_provenance(db, launched):
    job = start_generation_job_route(request("Warum ist der Himmel blau?"), db, settings())
    payload = db.get(GenerationJob, job["id"]).request_payload
    assert payload["topic_source"] == "manual"
    assert payload["topic_provenance"] == {"topic_source": "manual"}
    assert launched == ["start"]


def test_client_cannot_forge_provenance(db, launched):
    forged = request("Warum ist der Himmel blau?")
    forged.topic_provenance = {"topic_source": "topic_intelligence", "final_score": 1.0}
    job = start_generation_job_route(forged, db, settings())
    assert db.get(GenerationJob, job["id"]).request_payload["topic_provenance"] == {"topic_source": "manual"}


@pytest.mark.parametrize(("candidate_id", "status_code"), [("tc_missing", 422), (None, 422)])
def test_unknown_candidates_are_refused(db, launched, candidate_id, status_code):
    with pytest.raises(HTTPException) as error:
        start_generation_job_route(request("Warum ist der Himmel blau?", topic_source="topic_intelligence", topic_candidate_id=candidate_id), db, settings())
    assert error.value.status_code == status_code
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
    assert launched == []


def test_a_topic_is_generated_once(db, launched):
    candidate = proposal(db)
    payload = request(candidate["question"], topic_source="topic_intelligence", topic_candidate_id=candidate["candidate_id"])
    start_generation_job_route(payload, db, settings())
    with pytest.raises(HTTPException) as error:
        start_generation_job_route(payload, db, settings())
    assert error.value.status_code == 409
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1


def test_used_topic_is_not_proposed_again(db, launched):
    candidate = proposal(db)
    start_generation_job_route(request(candidate["question"], topic_source="topic_intelligence", topic_candidate_id=candidate["candidate_id"]), db, settings())
    following = service.next_topic(db, settings(), deps(), now=NOW)
    assert following["candidate"] is None or following["candidate"]["candidate_id"] != candidate["candidate_id"]


def test_project_state_and_fingerprint_carry_the_provenance(db):
    candidate = proposal(db)
    payload = request(candidate["question"], topic_source="topic_intelligence", topic_candidate_id=candidate["candidate_id"])
    project = create_project_route(payload, db, settings())
    stored = db.get(Project, project["id"])
    state = stored.revisions[0].state
    assert state["topic_provenance"]["candidate_id"] == candidate["candidate_id"]
    assert state["topic_provenance"]["topic_source"] == "topic_intelligence"
    fingerprint = build_fingerprint(state, project_id=stored.id, render_revision=1, render_sha256="0" * 64, file_size=1)
    assert fingerprint["content"]["topic"]["topic_source"] == "topic_intelligence"
    assert fingerprint["content"]["topic"]["candidate_id"] == candidate["candidate_id"]
    manual = create_project_route(request("Warum ist der Himmel blau?"), db, settings())
    manual_state = db.get(Project, manual["id"]).revisions[0].state
    assert manual_state["topic_provenance"] == {"topic_source": "manual"}


def test_generate_next_video_never_generates_or_publishes_by_itself(db, launched):
    result = next_topic_route(db, settings(), deps())
    assert result["status"] == "proposed"
    skip_topic_route(result["candidate"]["candidate_id"], db, settings(), deps())
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
    assert db.scalar(select(func.count()).select_from(Project)) == 0
    assert launched == []


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(f"{node.module or ''}:{','.join(alias.name for alias in node.names)}")
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_no_second_generation_pipeline():
    forbidden = ("pipeline", "services", "generation", "renderer", "script_writer", "triple_hook", "story_arc", "visual_director", "uploads", "publishing")
    for path in PACKAGE.glob("*.py"):
        for name in _imports(path):
            module = name.split(":")[0].lstrip(".")
            assert module.split(".")[0] not in forbidden, f"{path.name} imports {name}"
            assert "create_generation_job" not in name and "create_project" not in name and "build_initial_state" not in name
    main = (PACKAGE.parent / "main.py").read_text()
    assert main.count("create_generation_job(db, payload)") == 1
    assert generation.create_generation_job.__module__ == "clipforge.generation"


def test_one_scoring_authority():
    """Only scoring.py computes scores; others may only copy ``candidate.final_score``."""
    for path in PACKAGE.glob("*.py"):
        if path.name == "scoring.py":
            continue
        source = path.read_text()
        assert not re.search(r"final_score\s*=\s*+(?!candidate\.final_score\b)", source), path.name
        assert "DEFAULT_WEIGHTS" not in source and "CONFIDENCE_WEIGHT" not in source, path.name
    web = PACKAGE.parents[2] / "web" / "src"
    assert web.is_dir()
    for path in [*web.rglob("*.ts"), *web.rglob("*.tsx")]:
        if "test" in path.name:
            continue
        assert "final_score *" not in path.read_text() and "weight *" not in path.read_text(), path


def test_http_routes_are_wired_through_the_app(db, launched):
    from fastapi.testclient import TestClient

    from clipforge.config import get_settings
    from clipforge.database import get_db
    from clipforge.main import app
    from clipforge.topic_intelligence.routes import get_discovery_deps

    discovery = deps()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_discovery_deps] = lambda: discovery
    app.dependency_overrides[get_settings] = lambda: settings()
    try:
        client = TestClient(app)
        proposed = client.post("/api/topic-intelligence/next", json={"refresh": False}).json()
        assert proposed["status"] == "proposed"
        another = client.post(f"/api/topic-intelligence/candidates/{proposed['candidate']['candidate_id']}/skip").json()
        assert another["candidate"]["candidate_id"] != proposed["candidate"]["candidate_id"]
        assert client.post("/api/topic-intelligence/candidates/tc_nope/skip").status_code == 404
        job = client.post("/api/generation-jobs", json={
            "prompt": another["candidate"]["question"], "options": {"research": "off"},
            "topic_source": "topic_intelligence", "topic_candidate_id": another["candidate"]["candidate_id"],
        })
        assert job.status_code == 202
        assert client.post("/api/generation-jobs", json={
            "prompt": "Warum?", "topic_source": "topic_intelligence", "topic_candidate_id": another["candidate"]["candidate_id"],
        }).status_code == 409
        status = client.get("/api/topic-intelligence/status").json()
        assert status["run"]["score_version"] == "ti-score-v1"
    finally:
        app.dependency_overrides.clear()
