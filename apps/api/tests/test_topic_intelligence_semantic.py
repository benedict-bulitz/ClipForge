"""Topic Intelligence: semantic question validation (semantic-validator-v1, ti-score-v4).

Real Mac review of tq3/ti-score-v3: only 2/9 suggestions passed - deterministic
rules checked form, not meaning.
"""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from test_topic_intelligence_local_questions import StaticSource, raw, static_deps
from topic_support import NOW, FakeLLM, FakeValidator, bad, good_assessment, settings

from clipforge.models import GenerationJob, TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import scoring, semantic, service, transform

KEY = {"openai_api_key": "sk-test"}  # validation needs a key; the director AI mode stays local

QR = "Scannt ein selbstgemalter QR-Code?"
SMARTWATCH = "Was weiß deine Smartwatch wirklich über dein biologisches Alter?"
KOPFHOERER_TITLE = "Du hörst Musik FALSCH: Warum auch die besten Kopfhörer Nachhilfe brauchen"
KOPFHOERER = "Warum brauchen auch die besten Kopfhörer Nachhilfe?"
PSEUDO = "Sind wir nur noch Pseudofreunde?"
CARPLAY = "Kriegen wir Apple CarPlay beim virtuellen Cockpit installiert?"
TALG = "Was kann Rindertalg-Creme wirklich?"
SCHWINDEL = "Warum wird einem schwindelig, wenn man schnell aufsteht?"

JUDGEMENTS = {
    KOPFHOERER: bad(self_contained_clarity=3, clear_factual_payoff=3, natural_spoken_german=5,
                    issues=["unexplained_metaphor", "unclear_payoff"], reason="Metapher ohne Erklärung"),
    PSEUDO: bad(clear_factual_payoff=3, knowledge_short_fit=3, issues=["rhetorical_or_opinion", "unclear_payoff"]),
    CARPLAY: bad(universal_12plus_relevance=2, prior_knowledge_free=3, issues=["too_narrow_audience", "niche_context_required"]),
    TALG: bad(universal_12plus_relevance=4, prior_knowledge_free=3, issues=["niche_context_required"]),
}


def run_pool(db, titles, *, validator=None, monkeypatch=None, config=None, now=NOW, count=9):
    if validator is not None:
        monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", validator)
    source = StaticSource([raw(title, trend=0.8 - index * 0.005) for index, title in enumerate(titles)])
    result = service.suggestions(db, config or settings(**KEY), static_deps(source), count=count, now=now)
    return result, source


def record_for(db, question: str) -> TopicCandidateRecord:
    return next(record for record in db.scalars(select(TopicCandidateRecord)).all() if record.question == question)


# --- The real Mac questions ---------------------------------------------------------------


def test_real_mac_questions_are_judged_on_meaning(db, monkeypatch):
    validator = FakeValidator(JUDGEMENTS)
    result, _ = run_pool(db, [QR, SMARTWATCH, KOPFHOERER_TITLE, PSEUDO, CARPLAY, TALG, SCHWINDEL], validator=validator, monkeypatch=monkeypatch)
    served = {item["question"] for item in result["candidates"]}
    assert served == {QR, SMARTWATCH, SCHWINDEL}
    kopf = record_for(db, KOPFHOERER).rejection_reasons
    assert {"semantic_unexplained_metaphor", "semantic_not_self_contained", "semantic_unclear_payoff"} <= set(kopf)
    assert {"semantic_rhetorical_or_opinion", "semantic_unclear_payoff"} <= set(record_for(db, PSEUDO).rejection_reasons)
    assert {"semantic_too_narrow_audience", "semantic_not_universal"} <= set(record_for(db, CARPLAY).rejection_reasons)
    assert {"semantic_niche_context_required", "semantic_prior_knowledge"} <= set(record_for(db, TALG).rejection_reasons)


def test_qr_and_smartwatch_pass_every_dimension(db, monkeypatch):
    run_pool(db, [QR, SMARTWATCH], validator=FakeValidator(), monkeypatch=monkeypatch)
    for question in (QR, SMARTWATCH):
        record = record_for(db, question)
        assert not record.rejection_reasons
        info = record.score_breakdown["quality"]["semantic"]
        assert info["status"] == "validated" and info["validator_version"] == semantic.SEMANTIC_VALIDATOR_VERSION
        assert set(info["dimensions"]) == set(semantic.DIMENSIONS)
        assert min(info["dimensions"].values()) >= scoring.SEMANTIC_DIMENSION_MIN


def test_strong_universal_factual_question_passes(db, monkeypatch):
    result, _ = run_pool(db, [SCHWINDEL], validator=FakeValidator(), monkeypatch=monkeypatch, count=1)
    assert [item["question"] for item in result["candidates"]] == [SCHWINDEL]
    assert record_for(db, SCHWINDEL).score_version == "ti-score-v4"


def test_a_single_weak_dimension_rejects_even_without_issue_codes(db, monkeypatch):
    run_pool(db, [SCHWINDEL], validator=FakeValidator({SCHWINDEL: bad(natural_spoken_german=5)}), monkeypatch=monkeypatch)
    assert record_for(db, SCHWINDEL).rejection_reasons == ["semantic_unnatural_german"]


def test_obscure_source_passes_after_universal_reframing(db, monkeypatch):
    llm = FakeLLM({"GICON-Höhenwindturm": good_assessment(
        "Kann ein Windrad in großer Höhe deutlich mehr Strom erzeugen?", "technik", broad_appeal=8, accessibility=9)})
    monkeypatch.setattr(transform, "TRANSFORM_CLIENT_FACTORY", llm)
    validator = FakeValidator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", validator)
    source = StaticSource([raw("GICON-Höhenwindturm", kind="article", source="wikipedia_pageviews",
                               description="Windkraftanlage, die stärkeren Wind in großer Höhe nutzen soll")])
    config = settings(clipforge_ai_mode="openai", **KEY)
    result = service.suggestions(db, config, static_deps(source), count=1, now=NOW)
    assert [item["question"] for item in result["candidates"]] == ["Kann ein Windrad in großer Höhe deutlich mehr Strom erzeugen?"]
    assert validator.requests == [["Kann ein Windrad in großer Höhe deutlich mehr Strom erzeugen?"]]  # the question, not the source


def test_validator_sees_only_the_final_question(db, monkeypatch):
    validator = FakeValidator(JUDGEMENTS)
    run_pool(db, [KOPFHOERER_TITLE], validator=validator, monkeypatch=monkeypatch)
    assert validator.requests == [[KOPFHOERER]]  # no source headline, no article context


# --- Budget, batching, cache ---------------------------------------------------------------

MANY = [f"Warum {verb} {noun} im Winter?" for noun in ("Katzen", "Hunde", "Vögel", "Bäume", "Fische", "Bienen", "Pferde", "Kühe")
        for verb in ("frieren", "schlafen", "zittern", "wachsen", "schwimmen", "summen", "grasen", "wandern", "ruhen")]


def test_batch_validation_stays_within_the_ai_budget(db, monkeypatch):
    validator = FakeValidator(default=bad(clear_factual_payoff=2))  # nothing passes: worst case for requests
    run_pool(db, MANY, validator=validator, monkeypatch=monkeypatch)
    assert len(validator.requests) <= service.AI_REQUEST_BUDGET == 3
    assert all(len(batch) <= semantic.MAX_VALIDATION_BATCH == 20 for batch in validator.requests)
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    runs = db.scalars(select(TopicDiscoveryRun)).all()
    assert sum(service.ai_requests_in(item) for item in runs) <= service.AI_REQUEST_BUDGET
    assert service.semantic_report(run)["detail"]["validator_version"] == semantic.SEMANTIC_VALIDATOR_VERSION


def test_cached_validation_avoids_repeat_ai_calls(db, monkeypatch):
    validator = FakeValidator()
    run_pool(db, [QR, SMARTWATCH, SCHWINDEL], validator=validator, monkeypatch=monkeypatch, count=3)
    assert len(validator.requests) == 1
    # A new pool an hour later (pool expired) re-evaluates the same questions: all from cache.
    for record in db.scalars(select(TopicCandidateRecord)).all():
        record.status = "pooled"
    db.commit()
    run_pool(db, [QR, SMARTWATCH, SCHWINDEL], validator=validator, monkeypatch=monkeypatch, count=3, now=NOW + timedelta(hours=1))
    assert len(validator.requests) == 1
    assert record_for(db, QR).score_breakdown["quality"]["semantic"]["status"] == "cached"


def test_semantic_rejection_triggers_raw_pool_backfill_without_provider_calls(db, monkeypatch):
    rejected = MANY[:20]
    later = [SCHWINDEL, "Warum können wir uns selbst nicht kitzeln?", "Warum schmeckt Essen im Flugzeug anders?"]
    validator = FakeValidator({question: bad(universal_12plus_relevance=3) for question in rejected})
    titles = rejected + later
    result, source = run_pool(db, titles, validator=validator, monkeypatch=monkeypatch, count=3)
    assert source.calls == 1  # the existing raw pool is reused
    assert {item["question"] for item in result["candidates"]} == set(later)
    assert len(validator.requests) >= 2  # the first batch was rejected, evaluation continued


# --- Local mode and failures ------------------------------------------------------------------


def test_without_a_validator_local_mode_shows_fewer_stricter_suggestions(db):
    titles = [QR, SMARTWATCH, KOPFHOERER_TITLE, PSEUDO, CARPLAY, TALG, SCHWINDEL, "Warum können wir uns selbst nicht kitzeln?"]
    result, _ = run_pool(db, titles, config=settings())  # no key
    served = {item["question"] for item in result["candidates"]}
    assert served == {SCHWINDEL, "Warum können wir uns selbst nicht kitzeln?"}
    assert result["status"] == "partial"
    assert "unvalidated_weak_question_form" in record_for(db, PSEUDO).rejection_reasons
    assert "unvalidated_clickbait_source" in record_for(db, KOPFHOERER).rejection_reasons
    assert record_for(db, SCHWINDEL).score_breakdown["quality"]["semantic"]["status"] == "unavailable"
    assert service.semantic_report(db.scalar(select(TopicDiscoveryRun)))["status"] == "unavailable"


def test_validator_failure_falls_back_to_strict_local_rules_not_weaker_ones(db, monkeypatch):
    result, _ = run_pool(db, [PSEUDO, TALG, SCHWINDEL], validator=FakeValidator(fail=True), monkeypatch=monkeypatch)
    assert {item["question"] for item in result["candidates"]} == {SCHWINDEL}
    assert record_for(db, SCHWINDEL).score_breakdown["quality"]["semantic"]["status"] == "failed"
    assert service.semantic_report(db.scalar(select(TopicDiscoveryRun)))["status"] == "failed"


def test_clickbait_extracted_question_is_never_accepted_without_semantic_validation(db):
    run_pool(db, [KOPFHOERER_TITLE], config=settings())
    assert "unvalidated_clickbait_source" in record_for(db, KOPFHOERER).rejection_reasons


def test_pool_is_rebuilt_when_validation_becomes_available(db, monkeypatch):
    run_pool(db, [SCHWINDEL], config=settings())
    runs_before = db.scalar(select(func.count()).select_from(TopicDiscoveryRun))
    run_pool(db, [SCHWINDEL], validator=FakeValidator(), monkeypatch=monkeypatch, now=NOW + timedelta(minutes=2))
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) > runs_before


# --- Generation independence --------------------------------------------------------------------


def test_video_generation_never_waits_for_semantic_validation(db, monkeypatch):
    from clipforge.main import start_generation_job_route
    from clipforge.schemas import AdvancedOptions, ProjectCreate

    monkeypatch.setattr("clipforge.main.schedule_next_generation", lambda _settings: None)
    assert service._FLIGHT.acquire(blocking=False)  # a discovery/validation round is "running"
    try:
        job = start_generation_job_route(ProjectCreate(prompt="Warum ist der Himmel blau?", options=AdvancedOptions(research="off")), db, settings(**KEY))
    finally:
        service._FLIGHT.release()
    assert db.get(GenerationJob, job["id"]).status == "queued"
    root = Path(__file__).resolve().parents[1] / "clipforge"
    for module in ("main.py", "generation.py", "services.py", "pipeline.py"):
        tree = ast.parse((root / module).read_text())
        imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not any("semantic" in name for name in imported), module


# --- Diagnostics -------------------------------------------------------------------------------


def test_diagnostics_show_the_semantic_breakdown(db, monkeypatch):
    run_pool(db, [QR, CARPLAY], validator=FakeValidator(JUDGEMENTS), monkeypatch=monkeypatch)
    report = service.discovery_status(db, settings(**KEY), now=NOW)
    assert report["current_score_version"] == "ti-score-v4"
    assert report["config"]["semantic_validation"] == "enabled"
    assert report["config"]["semantic_validator_version"] == semantic.SEMANTIC_VALIDATOR_VERSION
    pool = report["pool"]
    assert pool["semantic_validation"]["status"] == "ok"
    rows = {row["question"]: row for row in pool["accepted_candidates"] + pool["rejected_candidates"]}
    carplay = rows[CARPLAY]["semantic"]
    assert carplay["status"] == "validated" and carplay["dimensions"]["universal_12plus_relevance"] == 0.2
    assert "too_narrow_audience" in carplay["issues"]
    assert "semantic_not_universal" in rows[CARPLAY]["reasons"]
