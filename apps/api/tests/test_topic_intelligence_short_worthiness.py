"""Topic Intelligence: short-worthiness (ti-score-v6, semantic-curator-v2).

Real Mac (68e03f9): 9 suggestions, all clear and broad - but only 3 strong shorts.  The
weak ones were generic advice ("Woran erkennt man ...?"), a survey measurement ("Wie
stark ist das Gerechtigkeitsempfinden in der Bevölkerung?") and a two-part list
question ("Welche Faktoren ..., und welchen Anteil hat ...?").  The fix is general:
curiosity strength, one specific reveal, a concrete premise, one core question.  No
domain is penalized; the ranked test questions are deliberately NOT the Mac nine.
"""
from __future__ import annotations

from sqlalchemy import select
from test_topic_intelligence_local_questions import StaticSource, raw, static_deps
from topic_support import NOW, FakeCurator, bad, settings

from clipforge.models import TopicCandidateRecord
from clipforge.topic_intelligence import scoring, semantic, service
from clipforge.topic_intelligence.signals import short_worthiness
from clipforge.topic_intelligence.text import short_shape_flags

KEY = {"openai_api_key": "sk-test"}

# Curator judgements (0-10).  STRONG: a concrete hook with a surprising, specific reveal.
STRONG = bad(curiosity_strength=9, payoff_specificity=9, reveal_potential=9, concreteness=9, visual_potential=8)
SOLID = bad()  # GOOD_JUDGEMENT: clear and broad, a decent short
# Useful but dry: fine content, weak default short.
DRY = bad(curiosity_strength=5, payoff_specificity=7, reveal_potential=4, concreteness=7, issues=["generic_advice"])
MULTI = bad(single_question_focus=3, payoff_specificity=4, issues=["multi_part_question", "list_answer"])
SURVEY = bad(curiosity_strength=4, reveal_potential=3, concreteness=3, issues=["abstract_or_survey"])
FLAT = bad(curiosity_strength=2, payoff_specificity=3, reveal_potential=2, concreteness=4, issues=["no_clear_reveal"])

KITZELN = "Warum kann man sich nicht selbst kitzeln?"
PASSWORT = "Woran erkennt man ein gutes Passwort?"
SCHLAF_MULTI = "Welche Faktoren beeinflussen den Schlaf, und was hilft am meisten?"
MUSKELKATER = "Warum bekommt man Muskelkater erst einen Tag nach dem Sport?"
ZUFRIEDEN = "Wie groß ist die Zufriedenheit mit der Arbeit in der Bevölkerung?"
GLAS = "Warum zerspringt heißes Glas in kaltem Wasser?"
BRUECKE = "Wie weit kann eine Brücke ohne Pfeiler gespannt werden?"
HERZ = "Warum kann ein großer Schreck das Herz aus dem Takt bringen?"
ERNAEHRUNG = "Welche Rolle spielt Ernährung für die Gesundheit?"
TELESKOP = "Wie werden neue Satelliten eigentlich gebaut?"


def pool(db, monkeypatch, judged: dict[str, dict], *, trends: dict[str, float] | None = None, extra=(), config=None):
    """Serve ``judged`` questions (curator judgement per question) through the real pipeline."""
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator(by_question=judged))
    trends = trends or {}
    topics = [raw(question, trend=trends.get(question, 0.6)) for question in judged]
    topics += list(extra)
    result = service.suggestions(db, config or settings(**KEY), static_deps(StaticSource(topics)), count=9, now=NOW)
    return [item["question"] for item in result["candidates"]]


def record(db, question: str) -> TopicCandidateRecord:
    return next(item for item in db.scalars(select(TopicCandidateRecord)).all() if item.question == question)


# --- The general property, on questions that are not the Mac nine ---------------------------------


def test_concrete_surprising_question_beats_generic_advice(db, monkeypatch):
    served = pool(db, monkeypatch, {PASSWORT: DRY, KITZELN: STRONG}, trends={PASSWORT: 0.9, KITZELN: 0.5})
    # V2: useful advice with low curiosity (5/10) is not "ranked slightly lower" - it is not a suggestion at all.
    assert served == [KITZELN]
    advice = record(db, PASSWORT)
    assert "low_curiosity" in advice.rejection_reasons
    assert advice.score_breakdown["quality"]["short_worthiness"]["penalties"] == {"issue_generic_advice": 0.12}


def test_one_payoff_question_beats_a_multi_part_question(db, monkeypatch):
    served = pool(db, monkeypatch, {SCHLAF_MULTI: MULTI, MUSKELKATER: SOLID}, trends={SCHLAF_MULTI: 0.95})
    assert served == [MUSKELKATER]
    reasons = record(db, SCHLAF_MULTI).rejection_reasons
    assert {"multi_part_question", "list_answer_question", "semantic_multi_part_question", "semantic_list_answer"} <= set(reasons)


def test_mechanism_question_beats_an_abstract_survey_question(db, monkeypatch):
    served = pool(db, monkeypatch, {ZUFRIEDEN: SURVEY, GLAS: SOLID}, trends={ZUFRIEDEN: 0.95})
    assert served == [GLAS]
    survey = record(db, ZUFRIEDEN)
    assert {"semantic_abstract_or_survey", "abstract_or_survey_question"} <= set(survey.rejection_reasons)
    assert "shape_abstract_measure" in survey.score_breakdown["quality"]["short_worthiness"]["penalties"]


def test_a_survey_measurement_is_rejected_even_when_the_curator_misses_it(db, monkeypatch):
    served = pool(db, monkeypatch, {ZUFRIEDEN: SOLID, GLAS: SOLID}, trends={ZUFRIEDEN: 0.95})
    assert served == [GLAS]
    assert "abstract_or_survey_question" in record(db, ZUFRIEDEN).rejection_reasons


def test_generic_factor_list_loses_to_a_concrete_causal_question(db, monkeypatch):
    # Even when the curator does not flag it, the "Welche Faktoren/Gründe ..." shape is a list answer.
    factors = "Welche Gründe gibt es für Kopfschmerzen?"
    served = pool(db, monkeypatch, {factors: SOLID, MUSKELKATER: SOLID}, trends={factors: 0.95, MUSKELKATER: 0.4})
    assert served == [MUSKELKATER]
    assert "list_answer_question" in record(db, factors).rejection_reasons


def test_useful_but_dry_guidance_does_not_outrank_a_high_curiosity_topic(db, monkeypatch):
    dry = "Wie lässt sich die eigene Reaktionszeit messen?"
    served = pool(db, monkeypatch, {dry: DRY, BRUECKE: STRONG}, trends={dry: 0.95, BRUECKE: 0.45})
    # Despite twice the demand: V2 removes "technically valid but not worth watching" (curiosity 5/10).
    assert served == [BRUECKE]
    assert record(db, dry).rejection_reasons == ["low_curiosity"]
    assert record(db, dry).final_score < record(db, BRUECKE).final_score


def test_a_strong_health_topic_can_still_win(db, monkeypatch):
    # No domain penalty: a health question with a concrete, surprising reveal ranks first.
    served = pool(
        db, monkeypatch, {HERZ: {**STRONG, "niche": "koerper_gesundheit"}, ERNAEHRUNG: bad(curiosity_strength=4, reveal_potential=3, issues=["broad_overview"]), TELESKOP: SOLID},
    )
    assert served[0] == HERZ
    assert record(db, HERZ).niche == "koerper_gesundheit"
    assert record(db, ERNAEHRUNG).final_score < record(db, TELESKOP).final_score


def test_trend_helps_a_strong_short_but_cannot_rescue_a_weak_concept(db, monkeypatch):
    rising = "Warum wird Eis bei Frost auf der Straße so glatt?"
    quiet = "Warum knistert Laub im Herbst unter den Füßen?"
    flat = "Was ist das Wetter?"
    # A flat concept with the strongest, corroborated demand (two sources) ...
    corroborated = [raw(flat, trend=0.98, source="brave_news_de", kind="news")]
    served = pool(
        db, monkeypatch, {rising: STRONG, quiet: STRONG, flat: FLAT},
        trends={rising: 0.95, quiet: 0.35, flat: 0.98}, extra=corroborated,
    )
    # ... trend decides between two strong shorts,
    assert served[:2] == [rising, quiet]
    # ... but never lifts the weak one: rejected whatever the demand, exceptional evidence or not.
    weak = record(db, flat)
    assert "weak_short_concept" in weak.rejection_reasons and flat not in served
    assert weak.score_breakdown["trend_quality"]["short_gate"] < 0.5
    assert record(db, rising).score_breakdown["trend_quality"]["short_gate"] == 1.0


def test_twelve_plus_accessibility_and_grounding_are_not_traded_for_short_worthiness(db, monkeypatch):
    niche = "Warum überhitzt der neue Prozessor im Gaming-Laptop so schnell?"
    invented = "Warum explodieren Handyakkus bei Kälte?"
    served = pool(db, monkeypatch, {
        niche: {**STRONG, "prior_knowledge_free": 3, "universal_12plus_relevance": 4, "issues": ["niche_context_required"]},
        invented: {**STRONG, "grounded": False},
        GLAS: SOLID,
    })
    assert served == [GLAS]
    assert {"semantic_prior_knowledge", "semantic_not_universal"} <= set(record(db, niche).rejection_reasons)
    assert "semantic_unsupported_premise" in record(db, invented).rejection_reasons
    assert scoring.PRIOR_KNOWLEDGE_GATE == 0.5 and scoring.SEMANTIC_DIMENSION_MIN == 0.6


# --- Without a curator: the question's shape still counts ---------------------------------------------


def test_local_mode_rejects_multi_part_and_list_questions_and_ranks_advice_lower(db):
    multi = "Wie entstehen Blitze und wo schlagen sie ein?"
    factors = "Welche Ursachen hat Heuschnupfen?"
    advice = "Wie kann man die eigene Ausdauer verbessern?"
    topics = [raw(question, trend=0.7) for question in (multi, factors, advice, MUSKELKATER)]
    result = service.suggestions(db, settings(), static_deps(StaticSource(topics)), count=9, now=NOW)
    served = [item["question"] for item in result["candidates"]]
    assert multi not in served and factors not in served
    assert "multi_part_question" in record(db, multi).rejection_reasons
    assert "list_answer_question" in record(db, factors).rejection_reasons
    assert record(db, advice).final_score < record(db, MUSKELKATER).final_score


def test_question_shapes_are_general_not_topic_lists():
    assert short_shape_flags("Welche Faktoren beeinflussen die Herzgesundheit, und welchen Anteil hat die Ernährung?") == {"multi_part", "list_answer"}
    assert short_shape_flags("Wie stark ist das Gerechtigkeitsempfinden in der Bevölkerung?") == {"abstract_measure"}
    assert short_shape_flags("Woran erkennt man verlässliche Gesundheitsinformationen?") == {"advice"}
    for fine in (
        "Wie hoch kann ein Windrad gebaut werden?",
        "Wie können zwei Passagierflugzeuge während des Flugs zusammenstoßen?",
        "Welche Lebewesen können eigenes Licht erzeugen?",  # "Welche" alone is not a list shape
        "Warum ist der Himmel blau und nicht violett?",  # one question with a contrast
        "Was passiert, wenn man einen Tag nichts trinkt?",
        HERZ, MUSKELKATER,
    ):
        assert short_shape_flags(fine) == set(), fine


def test_short_worthiness_is_one_signal_and_only_scoring_decides():
    strong = short_worthiness("why", set(), semantic.curated_signal({**STRONG, "usable": True}, status="curated"))
    dry = short_worthiness("how", {"advice"}, semantic.curated_signal({**DRY, "usable": True}, status="curated"))
    assert strong.value > 0.8 and strong.confidence == "medium" and strong.evidence["basis"] == "curator"
    assert dry.value < strong.value and dry.evidence["penalties"] == {"issue_generic_advice": 0.12}  # counted once
    local = short_worthiness("why", set())
    assert local.confidence == "low" and local.evidence["basis"] == "question_shape"
    assert "short_worthiness" in scoring.DEFAULT_WEIGHTS and abs(sum(scoring.DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
    assert semantic.SEMANTIC_CURATOR_VERSION == "semantic-curator-v3"
