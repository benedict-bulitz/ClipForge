"""Semantic answer grounding: the core answer must answer the question, not share its topic.

Real-Mac regressions of Research V2 (deterministic synthesis, 0 LLM calls)
certified answers that only shared keywords with the question: the Mars
*planet's* colour for the Martian *sky*, a street the Berlin Wall ran along
for *why* it was built, AI *detecting* pain for AI *feeling* pain, reverse
image search for how generative AI works, conflict advice for a cognitive
why-question.  ``research_semantic_fixtures`` reproduces those evidence
shapes; the unit tests below use other topics so nothing is keyed to them.
"""
from __future__ import annotations

import pytest

from clipforge.readiness import content_readiness
from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.research_v2.synthesis import SynthesisOut
from research_semantic_fixtures import AI_IMAGE, AI_PAIN, ARGUMENT, BERLIN, CASES, KAUGUMMI, MARS, MICROWAVE
from research_v2_support import research_settings


def _run(case, *, llm=None):
    return run_research(case.question, "de", research_settings(brave_search_api_key="test-key"), context={"question": case.question},
                        transport=case.web().transport(), llm=llm, cache_root=None)


def _core_source(run) -> str | None:
    core = run.package["core_answer"]
    if not core:
        return None
    return next(source["url"] for source in run.package["source_summary"]["sources"] if source["id"] in core["source_ids"])


# ---------------------------------------------------------------------------
# A-E: question-relative eligibility (topics unrelated to the Mac fixtures)
# ---------------------------------------------------------------------------

def test_a_why_rejects_definition_and_restatement():
    frame = question_frame("Warum ist ein Regenbogen bunt?")
    assert "no_cause_or_mechanism" in core_issues(frame, "Ein Regenbogen ist ein optisches Phänomen am Himmel nach einem Regenschauer.")
    assert "restates_phenomenon" in core_issues(frame, "Ein Regenbogen ist bunt, weil ein Regenbogen eben bunt ist.")
    assert not core_issues(frame, "Ein Regenbogen ist bunt, weil Wassertropfen das Sonnenlicht brechen und in seine Farben zerlegen.")


def test_b_why_rejects_geographic_detail():
    frame = question_frame("Warum wurde der Eiffelturm gebaut?")
    assert "no_cause_or_mechanism" in core_issues(frame, "Der Eiffelturm steht auf dem Marsfeld in Paris am Ufer der Seine.")
    assert not core_issues(frame, "Der Eiffelturm wurde gebaut, um zur Weltausstellung 1889 die Ingenieurskunst Frankreichs zu zeigen.")


def test_c_can_x_y_rejects_x_detecting_y_in_someone_else():
    frame = question_frame("Kann ein Roboter Angst fühlen?")
    issues = core_issues(frame, "Ein Roboter erkennt Angst in den Gesichtern von Menschen anhand ihrer Mimik.")
    assert {"observer_relation", "predicate_mismatch"} & set(issues)
    # An answer about the asked capability itself - including "unknown" - is eligible.
    assert not core_issues(frame, "Ob ein Roboter Angst fühlen kann, ist unter Forschern umstritten und bisher nicht belegt.")


def test_d_mechanism_rejects_advice():
    frame = question_frame("Warum bekommt man nach dem Sport Muskelkater?")
    advice = "Gegen Muskelkater nach dem Sport hilft Wärme, weil sie die Durchblutung der Muskeln fördert."
    assert "advice_not_explanation" in mechanism_issues(frame, advice)
    assert "advice_not_explanation" in core_issues(frame, advice)
    run = _run(ARGUMENT)
    assert run.package["core_answer"] is None and not run.package["explanation_spine"]["why_it_happens"]


def test_e_same_keyword_overlap_cannot_become_core_answer():
    frame = question_frame("Warum ist das Meer salzig?")
    assert "naming_not_cause" in core_issues(frame, "Das Tote Meer wird Salzmeer genannt, weil sein Wasser besonders salzig schmeckt.")
    assert "entity_mismatch" in core_issues(frame, "Salzige Chips schmecken gut, weil Salz den Geschmack verstärkt.")
    for case in (MARS, AI_IMAGE, AI_PAIN):
        run = _run(case)
        rejected = {item["text"][:40] for item in run.package["answer_grounding"]["rejected_core_candidates"]}
        assert rejected, case.question
        core = run.package["core_answer"]
        assert core is None or core["text"][:40] not in rejected


def test_english_why_uses_the_same_contract():
    frame = question_frame("Why is the sky on Mars red?", "en")
    assert "naming_not_cause" in core_issues(frame, "Mars is called the Red Planet because it looks red in the night sky.")
    assert not core_issues(frame, "The sky on Mars looks red because fine dust in the thin atmosphere scatters sunlight.")


# ---------------------------------------------------------------------------
# F-K: the Real-Mac evidence shapes through the full deterministic pipeline
# ---------------------------------------------------------------------------

def test_f_stronger_authoritative_answer_beats_weak_snippet():
    run = _run(MICROWAVE)
    assert _core_source(run) == MICROWAVE.pages[0][0]
    weak = MICROWAVE.snippets[0][0]
    core = run.package["core_answer"]
    assert all(source["url"] != weak for source in run.package["source_summary"]["sources"] if source["id"] in core["source_ids"])
    # Topic words alone are no agreement: the snippet's different claim does not count as support.
    assert core["independent_sources"] == 1


@pytest.mark.parametrize("case", [AI_PAIN, ARGUMENT, AI_IMAGE], ids=["ai_pain", "argument", "ai_image"])
def test_g_missing_direct_answer_is_insufficient(case):
    run = _run(case)
    assert run.package["status"] == "insufficient" and run.package["core_answer"] is None
    assert run.facts == [] and "no_direct_answer" in run.package["gaps"]
    assert run.package["answer_grounding"]["core_answer"] == "missing"
    state = {"research": {"required": True, "package": run.package}, "script": {"blocks": []}}
    readiness = content_readiness(state)
    assert not readiness["ready"] and readiness["research_required"]


def test_h_kaugummi_stays_missing_mechanism():
    run = _run(KAUGUMMI)
    assert run.package["status"] == "missing_mechanism" and "no_mechanism_evidence" in run.package["gaps"]
    assert run.package["explanation_spine"]["status"] == "missing_mechanism"
    assert run.package["core_answer"] is None and run.facts  # the observation is kept for the existing retry
    state = {"research": {"required": True, "package": run.package}, "script": {"blocks": []}}
    assert content_readiness(state)["research_required"]


def test_i_mars_dust_and_scattering_is_accepted():
    run = _run(MARS)
    assert _core_source(run) == MARS.pages[1][0]
    assert run.package["explanation_spine"]["status"] == "complete" and run.package["status"] != "insufficient"
    kinds = {item["kind"] for item in run.package["evidence"] if item["id"] in run.package["core_answer"]["evidence_ids"]}
    assert "mechanism" in kinds


def test_j_berlin_escape_reason_is_accepted():
    run = _run(BERLIN)
    assert _core_source(run) == BERLIN.pages[1][0]
    assert run.package["status"] == "sufficient"
    assert not core_issues(question_frame(BERLIN.question), run.package["core_answer"]["text"])


def test_k_microwave_mechanism_is_accepted_when_supported():
    run = _run(MICROWAVE)
    assert run.package["status"] == "sufficient" and run.package["explanation_spine"]["status"] == "complete"
    assert run.package["explanation_spine"]["why_it_happens"][0]["key"] == run.package["core_answer"]["key"]


class _Synth:
    """An LLM that answers fluently but off-question."""

    def __init__(self, text: str):
        self.text = text

    def decompose(self, _question, _language):
        return None

    def synthesize(self, payload):
        ids = [item["id"] for item in payload["evidence"]][:1]
        return SynthesisOut(core_answer={"text": self.text, "evidence_ids": ids})


def test_llm_synthesis_cannot_certify_an_off_question_core_answer():
    run = _run(AI_PAIN, llm=_Synth("Eine künstliche Intelligenz analysiert Biosignale, um Schmerz bei Patienten früher zu erkennen."))
    assert run.package["core_answer"] is None and run.package["status"] == "insufficient"
    assert any(str(item.get("reason", "")).startswith("core_answer_not_entailed") for item in run.package["rejected_claims"])


def test_every_fixture_is_bounded_and_deterministic():
    for case in CASES.values():
        first, second = _run(case), _run(case)
        assert first.package["status"] == second.package["status"]
        assert first.diagnostics["budget"]["used"]["llm_calls"] == 0
