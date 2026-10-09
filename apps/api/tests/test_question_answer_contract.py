"""Behavioral QAC recovery tests; all evidence and model responses are fixtures."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_script_story_rewrite import (
    DRAFT_EN,
    EN_FACTS,
    EN_QUESTION,
    REWRITE_EN,
    FakeEditor,
    block,
    critic,
    fact,
    lift_context,
    rewrite,
    verdict,
)

from clipforge import pipeline
from clipforge.config import Settings
from clipforge.question_answer_contract import (
    AnswerObligation,
    ObligationCoverage,
    QuestionAnswerContract,
    ResearchCoverageReport,
    checked_coverage,
)
from clipforge.readiness import content_readiness, not_ready_message
from clipforge.research import ResearchResult
from clipforge.research_v2.synthesis import deterministic_sub_questions
from clipforge.schemas import AdvancedOptions
from clipforge.script_story_rewrite import (
    ContractObligationEvaluation,
    RewriteResponse,
    _checked_verdict,
    _verifier_findings,
    run_script_story_quality,
)


def contract(question=EN_QUESTION, description="Explain how wing shape and airflow produce lift"):
    return QuestionAnswerContract(
        question_type="causal", core_question=question,
        primary_answer_obligation=AnswerObligation(
            id="primary", description=description, is_primary=True, is_required=True,
        ),
        required_supporting_obligations=[AnswerObligation(
            id="context", description="Additional historical context", is_primary=False, is_required=False,
        )], optional_context=[],
    )


def coverage(ids=(), *, status="satisfied"):
    return ResearchCoverageReport(
        is_sufficient=status == "satisfied", missing_obligations=[] if status == "satisfied" else ["primary"],
        coverage=[ObligationCoverage(
            obligation_id="primary", status=status, supporting_fact_ids=list(ids), reasoning="Fixture evidence coverage",
        ), ObligationCoverage(obligation_id="context", status="missing", reasoning="Optional context absent")],
    )


def qac_context():
    context = lift_context("en")
    context.update(question_answer_contract=contract().model_dump(), research_coverage=coverage(["lift_02", "lift_03", "lift_04"]).model_dump())
    return context


def evaluated(status="satisfied", *, primary=True, required=True, beats=REWRITE_EN, **kwargs):
    return verdict(beats, contract_sufficient=kwargs.pop("contract_sufficient", status == "satisfied"), contract_evaluations=[
        ContractObligationEvaluation(id="primary", status=status, is_primary=primary, is_required=required, reasoning="The mechanism is omitted: use the pressure difference."),
        ContractObligationEvaluation(id="context", status="missing", is_primary=False, is_required=False, reasoning="Optional context absent"),
    ], **kwargs)


@pytest.mark.parametrize("status", ["missing", "partially_satisfied", "circular", "unsupported", "insufficient_depth"])
@pytest.mark.parametrize("primary", [True, False])
def test_every_required_failure_blocks_even_generic_answered(status, primary):
    response = evaluated(status, primary=primary, contract_sufficient=True)
    findings = _verifier_findings(response)
    assert any(item["severity"] == "hard" for item in findings)
    if status in {"missing", "partially_satisfied"}:
        assert ("primary_answer_missing" if primary else "required_obligation_missing") in {item["code"] for item in findings}


@pytest.mark.parametrize("status", ["missing", "partially_satisfied"])
def test_optional_missing_or_partial_does_not_block(status):
    response = evaluated(status, primary=False, required=False)
    # The overall flag must agree with the required evaluations.
    response = response.model_copy(update={"contract_sufficient": True})
    assert not _verifier_findings(response)


def test_required_satisfied_and_optional_missing_passes():
    assert not _verifier_findings(evaluated())


def test_contract_flags_and_missing_evaluation_are_authoritative():
    response = evaluated("partially_satisfied", primary=False, required=False)
    checked = _checked_verdict(response, qac_context())
    assert "primary_answer_missing" in {item["code"] for item in _verifier_findings(checked)}
    checked = _checked_verdict(verdict(contract_sufficient=True), qac_context())
    assert "primary_answer_missing" in {item["code"] for item in _verifier_findings(checked)}


def test_supported_omission_gets_dedicated_repair_with_exact_evidence():
    omitted = [
        (role, "The wings are curved and tilted slightly up, bending the passing air downward." if "pressure above" in text else text,
         [identifier for identifier in ids if identifier != "lift_03"])
        for role, text, ids in REWRITE_EN
    ]
    editor = FakeEditor(rewrites=[rewrite(omitted), rewrite(REWRITE_EN)], verdicts=[evaluated("partially_satisfied", beats=omitted), evaluated()])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert editor.stages() == ["critique", "rewrite", "verify", "rewrite", "verify"]
    request = [brief for stage, brief in editor.calls if stage == "rewrite"][1]
    assert request["mode"] == "contract_repair"
    failure = request["failed_obligations"][0]
    assert failure["id"] == "primary" and failure["is_primary"] and failure["is_required"]
    assert failure["description"] == contract().primary_answer_obligation.description
    assert failure["supporting_fact_ids"] == ["lift_02", "lift_03", "lift_04"]
    assert failure["supporting_facts"] == [{"id": item["id"], "claim": item["claim"]} for item in EN_FACTS[1:4]]
    assert failure["verifier_reason"] == "The mechanism is omitted: use the pressure difference."
    assert request["research"]["usable_dossier"] == EN_FACTS
    assert len(request["research"]["facts"]) == len(EN_FACTS)
    assert request["hook_intent"] and request["reveal_contract"] and request["style"]["word_budget"] == 120 and request["language"] == "en"
    assert report["gate"]["ready"] and report["holistic"]["failure_type"] == "SUPPORTED_BUT_OMITTED"
    assert content_readiness({**qac_context(), "script": {"blocks": final, "script_story_quality_v1": report}, "explanation_audit": report["holistic"]["explanation_audit"]})["ready"]


def test_final_fresh_regeneration_removes_all_failed_draft_wording():
    bad = [(role, text.replace("The engines push", "FAILED DRAFT WORDING The engines push"), ids) for role, text, ids in REWRITE_EN]
    editor = FakeEditor(rewrites=[rewrite(bad), rewrite(bad), rewrite(REWRITE_EN)], verdicts=[evaluated("missing", beats=bad), evaluated("circular", beats=bad), evaluated()])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert editor.stages().count("rewrite") == editor.stages().count("verify") == 3
    fresh = [brief for stage, brief in editor.calls if stage == "rewrite"][2]
    assert fresh["mode"] == "fresh_regeneration"
    assert not {"draft", "previous_rewrite", "critic", "verifier_findings", "deterministic_findings", "information_gain"} & fresh.keys()
    assert "FAILED DRAFT WORDING" not in json.dumps(fresh)
    assert fresh["required_supported_fact_ids"] == ["lift_02", "lift_03", "lift_04"]
    assert fresh["question_answer_contract"] and fresh["hook_intent"] and fresh["reveal_contract"]
    assert report["gate"]["ready"] and report["holistic"]["selected_attempt"] == 3
    assert "FAILED DRAFT WORDING" not in " ".join(item["text"] for item in final)


def test_all_three_contract_failures_stay_blocked_with_diagnostics():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)] * 3, verdicts=[evaluated("missing")] * 3)
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert not report["gate"]["ready"] and editor.stages().count("rewrite") == 3
    assert report["holistic"]["failure_type"] == "SUPPORTED_BUT_OMITTED"
    assert report["holistic"]["attempts"][-1]["contract_evaluations"][0]["reasoning"] == "The mechanism is omitted: use the pressure difference."
    readiness = content_readiness({**qac_context(), "script": {"blocks": final, "script_story_quality_v1": report}})
    assert not readiness["ready"]
    assert "primary" in json.dumps(readiness["blocking"])
    assert not_ready_message(readiness) == "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."


def test_rewriter_cannot_misclassify_supported_evidence_as_needs_research():
    editor = FakeEditor(rewrites=[RewriteResponse(status="needs_research", research_insufficiency="No mechanism"), rewrite(REWRITE_EN)], verdicts=[evaluated()])
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert report["gate"]["ready"] and not report["research_insufficient"]
    assert [brief for stage, brief in editor.calls if stage == "rewrite"][1]["failed_obligations"][0]["supporting_fact_ids"] == ["lift_02", "lift_03", "lift_04"]


@pytest.mark.parametrize("language", ["de", "en"])
def test_user_failure_hides_internal_terms(language):
    readiness = {"research_required": True, "blocking": [{"code": "primary_answer_missing", "message": "fact_06 contract_sufficient verifier failure required_obligation_missing rewrite failed hard requirements twice"}]}
    message = not_ready_message(readiness, language)
    assert message == (
        "ClipForge konnte für diese Frage noch keine ausreichend belegte Antwort erstellen. Bitte versuche es erneut."
        if language == "de" else "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."
    )
    assert readiness["blocking"][0]["message"].startswith("fact_06")


@pytest.mark.parametrize("explicit_query", [None, "custom search"])
def test_explicit_focus_independent_of_query_reaches_discovery(explicit_query):
    intent = {"language": "en", "content_type": "factual_explainer", "question_intent": {}}
    query, context = pipeline.research_request(EN_QUESTION, intent, contract(), explicit_query, "pressure difference across wing")
    assert context["focus"] == "pressure difference across wing"
    assert "pressure difference across wing" not in deterministic_sub_questions(EN_QUESTION, query, "en", focus=context["focus"])[0].query
    _, derived = pipeline.research_request(EN_QUESTION, intent, contract())
    assert contract().primary_answer_obligation.description in derived["focus"]
    assert "Additional historical context" not in derived["focus"]
    _, default = pipeline.research_request(EN_QUESTION, intent)
    assert default["focus"] is None


@pytest.mark.parametrize("invalid", ["unknown_fact", "unusable"])
def test_coverage_cannot_claim_satisfied_without_usable_evidence(invalid):
    facts = [fact("good", "The wing directs the passing airflow downwards.")]
    if invalid == "unusable":
        facts[0].update(id=invalid, confidence=0.1)
    report = checked_coverage(contract(), coverage([invalid]), facts)
    assert not report.is_sufficient and report.missing_obligations == ["primary"]
    assert report.coverage[0].status == "unsupported"


class PipelineEditor(FakeEditor):
    def __init__(self, *, failures=0):
        super().__init__()
        self.failures = failures
        self.rewrite_count = 0

    def rewrite(self, brief):
        self._call("rewrite", brief)
        self.rewrite_count += 1
        return rewrite([(role, text, [identifier.replace("lift_", "fact_") for identifier in ids]) for role, text, ids in REWRITE_EN])

    def verify(self, brief):
        self._call("verify", brief)
        beats = [(item["role"], item["text"], item["fact_ids"]) for item in brief["candidate"]["beats"]]
        return evaluated("missing" if self.rewrite_count <= self.failures else "satisfied", beats=beats)


def install_pipeline(monkeypatch, *, retry_finds=True, missing=False, failures=0):
    calls = []
    model = contract()
    monkeypatch.setattr(pipeline, "generate_contract", lambda *_a: model)
    monkeypatch.setattr(pipeline, "plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="local", error=None))

    def research(_query, *_a, context=None, **_k):
        calls.append(copy.deepcopy(context))
        # An incomplete first dossier only supplies generic context.
        facts = EN_FACTS[:1] if missing and (len(calls) == 1 or not retry_finds) else EN_FACTS
        return ResearchResult(copy.deepcopy(facts), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture")

    def evaluate(_contract, facts, _settings):
        ids = [item["id"] for item in facts if item["claim"] in {fact["claim"] for fact in EN_FACTS[1:4]}]
        return coverage(ids, status="satisfied" if len(ids) == 3 else "missing")

    monkeypatch.setattr(pipeline, "research_topic", research)
    monkeypatch.setattr(pipeline, "evaluate_research_coverage", evaluate)
    monkeypatch.setattr(pipeline, "_generate_body_with_v2_or_fallback", lambda *_a, **_k: (_a[5], {"status": "legacy_fallback"}))
    editor = PipelineEditor(failures=failures)
    state = pipeline.build_initial_state(
        EN_QUESTION, AdvancedOptions(language="en", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        script_quality_provider=editor,
    )
    return state, calls, editor


@pytest.mark.parametrize("failures", [1, 2])
def test_supported_recovery_continues_pipeline_without_research_retry(monkeypatch, failures):
    state, calls, editor = install_pipeline(monkeypatch, failures=failures)
    assert len(calls) == 1 and not state["research"].get("retry")
    assert state["script"]["readiness"]["ready"], state["script"]["readiness"]
    assert editor.rewrite_count == failures + 1
    repair = [brief for stage, brief in editor.calls if stage == "rewrite"][1]
    assert repair["failed_obligations"][0]["supporting_fact_ids"] == ["fact_02", "fact_03", "fact_04"]


@pytest.mark.parametrize("retry_finds", [True, False])
def test_missing_research_spends_one_semantic_retry_and_rechecks(monkeypatch, retry_finds):
    state, calls, editor = install_pipeline(monkeypatch, missing=True, retry_finds=retry_finds)
    assert len(calls) == 2 and len(state["research"]["attempts"]) == 2
    assert calls[1]["focus"] == "Missing required answer: " + contract().primary_answer_obligation.description
    assert state["research"]["retry"]["failure_type"] == "RESEARCH_MISSING"
    if retry_finds:
        assert state["research"]["status"] == "verified_sources"
        assert state["script"]["readiness"]["ready"], state["script"]["readiness"]
        assert editor.rewrite_count == 1
        assert state["contract_coverage"]["coverage"][0]["supporting_fact_ids"] == ["fact_02", "fact_03", "fact_04"]
    else:
        assert state["research"]["status"] == "needs_research" and not state["script"]["readiness"]["ready"]
        assert state["research"]["diagnostics"]["contract_failure_reason"].endswith("primary")
        assert "primary" not in state["research"]["error"]


REGRESSIONS = [
    (
        "Why is Mars red?", "Explain why the dust is red through supported iron oxidation",
        "Mars looks red because its surface is covered in red dust.",
        "Iron in the surface material reacted with oxygen, forming iron oxides that give the dust its reddish color.",
        "Wind spreads that oxidized dust across the planet, so a thin colored coating shapes what we see from afar.",
    ),
    (
        "Why was the Berlin Wall built?", "Explain the immediate motive of stopping emigration from East Germany",
        "The Berlin Wall was built during the Cold War when Germany was divided into rival political systems.",
        "East Germany built the wall to stop people leaving for West Berlin and prevent the loss of workers and skilled professionals.",
        "Closing that route trapped residents behind the border, turning the city's political division into a physical barrier.",
    ),
]


@pytest.mark.parametrize("question,description,bad,mechanism,payoff", REGRESSIONS, ids=["mars", "berlin"])
def test_topic_regression_repairs_related_information_into_sufficient_answer(question, description, bad, mechanism, payoff):
    facts = [fact("fact_01", bad), fact("fact_02", mechanism), fact("fact_03", payoff)]
    context = {
        "prompt": question, "intent": {"question": question, "language": "en", "research_required": True},
        "facts": facts, "word_budget": 120,
        "question_answer_contract": contract(question, description).model_dump(),
        "research_coverage": coverage(["fact_02"]).model_dump(),
    }
    # The bad rewrite uses related facts, but the independent verifier demands the mechanism/motive.
    bad_beats = [("answer", bad, ["fact_01"]), ("payoff", payoff, ["fact_03"])]
    good_beats = [("explanation", mechanism, ["fact_02"]), ("payoff", payoff, ["fact_03"])]
    draft = [block(index, role, text, ids) for index, (role, text, ids) in enumerate(bad_beats, 1)]
    editor = FakeEditor(critic_response=critic(), rewrites=[rewrite(bad_beats), rewrite(good_beats)], verdicts=[evaluated("insufficient_depth", beats=bad_beats), evaluated(beats=good_beats)])
    final, report = run_script_story_quality(draft, context, editor)
    assert report["gate"]["ready"], report["holistic"]
    assert mechanism in " ".join(item["text"] for item in final)
    assert editor.stages().count("rewrite") == 2
    assert report["holistic"]["attempts"][0]["hard"]
    assert [brief for stage, brief in editor.calls if stage == "rewrite"][1]["failed_obligations"][0]["supporting_fact_ids"] == ["fact_02"]


def test_exhausted_supported_script_recovery_never_spends_web_retry(monkeypatch):
    state, calls, editor = install_pipeline(monkeypatch, failures=3)
    assert len(calls) == 1 and editor.rewrite_count == 3
    assert not state["script"]["readiness"]["ready"]
    assert not state["research"].get("retry")
    assert state["script"]["script_story_quality_v1"]["holistic"]["failure_type"] == "SUPPORTED_BUT_OMITTED"


def test_contract_repair_quality_failure_still_gets_fresh_regeneration():
    editor = FakeEditor(
        rewrites=[rewrite(REWRITE_EN)] * 3,
        verdicts=[evaluated("missing"), evaluated(grounded=False), evaluated()],
    )
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert report["gate"]["ready"] and report["holistic"]["selected_attempt"] == 3


@pytest.mark.parametrize("status", ["missing", "partially_satisfied", "circular", "unsupported", "insufficient_depth", "satisfied"])
def test_research_coverage_structured_required_status_is_authoritative(status):
    report = coverage(["good"], status=status).model_copy(update={"is_sufficient": True})
    checked = checked_coverage(contract(), report, [fact("good", "A pressure difference pushes the wings upwards.")])
    assert checked.is_sufficient == (status == "satisfied")
