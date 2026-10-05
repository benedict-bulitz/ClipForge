"""Core-answer selection: relation direction and the question's distinguishing condition.

Real Mac after f3268fe:
* Berlin: "Im Gegenteil: Weil der Mauerbau Freunde und Verwandte in Berlin voneinander
  getrennt hatte, versuchten ... im Berliner Umland noch viele Menschen ... zu fliehen."
  won over "Gebaut wurde sie 1961, um den Flüchtlingsstrom ... zu stoppen." - the
  modifier "Berliner" (of "Berliner Umland") satisfied the entity "Berliner Mauer".
* Microwave: "Mikrowellen erwärmen Lebensmittel mithilfe von ... Wellen ..." won: the
  three-entity quota let an answer skip "Mitte", the condition actually asked about.

Unit and end-to-end tests use other topics; the fixtures mirror the Mac shapes.
"""
from __future__ import annotations

from types import SimpleNamespace

from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import core_issues, question_frame
from clipforge.schemas import AdvancedOptions
from research_semantic_fixtures import ARGUMENT, BERLIN, MARS, MICROWAVE, Case
from research_v2_support import research_settings

DAM = question_frame("Warum wurde der Staudamm gebaut?")
PURPOSE = "Errichtet wurde er 1955, um das Tal vor den jährlichen Frühjahrshochwassern zu schützen."
CONSEQUENCE = (
    "Weil der Staudammbau das alte Dorf überflutet hatte, zogen viele Familien in die neue Siedlung "
    "unterhalb vom Staudamm."
)


def _run(case):
    return run_research(case.question, "de", research_settings(brave_search_api_key="test-key"), context={"question": case.question},
                        transport=case.web().transport(), llm=None, cache_root=None)


def test_1_consequence_after_the_action_cannot_answer_why_it_happened():
    issues = core_issues(DAM, CONSEQUENCE)
    assert {"asked_action_is_the_cause", "asked_thing_is_the_cause_not_the_effect"} & set(issues)
    # Even when the effect clause names the asked thing again.
    again = "Weil der Staudammbau das Dorf überflutet hatte, protestierten viele Familien gegen den Staudamm."
    assert "asked_action_is_the_cause" in core_issues(DAM, again)


def test_2_direct_purpose_beats_consequence():
    dam = Case(DAM.question, "Staudamm", pages=[
        # The consequence comes from the higher-authority page ...
        ("https://www.wasserwirtschaft.bund.de/staudamm", "Der Staudamm", [CONSEQUENCE], {}),
        # ... the direct purpose from a specialist page whose pronoun refers to its title.
        ("https://www.technik-geschichte.de/staudamm", "Der Staudamm im Tal", [PURPOSE], {}),
    ])
    run = _run(dam)
    assert not core_issues(DAM, PURPOSE, "Der Staudamm im Tal")
    core = run.package["core_answer"]
    assert core and "um das Tal" in core["text"] and "Familien" not in core["text"]


def test_3_berlin_correct_purpose_is_selected():
    run = _run(BERLIN)
    frame = question_frame(BERLIN.question)
    consequence = BERLIN.pages[1][2][1]
    assert "Berliner Umland" in consequence and core_issues(frame, consequence)
    assert "um den Flüchtlingsstrom" in run.package["core_answer"]["text"]


def test_4_parent_topic_mechanism_cannot_answer_a_more_specific_phenomenon():
    frame = question_frame("Warum friert ein See im Winter zuerst am Ufer zu?")
    generic = "Ein See friert im Winter zu, weil Wasser bei null Grad gefriert und dabei Wärme an die kalte Luft abgibt."
    assert "misses_distinguishing_condition" in core_issues(frame, generic)
    specific = "Ein See friert im Winter am Ufer zuerst zu, weil das flache Wasser dort schneller auskühlt als in der Tiefe."
    assert not core_issues(frame, specific)


def test_5_generic_how_microwaves_heat_is_rejected_as_core():
    frame = question_frame(MICROWAVE.question)
    generic = MICROWAVE.pages[0][2][0]
    assert "misses_distinguishing_condition" in core_issues(frame, generic)
    only_generic = Case(MICROWAVE.question, MICROWAVE.keyword, pages=[MICROWAVE.pages[0]])
    run = _run(only_generic)
    core = run.package["core_answer"]
    assert core is None or "Wassermoleküle" not in core["text"]


def test_6_centre_or_cold_spot_explanation_is_accepted():
    frame = question_frame(MICROWAVE.question)
    for text in (
        "Essen bleibt in der Mitte kalt, weil die Mikrowellen nur die äußeren Zentimeter direkt erwärmen.",
        "In der Mikrowelle entstehen kalte Stellen im Essen, weil sich die Wellen an manchen Orten gegenseitig auslöschen.",
    ):
        assert not core_issues(frame, text), text
    assert "Zentimeter" in _run(MICROWAVE).package["core_answer"]["text"]


def test_7_specificity_does_not_require_identical_wording():
    frame = question_frame(MICROWAVE.question)
    # "innen" / "Kern" / "äußere Schichten" express the asked position without the word "Mitte".
    for text in (
        "Das Essen in der Mikrowelle bleibt innen kühl, weil die Wellen nur wenige Zentimeter tief eindringen.",
        "Der Kern des Essens in der Mikrowelle wird später warm, weil Wärme nur langsam von außen nach innen wandert.",
        "Mikrowellen erwärmen im Essen zuerst die äußeren Schichten, weil sie nur wenige Zentimeter tief eindringen.",
    ):
        assert not core_issues(frame, text), text
    # A coordinated last entity is no distinguishing modifier ("Hunde und Mäuse").
    assert not core_issues(question_frame("Warum vertragen sich Katzen, Hunde und Mäuse oft nicht?"),
                           "Katzen und Hunde vertragen sich oft nicht, weil sie die Körpersprache des anderen falsch deuten.")


def test_8_post_argument_remains_fail_closed():
    run = _run(ARGUMENT)
    assert run.package["status"] == "insufficient" and run.package["core_answer"] is None and run.facts == []


def test_9_mars_remains_correct():
    core = _run(MARS).package["core_answer"]
    assert core and "Staub" in core["text"] and "roter Planet" not in core["text"]


def test_10_retry_still_exactly_one(monkeypatch, tmp_path):
    calls: list[dict] = []
    weak = {"status": "partial", "core_answer": {"key": "c", "text": "x", "evidence_ids": ["e"], "basis": "snippet", "authority_tier": 3}}
    fact = {"claim": "Mikrowellen dringen nur wenige Zentimeter tief in das Essen ein, deshalb wird zuerst der Rand warm.",
            "confidence": 0.7, "importance": 0.9, "sources": [{"label": "s", "url": "https://s.test"}], "verification": "source_snippet"}

    def research(query, *_a, context=None, **_k):
        calls.append(dict(context or {}))
        return ResearchResult([dict(fact)], [{"label": "s", "url": "https://s.test"}], "verified_sources", "research_v2", None, weak, {})

    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    state = build_initial_state(MICROWAVE.question, AdvancedOptions(), research_settings(render_root=tmp_path))
    assert [call.get("focus") for call in calls] == [None, "strengthen"]
    assert state["research"]["retry"]["attempted"] and not state["research"]["retry"]["replaced"]
