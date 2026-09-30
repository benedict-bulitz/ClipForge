"""Topic Intelligence: the Home suggestion chips (3 visible + a hidden reserve)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from topic_support import NOW, FakeWiki, deps, settings, spike

from clipforge.models import GenerationJob, Project, TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import service
from clipforge.topic_intelligence.scoring import DIVERSITY_TOLERANCE

ARTICLES = [
    {"title": title, "views": 50_000 - index, "description": description, "extract": extract,
     "history": spike(500, 5_000 - index * 100)}
    for index, (title, description, extract) in enumerate([
        ("Polarlicht", "Leuchterscheinung am Nachthimmel", "Ein Polarlicht ist eine Leuchterscheinung der Hochatmosphäre."),
        ("Regenbogen", "Optisches Phänomen", "Der Regenbogen ist eine optische Erscheinung aus Licht und Wasser."),
        ("Gewitter", "Wetterereignis mit Blitz und Donner", "Das Gewitter ist ein Wetterereignis mit Blitz und Donner."),
        ("Hagel", "Niederschlag aus Eiskörnern", "Hagel ist Niederschlag aus Eiskörnern."),
        ("Sternschnuppe", "Leuchterscheinung eines Meteoroiden", "Eine Sternschnuppe ist eine Leuchterscheinung am Himmel."),
        ("Nebel", "Wetterereignis in Bodennähe", "Nebel ist ein Wetterereignis mit kleinen Wassertröpfchen."),
        ("Muskelkater", "Muskelschmerz nach Belastung", "Muskelkater bezeichnet Muskelschmerzen nach Anstrengung."),
        ("Schluckauf", "Unwillkürliche Kontraktion des Zwerchfells", "Schluckauf ist eine Kontraktion des Zwerchfells."),
        ("Halo", "Lichterscheinung um Sonne oder Mond", "Ein Halo ist eine Lichterscheinung um die Sonne."),
        ("Wetterleuchten", "Leuchterscheinung eines fernen Gewitters", "Das Wetterleuchten ist eine Leuchterscheinung."),
        ("Seitenstechen", "Symptom beim Laufen", "Seitenstechen ist ein Symptom beim Laufen."),
        ("Morgenrot", "Optisches Phänomen am Morgen", "Das Morgenrot ist eine Erscheinung der Atmosphäre."),
    ])
]


def chips(db, wiki=None, **kwargs):
    return service.suggestions(db, settings(), deps(wiki=wiki or FakeWiki(ARTICLES)), now=kwargs.pop("now", NOW), **kwargs)


def ids(result) -> list[str]:
    return [item["candidate_id"] for item in result["candidates"]]


def test_three_visible_plus_reserve_are_distinct_ranked_candidates(db):
    result = chips(db, count=9)
    assert result["status"] == "ok"
    assert len(result["candidates"]) == 9 and len(set(ids(result))) == 9
    run = db.scalar(select(TopicDiscoveryRun))
    usable = [item for item in run.ranked_candidate_ids if not db.get(TopicCandidateRecord, item).rejection_reasons]
    # The scoring authority's order, only reshuffled among near-equal scores for diversity.
    assert ids(result)[0] == usable[0]
    ninth_best = db.get(TopicCandidateRecord, usable[8]).final_score
    assert all(item["final_score"] >= ninth_best - DIVERSITY_TOLERANCE for item in result["candidates"])
    assert all(db.get(TopicCandidateRecord, item).status == "proposed" for item in ids(result))
    assert all(item["question"].endswith("?") for item in result["candidates"])


def test_refill_excludes_what_the_client_already_shows(db):
    first = chips(db, count=6)
    refill = chips(db, count=3, exclude=ids(first), now=NOW + timedelta(minutes=1))
    assert refill["candidates"] and not set(ids(refill)) & set(ids(first))


def test_refill_reuses_the_pool_without_new_discovery(db):
    wiki = FakeWiki(ARTICLES)
    first = chips(db, wiki=wiki, count=6)
    calls = len(wiki.calls)
    chips(db, wiki=wiki, count=2, exclude=ids(first), now=NOW + timedelta(minutes=2))
    assert len(wiki.calls) == calls
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == 1


def test_picked_and_dismissed_chips_are_remembered(db):
    first = chips(db, count=4)
    picked, dismissed = ids(first)[0], ids(first)[1:4]
    chips(db, count=1, picked=[picked], dismissed=dismissed, exclude=[], now=NOW + timedelta(minutes=1))
    assert db.get(TopicCandidateRecord, picked).status == "picked"
    assert {db.get(TopicCandidateRecord, item).status for item in dismissed} == {"skipped"}
    # Even without a client-side exclude list they do not come back soon, also after a pool refresh.
    later = chips(db, count=6, now=NOW + timedelta(hours=1))
    assert not set(ids(later)) & set(ids(first)[:4])


def test_no_near_duplicate_of_an_excluded_question(db):
    first = chips(db, count=3)
    shown = first["candidates"][0]
    twin = db.get(TopicCandidateRecord, ids(chips(db, count=1, exclude=ids(first)))[0])
    twin_id = twin.candidate_id
    twin.question = shown["question"].replace("eigentlich ", "wirklich ")  # a rephrasing of a visible chip
    twin.status = "pooled"
    db.commit()
    again = chips(db, count=6, exclude=ids(first))
    assert twin_id not in ids(again)


def test_recent_projects_are_never_suggested(db):
    db.add(Project(original_prompt="Wie entsteht eigentlich ein Polarlicht?", title="Polarlicht"))
    db.commit()
    result = chips(db, count=9)
    assert all("Polarlicht" not in item["question"] for item in result["candidates"])


def test_suggestions_never_start_generation(db):
    chips(db, count=6)
    assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
    assert db.scalar(select(func.count()).select_from(Project)) == 0


def test_picked_chip_can_still_be_generated_with_provenance(db, monkeypatch):
    from clipforge.main import start_generation_job_route
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: None)
    first = chips(db, count=3)
    chosen = first["candidates"][0]
    chips(db, count=1, picked=[chosen["candidate_id"]], exclude=ids(first))
    job = start_generation_job_route(ProjectCreate(
        prompt=chosen["question"], options=AdvancedOptions(research="off"),
        topic_source="topic_intelligence", topic_candidate_id=chosen["candidate_id"],
    ), db, settings())
    assert db.get(GenerationJob, job["id"]).request_payload["topic_provenance"]["candidate_id"] == chosen["candidate_id"]


def test_discovery_failure_returns_unavailable_without_candidates(db):
    result = chips(db, wiki=FakeWiki(fail=True), count=3)
    assert result["status"] == "unavailable" and result["candidates"] == []
    assert result["message"] == "Topic discovery is temporarily unavailable."


def test_exhausted_pool_refreshes_once_then_reports_exhausted(db):
    first = chips(db, count=12)
    runs = db.scalar(select(func.count()).select_from(TopicDiscoveryRun))
    result = chips(db, count=3, dismissed=ids(first)[:12], now=NOW + timedelta(minutes=11))
    # One normal refresh, plus at most one broadening pass - bounded, never a loop.
    assert runs + 1 <= db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) <= runs + 2
    assert not set(ids(result)) & set(ids(first))


def test_startup_warm_up_fills_the_pool_once(db):
    factory = sessionmaker(bind=db.get_bind())
    discovery = deps(wiki=FakeWiki(ARTICLES))
    assert service.warm_pool(factory, settings(), lambda _db: discovery, now=NOW) == "ok"
    assert service.warm_pool(factory, settings(), lambda _db: discovery, now=NOW + timedelta(minutes=5)) is None  # still fresh
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == 1
    # A page load right after the warm-up is served from the pool: no new discovery.
    wiki_calls = len(discovery.sources[0].http_get.calls)
    service.suggestions(db, settings(), discovery, count=3, now=NOW + timedelta(minutes=6))
    assert len(discovery.sources[0].http_get.calls) == wiki_calls


def test_app_startup_launches_the_warm_up_in_the_background(no_topic_warmup):
    from fastapi.testclient import TestClient

    from clipforge.main import app

    with TestClient(app):
        pass
    assert len(no_topic_warmup) == 1


def test_generation_is_never_held_up_by_running_discovery(tmp_path, monkeypatch):
    """A generation request completes while a discovery refresh waits on a slow provider."""
    import threading
    import time

    from sqlalchemy import create_engine

    from clipforge.database import Base
    from clipforge.main import start_generation_job_route
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}", connect_args={"check_same_thread": False, "timeout": 2})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: None)
    entered, release = threading.Event(), threading.Event()
    wiki = FakeWiki(ARTICLES)

    def slow_http(url, params, headers):
        if "/per-article/" in url and not entered.is_set():
            entered.set()
            release.wait(10)
        return wiki(url, params, headers)

    discovery = deps()
    from clipforge.topic_intelligence.sources import WikipediaPageviewsSource

    discovery.sources[0] = WikipediaPageviewsSource(slow_http)
    worker = threading.Thread(target=lambda: service.warm_pool(factory, settings(), lambda _db: discovery, now=NOW))
    worker.start()
    try:
        assert entered.wait(5)
        with factory() as db:
            started = time.monotonic()
            job = start_generation_job_route(ProjectCreate(prompt="Warum ist der Himmel blau?", options=AdvancedOptions(research="off")), db, settings())
            assert time.monotonic() - started < 1.5
            assert db.get(GenerationJob, job["id"]).status == "queued"
    finally:
        release.set()
        worker.join(10)


def test_a_short_pool_is_not_rediscovered_on_every_refill(db):
    wiki = FakeWiki(ARTICLES[:2])  # only two usable topics exist
    first = chips(db, wiki=wiki, count=9)
    runs = db.scalar(select(func.count()).select_from(TopicDiscoveryRun))
    calls = len(wiki.calls)
    for minute in (1, 2, 3):
        again = chips(db, wiki=wiki, count=3, exclude=ids(first), now=NOW + timedelta(minutes=minute))
        assert again["status"] == "exhausted" and again["summary"]["evaluated"] >= 1
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == runs  # no refresh, no second broadening
    assert len(wiki.calls) == calls
    assert service.was_broadened(db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc())))
