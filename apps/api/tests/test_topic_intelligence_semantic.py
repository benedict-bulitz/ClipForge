"""Topic Intelligence: combined AI curation (semantic-curator-v1, ti-score-v5).

Real Mac (ti-score-v4, ai_mode=local): 96 raw topics, 60 evaluated, 52
transformation failures, only 4 reached validation, rewrite_requests=0 -
the AI was never used for question creation because CLIPFORGE_AI_MODE=local.
"""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from test_topic_intelligence_local_questions import StaticSource, pool_of_96, raw, static_deps
from topic_support import NOW, FakeCurator, bad, curated, settings

from clipforge.models import GenerationJob, TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import scoring, semantic, service

KEY = {"openai_api_key": "sk-test"}  # CLIPFORGE_AI_MODE stays "local"

QR = "Scannt ein selbstgemalter QR-Code?"
SMARTWATCH = "Was weiß deine Smartwatch wirklich über dein biologisches Alter?"
KOPFHOERER_TITLE = "Du hörst Musik FALSCH: Warum auch die besten Kopfhörer Nachhilfe brauchen"
KOPFHOERER = "Warum brauchen auch die besten Kopfhörer Nachhilfe?"
PSEUDO = "Sind wir nur noch Pseudofreunde?"
CARPLAY = "Kriegen wir Apple CarPlay beim virtuellen Cockpit installiert?"
TALG = "Was kann Rindertalg-Creme wirklich?"
MAENNER = "Was tun Männer am häufigsten für ihre Gesundheit?"
SCHWINDEL = "Warum wird einem schwindelig, wenn man schnell aufsteht?"
BRUST_TITLE = "Brustkrebsvorsorge in Zukunft mit einer einfachen Blutprobe?"
BRUST = "Kann Brustkrebs künftig mit einem einfachen Bluttest erkannt werden?"
HUNDE_TITLE = "Hundehirn: Studie zeigt, wie Konsonanten die Erkennung von Wortmustern prägen"
HUNDE = "Verarbeiten Hunde Wörter ähnlicher wie Menschen als gedacht?"

BY_QUESTION = {
    KOPFHOERER: bad(self_contained_clarity=3, clear_factual_payoff=3, natural_spoken_german=5, issues=["unexplained_metaphor", "unclear_payoff"]),
    PSEUDO: bad(clear_factual_payoff=3, knowledge_short_fit=3, issues=["rhetorical_or_opinion", "unclear_payoff"]),
    CARPLAY: bad(universal_12plus_relevance=2, prior_knowledge_free=3, issues=["too_narrow_audience", "niche_context_required"]),
    TALG: bad(universal_12plus_relevance=4, prior_knowledge_free=3, issues=["niche_context_required"]),
    MAENNER: bad(universal_12plus_relevance=4, knowledge_short_fit=5, issues=["demographic_subgroup_only"]),
}
BY_TOPIC = {
    BRUST_TITLE: curated(BRUST, "koerper_gesundheit"),
    HUNDE_TITLE: curated(HUNDE, "natur_tiere"),
}


def run_pool(db, titles, *, curator=None, monkeypatch=None, config=None, now=NOW, count=9, kind="news"):
    if curator is not None:
        monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    source = StaticSource([raw(title, trend=0.8 - index * 0.005, kind=kind, source="brave_news_de" if kind == "news" else "youtube_trending_de")
                           for index, title in enumerate(titles)])
    result = service.suggestions(db, config or settings(**KEY), static_deps(source), count=count, now=now)
    return result, source


def record_for(db, question: str) -> TopicCandidateRecord:
    return next(record for record in db.scalars(select(TopicCandidateRecord)).all() if record.question == question)


def real_curator() -> FakeCurator:
    return FakeCurator(BY_TOPIC, by_question=BY_QUESTION)


# --- Decoupled from the director AI mode -------------------------------------------------------


def test_local_clipforge_mode_with_a_key_enables_topic_ai(db, monkeypatch):
    config = settings(**KEY)
    assert config.clipforge_ai_mode == "local"
    assert semantic.semantic_enabled(config)
    curator = real_curator()
    result, _ = run_pool(db, [SCHWINDEL], curator=curator, monkeypatch=monkeypatch, config=config, count=1)
    assert len(curator.requests) == 1 and result["candidates"][0]["question"] == SCHWINDEL
    status = service.discovery_status(db, config, now=NOW)
    assert status["config"]["ai_mode"] == "local" and status["config"]["topic_ai"].startswith("enabled")
    assert status["config"]["question_rewriting"] == "curator"


def test_no_key_means_no_topic_ai_even_in_openai_mode(db):
    assert not semantic.semantic_enabled(settings(clipforge_ai_mode="openai", openai_api_key=None))
    assert not semantic.semantic_enabled(settings(topic_semantic_validation=False, **KEY))


# --- One combined, bounded call ------------------------------------------------------------------


def test_the_best_thirty_raw_topics_are_curated_within_three_ai_calls(db, monkeypatch):
    curator = FakeCurator(default=bad(clear_factual_payoff=2))  # nothing passes: worst case
    source = StaticSource(pool_of_96())
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    service.suggestions(db, settings(**KEY), static_deps(source), count=9, now=NOW)
    assert len(curator.requests) == 3
    # 10 per request (v2's 20 timed out on the real Mac): the top ~30 of the pool, not 60 badly.
    assert [len(request) for request in curator.requests] == [10, 10, 10]
    assert source.calls == 1


def test_question_creation_and_validation_happen_in_the_same_call(db, monkeypatch):
    curator = real_curator()
    run_pool(db, [BRUST_TITLE, HUNDE_TITLE, SCHWINDEL], curator=curator, monkeypatch=monkeypatch)
    assert len(curator.requests) == 1  # no separate rewrite + validation requests
    record = record_for(db, BRUST)
    info = record.score_breakdown["quality"]["semantic"]
    assert info["status"] == "curated" and info["curator_version"] == semantic.SEMANTIC_CURATOR_VERSION
    assert set(info["dimensions"]) == set(semantic.DIMENSIONS)
    assert record.provenance["transformation"] == "curator"


def test_statement_headlines_become_grounded_questions(db, monkeypatch):
    result, _ = run_pool(db, [BRUST_TITLE, HUNDE_TITLE], curator=real_curator(), monkeypatch=monkeypatch)
    assert {item["question"] for item in result["candidates"]} == {BRUST, HUNDE}
    for question in (BRUST, HUNDE):
        assert record_for(db, question).score_breakdown["quality"]["semantic"]["grounded"] is True


def test_evidence_reaches_the_curator_with_the_local_question_as_a_hint(db, monkeypatch):
    curator = real_curator()
    run_pool(db, [KOPFHOERER_TITLE, HUNDE_TITLE], curator=curator, monkeypatch=monkeypatch)
    topics = {item["topic"]: item for item in curator.requests[0]}
    assert topics[KOPFHOERER_TITLE]["local_question"] == KOPFHOERER
    assert "local_question" not in topics[HUNDE_TITLE]  # a statement: the curator decides
    assert topics[HUNDE_TITLE]["evidence"][0]["title"] == HUNDE_TITLE


def test_unsupported_premise_is_rejected(db, monkeypatch):
    curator = FakeCurator({BRUST_TITLE: curated("Heilt ein Bluttest künftig jeden Brustkrebs?", "koerper_gesundheit", grounded=False)})
    run_pool(db, [BRUST_TITLE], curator=curator, monkeypatch=monkeypatch)
    assert "semantic_unsupported_premise" in record_for(db, "Heilt ein Bluttest künftig jeden Brustkrebs?").rejection_reasons


# --- Semantic gates on the real Mac questions ------------------------------------------------------


def test_metaphor_rhetoric_niche_and_demographic_questions_are_rejected(db, monkeypatch):
    titles = [QR, SMARTWATCH, KOPFHOERER_TITLE, PSEUDO, CARPLAY, TALG, MAENNER, SCHWINDEL]
    result, _ = run_pool(db, titles, curator=real_curator(), monkeypatch=monkeypatch, kind="video")
    assert {item["question"] for item in result["candidates"]} == {QR, SMARTWATCH, SCHWINDEL}
    assert {"semantic_unexplained_metaphor", "semantic_not_self_contained"} <= set(record_for(db, KOPFHOERER).rejection_reasons)
    assert {"semantic_rhetorical_or_opinion", "semantic_unclear_payoff"} <= set(record_for(db, PSEUDO).rejection_reasons)
    assert {"semantic_too_narrow_audience", "semantic_not_universal"} <= set(record_for(db, CARPLAY).rejection_reasons)
    assert "semantic_prior_knowledge" in record_for(db, TALG).rejection_reasons
    assert {"semantic_demographic_subgroup_only", "semantic_not_universal"} <= set(record_for(db, MAENNER).rejection_reasons)


def test_understandable_is_not_the_same_as_universal(db, monkeypatch):
    # Clear and prior-knowledge-free, but only for one subgroup: still not a default suggestion.
    judgement = bad(universal_12plus_relevance=5)
    run_pool(db, [MAENNER], curator=FakeCurator(by_question={MAENNER: judgement}), monkeypatch=monkeypatch, kind="video")
    record = record_for(db, MAENNER)
    dims = record.score_breakdown["quality"]["semantic"]["dimensions"]
    assert dims["self_contained_clarity"] >= 0.8 and dims["prior_knowledge_free"] >= 0.8
    assert record.rejection_reasons == ["semantic_not_universal"]


def test_gates_are_unchanged(db):
    assert scoring.SCORE_VERSION == "ti-score-v6"
    assert scoring.SEMANTIC_DIMENSION_MIN == 0.6 and scoring.QUALITY_FLOOR == 0.55 and scoring.PRIOR_KNOWLEDGE_GATE == 0.5


# --- Backfill, availability, cache -------------------------------------------------------------------


def test_backfill_continues_through_the_raw_pool_without_provider_calls(db, monkeypatch):
    weak = [f"Pressemitteilung {name} zum Quartal" for name in ("Nord", "Süd", "West", "Ost", "Berg", "Tal", "Stern", "Blitz",
                                                              "Wald", "Fluss", "Sonne", "Mond", "Wind", "Regen", "Feld",
                                                              "Hafen", "Brücke", "Turm", "Markt", "Garten")]
    curator = FakeCurator({**BY_TOPIC, **{title: {"usable": False, "question": ""} for title in weak}})
    result, source = run_pool(db, [*weak, BRUST_TITLE, HUNDE_TITLE], curator=curator, monkeypatch=monkeypatch)
    assert source.calls == 1
    assert [len(request) for request in curator.requests] == [10, 10, 2]  # 20 unusable, curation continued in the same pool
    assert {item["question"] for item in result["candidates"]} == {BRUST, HUNDE}


def test_accepted_candidates_stay_available_and_two_accepted_give_two_suggestions(db, monkeypatch):
    curator = real_curator()
    first, _ = run_pool(db, [BRUST_TITLE, HUNDE_TITLE, PSEUDO], curator=curator, monkeypatch=monkeypatch, count=9)
    assert first["status"] == "partial" and len(first["candidates"]) == 2
    ids = [item["candidate_id"] for item in first["candidates"]]
    # Background refills ask for more while the two are shown: they must never be consumed by that.
    for minute in (1, 2, 3):
        service.suggestions(db, settings(**KEY), static_deps(StaticSource([])), count=7, exclude=ids, now=NOW + timedelta(minutes=minute))
    assert {db.get(TopicCandidateRecord, candidate_id).status for candidate_id in ids} == {"proposed"}
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    assert service.pool_summary(db, run)["available"] == 2
    again = service.suggestions(db, settings(**KEY), static_deps(StaticSource([])), count=3, now=NOW + timedelta(minutes=4))
    assert {item["candidate_id"] for item in again["candidates"]} == set(ids)


def test_only_an_explicit_dismissal_skips_a_candidate(db, monkeypatch):
    first, _ = run_pool(db, [BRUST_TITLE, HUNDE_TITLE], curator=real_curator(), monkeypatch=monkeypatch, count=9)
    dismissed = first["candidates"][0]["candidate_id"]
    service.suggestions(db, settings(**KEY), static_deps(StaticSource([])), count=3, dismissed=[dismissed], now=NOW + timedelta(minutes=1))
    assert db.get(TopicCandidateRecord, dismissed).status == "skipped"
    assert db.get(TopicCandidateRecord, first["candidates"][1]["candidate_id"]).status == "proposed"


def test_curation_is_cached_per_topic_and_curator_version(db, monkeypatch):
    curator = real_curator()
    run_pool(db, [BRUST_TITLE, HUNDE_TITLE], curator=curator, monkeypatch=monkeypatch)
    assert len(curator.requests) == 1
    for record in db.scalars(select(TopicCandidateRecord)).all():
        record.status = "pooled"
    db.commit()
    run_pool(db, [BRUST_TITLE, HUNDE_TITLE], curator=curator, monkeypatch=monkeypatch, now=NOW + timedelta(hours=1))
    assert len(curator.requests) == 1  # a new pool, the same topics: nothing paid twice
    assert record_for(db, BRUST).score_breakdown["quality"]["semantic"]["status"] == "cached"


# --- Degradation and independence ------------------------------------------------------------------------


def test_curator_failure_serves_no_unvalidated_filler(db, monkeypatch):
    # With the curator enabled, a topic it could not judge is "not evaluated": never served as a
    # strict-local fallback question, never persisted as a low-quality rejection.
    result, _ = run_pool(db, [PSEUDO, TALG, SCHWINDEL], curator=FakeCurator(fail=True), monkeypatch=monkeypatch, kind="video")
    assert result["candidates"] == []
    assert db.scalars(select(TopicCandidateRecord)).all() == []
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence))
    assert service.semantic_report(run)["status"] == "failed"
    assert service.pool_summary(db, run)["evaluation"]["unevaluated"] == {"curator_failed": 3}


def test_without_a_key_local_mode_shows_fewer_stricter_suggestions(db):
    result, _ = run_pool(db, [QR, PSEUDO, KOPFHOERER_TITLE, SCHWINDEL], config=settings(), kind="video")
    assert {item["question"] for item in result["candidates"]} == {SCHWINDEL}
    assert result["status"] == "partial"
    assert "unvalidated_clickbait_source" in record_for(db, KOPFHOERER).rejection_reasons


def test_video_generation_provider_and_latency_are_unchanged(db, monkeypatch):
    from clipforge.main import start_generation_job_route
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: None)
    config = settings(**KEY)
    assert service._FLIGHT.acquire(blocking=False)  # a curation round is "running"
    try:
        job = start_generation_job_route(ProjectCreate(prompt="Warum ist der Himmel blau?", options=AdvancedOptions(research="off")), db, config)
    finally:
        service._FLIGHT.release()
    assert db.get(GenerationJob, job["id"]).status == "queued"
    assert config.clipforge_ai_mode == "local"  # the key enables topic curation only, not the director
    root = Path(__file__).resolve().parents[1] / "clipforge"
    for module in ("main.py", "generation.py", "services.py", "pipeline.py", "ai.py"):
        tree = ast.parse((root / module).read_text())
        imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not any("semantic" in name or "topic_intelligence" == name.split(".")[-1] for name in imported if module != "main.py"), module


def test_diagnostics_show_the_curation_breakdown(db, monkeypatch):
    run_pool(db, [QR, CARPLAY], curator=real_curator(), monkeypatch=monkeypatch, kind="video")
    report = service.discovery_status(db, settings(**KEY), now=NOW)
    assert report["current_score_version"] == scoring.SCORE_VERSION
    assert report["config"]["semantic_curator_version"] == semantic.SEMANTIC_CURATOR_VERSION
    pool = report["pool"]
    assert pool["semantic_validation"]["status"] == "ok" and pool["semantic_validation"]["curated"] == 2
    assert pool["evaluation"]["ai_requests"] == 1 and pool["evaluation"]["ai_request_budget"] == 3
    rows = {row["question"]: row for row in pool["accepted_candidates"] + pool["rejected_candidates"]}
    assert rows[CARPLAY]["semantic"]["dimensions"]["universal_12plus_relevance"] == 0.2
    assert "semantic_not_universal" in rows[CARPLAY]["reasons"]
