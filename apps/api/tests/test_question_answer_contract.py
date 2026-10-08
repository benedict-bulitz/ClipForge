from unittest.mock import patch

import pytest

from clipforge.config import Settings
from clipforge.pipeline import _build_initial_state
from clipforge.question_answer_contract import (
    AnswerObligation,
    QuestionAnswerContract,
    ResearchCoverageReport,
    generate_contract,
)
from clipforge.schemas import AdvancedOptions
from clipforge.script_story_rewrite import (
    ContractObligationEvaluation,
    VerifierResponse,
    _verifier_findings,
)


@pytest.fixture
def mock_settings():
    return Settings(openai_api_key="sk-test", openai_director_model="gpt-4o")

def test_1_contract_missing_overrides_generic_answers_question_true():
    response = VerifierResponse(
        grounded=True,
        answers_question=True,
        payoff_fulfilled=True,
        premature_reveal=False,
        hook_promise_kept=True,
        answer_payoff_duplicate=False,
        better_than_draft=True,
        findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""},
        explanation_audit=[],
        contract_sufficient=False,
        contract_evaluations=[
            ContractObligationEvaluation(
                id="req1", status="missing", reasoning="Missing", is_primary=False, is_required=True
            )
        ]
    )
    findings = _verifier_findings(response)
    codes = [f["code"] for f in findings]
    assert "contract_insufficient" in codes
    assert "required_obligation_missing" in codes

def test_2_primary_obligation_circular_blocks():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=False,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="circular", reasoning="Circular logic", is_primary=True)
        ]
    )
    findings = _verifier_findings(response)
    codes = [f["code"] for f in findings]
    assert "answer_circular" in codes
    assert "contract_insufficient" in codes

def test_3_required_obligation_insufficient_depth_blocks():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=False,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="insufficient_depth", reasoning="Shallow", is_primary=False, is_required=True)
        ]
    )
    findings = _verifier_findings(response)
    codes = [f["code"] for f in findings]
    assert "insufficient_causal_depth" in codes
    assert "contract_insufficient" in codes

@patch("clipforge.pipeline.research_topic")
@patch("clipforge.pipeline.evaluate_research_coverage")
@patch("clipforge.pipeline.generate_contract")
def test_4_research_first_pass_misses_retry_finds(mock_gen, mock_eval, mock_research, mock_settings):
    # Mocking a pipeline run to test retry logic
    mock_gen.return_value = QuestionAnswerContract(
        question_type="explanation", core_question="Test?",
        primary_answer_obligation=AnswerObligation(id="1", description="main", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )

    # First pass missing, second pass finds
    eval_missing = ResearchCoverageReport(is_sufficient=False, missing_obligations=["1"], coverage=[])
    eval_found = ResearchCoverageReport(is_sufficient=True, missing_obligations=[], coverage=[])
    mock_eval.side_effect = [eval_missing, eval_found]

    class MockResult:
        def __init__(self, f):
            self.facts = f
            self.sources = [{"label": "test", "url": "test"}]
            self.status = "connected"
            self.provider = "test"
            self.error = None

    mock_research.side_effect = [MockResult([{"claim": "a"}]), MockResult([{"claim": "b"}])]

    state = _build_initial_state("Test?", AdvancedOptions(), mock_settings)
    assert mock_research.call_count == 2
    assert state["research"]["status"] == "connected"
    assert state["research"]["retry"]["reason"] == "contract_insufficient"

@patch("clipforge.pipeline.research_topic")
@patch("clipforge.pipeline.evaluate_research_coverage")
@patch("clipforge.pipeline.generate_contract")
def test_5_research_retry_still_misses_blocks(mock_gen, mock_eval, mock_research, mock_settings):
    mock_gen.return_value = QuestionAnswerContract(
        question_type="explanation", core_question="Test?",
        primary_answer_obligation=AnswerObligation(id="1", description="main", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )

    eval_missing = ResearchCoverageReport(is_sufficient=False, missing_obligations=["1"], coverage=[])
    mock_eval.side_effect = [eval_missing, eval_missing]

    class MockResult:
        def __init__(self):
            self.facts = [{"claim": "a"}]
            self.sources = [{"label": "test", "url": "test"}]
            self.status = "connected"
            self.provider = "test"
            self.error = None

    mock_research.return_value = MockResult()
    state = _build_initial_state("Test?", AdvancedOptions(), mock_settings)
    assert state["research"]["status"] == "needs_research"
    assert state["script"]["readiness"]["ready"] == False

def test_6_initial_draft_misses_but_rewrite_uses():
    from clipforge.script_story_rewrite import REWRITE_INSTRUCTIONS
    assert "If the research contains a mandatory fact but the draft missed it, you MUST use it" in REWRITE_INSTRUCTIONS
    assert "MUST satisfy every required answer obligation" in REWRITE_INSTRUCTIONS

def test_7_answer_spans_multiple_beats_passes():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=True,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="satisfied", reasoning="Satisfied across beats 2 and 3", is_primary=True)
        ]
    )
    findings = _verifier_findings(response)
    assert not any(f["code"] == "contract_insufficient" for f in findings)

def test_8_different_semantic_wording_passes():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=True,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="satisfied", reasoning="Semantically equivalent wording used", is_primary=True)
        ]
    )
    findings = _verifier_findings(response)
    assert not any(f["code"] == "contract_insufficient" for f in findings)

def test_9_strong_concise_answer_not_overexpanded():
    from clipforge.script_story_rewrite import REWRITE_INSTRUCTIONS
    assert "compress or expand" in REWRITE_INSTRUCTIONS
    assert "Quality beats" in REWRITE_INSTRUCTIONS

def test_10_definition_question():
    contract = QuestionAnswerContract(
        question_type="definition",
        core_question="What is a neutron star?",
        primary_answer_obligation=AnswerObligation(id="def", description="collapsed core of star", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "definition"
    assert contract.minimum_answer_depth == 1

def test_11_comparison_question():
    contract = QuestionAnswerContract(
        question_type="comparison",
        core_question="Difference between virus and bacteria?",
        primary_answer_obligation=AnswerObligation(id="comp", description="living vs non-living mechanism", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "comparison"

def test_12_quantity_question():
    contract = QuestionAnswerContract(
        question_type="quantity",
        core_question="How many galaxies?",
        primary_answer_obligation=AnswerObligation(id="qty", description="billions/trillions estimate", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "quantity"

@patch("clipforge.pipeline.generate_contract")
def test_13_contract_ai_unavailable_fallback(mock_gen, mock_settings):
    mock_gen.side_effect = Exception("API down")
    state = _build_initial_state("Test?", AdvancedOptions(), mock_settings)
    assert state["research"].get("contract_diagnostic") in ("error", "unavailable", "deterministic_fallback")

@patch("clipforge.pipeline.research_topic")
@patch("clipforge.pipeline.evaluate_research_coverage")
@patch("clipforge.pipeline.generate_contract")
def test_14_coverage_ai_unavailable_fallback(mock_gen, mock_eval, mock_research, mock_settings):
    mock_gen.return_value = QuestionAnswerContract(
        question_type="explanation", core_question="Test?",
        primary_answer_obligation=AnswerObligation(id="1", description="main", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    mock_eval.side_effect = Exception("OpenAI API Down")

    class MockResult:
        def __init__(self):
            self.facts = [{"claim": "test"}]
            self.sources = [{"label": "test", "url": "test"}]
            self.status = "connected"
            self.provider = "test"
            self.error = None

    mock_research.return_value = MockResult()

    state = _build_initial_state("Test?", AdvancedOptions(), mock_settings)
    assert state["research"]["status"] == "connected"  # Survives API failure


def test_15_fiction_flow_no_contract(mock_settings):
    opts = AdvancedOptions(content_type="fictional_story", research_enabled=False)
    state = _build_initial_state("Write a story", opts, mock_settings)
    assert state["research"]["required"] == False

@pytest.fixture(autouse=True)
def forbid_real_contract_openai(monkeypatch):
    import clipforge.question_answer_contract as qac
    def forbidden(*args, **kwargs):
        raise AssertionError("Real OpenAI contract generation is forbidden in the test suite.")
    monkeypatch.setattr(qac, "OpenAI", forbidden)

def test_16_no_network_access():
    with pytest.raises(AssertionError, match="Real OpenAI contract generation is forbidden"):
        generate_contract("Test?", "en", Settings(openai_api_key="sk-test"))


def test_optional_obligation_missing_does_not_block():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=True,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="missing", reasoning="Missing optional", is_primary=False, is_required=False),
            ContractObligationEvaluation(id="req2", status="satisfied", reasoning="Satisfied req", is_primary=True, is_required=True)
        ]
    )
    findings = _verifier_findings(response)
    assert not any(f["severity"] == "hard" for f in findings)

def test_required_obligation_partially_satisfied_blocks():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=False,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="partially_satisfied", reasoning="Partial", is_primary=False, is_required=True)
        ]
    )
    findings = _verifier_findings(response)
    codes = [f["code"] for f in findings if f["severity"] == "hard"]
    assert "required_obligation_missing" in codes

def test_primary_required_partially_satisfied_blocks():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=False,
        contract_evaluations=[
            ContractObligationEvaluation(id="req1", status="partially_satisfied", reasoning="Partial", is_primary=True, is_required=True)
        ]
    )
    findings = _verifier_findings(response)
    codes = [f["code"] for f in findings if f["severity"] == "hard"]
    assert "primary_answer_missing" in codes

def test_required_satisfied_optional_missing_passes():
    response = VerifierResponse(
        grounded=True, answers_question=True, payoff_fulfilled=True,
        premature_reveal=False, hook_promise_kept=True, answer_payoff_duplicate=False, better_than_draft=True, findings=[],
        answer_sufficiency={"verdict": "answered", "one_sentence_answer": "ok", "missing": ""}, explanation_audit=[],
        contract_sufficient=True,
        contract_evaluations=[
            ContractObligationEvaluation(id="opt1", status="missing", reasoning="Missing", is_primary=False, is_required=False),
            ContractObligationEvaluation(id="req1", status="satisfied", reasoning="Satisfied", is_primary=True, is_required=True)
        ]
    )
    findings = _verifier_findings(response)
    assert not any(f["severity"] == "hard" for f in findings)
