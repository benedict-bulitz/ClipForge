"""Why-relation entailment and the single weak-core retry (Real-Mac follow-up).

* Berlin: "Beim Bau der Mauer 1961 zog die DDR die Sperranlagen ... Ebertstraße,
  sodass das Gelände ... abgeschnitten wurde" answered "Warum wurde die Mauer
  gebaut?" - a result of building it, not its purpose.
* Post-argument: "Trotzdem kann man fiese Streite umgehen: indem man ..." -
  advice, not why answers come late.
* Microwave: the audit command showed a weak snippet core with no retry - the
  retry lived only inside generation, the audit called research directly.

Unit tests use other topics; the fixtures mirror the Mac evidence shapes.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
from types import SimpleNamespace

from clipforge.pipeline import build_initial_state, production_research
from clipforge.research import ResearchResult
from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.schemas import AdvancedOptions
from research_semantic_fixtures import AI_IMAGE, AI_PAIN, ARGUMENT, BERLIN, KAUGUMMI, MARS, MICROWAVE, Case
from research_v2_support import research_settings

BRIDGE = question_frame("Warum wurde die Brücke gebaut?")


def _run(case):
    return run_research(case.question, "de", research_settings(brave_search_api_key="test-key"), context={"question": case.question},
                        transport=case.web().transport(), llm=None, cache_root=None)


# 1-3: purpose vs consequence ---------------------------------------------------

def test_1_built_causing_y_does_not_answer_why_built():
    assert BRIDGE.relation == "purpose"
    issues = core_issues(BRIDGE, "Die Brücke wurde 1890 über den Fluss gebaut, sodass die Fähre ihren Betrieb einstellen musste.")
    assert "consequence_not_purpose" in issues


def test_2_built_to_prevent_y_answers_why_built():
    assert not core_issues(BRIDGE, "Die Brücke wurde 1890 gebaut, um die gefährliche Überfahrt mit der Fähre bei Hochwasser zu ersetzen.")
    assert not core_issues(BRIDGE, "Die Brücke wurde gebaut, weil die Stadt nach dem Hochwasser eine sichere Verbindung zum Hafen brauchte.")
    english = question_frame("Why was the bridge built?", "en")
    assert english.relation == "purpose"
    assert not core_issues(english, "The bridge was built in 1890 to replace the dangerous ferry crossing during floods.")


def test_3_consequence_clauses_cannot_substitute_for_purpose():
    for text in (
        "Nach dem Bau der Brücke wuchs die Stadt schnell, wodurch neue Viertel am Ufer entstanden.",
        "Die Brücke wurde aus Stahl gebaut und infolgedessen mehrmals neu gestrichen.",
    ):
        assert {"consequence_not_purpose", "no_purpose_or_reason"} & set(core_issues(BRIDGE, text)), text
        assert {"consequence_not_purpose", "no_purpose_or_reason"} & set(mechanism_issues(BRIDGE, text, "Brücke")), text
    # A phenomenon is not an action: its "deshalb" effect clause may be the explanation.
    microwave = question_frame(MICROWAVE.question)
    assert microwave.relation == "cause"
    assert not core_issues(microwave, MICROWAVE.pages[1][2][0])


# 4-6: advice vs explanation -------------------------------------------------------

def test_4_generic_advice_cannot_answer_a_phenomenon_why():
    frame = question_frame("Warum bekommen wir im Winter öfter Schnupfen?")
    for text in (
        "Im Winter kann man Schnupfen vermeiden, indem man sich regelmäßig die Hände wäscht.",
        "Gegen Schnupfen im Winter solltest du viel trinken, weil die Schleimhäute dann feucht bleiben.",
        "Am besten schützt man sich im Winter vor Schnupfen, indem man überfüllte Räume meidet.",
    ):
        assert "advice_not_explanation" in core_issues(frame, text), text
        assert "advice_not_explanation" in mechanism_issues(frame, text, "Winter Schnupfen"), text


def test_5_a_process_explanation_with_indem_still_passes():
    frame = question_frame("Wie funktioniert das Gedächtnis?")
    assert frame.relation == "mechanism"
    assert not core_issues(frame, "Das Gedächtnis funktioniert, indem das Gehirn die Verbindungen zwischen Nervenzellen bei jeder Wiederholung verstärkt.")
    why = question_frame("Warum schwimmt Eis auf Wasser?")
    assert not core_issues(why, "Eis schwimmt auf Wasser, weil sich Wassermoleküle beim Gefrieren so anordnen, dass Eis weniger dicht ist.")


def test_6_delayed_response_question_rejects_conflict_prevention_advice():
    frame = question_frame(ARGUMENT.question)
    advice = ARGUMENT.pages[0][2][2]
    assert "advice_not_explanation" in core_issues(frame, advice)
    run = _run(ARGUMENT)
    assert run.package["status"] == "insufficient" and run.package["core_answer"] is None and run.facts == []


# 7-8: Berlin ------------------------------------------------------------------------

def test_7_berlin_ebertstrasse_sentence_is_rejected():
    frame = question_frame(BERLIN.question)
    ebertstrasse = BERLIN.pages[0][2][0]
    assert "consequence_not_purpose" in core_issues(frame, ebertstrasse)
    only = Case(BERLIN.question, BERLIN.keyword, pages=[BERLIN.pages[0]])
    run = _run(only)
    assert run.package["core_answer"] is None and run.package["status"] in {"insufficient", "missing_mechanism"}


def test_8_berlin_escape_prevention_purpose_is_accepted():
    run = _run(BERLIN)
    core = run.package["core_answer"]
    assert core and "um den Flüchtlingsstrom" in core["text"] and "Ebertstraße" not in core["text"]
    assert run.package["answer_grounding"]["relation"] == "purpose"


# 9-11: the single weak-core retry, production and audit ----------------------------------

WEAK = {"status": "partial", "core_answer": {"key": "c", "text": "x", "evidence_ids": ["ev_01"], "basis": "snippet", "authority_tier": 3}}
STRONG = {"status": "sufficient", "core_answer": {"key": "c", "text": "y", "evidence_ids": ["ev_02"], "basis": "full_text", "authority_tier": 0}}


def _fake_research(packages: list[dict]) -> tuple[list[dict], object]:
    calls: list[dict] = []
    fact = {"claim": "Mikrowellen dringen nur wenige Zentimeter tief in das Essen ein, deshalb wird zuerst der Rand warm.",
            "confidence": 0.7, "importance": 0.9, "sources": [{"label": "s", "url": "https://s.test"}], "verification": "source_snippet"}

    def research(query, *_a, context=None, **_k):
        calls.append({"query": query, **dict(context or {})})
        package = packages[min(len(calls), len(packages)) - 1]
        return ResearchResult([dict(fact)], [{"label": "s", "url": "https://s.test"}], "verified_sources", "research_v2", None, package, {})

    return calls, research


def test_9_weak_snippet_core_triggers_exactly_one_strengthening_retry(monkeypatch, tmp_path):
    calls, research = _fake_research([WEAK, STRONG])
    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    state = build_initial_state(MICROWAVE.question, AdvancedOptions(), research_settings(render_root=tmp_path))
    assert [call.get("focus") for call in calls] == [None, "strengthen"]
    assert state["research"]["retry"]["replaced"] and state["research"]["package"]["core_answer"]["basis"] == "full_text"


def _load_audit():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research_v2_audit.py"
    spec = importlib.util.spec_from_file_location("research_v2_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_10_audit_script_exercises_the_production_retry_path(monkeypatch, tmp_path):
    production_calls, research = _fake_research([WEAK, WEAK])
    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    settings = research_settings(render_root=tmp_path)
    _result, report = production_research(MICROWAVE.question, settings)
    production = [dict(call) for call in production_calls]
    production_calls.clear()
    audit = _load_audit()
    monkeypatch.setattr(audit, "get_settings", lambda: settings)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        audit.print_v2(MICROWAVE.question, "de")
    assert production_calls == production  # same queries, contexts and retry as generation
    text = output.getvalue()
    assert "RETRY        attempted=yes reason=weak_core_source" in text and "replaced_original=no" in text
    assert report == {**report, "attempted": True, "replaced": False}


def test_11_retry_stays_bounded_to_one(monkeypatch, tmp_path):
    # Weak first, nothing stronger, and the script still is not ready: no further retry.
    calls, research = _fake_research([WEAK, {"status": "insufficient", "core_answer": None}])
    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    state = build_initial_state(MICROWAVE.question, AdvancedOptions(), research_settings(render_root=tmp_path))
    assert len(calls) == 2 and len(state["research"]["attempts"]) == 2
    # A strong first package spends nothing.
    calls, research = _fake_research([STRONG])
    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    _result, report = production_research(MICROWAVE.question, research_settings(render_root=tmp_path))
    assert len(calls) == 1 and report["attempted"] is False and report["reason"] == "core_answer_not_weak"


# 12: preserved behaviour --------------------------------------------------------------------

def test_12_mars_ai_pain_ai_image_kaugummi_stay_green():
    mars = _run(MARS)
    assert mars.package["core_answer"] and "Staub" in mars.package["core_answer"]["text"]
    assert _run(AI_PAIN).package["status"] == "insufficient"
    assert _run(AI_IMAGE).package["status"] == "insufficient"
    kaugummi = _run(KAUGUMMI)
    assert kaugummi.package["status"] == "missing_mechanism" and "no_mechanism_evidence" in kaugummi.package["gaps"]
