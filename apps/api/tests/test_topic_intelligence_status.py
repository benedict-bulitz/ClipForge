"""Topic Intelligence: explicit suggestion states and the diagnosis that tells them apart."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from topic_support import NOW, FakeWiki, deps, settings, spike

from clipforge.models import TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import scoring, service
from clipforge.topic_intelligence.routes import topic_status_route

GOOD = [
    {"title": "Schluckauf", "views": 12_000, "description": "Unwillkürliche Kontraktion des Zwerchfells",
     "extract": "Schluckauf ist eine unwillkürliche Kontraktion des Zwerchfells.", "history": spike(2_000, 4_000)},
    {"title": "Polarlicht", "views": 30_000, "description": "Leuchterscheinung am Nachthimmel",
     "extract": "Ein Polarlicht ist eine Leuchterscheinung.", "history": spike(3_000, 9_000)},
]
WEAK = [
    {"title": "29. September", "views": 38_000, "description": "Tag im Gregorianischen Kalender",
     "extract": "Der 29. September ist der 272. Tag des Jahres.", "history": spike(400, 12_000)},
    {"title": "Gol-Transportes-Aéreos-Flug 1907", "views": 36_000, "description": "Flugunfall in Brasilien 2006",
     "extract": "Ein Linienflug, der 2006 kollidierte.", "history": spike(200, 8_000)},
]


@pytest.fixture(autouse=True)
def reset_warmup():
    service.WARMUP.update(state="idle", started_at=None, finished_at=None, result=None, error=None)
    yield
    service.WARMUP.update(state="idle", started_at=None, finished_at=None, result=None, error=None)


def chips(db, articles, *, count=3, now=NOW, wiki=None, **kwargs):
    return service.suggestions(db, settings(), deps(wiki=wiki or FakeWiki(articles)), count=count, now=now, **kwargs)


def test_quality_floor_rejecting_everything_is_explicit_not_empty_ok(db):
    result = chips(db, WEAK)
    assert result["status"] == "exhausted" and result["candidates"] == []
    summary = result["summary"]
    assert summary["accepted"] == 0 and summary["rejected"] >= 2
    assert summary["rejection_reasons"]["question_no_question_transformation"] >= 2
    assert {item["topic"] for item in summary["rejected_candidates"]} >= {"29. September", "Gol-Transportes-Aéreos-Flug 1907"}
    assert service.diagnose(db, settings(), now=NOW) == "question_transformation_failed"


def test_quality_floor_is_not_lowered_to_fill_three_slots(db):
    result = chips(db, GOOD + WEAK, count=3)
    assert result["status"] == "partial"
    questions = {item["question"] for item in result["candidates"]}
    assert all("29. September" not in question and "1907" not in question for question in questions)
    assert len(result["candidates"]) == 2
    assert service.diagnose(db, settings(), now=NOW) == "fewer_than_three_candidates"


def test_running_discovery_is_reported_instead_of_blocking(db):
    chips(db, GOOD, count=1)
    first = db.scalar(select(TopicCandidateRecord).where(TopicCandidateRecord.status == "proposed"))
    assert service._FLIGHT.acquire(blocking=False)
    try:
        result = chips(db, GOOD, picked=[first.candidate_id])
        assert result["status"] == "discovering" and result["retry_after_seconds"] > 0
        assert service.diagnose(db, settings(), now=NOW) == "discovery_running"
    finally:
        service._FLIGHT.release()
    assert db.get(TopicCandidateRecord, first.candidate_id).status == "picked"  # the pick was still recorded


def test_provider_failure_is_diagnosed(db):
    result = chips(db, [], wiki=FakeWiki(fail=True))
    assert result["status"] == "unavailable"
    assert result["message"] == "Topic discovery is temporarily unavailable."
    assert service.diagnose(db, settings(), now=NOW) == "provider_failure"
    report = service.discovery_status(db, settings(), now=NOW)
    assert {item["name"]: item["status"] for item in report["run"]["sources"]}["wikipedia_pageviews"] == "failed"


def test_stale_and_expired_pools_are_diagnosed(db):
    assert service.diagnose(db, settings(), now=NOW) == "no_pool_yet"
    chips(db, GOOD)
    assert service.diagnose(db, settings(), now=NOW) == "fewer_than_three_candidates"
    assert service.diagnose(db, settings(), now=NOW + timedelta(hours=2)) == "pool_expired_refreshes_on_next_request"
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    run.score_version = "ti-score-v1"
    db.commit()
    assert service.diagnose(db, settings(), now=NOW) == "stale_pool_other_version"


def test_warmup_failure_and_success_are_recorded(db):
    factory = sessionmaker(bind=db.get_bind())

    def broken(_db):
        raise RuntimeError("keyring locked")

    service.run_warmup(factory, settings(), broken)
    assert service.WARMUP["state"] == "failed" and "keyring locked" in service.WARMUP["error"]
    assert service.diagnose(db, settings(), now=NOW) == "warmup_failed"
    discovery = deps(wiki=FakeWiki(GOOD))
    service.run_warmup(factory, settings(), lambda _db: discovery)
    assert service.WARMUP["state"] == "done" and service.WARMUP["result"] in {"ok", "partial", "pool_already_fresh"}


def test_status_endpoint_reports_everything_needed_to_debug(db):
    chips(db, GOOD + WEAK)
    report = topic_status_route(db, settings())
    assert report["current_score_version"] == scoring.SCORE_VERSION
    assert report["diagnosis"] in {"ok", "fewer_than_three_candidates", "pool_expired_refreshes_on_next_request"}
    assert report["config"]["question_rewriting"] == "template"
    assert report["config"]["youtube_connected"] is False
    assert report["pool"]["accepted"] >= 2 and report["pool"]["rejected"] >= 2
    assert report["pool"]["rejection_reasons"]["question_no_question_transformation"] >= 2
    assert {"accepted_candidates", "rejected_candidates", "raw_topics", "evaluated"} <= set(report["pool"])
    assert report["caches"] and report["warmup"]["state"] == "idle"
    assert report["run"]["sources"]
