"""Diagnostic fidelity without relaxing any contract acceptance invariant."""
import json

import pytest
from test_contract_minimality import (
    SETTINGS,
    approved_audit,
    install_parser,
    mars_repair,
    passed_check,
)
from test_question_answer_contract import contract, install_pipeline

from clipforge import pipeline
from clipforge import question_answer_contract as qac
from clipforge.config import Settings
from clipforge.contract_diagnostics import ContractGenerationFailure, sanitized
from clipforge.schemas import AdvancedOptions


def reject_check(monkeypatch, mutate):
    proposed, audit, repaired = mars_repair()
    check = passed_check(audit)
    mutate(check)
    parse = install_parser(monkeypatch, [proposed, audit, repaired, check])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(proposed.core_question, "de", SETTINGS)
    assert parse.call_count == 5
    diagnostic = caught.value.diagnostics
    assert diagnostic["failure_stage"] == "repair_check"
    assert diagnostic["failure_code"] == "REPAIR_CHECK_REJECTED"
    return diagnostic


@pytest.mark.parametrize("flag", ["minimally_sufficient", "preserves_primary_cause_or_motive",
                                  "preserves_question_semantics", "preserves_explicit_constraints", "preserves_necessary_chain"])
def test_each_negative_flag_remains_rejected_and_identifiable(monkeypatch, flag):
    details = reject_check(monkeypatch, lambda check: setattr(check, flag, False))
    assert details["verification_flags"][flag] is False
    assert details["failure_kind"] == "semantic"
    assert details["failed_invariants"] == [{
        "code": "CHECK_" + flag.upper(), "invariant": f"{flag} must be true.",
        "component_ids": details["failed_invariants"][0]["component_ids"], "kind": "semantic",
    }]
    if flag == "preserves_primary_cause_or_motive":
        assert details["failed_invariants"][0]["component_ids"]
    assert details["reviewer_explanations"]["overall"]


@pytest.mark.parametrize("necessary", [True, False])
def test_required_loss_and_incorrect_optional_demotion_identify_component(monkeypatch, necessary):
    def mutate(check):
        check.components[0 if necessary else -1].correctly_required_or_optional = False
        check.components[0 if necessary else -1].reason = "Essential cause absent" if necessary else "Optional detail still required"
    details = reject_check(monkeypatch, mutate)
    failure = details["failed_invariants"][0]
    assert failure["code"] == ("NECESSARY_COMPONENT_NOT_PRESERVED" if necessary else "OPTIONAL_COMPONENT_NOT_CORRECTLY_DEMOTED")
    assert failure["component_ids"]
    assert details["necessary_components"][failure["component_ids"][0]] is necessary
    assert any(item["reason"] == ("Essential cause absent" if necessary else "Optional detail still required")
               for item in details["reviewer_explanations"]["components"])


@pytest.mark.parametrize("mutation", ["missing", "unknown", "duplicate"])
def test_mismatched_component_ids_are_structural_rejections(monkeypatch, mutation):
    def mutate(check):
        if mutation == "missing":
            check.components.pop()
        elif mutation == "unknown":
            check.components[-1].component_id = "unknown_component"
        else:
            check.components[-1].component_id = check.components[0].component_id
    details = reject_check(monkeypatch, mutate)
    assert details["failure_kind"] == "structural"
    codes = {item["code"] for item in details["failed_invariants"]}
    assert "CHECK_COMPONENT_IDS_MISMATCH" in codes
    if mutation == "missing":
        assert "CHECK_COMPONENT_COUNT_MISMATCH" in codes
    assert details["expected_component_ids"] != details["actual_component_ids"]


def test_multiple_failures_are_all_retained(monkeypatch):
    def mutate(check):
        check.minimally_sufficient = False
        check.preserves_primary_cause_or_motive = False
        check.components.pop()
    details = reject_check(monkeypatch, mutate)
    assert details["failure_kind"] == "mixed"
    assert len(details["failed_invariants"]) == 4


def test_inconsistent_audit_approval_diagnostics(monkeypatch):
    model = contract()
    audit = approved_audit(model)
    audit.primary_contains_unnecessary_conjunction = True
    parse = install_parser(monkeypatch, [model, audit])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(model.core_question, "en", SETTINGS)
    details = caught.value.diagnostics
    assert details["failure_stage"] == "necessity_audit"
    assert details["failure_code"] == "INCONSISTENT_AUDIT_APPROVAL"
    assert details["failed_invariants"][0]["code"] == "APPROVED_UNNECESSARY_CONJUNCTION"
    assert details["verification_flags"]["primary_contains_unnecessary_conjunction"]
    assert parse.call_count == 3


@pytest.mark.parametrize("stage", ["proposed_contract", "contract_repair"])
def test_invalid_contract_structure_is_identified_without_more_calls(monkeypatch, stage):
    proposed, audit, repaired = mars_repair()
    invalid = proposed if stage == "proposed_contract" else repaired
    invalid.primary_answer_obligation.description = " "
    invalid.minimum_answer_depth = 0
    invalid.required_supporting_obligations[0].id = invalid.primary_answer_obligation.id
    parse = install_parser(monkeypatch, [proposed, audit, repaired])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(proposed.core_question, "de", SETTINGS)
    details = caught.value.diagnostics
    assert details["failure_stage"] == stage
    assert details["failure_code"] == "INVALID_CONTRACT_STRUCTURE"
    assert {item["code"] for item in details["failed_invariants"]} == {"EMPTY_PRIMARY", "INVALID_DEPTH", "DUPLICATE_OBLIGATION_IDS"}
    assert parse.call_count == (1 if stage == "proposed_contract" else 4)


@pytest.mark.parametrize("repair", [False, True])
def test_success_uses_bounded_question_first_call_budget(monkeypatch, repair):
    if repair:
        model, audit, repaired = mars_repair()
        outputs = [model, audit, repaired, passed_check(audit)]
    else:
        model = contract()
        outputs = [model, approved_audit(model)]
    parse = install_parser(monkeypatch, outputs)
    result = qac.generate_contract(model.core_question, "de", SETTINGS)
    assert result.primary_answer_obligation.is_required
    assert result._minimality_review["repaired"] is repair
    assert parse.call_count == (5 if repair else 3)


@pytest.mark.parametrize("stage,index", [("contract_planner", 0), ("necessity_audit", 1), ("contract_repair", 2), ("repair_check", 3)])
def test_provider_failures_have_stage_and_redacted_message(monkeypatch, stage, index):
    proposed, audit, repaired = mars_repair()
    outputs = [proposed, audit, repaired, passed_check(audit)]
    outputs[index] = RuntimeError("Authorization: Bearer SECRET_TOKEN\nAPI_KEY=fixture\nsk-live-secret-token")
    parse = install_parser(monkeypatch, outputs)
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(proposed.core_question, "de", SETTINGS)
    assert caught.value.diagnostics["failure_stage"] == stage
    assert caught.value.diagnostics["failure_code"] == "PROVIDER_CALL_FAILED"
    assert caught.value.diagnostics["failure_kind"] == "provider"
    assert not any(secret in str(caught.value) for secret in ["SECRET_TOKEN", "fixture", "sk-live-secret-token"])
    assert parse.call_count == index + 1 + (index > 0)


def test_rejected_check_diagnostics_persist_without_final_contract(monkeypatch):
    install_pipeline(monkeypatch)
    monkeypatch.setattr(pipeline, "generate_contract", qac.generate_contract)
    proposed, audit, repaired = mars_repair()
    check = passed_check(audit)
    check.minimally_sufficient = False
    check.components[-1].correctly_required_or_optional = False
    check.reason = "Optional component still mandatory; api_key=sk-reliability-secret; Authorization: Bearer secret-token"
    check.components[-1].reason = "sk-live-secret-token"
    parse = install_parser(monkeypatch, [proposed, audit, repaired, check])
    state = pipeline.build_initial_state(
        proposed.core_question, AdvancedOptions(language="en", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="sk-reliability-secret"),
    )
    assert state["contract"] is None
    assert not state["script"]["readiness"]["ready"]
    assert state["script"]["readiness"]["message"] == "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."
    diagnostic = state["research"]["diagnostics"]["contract_failure"]
    assert diagnostic["failure_stage"] == "repair_check"
    assert diagnostic["verification_flags"]["minimally_sufficient"] is False
    assert len(diagnostic["failed_invariants"]) == 2
    persisted = json.dumps(json.loads(json.dumps(state)))
    assert not any(secret in persisted for secret in ["sk-reliability-secret", "secret-token", "sk-live-secret-token"])
    assert "Optional component still mandatory" in persisted
    assert "Optional component still mandatory" not in state["script"]["readiness"]["message"]
    assert parse.call_count == 5


def test_sanitizer_redacts_headers_keys_and_nested_component_ids():
    value = {"Authorization": "Bearer secret", "api_key": "secret", "secret": [{"id": "secret", "reason": "Bearer unknown-token"}]}
    clean = sanitized(value, ("secret",))
    assert "secret" not in json.dumps(clean)
    assert "unknown-token" not in json.dumps(clean)


@pytest.mark.parametrize("stage,index", [("contract_planner", 0), ("necessity_audit", 1), ("contract_repair", 2), ("repair_check", 3)])
def test_missing_structured_output_keeps_failure_stage(monkeypatch, stage, index):
    proposed, audit, repaired = mars_repair()
    outputs = [proposed, audit, repaired, passed_check(audit)]
    outputs[index] = None
    parse = install_parser(monkeypatch, outputs)
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(proposed.core_question, "de", SETTINGS)
    assert caught.value.diagnostics["failure_stage"] == stage
    assert caught.value.diagnostics["failure_code"] == "INVALID_STRUCTURED_OUTPUT"
    assert caught.value.diagnostics["failure_kind"] == "structural"
    assert parse.call_count == index + 1 + (index > 0)


def test_json_parse_failure_is_structural_and_redacted(monkeypatch):
    model = contract()
    install_parser(monkeypatch, [model, json.JSONDecodeError("sk-malformed-secret", "invalid", 0)])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(model.core_question, "en", SETTINGS)
    details = caught.value.diagnostics
    assert details["failure_stage"] == "necessity_audit"
    assert details["failure_code"] == "MALFORMED_STRUCTURED_OUTPUT"
    assert details["failure_kind"] == "structural"
    assert "sk-malformed-secret" not in json.dumps(details)
