"""Topic Intelligence: discovery is bounded - no provider, curator or lock waits forever.

Real Mac (d86d9b3): diagnosis=discovery_running, warmup.state=running, raw_topics=96,
evaluated=0 and Home stuck on "Themenvorschläge werden gesucht…".  96 raw topics were
committed by the curator's pre-request commit: the warm-up sat in the OpenAI request,
which had no timeout of its own.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_topic_intelligence_local_questions import StaticSource, raw
from topic_support import NOW, FakeCurator, FakeWiki, settings

from clipforge.models import GenerationJob, TopicDiscoveryRun
from clipforge.topic_intelligence import runtime, semantic, service
from clipforge.topic_intelligence.service import DiscoveryDeps
from clipforge.topic_intelligence.sources import WikipediaPageviewsSource, YouTubeCompetitionProbe

KEY = {"openai_api_key": "sk-test"}
SCHWINDEL = "Warum wird einem schwindelig, wenn man schnell aufsteht?"
BLUE = "Warum ist der Himmel eigentlich blau?"


@pytest.fixture(autouse=True)
def fast_bounds(monkeypatch):
    monkeypatch.setattr(runtime, "PROVIDER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(runtime, "CURATOR_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(runtime, "CURATOR_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(runtime, "FLIGHT_WAIT_SECONDS", 0.2)
    service.WARMUP.update(state="idle", started_at=None, finished_at=None, result=None, error=None)
    yield
    assert not runtime.FLIGHT.locked(), "a test left the discovery flight held"


@pytest.fixture()
def gate():
    """Releases every hung call at teardown so no abandoned thread outlives the test."""
    event = threading.Event()
    yield event
    event.set()


class HangingHttp:
    """A provider whose socket never answers (until the test releases it)."""

    def __init__(self, gate: threading.Event, db=None, answer: Any = None) -> None:
        self.gate = gate
        self.db = db
        self.answer = answer or FakeWiki()
        self.entered = threading.Event()
        self.transaction_open: list[bool] = []
        self.calls = 0

    def __call__(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self.calls += 1
        if self.db is not None:
            self.transaction_open.append(self.db.in_transaction())
        self.entered.set()
        self.gate.wait()
        return self.answer(url, params, headers)


class HangingCurator(FakeCurator):
    def __init__(self, gate: threading.Event, db=None) -> None:
        super().__init__()
        self.gate = gate
        self.db = db
        self.transaction_open: list[bool] = []

    def parse(self, **kwargs: Any):
        if self.db is not None:
            self.transaction_open.append(self.db.in_transaction())
        self.gate.wait()
        return super().parse(**kwargs)


def with_sources(*sources) -> DiscoveryDeps:
    return DiscoveryDeps(sources=list(sources), probe=YouTubeCompetitionProbe(None, None))


def static(*titles: str) -> StaticSource:
    return StaticSource([raw(title, trend=0.8) for title in titles])


def start_warmup(factory, deps_factory) -> threading.Thread:
    # The suite stubs ``warm_pool_in_background`` (no real providers at app start); run the same body.
    thread = threading.Thread(target=service.run_warmup, args=(factory, settings(), deps_factory), daemon=True)
    thread.start()
    return thread


def wait_until(predicate, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


# --- Provider and curator timeouts --------------------------------------------------------------


def test_a_hung_provider_times_out_and_the_other_providers_continue(db, gate):
    hung = HangingHttp(gate, db)
    started = time.monotonic()
    result = service.suggestions(db, settings(), with_sources(WikipediaPageviewsSource(hung), static(SCHWINDEL)), count=3, now=NOW)
    assert time.monotonic() - started < 5
    assert [item["question"] for item in result["candidates"]] == [SCHWINDEL]
    reports = {item["name"]: item for item in result["pool"]["sources"]}
    assert reports["wikipedia_pageviews"]["status"] == "timeout"
    assert "timed out" in reports["wikipedia_pageviews"]["error"]
    assert reports["static_de"]["status"] == "ok"
    status = service.discovery_status(db, settings(), now=NOW)
    assert status["discovery_running"] is False and status["discovery"]["discovery_stage"] == "idle"
    assert any(event["code"] == "timeout:wikipedia_pageviews" for event in status["discovery"]["events"])
    # The socket was waited on without an open database transaction.
    assert hung.transaction_open == [False]


def test_a_timed_out_provider_is_not_waited_on_again_in_the_same_refresh(db, gate):
    hung = HangingHttp(gate)
    deps = with_sources(WikipediaPageviewsSource(hung), static(SCHWINDEL, BLUE))
    assert runtime.FLIGHT.acquire(blocking=False)
    try:
        first = service.discover(db, settings(), deps, now=NOW)
        started = time.monotonic()
        second = service.discover(db, settings(), deps, now=NOW, broaden_from=first)  # e.g. the broadening pass
        assert time.monotonic() - started < runtime.PROVIDER_TIMEOUT_SECONDS
    finally:
        runtime.FLIGHT.release()
    assert hung.calls == 1
    assert {item["name"]: item["status"] for item in second.sources}["wikipedia_pageviews"] == "timeout"
    # A new refresh (new flight) tries the provider again.
    service.suggestions(db, settings(), deps, count=3, now=NOW + service.MIN_REFRESH_INTERVAL * 5)
    assert hung.calls == 2


def test_a_hung_curator_times_out_and_discovery_ends_with_strict_local_rules(db, gate, monkeypatch):
    curator = HangingCurator(gate, db)
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    started = time.monotonic()
    result = service.suggestions(db, settings(**KEY), with_sources(static(SCHWINDEL, "Sind wir nur noch Pseudofreunde?")), count=3, now=NOW)
    assert time.monotonic() - started < 5
    assert [item["question"] for item in result["candidates"]] == [SCHWINDEL]  # strict local fallback
    assert result["summary"]["semantic_validation"]["status"] == "failed"
    assert "CallTimeout" in " ".join(result["summary"]["semantic_validation"]["errors"])
    assert curator.client_options == {"timeout": runtime.CURATOR_TIMEOUT_SECONDS, "max_retries": 0}
    assert curator.transaction_open == [False]
    assert not runtime.FLIGHT.locked()


def test_no_new_curator_request_after_the_ai_deadline(db, monkeypatch):
    curator = FakeCurator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    monkeypatch.setattr(runtime, "AI_DEADLINE_SECONDS", 0.0)
    result = service.suggestions(db, settings(**KEY), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    assert curator.requests == []
    assert [item["question"] for item in result["candidates"]] == [SCHWINDEL]
    assert "ai_deadline" in " ".join(result["summary"]["semantic_validation"]["errors"])


# --- Lock safety ----------------------------------------------------------------------------------


def test_an_exception_releases_the_flight_and_a_retry_works(db, monkeypatch):
    class Broken(FakeCurator):
        def parse(self, **kwargs: Any):
            raise RuntimeError("unexpected client failure")

    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", Broken())
    failed = service.suggestions(db, settings(**KEY), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    assert failed["status"] == "unavailable" and failed["message"] == service.DISCOVERY_FAILED_MESSAGE
    assert "unexpected client failure" in failed["error"]
    assert not runtime.FLIGHT.locked()
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    assert run.status == "failed" and run.sources[-1]["name"] == "discovery"
    assert service.diagnose(db, settings(**KEY), now=NOW) == "discovery_failed"
    # "Neue Vorschläge" after the failure: a fresh attempt, not a stuck state.
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator())
    retried = service.suggestions(db, settings(**KEY), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    assert [item["question"] for item in retried["candidates"]] == [SCHWINDEL]
    assert service.diagnose(db, settings(**KEY), now=NOW) != "discovery_failed"


def test_a_failed_follow_up_discovery_keeps_the_existing_pool(db, monkeypatch):
    deps = with_sources(static(SCHWINDEL, BLUE))
    first = service.suggestions(db, settings(), deps, count=1, now=NOW)
    shown = first["candidates"][0]

    attempts: list[int] = []

    def failing(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError("provider exploded mid-refresh")

    monkeypatch.setattr(service, "_discover", failing)
    # Pool short + old enough -> a refresh runs and fails: the chip that is left is still served.
    later = NOW + service.MIN_REFRESH_INTERVAL + service.MIN_REFRESH_INTERVAL / 10
    result = service.suggestions(db, settings(), deps, count=3, exclude=[shown["candidate_id"]], now=later)
    assert result["status"] == "partial" and len(result["candidates"]) == 1
    assert result["candidates"][0]["question"] != shown["question"]
    assert attempts  # the refresh really ran (and failed)
    assert not runtime.FLIGHT.locked()


def test_startup_warmup_cannot_deadlock_with_a_suggestion_request(db, gate, monkeypatch):
    monkeypatch.setattr(runtime, "PROVIDER_TIMEOUT_SECONDS", 10.0)  # the warm-up is slow, not timed out
    factory = sessionmaker(bind=db.get_bind())
    hung = HangingHttp(gate)
    warmup = start_warmup(factory, lambda _db: with_sources(WikipediaPageviewsSource(hung)))
    assert hung.entered.wait(5), service.WARMUP
    # While the warm-up waits on its provider: Home is answered immediately, with the live stage.
    started = time.monotonic()
    busy = service.suggestions(db, settings(), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    proposal = service.next_topic(db, settings(), with_sources(static(SCHWINDEL)), now=NOW)
    assert time.monotonic() - started < 2
    assert busy["status"] == "discovering" and proposal["status"] == "discovering"
    assert busy["discovery"]["discovery_stage"] == "provider_fetch"
    status = service.discovery_status(db, settings(), now=NOW)
    live = status["discovery"]
    assert status["diagnosis"] == "discovery_running" and live["lock_held"] and live["owner"] == "warmup"
    assert live["active_provider"] == "wikipedia_pageviews" and live["elapsed_seconds"] >= 0
    assert live["stage_started_at"] and [item["stage"] for item in live["stages"]][:2] == ["lock_acquired", "prune"]
    assert status["warmup"]["state"] == "running"
    # Manual generation never waits for topic discovery.
    from clipforge.main import start_generation_job_route
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: None)
    job = start_generation_job_route(ProjectCreate(prompt="Warum ist der Himmel blau?", options=AdvancedOptions(research="off")), db, settings())
    assert db.get(GenerationJob, job["id"]).status == "queued"
    gate.set()
    warmup.join(5)
    assert not warmup.is_alive() and service.WARMUP["state"] == "done"
    assert not runtime.FLIGHT.locked()


def test_a_warmup_beyond_the_hard_limit_is_abandoned_and_discovery_can_retry(db, gate, monkeypatch):
    monkeypatch.setattr(runtime, "PROVIDER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "FLIGHT_MAX_SECONDS", 0.3)
    factory = sessionmaker(bind=db.get_bind())
    hung = HangingHttp(gate)
    warmup = start_warmup(factory, lambda _db: with_sources(WikipediaPageviewsSource(hung)))
    assert hung.entered.wait(5)
    wait_until(lambda: not runtime.FLIGHT.locked())  # the status/watchdog read abandons the stuck owner
    assert service.WARMUP["state"] == "failed" and "discovery_timed_out" in service.WARMUP["error"]
    assert "provider_fetch" in service.WARMUP["error"]
    status = service.discovery_status(db, settings(), now=NOW)
    assert status["discovery_running"] is False and "discovery_timed_out" in status["discovery"]["last_error"]
    # A manual retry is possible at once.
    monkeypatch.setattr(runtime, "FLIGHT_MAX_SECONDS", 300.0)
    retried = service.suggestions(db, settings(), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    assert [item["question"] for item in retried["candidates"]] == [SCHWINDEL]
    # The abandoned warm-up stops at its next stage and records a failed run; it never overwrites the new pool.
    gate.set()
    warmup.join(5)
    assert not warmup.is_alive()
    db.expire_all()
    runs = db.scalars(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence)).all()
    assert [run.status for run in runs] == ["failed", "ok"]
    assert service.WARMUP["state"] == "failed"
    assert not runtime.FLIGHT.locked()
    again = service.suggestions(db, settings(), with_sources(static(SCHWINDEL)), count=3, now=NOW)
    assert [item["question"] for item in again["candidates"]] == [SCHWINDEL]


def test_flight_release_by_an_abandoned_owner_does_not_free_the_new_owner(monkeypatch):
    monkeypatch.setattr(runtime, "FLIGHT_MAX_SECONDS", 0.05)
    flight = runtime.Flight()
    owner_done = threading.Event()
    abandoned_error: list[str] = []

    def old_owner():
        assert flight.acquire(blocking=False, owner="warmup")
        time.sleep(0.2)
        try:
            flight.stage("curation_batch")
        except runtime.DiscoveryAbandoned as exc:
            abandoned_error.append(str(exc))
        flight.release()  # no-op: it no longer owns the flight
        owner_done.set()

    thread = threading.Thread(target=old_owner)
    thread.start()
    time.sleep(0.1)
    assert flight.acquire(timeout=1, owner="request")  # expired owner is abandoned
    monkeypatch.setattr(runtime, "FLIGHT_MAX_SECONDS", 300.0)
    owner_done.wait(2)
    thread.join(2)
    assert abandoned_error and flight.locked()  # still held by the new owner
    flight.release()
    assert not flight.locked()


def test_call_with_timeout_propagates_errors_and_values():
    assert runtime.call_with_timeout(lambda: 42, 1, name="value") == 42
    with pytest.raises(ZeroDivisionError):
        runtime.call_with_timeout(lambda: 1 / 0, 1, name="error")
    blocker = threading.Event()
    with pytest.raises(runtime.CallTimeout):
        runtime.call_with_timeout(blocker.wait, 0.05, name="hang")
    blocker.set()
