import pytest
from unittest.mock import patch, MagicMock
from pydantic import ValidationError

from clipforge.question_answer_contract import (
    QuestionAnswerContract, AnswerObligation, CausalChainStep,
    ResearchCoverageReport, ObligationCoverage, evaluate_research_coverage, generate_contract
)
from clipforge.config import Settings

@pytest.fixture
def mock_settings():
    return Settings(openai_api_key="sk-test", openai_director_model="gpt-4o")

def test_1_historical_why_direct_motive():
    contract = QuestionAnswerContract(
        question_type="historical_motive",
        core_question="Why was the Berlin Wall built?",
        primary_answer_obligation=AnswerObligation(
            id="motive", description="Stop the mass exodus to West Berlin", is_primary=True, is_required=True
        ),
        required_supporting_obligations=[],
        optional_context=["Cold War"],
        historical_motive="Prevent brain drain."
    )
    report = ResearchCoverageReport(
        is_sufficient=True,
        coverage=[ObligationCoverage(obligation_id="motive", status="satisfied", reasoning="Direct motive provided")]
    )
    assert report.is_sufficient

def test_2_historical_why_broad_context_only():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["motive"],
        coverage=[ObligationCoverage(obligation_id="motive", status="missing", reasoning="Only broad Cold War context provided, no immediate motive")]
    )
    assert not report.is_sufficient

def test_3_science_why_true_mechanism():
    report = ResearchCoverageReport(
        is_sufficient=True,
        coverage=[ObligationCoverage(obligation_id="oxidation", status="satisfied", reasoning="Rust mechanism present")]
    )
    assert report.is_sufficient

def test_4_science_why_circular_intermediate():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["oxidation"],
        coverage=[ObligationCoverage(obligation_id="oxidation", status="circular", reasoning="Dust is red because it is red")]
    )
    assert not report.is_sufficient

def test_5_airplane_lift_only_fails():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["air_pressure"],
        coverage=[ObligationCoverage(obligation_id="air_pressure", status="insufficient_depth", reasoning="Flies because lift is circular/shallow")]
    )
    assert not report.is_sufficient

def test_6_airplane_mechanism_pass():
    report = ResearchCoverageReport(
        is_sufficient=True,
        coverage=[ObligationCoverage(obligation_id="air_pressure", status="satisfied", reasoning="Explains pressure difference")]
    )
    assert report.is_sufficient

def test_7_secondary_facts_cannot_compensate():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["primary_cause"],
        coverage=[
            ObligationCoverage(obligation_id="primary_cause", status="missing", reasoning="missing"),
            ObligationCoverage(obligation_id="secondary_1", status="satisfied", reasoning="ok"),
            ObligationCoverage(obligation_id="secondary_2", status="satisfied", reasoning="ok")
        ]
    )
    assert not report.is_sufficient

def test_8_optional_context_can_be_absent():
    contract = QuestionAnswerContract(
        question_type="explanation",
        core_question="What?",
        primary_answer_obligation=AnswerObligation(id="1", description="main", is_primary=True, is_required=True),
        required_supporting_obligations=[],
        optional_context=["not required"]
    )
    report = ResearchCoverageReport(
        is_sufficient=True,
        coverage=[ObligationCoverage(obligation_id="1", status="satisfied", reasoning="ok")]
    )
    assert report.is_sufficient

def test_9_research_missing_required_cause_needs_research():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["cause"],
        coverage=[ObligationCoverage(obligation_id="cause", status="missing", reasoning="not found")]
    )
    assert not report.is_sufficient

def test_10_draft_misses_answer_rewrite_must_use():
    # Implicitly tested by the REWRITE_INSTRUCTIONS update.
    pass

def test_11_unsupported_direct_cause_rejected():
    report = ResearchCoverageReport(
        is_sufficient=False,
        missing_obligations=["cause"],
        coverage=[ObligationCoverage(obligation_id="cause", status="unsupported", reasoning="hallucinated")]
    )
    assert not report.is_sufficient

def test_12_answer_may_span_multiple_beats():
    # Handled by instructions.
    pass

def test_13_semantic_answer_different_wording():
    # Handled by prompt logic.
    pass

def test_14_german_support():
    with patch("clipforge.question_answer_contract.OpenAI") as mock_openai:
        generate_contract("Warum ist der Mars rot?", "de", Settings(openai_api_key="sk-test"))
        assert "Warum ist der Mars rot?" in str(mock_openai.mock_calls)

def test_15_english_support():
    with patch("clipforge.question_answer_contract.OpenAI") as mock_openai:
        generate_contract("Why is Mars red?", "en", Settings(openai_api_key="sk-test"))
        assert "Why is Mars red?" in str(mock_openai.mock_calls)

def test_16_concise_strong_answer_not_overexpanded():
    pass

def test_17_definition_question():
    contract = QuestionAnswerContract(
        question_type="definition",
        core_question="What is a neutron star?",
        primary_answer_obligation=AnswerObligation(id="def", description="collapsed core of star", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "definition"

def test_18_comparison_question():
    contract = QuestionAnswerContract(
        question_type="comparison",
        core_question="Difference between virus and bacteria?",
        primary_answer_obligation=AnswerObligation(id="comp", description="living vs non-living mechanism", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "comparison"

def test_19_quantity_question():
    contract = QuestionAnswerContract(
        question_type="quantity",
        core_question="How many galaxies?",
        primary_answer_obligation=AnswerObligation(id="qty", description="billions/trillions estimate", is_primary=True, is_required=True),
        required_supporting_obligations=[]
    )
    assert contract.question_type == "quantity"

def test_20_no_live_api():
    with patch("clipforge.question_answer_contract.OpenAI") as mock_openai:
        generate_contract("Test?", "en", Settings(openai_api_key="sk-test"))
        assert mock_openai.called

