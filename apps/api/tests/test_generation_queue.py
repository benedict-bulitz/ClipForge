from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from clipforge.config import Settings
from clipforge.generation import (
    claim_next_generation_job,
    clear_queued_generation_jobs,
    create_generation_job,
    list_generation_jobs,
    mark_interrupted_generation_jobs,
    remove_queued_generation_job,
    schedule_next_generation,
)
from clipforge.main import list_project_overview
from clipforge.models import GenerationJob, Project, ProjectRevision
from clipforge.schemas import AdvancedOptions, ProjectCreate


def request(topic: str) -> ProjectCreate:
    return ProjectCreate(prompt=topic, options=AdvancedOptions(research="off"))


def test_fifo_claims_one_job_and_keeps_later_jobs_queued(db):
    first, _ = create_generation_job(db, request("First topic"))
    second, _ = create_generation_job(db, request("Second topic"))
    third, _ = create_generation_job(db, request("Third topic"))

    claimed = claim_next_generation_job(db)

    assert claimed is not None and claimed.id == first.id
    assert db.get(GenerationJob, first.id).status == "running"
    assert db.get(GenerationJob, second.id).status == "queued"
    assert db.get(GenerationJob, third.id).status == "queued"
    queue = list_generation_jobs(db)
    assert [(item["id"], item["queue_position"]) for item in queue] == [
        (first.id, None), (second.id, 1), (third.id, 2)
    ]


def test_second_scheduler_cannot_claim_a_second_running_job(db):
    first, _ = create_generation_job(db, request("First topic"))
    second, _ = create_generation_job(db, request("Second topic"))

    assert claim_next_generation_job(db).id == first.id
    assert claim_next_generation_job(db) is None
    assert db.get(GenerationJob, second.id).status == "queued"


def test_queued_jobs_survive_restart_but_running_job_is_safely_interrupted(db):
    running, _ = create_generation_job(db, request("Interrupted topic"))
    queued, _ = create_generation_job(db, request("Queued topic"))
    running.status = "running"
    db.commit()

    assert mark_interrupted_generation_jobs(db) == 1
    assert db.get(GenerationJob, running.id).status == "failed"
    assert db.get(GenerationJob, queued.id).status == "queued"


def test_scheduler_launches_only_one_persisted_claim(db):
    first, _ = create_generation_job(db, request("First topic"))
    second, _ = create_generation_job(db, request("Second topic"))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=True)
    launched: list[str] = []
    settings = Settings(clipforge_ai_mode="local", openai_api_key=None)

    assert schedule_next_generation(settings, session_factory=factory, launch=launched.append) == first.id
    assert schedule_next_generation(settings, session_factory=factory, launch=launched.append) is None
    assert launched == [first.id]
    assert db.get(GenerationJob, second.id).status == "queued"


def test_terminal_job_releases_slot_for_next_fifo_job(db):
    first, _ = create_generation_job(db, request("First topic"))
    second, _ = create_generation_job(db, request("Second topic"))
    assert claim_next_generation_job(db).id == first.id
    first.status = "failed"
    first.completed_at = datetime.now(UTC)
    db.commit()

    assert claim_next_generation_job(db).id == second.id
    assert db.get(GenerationJob, second.id).status == "running"


def test_completed_history_is_never_requeued(db):
    completed, _ = create_generation_job(db, request("Finished topic"))
    completed.status = "completed"
    completed.completed_at = datetime.now(UTC) - timedelta(seconds=1)
    queued, _ = create_generation_job(db, request("Queued topic"))
    db.commit()

    assert claim_next_generation_job(db).id == queued.id
    assert db.get(GenerationJob, completed.id).status == "completed"


def test_removing_one_queued_job_preserves_active_job_and_fifo_order(db):
    active, _ = create_generation_job(db, request("Active"))
    first, _ = create_generation_job(db, request("First waiting"))
    middle, _ = create_generation_job(db, request("Middle waiting"))
    last, _ = create_generation_job(db, request("Last waiting"))
    assert claim_next_generation_job(db).id == active.id

    assert remove_queued_generation_job(db, middle.id) is True
    assert db.get(GenerationJob, active.id).status == "running"
    assert db.get(GenerationJob, middle.id).status == "removed"
    active_queue = [item for item in list_generation_jobs(db) if item["status"] in ("running", "queued")]
    assert [(item["id"], item["queue_position"]) for item in active_queue] == [
        (active.id, None), (first.id, 1), (last.id, 2)
    ]
    db.expire_all()
    assert db.get(GenerationJob, middle.id).status == "removed"


def test_clear_queue_removes_waiting_jobs_but_keeps_active_and_history(db):
    active, _ = create_generation_job(db, request("Active"))
    first, _ = create_generation_job(db, request("Same question?"))
    second, _ = create_generation_job(db, request("Same question?"))
    completed, _ = create_generation_job(db, request("Completed"))
    assert claim_next_generation_job(db).id == active.id
    completed.status = "completed"
    completed.completed_at = datetime.now(UTC)
    db.commit()

    assert clear_queued_generation_jobs(db) == 2
    assert db.get(GenerationJob, active.id).status == "running"
    assert db.get(GenerationJob, first.id).status == "removed"
    assert db.get(GenerationJob, second.id).status == "removed"
    assert db.get(GenerationJob, completed.id).status == "completed"
    assert [item["id"] for item in list_generation_jobs(db) if item["status"] in ("running", "queued")] == [active.id]
    assert clear_queued_generation_jobs(db) == 0


def test_four_topics_keep_distinct_persisted_identity_through_refresh_and_completion(db):
    topics = [
        "Warum wird uns beim Aufstehen schwarz vor Augen?",
        "Warum bekommen wir Schluckauf?",
        "Warum können Flugzeuge fliegen?",
        "Warum knackt Holz im Feuer?",
    ]
    jobs = [create_generation_job(db, request(topic))[0] for topic in topics]
    ids = [job.project_id for job in jobs]
    assert len(set(ids)) == 4
    assert claim_next_generation_job(db).project_id == ids[0]

    def snapshot():
        return [(item["project_id"], item["prompt"], item["status"], item["queue_position"]) for item in list_generation_jobs(db)]

    expected = [(ids[0], topics[0], "running", None)] + [
        (ids[index], topics[index], "queued", index) for index in range(1, 4)
    ]
    assert snapshot() == expected
    db.expire_all()  # Simulate a fresh API read after polling/reload.
    assert snapshot() == expected
    assert len([item for item in snapshot() if item[2] in ("running", "queued")]) == 4

    first = db.get(GenerationJob, jobs[0].id)
    first.status = "completed"
    first.completed_at = datetime.now(UTC)
    db.add(Project(id=ids[0], original_prompt=topics[0], title=topics[0], status="rendered", current_revision=1, active_tip_revision=1, revisions=[ProjectRevision(number=1, instruction="Initial", kind="initial", state={"script": {"text": "Done"}}, changed_components=[])]))
    db.commit()
    assert claim_next_generation_job(db).project_id == ids[1]
    assert snapshot()[1:] == [
        (ids[1], topics[1], "running", None),
        (ids[2], topics[2], "queued", 1),
        (ids[3], topics[3], "queued", 2),
    ]
    overview = list_project_overview(db)
    assert len(overview) == 4
    assert any(item["id"] == ids[0] and item["status"] == "rendered" for item in overview)


def test_repeated_same_question_creates_another_project(db):
    first, _ = create_generation_job(db, request("Warum bekommen wir Schluckauf?"))
    second, _ = create_generation_job(db, request("Warum bekommen wir Schluckauf?"))
    assert first.project_id != second.project_id
    assert len(list_generation_jobs(db)) == 2


def test_overview_returns_every_persisted_project_beyond_old_twenty_item_limit(db):
    for index in range(25):
        db.add(Project(id=f"history-{index}", original_prompt=f"Topic {index}", title=f"Topic {index}", status="rendered", current_revision=1, active_tip_revision=1))
    db.commit()

    overview = list_project_overview(db)

    assert len(overview) == 25
    assert {item["id"] for item in overview} == {f"history-{index}" for index in range(25)}


def test_queue_listing_uses_same_stable_tie_break_as_worker_claim(db):
    first, _ = create_generation_job(db, request("First tied topic"))
    second, _ = create_generation_job(db, request("Second tied topic"))
    instant = datetime(2026, 9, 23, tzinfo=UTC)
    first.created_at = instant
    second.created_at = instant
    db.commit()

    expected = sorted([first.id, second.id])
    assert [item["id"] for item in list_generation_jobs(db)] == expected
    assert claim_next_generation_job(db).id == expected[0]
