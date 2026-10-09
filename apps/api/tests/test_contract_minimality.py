"""Offline semantic-review fixtures; no topic-specific production rules."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_question_answer_contract import contract, coverage
from test_script_story_rewrite import fact

from clipforge import question_answer_contract as qac
from clipforge.config import Settings
from clipforge.contract_minimality import (
    ComponentPreservation,
    MinimumAnswerComponent,
    NecessityAudit,
    NecessityComponent,
    QuestionMinimum,
    RepairCheck,
    requirement_ids,
)

SETTINGS = Settings(openai_api_key="fixture", openai_director_model="configured-director")
MARS_QUESTION = "Warum ist der Mars rot?"
SURFACE = "Eisenoxide / verrostetes Eisen als Oberflächenstaub verleihen dem Mars sein rotes Aussehen."
ATMOSPHERE = "Eisenoxid-Staub ist auch in der Atmosphäre weit verbreitet."
LIVE_FACTS = [
    fact("fact_01", "Am Himmel erscheint der Planet Mars als roter Stern. Diese Farbe ist auf die Tatsache zurückzuführen, dass sein Boden hauptsächlich aus Eisenoxid besteht."),
    fact("fact_03", "Seine Farbe hat Mars von verrostetem Eisen, das die Oberfläche als Staub bedeckt."),
    fact("fact_09", "Die Atmosphäre war besonders dünn und oxidierte langsam den eisenhaltigen Marsboden."),
]


def approved_audit(model):
    return NecessityAudit(
        minimally_sufficient=True, minimal_answer=model.primary_answer_obligation.description,
        components=[NecessityComponent(
            id=f"component_{index}", source_requirement_ids=[identifier],
            description=model.primary_answer_obligation.description,
            strictly_necessary=True, semantic_role="primary_cause_or_motive", atomic=True,
            minimum_answer_component_ids=[f"component_{index}"],
            answer_without_component="The remaining observation does not explain the cause.",
            omission_still_answers_question=False, reason="Omitting this essential component leaves the question unresolved.",
        ) for index, identifier in enumerate(sorted(requirement_ids(model)))],
        primary_contains_unnecessary_conjunction=False, correction_required=False,
        minimum_necessary_depth=model.minimum_answer_depth,
        preserves_question_semantics=True, preserves_explicit_constraints=True,
    )


def mars_repair():
    proposed = contract(MARS_QUESTION, SURFACE + " UND " + ATMOSPHERE)
    audit = approved_audit(proposed)
    audit.minimally_sufficient = False
    audit.correction_required = True
    audit.primary_contains_unnecessary_conjunction = True
    primary = next(c for c in audit.components if "obligation:primary" in c.source_requirement_ids)
    primary.description = SURFACE
    audit.components.append(NecessityComponent(
        id="optional_atmosphere", source_requirement_ids=["obligation:primary"],
        description=ATMOSPHERE, strictly_necessary=False, semantic_role="extension", atomic=True,
        minimum_answer_component_ids=[], answer_without_component=SURFACE, omission_still_answers_question=True,
        reason="Ohne atmosphärische Verteilung beantwortet der Oberflächenstaub die ursprüngliche Frage bereits.",
    ))
    repaired = contract(MARS_QUESTION, SURFACE)
    repaired.optional_context = [ATMOSPHERE]
    return proposed, audit, repaired


def passed_check(audit):
    return RepairCheck(
        minimally_sufficient=True, preserves_question_semantics=True,
        preserves_explicit_constraints=True, preserves_primary_cause_or_motive=True,
        preserves_necessary_chain=True,
        components=[ComponentPreservation(component_id=c.id, correctly_required_or_optional=True,
                                         reason="Necessary cause remains required; extension remains optional.")
                    for c in audit.components], reason="Matches the necessity audit.",
    )


def response(parsed, refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, refusal=refusal))])


def minimum_for(model, audit):
    return QuestionMinimum(
        question_intent=model.core_question, minimal_answer=audit.minimal_answer,
        necessary_components=list({c.id: MinimumAnswerComponent(id=c.id, description=c.description, necessity_reason=c.reason)
                                   for c in audit.components if c.strictly_necessary}.values()),
        explicit_constraints=[], optional_extensions=[c.description for c in audit.components if not c.strictly_necessary],
        minimum_necessary_depth=audit.minimum_necessary_depth,
        preserves_question_semantics=True, preserves_explicit_constraints=True,
    )


def install_parser(monkeypatch, outputs):
    # Legacy fixtures describe planner/audit/repair/check; add the new independent
    # scope explicitly to their recorded sequence and account for its call below.
    model = outputs[0] if isinstance(outputs[0], qac.QuestionAnswerContract) else contract()
    audit = copy.deepcopy(outputs[1]) if isinstance(outputs[1], NecessityAudit) else approved_audit(model)
    for c in audit.components:
        c.minimum_answer_component_ids = [c.id] if c.strictly_necessary else []
        c.omission_still_answers_question = not c.strictly_necessary
    minimum = minimum_for(model, audit)
    # Preserve intentionally missing/contradictory output fixtures as written.
    if isinstance(outputs[1], NecessityAudit):
        outputs = [outputs[0], audit, *outputs[2:]]
    outputs = [outputs[0], minimum, *outputs[1:]]
    parse = Mock(side_effect=[item if isinstance(item, Exception) else response(item) for item in outputs])
    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))
    monkeypatch.setattr(qac, "OpenAI", lambda **_kwargs: client)
    return parse


def test_live_surface_facts_satisfy_repaired_contract_without_atmospheric_distribution(monkeypatch):
    proposed, audit, repaired = mars_repair()
    parse = install_parser(monkeypatch, [proposed, audit, repaired, passed_check(audit), coverage(["fact_01", "fact_03", "fact_09"])])
    result = qac.generate_contract(MARS_QUESTION, "de", SETTINGS)
    assert result.primary_answer_obligation.is_required
    assert result.primary_answer_obligation.description == SURFACE
    assert ATMOSPHERE in result.optional_context
    report = qac.evaluate_research_coverage(result, LIVE_FACTS, SETTINGS)
    assert report.is_sufficient
    assert report.coverage[0].supporting_fact_ids == ["fact_01", "fact_03", "fact_09"]
    payload = parse.call_args_list[-1].kwargs["messages"][1]["content"]
    assert SURFACE in payload and "fact_03" in payload
    assert "weit verbreitet" not in json.dumps([f["claim"] for f in LIVE_FACTS])
    assert result._minimality_review["repair_check"]["preserves_primary_cause_or_motive"]
    assert parse.call_count == 6  # bounded five contract calls, then unchanged coverage call


@pytest.mark.parametrize("question,description,language", [
    ("Warum ist Staub in der Marsatmosphäre rot?", "Oxidiertes Eisen im atmosphärischen Staub erklärt dessen rote Farbe.", "de"),
    ("Why is dust in the atmosphere red?", "Oxidized material in atmospheric dust explains its red color.", "en"),
    ("Warum wurde die Berliner Mauer gebaut?", "Die unmittelbare Absicht war, die Abwanderung zu stoppen.", "de"),
    ("Why was the barrier built?", "Its immediate purpose was to stop emigration.", "en"),
])
def test_explicit_constraints_and_historical_motives_remain_required(monkeypatch, question, description, language):
    model = contract(question, description)
    model.optional_context = ["Broader historical context and later consequences."]
    if "gebaut" in question or "built" in question:
        model.question_type = "historical_motive"
        model.historical_motive = description
    parse = install_parser(monkeypatch, [model, approved_audit(model)])
    result = qac.generate_contract(question, language, SETTINGS)
    assert result.primary_answer_obligation.description == description
    assert result.primary_answer_obligation.is_required
    assert parse.call_count == 3
    payload = json.loads(parse.call_args.kwargs["messages"][1]["content"])
    assert payload["original_question"] == question
    assert payload["target_language"] == language
    assert payload["proposed_contract"] == model.model_dump()
    for call in parse.call_args_list:
        assert call.kwargs["model"] == "configured-director"
        assert "temperature" not in call.kwargs


def test_genuinely_necessary_multistep_chain_has_no_global_depth_cap(monkeypatch):
    model = contract("How does an undersea earthquake generate a tsunami that grows near shore?",
                     "Seafloor displacement transfers energy to the water; propagation and shoaling increase wave height.")
    model.minimum_answer_depth = 4
    model.causal_mechanistic_chain = [qac.CausalChainStep(id=str(i), description=description, depth_level=i)
                                    for i, description in enumerate([
                                        "Seafloor moves vertically", "Water column is displaced",
                                        "Energy propagates as long waves", "Shoaling increases height in shallow water"], 1)]
    model.required_mechanism_concepts = ["energy transfer", "shoaling"]
    model.required_supporting_obligations = [qac.AnswerObligation(
        id="shoaling", description="Explain shallow-water wave height growth", is_primary=False, is_required=True)]
    parse = install_parser(monkeypatch, [model, approved_audit(model)])
    result = qac.generate_contract(model.core_question, "en", SETTINGS)
    assert result.minimum_answer_depth == 4
    assert len(result.causal_mechanistic_chain) == 4
    assert result.required_supporting_obligations[0].is_required
    assert parse.call_count == 3


@pytest.mark.parametrize("language", ["de", "en"])
def test_compound_primary_repair_retains_cause_and_demotes_extension(monkeypatch, language):
    model = contract("Why does the device stop?", "Overheating triggers shutdown AND the casing is blue.")
    audit = approved_audit(model)
    audit.minimally_sufficient = False
    audit.correction_required = True
    audit.primary_contains_unnecessary_conjunction = True
    audit.components.append(NecessityComponent(id="extension", source_requirement_ids=["obligation:primary"],
                                              description="Blue casing", strictly_necessary=False, semantic_role="extension", atomic=True,
                                              minimum_answer_component_ids=[], answer_without_component="Overheating triggers shutdown.",
                                              omission_still_answers_question=True,
                                              reason="Omitting casing color does not alter the shutdown explanation."))
    repaired = contract(model.core_question, "Overheating triggers protective shutdown.")
    repaired.optional_context = ["The casing is blue."]
    parse = install_parser(monkeypatch, [model, audit, repaired, passed_check(audit)])
    result = qac.generate_contract(model.core_question, language, SETTINGS)
    assert result.primary_answer_obligation.description == repaired.primary_answer_obligation.description
    assert result.primary_answer_obligation.is_required and parse.call_count == 5
    assert result.optional_context == repaired.optional_context


def test_missing_actual_essential_cause_still_blocks(monkeypatch):
    proposed, audit, repaired = mars_repair()
    parse = install_parser(monkeypatch, [proposed, audit, repaired, passed_check(audit), coverage([], status="missing")])
    result = qac.generate_contract(MARS_QUESTION, "de", SETTINGS)
    report = qac.evaluate_research_coverage(result, [fact("context", "Mars is cold.")], SETTINGS)
    assert not report.is_sufficient and report.missing_obligations == ["primary"]
    assert parse.call_count == 6


@pytest.mark.parametrize("bad", [None, {"minimally_sufficient": True}, RuntimeError("reviewer unavailable")])
def test_missing_malformed_or_unavailable_reviewer_fails_closed(monkeypatch, bad):
    model = contract()
    parse = install_parser(monkeypatch, [model, bad])
    with pytest.raises((ValueError, RuntimeError)):
        qac.generate_contract(model.core_question, "en", SETTINGS)
    assert parse.call_count == 3


def test_refused_reviewer_fails_even_with_parsed_output(monkeypatch):
    model = contract()
    parse = install_parser(monkeypatch, [model, approved_audit(model)])
    parse.side_effect = [response(model), response(minimum_for(model, approved_audit(model))), response(approved_audit(model), refusal="rejected")]
    with pytest.raises(ValueError):
        qac.generate_contract(model.core_question, "en", SETTINGS)


@pytest.mark.parametrize("failure", ["omitted_requirement", "unknown_requirement", "duplicate_component", "optional_but_approved",
                                      "primary_removed", "changed_semantics", "explicit_constraint_lost", "depth_mismatch"])
def test_incomplete_or_contradictory_audits_never_pass(monkeypatch, failure):
    model = contract()
    audit = approved_audit(model)
    if failure == "omitted_requirement":
        audit.components.pop()
    elif failure == "unknown_requirement":
        audit.components[0].source_requirement_ids.append("unknown")
    elif failure == "duplicate_component":
        audit.components.append(copy.deepcopy(audit.components[0]))
    elif failure == "optional_but_approved":
        audit.components[0].strictly_necessary = False
    elif failure == "primary_removed":
        for c in audit.components:
            if "obligation:primary" in c.source_requirement_ids:
                c.strictly_necessary = False
    elif failure == "changed_semantics":
        audit.preserves_question_semantics = False
    elif failure == "explicit_constraint_lost":
        audit.preserves_explicit_constraints = False
    else:
        audit.minimum_necessary_depth = 2
    parse = install_parser(monkeypatch, [model, audit])
    with pytest.raises(ValueError):
        qac.generate_contract(model.core_question, "en", SETTINGS)
    assert parse.call_count == 3


@pytest.mark.parametrize("failure", ["dropped_cause", "still_required_extension", "missing_component", "duplicate_component",
                                      "changed_question", "changed_depth", "repair_missing", "check_unavailable"])
def test_repair_rejection_stops_without_another_attempt(monkeypatch, failure):
    proposed, audit, repaired = mars_repair()
    check = passed_check(audit)
    if failure == "dropped_cause":
        check.preserves_primary_cause_or_motive = False
    elif failure == "still_required_extension":
        check.components[-1].correctly_required_or_optional = False
    elif failure == "missing_component":
        check.components.pop()
    elif failure == "duplicate_component":
        check.components[-1] = copy.deepcopy(check.components[0])
    elif failure == "changed_question":
        repaired.core_question = "Different question"
    elif failure == "changed_depth":
        repaired.minimum_answer_depth = 3
    elif failure == "repair_missing":
        repaired = None
    else:
        check = RuntimeError("verification unavailable")
    parse = install_parser(monkeypatch, [proposed, audit, repaired, check])
    with pytest.raises((ValueError, RuntimeError)):
        qac.generate_contract(MARS_QUESTION, "de", SETTINGS)
    assert parse.call_count <= 5


def test_review_prompt_enforces_counterfactual_and_full_component_audit(monkeypatch):
    model = contract()
    parse = install_parser(monkeypatch, [model, approved_audit(model)])
    qac.generate_contract(model.core_question, "en", SETTINGS)
    prompt = parse.call_args.kwargs["messages"][0]["content"]
    for instruction in ["counterfactual", "EVERY", "compound", "other essential information", "direct underlying cause/motive",
                        "explicit user constraints", "multi-step", "global depth cap"]:
        assert instruction in prompt
    assert parse.call_args.kwargs["response_format"] is NecessityAudit


def test_repair_updates_support_chain_concepts_and_depth_consistently(monkeypatch):
    model = contract("Why does the device stop?", "Heat triggers protective shutdown.")
    model.minimum_answer_depth = 3
    model.required_supporting_obligations = [qac.AnswerObligation(
        id="casing", description="Describe the casing color", is_primary=False, is_required=True)]
    model.causal_mechanistic_chain = [qac.CausalChainStep(id="extra", description="Describe emitted indicator light", depth_level=3)]
    model.required_mechanism_concepts = ["indicator light emission"]
    audit = approved_audit(model)
    audit.minimally_sufficient = False
    audit.correction_required = True
    audit.minimum_necessary_depth = 1
    for c in audit.components:
        if c.source_requirement_ids[0] in {"obligation:casing", "chain:extra", "concept:0"}:
            c.strictly_necessary = False
            c.reason = "This extension can be omitted without losing the causal answer."
    repaired = contract(model.core_question, model.primary_answer_obligation.description)
    repaired.optional_context = ["Casing color and indicator light emission are additional context."]
    parse = install_parser(monkeypatch, [model, audit, repaired, passed_check(audit)])
    result = qac.generate_contract(model.core_question, "en", SETTINGS)
    assert result.minimum_answer_depth == 1
    assert not result.causal_mechanistic_chain and not result.required_mechanism_concepts
    assert not any(item.is_required for item in result.required_supporting_obligations)
    assert result.primary_answer_obligation.is_required
    assert parse.call_count == 5


@pytest.mark.parametrize("failed_stage", ["review", "repair", "repair_check"])
def test_guard_failure_reaches_existing_sanitized_render_block(monkeypatch, failed_stage):
    from test_question_answer_contract import install_pipeline

    from clipforge import pipeline

    calls, _editor, _state = install_pipeline(monkeypatch)
    monkeypatch.setattr(pipeline, "generate_contract", qac.generate_contract)
    proposed, audit, repaired = mars_repair()
    outputs = [proposed, audit, repaired, passed_check(audit)]
    index = {"review": 1, "repair": 2, "repair_check": 3}[failed_stage]
    outputs[index] = RuntimeError("PRIVATE provider diagnostics")
    parse = install_parser(monkeypatch, outputs)
    from clipforge.schemas import AdvancedOptions

    state = pipeline.build_initial_state(
        MARS_QUESTION, AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="fixture"),
    )
    assert state["contract"] is None
    assert state["research"]["status"] == "contract_unavailable"
    assert state["research"]["diagnostics"]["failure_type"] == "CONTRACT_UNAVAILABLE"
    assert "PRIVATE" in state["research"]["diagnostics"]["contract_failure_reason"]
    assert not state["script"]["readiness"]["ready"]
    assert "PRIVATE" not in state["script"]["readiness"]["message"]
    assert parse.call_count == index + 2
    # No unreviewed contract is ever passed into research.
    assert all("question_answer_contract" not in context for context in calls)
