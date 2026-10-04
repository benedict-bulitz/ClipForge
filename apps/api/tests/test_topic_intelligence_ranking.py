"""Topic Intelligence ti-score-v2: mass-audience ranking quality."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from topic_support import (
    NOW,
    FakeCurator,
    FakeWiki,
    curated,
    deps,
    settings,
    spike,
)

from clipforge.database import Base
from clipforge.models import TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import scoring, semantic, service
from clipforge.topic_intelligence.candidate import (
    SIGNAL_NAMES,
    Signal,
    TopicCandidate,
    candidate_id_for,
)
from clipforge.topic_intelligence.routes import diagnostics_route
from clipforge.topic_intelligence.signals import curiosity as curiosity_signal
from clipforge.topic_intelligence.signals import payoff as payoff_signal

OBSCURE = [
    {"title": "GICON-Höhenwindturm", "views": 40_000, "description": "Windkraftanlage in Brandenburg",
     "extract": "Der GICON-Höhenwindturm ist eine Windkraftanlage in Brandenburg.", "history": spike(150, 9_000)},
    {"title": "29. September", "views": 38_000, "description": "Tag im Gregorianischen Kalender",
     "extract": "Der 29. September ist der 272. Tag des gregorianischen Kalenders.", "history": spike(400, 12_000)},
    {"title": "Gol-Transportes-Aéreos-Flug 1907", "views": 36_000, "description": "Flugunfall in Brasilien 2006",
     "extract": "Gol-Transportes-Aéreos-Flug 1907 war ein Linienflug, der 2006 kollidierte.", "history": spike(200, 8_000)},
]
BROAD = [
    {"title": "Schluckauf", "views": 12_000, "description": "Unwillkürliche Kontraktion des Zwerchfells",
     "extract": "Schluckauf ist eine wiederholte, unwillkürliche Kontraktion des Zwerchfells.", "history": spike(2_000, 4_000)},
    {"title": "Polarlicht", "views": 30_000, "description": "Leuchterscheinung am Nachthimmel",
     "extract": "Ein Polarlicht ist eine Leuchterscheinung durch angeregte Atome.", "history": spike(3_000, 9_000)},
]


def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def served(db, articles, *, config=None, count=3, **kwargs):
    return service.suggestions(db, config or settings(), deps(wiki=FakeWiki(articles), **kwargs), count=count, now=NOW)


def records(db) -> dict[str, TopicCandidateRecord]:
    return {record.topic: record for record in db.scalars(select(TopicCandidateRecord)).all()}


# --- The reported failure, reproduced end to end -------------------------------------


def test_broad_compelling_topic_beats_obscure_wikipedia_spikes(db):
    result = served(db, OBSCURE + BROAD)
    questions = [item["question"] for item in result["candidates"]]
    assert set(questions[:2]) == {"Wie entsteht eigentlich ein Polarlicht?", "Warum bekommen wir Schluckauf?"}
    by_topic = records(db)
    weak = {"below_quality_floor", "question_no_question_transformation", "requires_prior_knowledge"}
    for topic in ("GICON-Höhenwindturm", "Gol-Transportes-Aéreos-Flug 1907"):
        assert weak & set(by_topic[topic].rejection_reasons), topic
    assert "29. September" not in by_topic  # a calendar page is filtered out before evaluation
    for topic in ("GICON-Höhenwindturm", "Gol-Transportes-Aéreos-Flug 1907"):
        # Each still had the biggest raw spike: it is the ranking, not discovery, that changed.
        assert by_topic[topic].signals["trend"]["value"] == 1.0
    assert by_topic["Schluckauf"].signals["trend"]["value"] < 0.5


def test_date_page_spike_alone_is_insufficient(db):
    result = served(db, [OBSCURE[1]])
    # A calendar page is filtered out before evaluation (never a generic wrapper, never an AI call) ...
    assert "29. September" not in records(db) and result["candidates"] == []
    # ... and a date question with the biggest spike still fails the gates.
    dated = score(candidate(
        "date", question="Was geschah am 29. September?", topic="29. September",
        trend=Signal(1.0, "high", sources=["wikipedia_pageviews"]), **GOOD,
    ))
    assert dated.rejected
    assert {"requires_prior_knowledge"} <= set(dated.rejection_reasons)
    assert dated.score_breakdown["components"]["trend"]["trend_quality_factor"] < 0.5


def test_quality_floor_prevents_filler_and_broadens_discovery_once(db):
    result = served(db, OBSCURE)
    assert result["candidates"] == []  # no least-bad filler
    assert result["status"] == "exhausted"
    runs = db.scalars(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence)).all()
    # Every raw topic was already evaluated: no broadening pass (it could only repeat cached data), never a loop.
    assert len(runs) == 1 and not service.was_broadened(runs[0])
    assert service.pool_summary(db, runs[0])["evaluation"]["remaining_raw_groups"] == 0


def test_curator_reframes_obscure_topics_into_broad_questions_and_they_may_win(db, monkeypatch):
    curator = FakeCurator({
        "Gol-Transportes-Aéreos-Flug 1907": curated(
            "Wie kann es sein, dass zwei Flugzeuge mitten in der Luft zusammenstoßen?", "technik", curiosity_gap=9),
        "Schluckauf": curated("Warum bekommen wir Schluckauf?", "koerper_gesundheit"),
    })
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    served(db, OBSCURE + BROAD, config=settings(openai_api_key="sk-test"))
    by_topic = records(db)
    flight = by_topic["Gol-Transportes-Aéreos-Flug 1907"]
    assert not flight.rejection_reasons  # the reframed question needs no identifier
    assert flight.provenance["mechanism"] == "paradox"
    assert "identifier" not in flight.score_breakdown["penalties"]["obscurity"]
    # A calendar page is obvious garbage: it never reaches the curator.
    assert "29. September" not in by_topic
    assert all(topic["topic"] != "29. September" for request in curator.requests for topic in request)
    assert not by_topic["Schluckauf"].rejection_reasons


# --- Scoring-level properties ----------------------------------------------------------


def candidate(key: str, *, question: str = "Warum passiert das?", topic: str = "Thema", niche: str = "wissenschaft", **signals: Signal) -> TopicCandidate:
    base = {name: Signal.unavailable("not_measured") for name in SIGNAL_NAMES}
    features, evidence = service.quality_signals(question, topic, "", niche, {}, "template")
    base["accessibility"] = features["accessibility"]
    base["question_form"] = features["question_form"]
    # Like build_candidate without a curator: curiosity/payoff from the question's mechanism (low confidence).
    base["curiosity"] = curiosity_signal(evidence["mechanism"], None)
    base["payoff"] = payoff_signal(evidence["mechanism"], None)
    base.update(signals)
    return TopicCandidate(
        candidate_id=candidate_id_for(key), topic=topic, question=question, rationale="", source_signals=[], discovered_at=NOW,
        language="de", region="DE", niche=niche, signals=base, freshness_at=NOW, provenance={},
    )


def score(item: TopicCandidate) -> TopicCandidate:
    weights, version = scoring.resolve_weights(settings())
    return scoring.score_candidate(item, weights=weights, version=version, now=NOW)


GOOD = {"suitability": Signal(0.8, "medium"), "broad_appeal": Signal(0.85, "medium"), "visual": Signal(0.8, "medium")}


def test_obscure_topic_needs_reframing_even_with_exceptional_evidence():
    evidence = {
        "trend": Signal(0.95, "high", sources=["wikipedia_pageviews", "brave_news_de", "youtube_trending_de"]),
        "outlier": Signal(0.9, "high"),
        **GOOD,
    }
    named = score(candidate("named", question="Warum sank die MS Estonia 1994 so schnell?", topic="MS Estonia 1994", **evidence))
    reframed = score(candidate("reframed", question="Wie kann ein großes Schiff innerhalb einer Stunde sinken?", topic="MS Estonia 1994", **evidence))
    middling = score(candidate(
        "middling", question="Warum ist Wasser nass?", topic="Wasser",
        trend=Signal(0.3, "medium", sources=["wikipedia_pageviews"]), suitability=Signal(0.6, "medium"),
        broad_appeal=Signal(0.7, "medium"),
    ))
    assert "requires_prior_knowledge" in named.rejection_reasons  # evidence does not lift the 12+ gate
    assert "names_obscure_entity" in named.score_breakdown["quality"]["prior_knowledge"]
    assert not reframed.rejected
    assert set(reframed.score_breakdown["quality"]["exceptional_evidence"]) >= {"corroborated_strong_trend", "strong_outlier"}
    assert reframed.final_score > middling.final_score


def test_multi_source_corroboration_improves_trend_rank_and_confidence():
    single = score(candidate("a", trend=Signal(0.8, "low", sources=["wikipedia_pageviews"]), **GOOD))
    corroborated_signal = Signal(0.8, "medium", sources=["brave_news_de", "wikipedia_pageviews"])
    corroborated = score(candidate("a", trend=corroborated_signal, **GOOD))
    assert corroborated.final_score > single.final_score
    assert corroborated.score_breakdown["trend_quality"]["source_factor"] == 1.0
    assert single.score_breakdown["trend_quality"]["source_factor"] == scoring.TREND_SINGLE_WIKIPEDIA
    order = ["unavailable", "low", "medium", "high"]
    assert order.index(corroborated.confidence) >= order.index(single.confidence)


def test_generic_wrapper_loses_to_a_concrete_question_on_the_same_topic():
    trend = Signal(0.7, "medium", sources=["wikipedia_pageviews"])
    generic = score(candidate("g", question="Was steckt eigentlich hinter Polarlicht?", topic="Polarlicht", trend=trend, **GOOD))
    concrete = score(candidate("c", question="Warum leuchten Polarlichter manchmal grün und manchmal rot?", topic="Polarlicht", trend=trend, **GOOD))
    assert concrete.final_score - generic.final_score > 0.1
    assert generic.score_breakdown["penalties"]["obscurity"] == {"generic_wrapper": scoring.OBSCURITY_PENALTIES["generic_wrapper"]}
    assert generic.score_breakdown["components"]["question_form"]["value"] == 0.25


def test_mass_audience_quality_outweighs_a_bigger_spike():
    # v7: question quality is curiosity + payoff (was: suitability, now weight 0).
    niche_spike = score(candidate(
        "spike", question="Wie funktioniert ein Höhenwindturm?", topic="Höhenwindturm", niche="technik",
        trend=Signal(1.0, "high", sources=["wikipedia_pageviews"]), suitability=Signal(0.55, "low"), broad_appeal=Signal(0.5, "medium"),
        curiosity=Signal(0.55, "low"), payoff=Signal(0.55, "low"),
    ))
    broad = score(candidate(
        "broad", question="Warum fühlen wir uns nach einem Mittagsschlaf manchmal schlechter?", topic="Schlafträgheit",
        niche="koerper_gesundheit", trend=Signal(0.4, "medium", sources=["wikipedia_pageviews"]),
        suitability=Signal(0.85, "medium"), broad_appeal=Signal(0.9, "medium"),
        curiosity=Signal(0.85, "medium"), payoff=Signal(0.85, "medium"),
    ))
    assert broad.final_score > niche_spike.final_score
    weights = scoring.DEFAULT_WEIGHTS
    worth_watching = ("curiosity", "payoff", "short_worthiness", "knowledge_value")
    assert sum(weights[name] for name in worth_watching) > 3 * (weights["trend"] + weights["demand"])


def test_missing_own_analytics_and_quality_data_stay_neutral():
    item = score(candidate("n", **GOOD))
    own = item.score_breakdown["components"]["own_performance"]
    assert own["confidence"] == "unavailable" and own["effective"] == 0.5
    nothing = TopicCandidate(
        candidate_id="tc_x", topic="x", question="", rationale="", source_signals=[], discovered_at=NOW, language="de",
        region="DE", niche="unknown", signals={}, freshness_at=NOW, provenance={},
    )
    scored = score(nothing)
    assert scored.score_breakdown["quality"]["mass_audience"] is None
    assert "below_quality_floor" not in scored.rejection_reasons  # unknown quality is not bad quality


def test_diversity_prefers_another_subject_among_near_equals_only():
    items = [
        scoring.RankedItem("a", 0.70, "weltraum", "why"),
        scoring.RankedItem("b", 0.69, "weltraum", "why"),
        scoring.RankedItem("c", 0.68, "koerper_gesundheit", "how"),
        scoring.RankedItem("d", 0.50, "natur_tiere", "what_if"),
    ]
    order = [item.candidate_id for item in scoring.diversify(items)]
    assert order[:2] == ["a", "c"]  # c is within tolerance and adds a new subject
    assert order.index("d") == 3  # a much weaker candidate is never pulled up for variety
    shown = [("weltraum", "why"), ("koerper_gesundheit", "how")]
    assert scoring.diversify(items[:3], shown)[0].candidate_id == "a"  # nothing different within tolerance: quality wins


def test_top_three_suggestions_are_diverse(db):
    articles = [
        {"title": title, "views": 20_000, "description": description, "extract": f"{title}: {description}.", "history": spike(1_000, 3_000)}
        for title, description in [
            ("Gewitter", "Wetterereignis mit Blitz und Donner"),
            ("Hagel", "Niederschlag aus Eis"),
            ("Regenbogen", "Optische Erscheinung"),
            ("Schluckauf", "Unwillkürliche Kontraktion des Zwerchfells"),
        ]
    ]
    result = served(db, articles, count=3)
    by_id = {record.candidate_id: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    niches = [by_id[item["candidate_id"]].niche for item in result["candidates"]]
    assert "koerper_gesundheit" in niches  # not three weather questions in a row


def test_ranking_is_deterministic_across_runs():
    first, second = fresh_db(), fresh_db()
    served(first, OBSCURE + BROAD, count=6)
    served(second, OBSCURE + BROAD, count=6)
    run_a = first.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    run_b = second.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))
    assert run_a.ranked_candidate_ids == run_b.ranked_candidate_ids
    assert [first.get(TopicCandidateRecord, item).final_score for item in run_a.ranked_candidate_ids] == [
        second.get(TopicCandidateRecord, item).final_score for item in run_b.ranked_candidate_ids]


def test_novelty_and_outlier_logic_are_unchanged_by_v2(db):
    from clipforge.models import Project
    from clipforge.topic_intelligence.signals import outlier_vs_channel

    db.add(Project(original_prompt="Warum bekommen wir eigentlich Schluckauf?", title="Schluckauf"))
    db.commit()
    served(db, BROAD)
    assert "duplicate_of_previous_topic" in records(db)["Schluckauf"].rejection_reasons
    assert outlier_vs_channel(10_000, [1_000] * 10).value == 1.0
    assert outlier_vs_channel(200_000, [200_000] * 10).value == 0.0


# --- Versioning and diagnostics ----------------------------------------------------------


def test_a_pool_scored_by_another_version_is_not_reused(db):
    served(db, BROAD)
    run = db.scalar(select(TopicDiscoveryRun))
    run.score_version = "ti-score-v1"
    db.commit()
    served(db, BROAD)
    versions = [item.score_version for item in db.scalars(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence)).all()]
    assert versions[-1] == scoring.SCORE_VERSION and len(versions) >= 2


def test_diagnostics_show_components_penalties_and_can_rescore_v1_records(db):
    served(db, OBSCURE + BROAD, count=3)
    record = records(db)["GICON-Höhenwindturm"]
    record.topic = "29. September"
    # Simulate a candidate persisted by ti-score-v1 (no v2 feature signals, v1 score).
    record.signals = {name: value for name, value in record.signals.items() if name not in {"broad_appeal", "accessibility", "question_form"}}
    record.question = "Was steckt eigentlich hinter 29. September?"
    record.provenance = {**record.provenance, "question_issues": []}
    record.score_version, record.final_score, record.rejection_reasons = "ti-score-v1", 0.69, []
    db.commit()
    report = service.diagnostics(db, settings(), limit=20, rescore=True)
    assert report["current_score_version"] == scoring.SCORE_VERSION
    row = next(item for item in report["candidates"] if item["topic"] == "29. September")
    assert row["persisted"]["score_version"] == "ti-score-v1" and row["persisted"]["final_score"] == 0.69
    assert row["rescored"]["score_version"] == scoring.SCORE_VERSION
    assert {"below_quality_floor", "requires_prior_knowledge"} & set(row["rescored"]["rejection_reasons"])
    assert row["rescored"]["penalties"]["obscurity"]["date_page"] > 0
    assert row["rescored"]["quality"]["universal_accessibility"] < 0.5
    assert set(row["rescored"]["components"]) == set(SIGNAL_NAMES)
    shown = service.diagnostics(db, settings(), limit=9, shown=True)
    assert [item["status"] for item in shown["candidates"]] and all(item["served_at"] for item in shown["candidates"])


def test_diagnostics_endpoint_is_developer_only(db):
    assert diagnostics_route(db, settings(), limit=5)["current_score_version"] == scoring.SCORE_VERSION
    with pytest.raises(HTTPException) as error:
        diagnostics_route(db, settings(environment="production"), limit=5)
    assert error.value.status_code == 404
