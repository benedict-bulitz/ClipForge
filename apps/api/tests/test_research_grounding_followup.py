"""Real-Mac grounding follow-up: pronoun antecedents, page-purpose language, comparable numbers, weak cores.

* Berlin: the direct answer "Gebaut wurde sie 1961, um ..." names the wall only
  by pronoun and was rejected; a consequence of the wall ("Weil der Mauerbau
  ... trennte, versuchten ... zu fliehen") won instead.
* Post-argument: the article's own purpose ("warum es mir so wichtig ist, dir
  mit diesem Artikel ... Antworten zu liefern. Damit du ...") passed as a cause.
* Berlin: "3,5 Millionen flohen" vs "140 getötet" was recorded as a numeric
  contradiction - two different metrics.
* Microwave: a valid core answer rested on a snippet from an unknown source.

The unit tests use other topics; the fixtures mirror the Mac evidence shapes.
"""
from __future__ import annotations

from types import SimpleNamespace

from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.research_v2.corroboration import comparable_quantities, find_contradictions, group_claims
from clipforge.research_v2.evidence import EvidenceUnit, numbers_in
from clipforge.schemas import AdvancedOptions
from research_semantic_fixtures import AI_IMAGE, AI_PAIN, ARGUMENT, BERLIN, KAUGUMMI, MARS, MICROWAVE, Case
from research_v2_support import research_settings

TOWER = question_frame("Warum wurde der Leuchtturm gebaut?")
TOWER_ANSWER = "Errichtet wurde er 1850, damit Schiffe die gefährliche Sandbank vor der Küste bei Nacht sicher umfahren konnten."


def _run(case):
    return run_research(case.question, "de", research_settings(brave_search_api_key="test-key"), context={"question": case.question},
                        transport=case.web().transport(), llm=None, cache_root=None)


def _unit(uid: str, source: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(id=uid, text=text, excerpt=text, source_id=source, sub_question="q_core", kinds=["number"],
                        relevance=0.8, matched=[], numbers=sorted(numbers_in(text)))


# 1-2: pronoun antecedents ----------------------------------------------------

def test_1_pronoun_inherits_a_safely_established_entity():
    # From the previous sentence ...
    assert not core_issues(TOWER, TOWER_ANSWER, "Der Leuchtturm steht seit über 170 Jahren auf der kleinen Insel.")
    # ... or, for a paragraph-initial sentence, from the page title.
    assert not core_issues(TOWER, TOWER_ANSWER, "Der Leuchtturm von Westerhever")
    # The Mac shape end to end: the pronoun answer wins and carries its referent downstream.
    core = _run(BERLIN).package["core_answer"]
    assert "Gebaut wurde sie" in core["text"] and core["text"].startswith("Die Berliner Mauer")


def test_2_unrelated_pronoun_does_not_inherit_the_entity():
    # The previous sentence is about something else, although it mentions the tower.
    assert "entity_mismatch" in core_issues(TOWER, TOWER_ANSWER, "Der Kapitän sah den Leuchtturm schon von weitem.")
    # A title that is not about the entity, and a sentence without any pronoun.
    assert "entity_mismatch" in core_issues(TOWER, TOWER_ANSWER, "Schiffsunglücke an der Nordsee")
    assert "entity_mismatch" in core_issues(TOWER, "Errichtet wurde 1850 eine Mole, damit Schiffe sicher anlegen konnten.",
                                            "Der Leuchtturm von Westerhever")


# 3-4: page-purpose language ---------------------------------------------------

def test_3_article_purpose_cannot_answer_a_phenomenon_why():
    frame = question_frame("Warum ist Gähnen ansteckend?")
    text = "In diesem Artikel zeigen wir dir, warum Gähnen ansteckend ist, weil es uns wichtig ist, dir verlässliche Antworten zu geben."
    assert "author_or_article_purpose" in core_issues(frame, text)
    run = _run(ARGUMENT)
    assert run.package["status"] == "insufficient" and run.package["core_answer"] is None and run.facts == []
    reasons = {issue for item in run.package["answer_grounding"]["rejected_core_candidates"] for issue in item.get("issues") or []}
    assert "author_or_article_purpose" in reasons


def test_4_advice_purpose_cannot_be_a_mechanism():
    frame = question_frame("Warum wird man nach dem Mittagessen müde?")
    text = "Iss mittags weniger Nudeln, damit du nach dem Mittagessen nicht müde wirst, weil schwere Mahlzeiten den Kreislauf belasten."
    assert "author_or_article_purpose" in mechanism_issues(frame, text, "Mittagessen müde")


# 5-6: numeric comparability ----------------------------------------------------

def test_5_different_metrics_are_not_contradictions():
    fled = _unit("ev_01", "src_01", "Über die Grenze flohen bis 1961 rund 3,5 Millionen Menschen aus der DDR.")
    killed = _unit("ev_02", "src_02", "An der Grenze wurden mindestens 140 Menschen aus der DDR getötet.")
    assert comparable_quantities(fled, killed, frozenset({"mauer"})) is None
    assert _run(BERLIN).package["contradictions"] == []


def test_6_the_same_metric_can_still_conflict():
    low = _unit("ev_01", "src_01", "Bis 1961 flohen rund 2,6 Millionen Menschen aus der DDR in den Westen.")
    high = _unit("ev_02", "src_02", "Bis 1961 flohen etwa 3,5 Millionen Menschen aus der DDR in den Westen.")
    assert comparable_quantities(low, high, frozenset({"mauer"})) == (2.6e6, 3.5e6)
    clusters = {"src_01": {"cluster": "src_01"}, "src_02": {"cluster": "src_02"}}
    groups = group_claims([low, high], clusters, frozenset({"mauer"}))
    sources = {"src_01": {"authority": "medium"}, "src_02": {"authority": "medium"}}
    records = find_contradictions(groups, sources, frozenset({"mauer"}))
    assert records and records[0]["type"] == "numeric"


# 7-12: the Mac questions ---------------------------------------------------------

def test_7_berlin_direct_escape_prevention_answer_wins():
    run = _run(BERLIN)
    frame = question_frame(BERLIN.question)
    consequence = BERLIN.pages[1][2][1]
    assert "asked_thing_is_the_cause_not_the_effect" in core_issues(frame, consequence)
    assert "Mauerbau Freunde" not in run.package["core_answer"]["text"]
    assert run.package["status"] == "sufficient"


def test_8_post_argument_without_cognitive_explanation_is_insufficient():
    run = _run(ARGUMENT)
    assert run.package["status"] == "insufficient" and "no_direct_answer" in run.package["gaps"]


def test_9_to_12_previous_behaviour_is_preserved():
    mars = _run(MARS)
    assert mars.package["core_answer"] and "Staub" in mars.package["core_answer"]["text"]
    for case in (AI_PAIN, AI_IMAGE):
        assert _run(case).package["status"] == "insufficient"
    kaugummi = _run(KAUGUMMI)
    assert kaugummi.package["status"] == "missing_mechanism" and "no_mechanism_evidence" in kaugummi.package["gaps"]


# 13: weak core sources -------------------------------------------------------------

WEAK_MICROWAVE = Case(MICROWAVE.question, MICROWAVE.keyword, pages=[], snippets=MICROWAVE.snippets)


def test_13_weak_snippet_core_is_reported_truthfully():
    run = _run(WEAK_MICROWAVE)
    assert run.package["core_answer"]["basis"] == "snippet"
    assert run.package["status"] == "partial" and run.package["confidence"] == "low"
    assert "core_answer_snippet_only" in run.package["gaps"]


def _package(basis: str, tier: int) -> dict:
    return {"status": "partial" if basis == "snippet" else "sufficient",
            "core_answer": {"key": "claim_01", "text": "x", "evidence_ids": ["ev_01"], "basis": basis, "authority_tier": tier}}


def _wire_weak(monkeypatch, second_package: dict) -> list[dict]:
    calls: list[dict] = []
    fact = {"claim": "Mikrowellen dringen nur wenige Zentimeter tief in das Essen ein, deshalb wird zuerst der Rand warm.",
            "confidence": 0.7, "importance": 0.9, "sources": [{"label": "s", "url": "https://s.test"}], "verification": "source_snippet"}

    def research(query, *_a, context=None, **_k):
        calls.append(dict(context or {}))
        package = _package("snippet", 2) if len(calls) == 1 else second_package
        return ResearchResult([dict(fact)], [{"label": "s", "url": "https://s.test"}], "verified_sources", "research_v2", None, package, {})

    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    return calls


def test_13_weak_core_spends_the_single_retry_and_keeps_a_stronger_answer(monkeypatch, tmp_path):
    calls = _wire_weak(monkeypatch, _package("full_text", 0))
    state = build_initial_state(MICROWAVE.question, AdvancedOptions(), research_settings(render_root=tmp_path))
    assert [call.get("focus") for call in calls] == [None, "strengthen"]
    retry = state["research"]["retry"]
    assert retry["attempted"] and retry["reason"] == "weak_core_source" and retry["focus"] == "strengthen" and retry["replaced"]
    assert state["research"]["package"]["core_answer"]["basis"] == "full_text"


def test_13_no_stronger_source_keeps_the_partial_answer_and_no_second_retry(monkeypatch, tmp_path):
    calls = _wire_weak(monkeypatch, {"status": "insufficient", "core_answer": None})
    state = build_initial_state(MICROWAVE.question, AdvancedOptions(), research_settings(render_root=tmp_path))
    assert len(calls) == 2  # exactly one retry in total, whatever readiness says afterwards
    assert state["research"]["retry"]["replaced"] is False
    assert state["research"]["package"]["core_answer"]["basis"] == "snippet"
