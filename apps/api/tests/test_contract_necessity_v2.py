"""Question-first scope, actual Berlin V2 overconstraint, and evidence-bound coverage."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_contract_minimality import SETTINGS, approved_audit, minimum_for, passed_check, response
from test_question_answer_contract import contract

from clipforge import question_answer_contract as qac
from clipforge.contract_diagnostics import ContractGenerationFailure
from clipforge.contract_minimality import (
    MinimumAnswerComponent,
    NecessityAudit,
    NecessityComponent,
    QuestionMinimum,
    requirement_ids,
)
from clipforge.novelty import fact_is_supported

LIVE = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v2_necessity.json').read_text())
GOAL = 'die massenhafte Flucht aus der DDR über Berlin in den Westen zu stoppen'
CORE = 'Erklären, dass die Berliner Mauer gebaut wurde, um ' + GOAL + '.'


def raw_parser(monkeypatch, outputs):
    parse = Mock(side_effect=[item if isinstance(item, Exception) else response(item) for item in outputs])
    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))
    monkeypatch.setattr(qac, 'OpenAI', lambda **_kwargs: client)
    return parse


def berlin_review():
    proposed = qac.QuestionAnswerContract.model_validate(LIVE['contract'])
    minimum = QuestionMinimum(
        question_intent='Unmittelbare Absicht des Mauerbaus, nicht die dahinterliegenden politischen Ziele.',
        minimal_answer=CORE, necessary_components=[MinimumAnswerComponent(
            id='immediate_motive', description=CORE,
            necessity_reason='Ohne die Absicht, Abwanderung zu stoppen, bleibt der Baugrund unbeantwortet.')],
        explicit_constraints=[], optional_extensions=['Herrschaftssicherung', 'Fortbestand des Staates', 'Fluchtweggeographie'],
        minimum_necessary_depth=1, preserves_question_semantics=True, preserves_explicit_constraints=True,
    )
    # Retain the saved audit's requirement coverage, but classify each conjunct
    # against the independent minimum instead of the planner's expanded scope.
    old = LIVE['minimality']['audit']
    components = []
    for saved_item in old['components']:
        item = {**saved_item, 'source_requirement_ids': [identifier for identifier in saved_item['source_requirement_ids']
                                                        if identifier in requirement_ids(proposed)]}
        if not item['source_requirement_ids']:
            continue  # already optional in the saved repaired proposal
        necessary = item['id'] == 'mass_flucht_als_ausloeser'
        components.append(NecessityComponent(
            **{k:v for k,v in item.items() if k not in {'strictly_necessary', 'reason'}},
            strictly_necessary=necessary, atomic=True,
            semantic_role='primary_cause_or_motive' if necessary else 'extension',
            minimum_answer_component_ids=['immediate_motive'] if necessary else [],
            answer_without_component='Der Baugrund fehlt.' if necessary else CORE,
            omission_still_answers_question=not necessary,
            reason='Unmittelbare Absicht muss bleiben.' if necessary else 'Die unmittelbare Absicht beantwortet die allgemeine Frage schon.',
        ))
    # The saved rejected-scope primary had two additional objective conjuncts.
    strategic = next(c for c in components if c.id == 'sicherung_der_sed_herrschaft')
    strategic.description = 'Sicherung der SED-Herrschaft.'
    components.append(strategic.model_copy(update={'id': 'staatlicher_fortbestand', 'description': 'Sicherung des Fortbestands der DDR.'}))
    audit = NecessityAudit(**{**old, 'minimal_answer': CORE, 'components': components,
                             'primary_contains_unnecessary_conjunction': True, 'minimum_necessary_depth': 1})
    repaired = proposed.model_copy(deep=True)
    repaired.primary_answer_obligation.description = CORE
    repaired.required_supporting_obligations = []
    repaired.optional_context += [c.description for c in components if not c.strictly_necessary]
    repaired.historical_motive = 'Die unmittelbare Absicht war, ' + GOAL + '.'
    repaired.causal_mechanistic_chain = [qac.CausalChainStep(id='step_3', description=CORE, depth_level=1)]
    repaired.required_mechanism_concepts = [GOAL]
    repaired.minimum_answer_depth = 1
    return proposed, minimum, audit, repaired


def evidence_report(model, facts):
    """Offline verifier oracle: a verbatim purpose clause is supported; additional
    required qualifiers have no entailment in this fixture. Production still
    performs its own exact-ID/usable-evidence checks on this response.
    """
    supporting = [f['id'] for f in facts if fact_is_supported(f) and GOAL in f['claim']]
    evaluations = []
    for obligation in qac.answer_obligations(model):
        matched = obligation.description == CORE and bool(supporting)
        evaluations.append(qac.ObligationCoverage(
            obligation_id=obligation.id, status='satisfied' if matched else 'missing',
            supporting_fact_ids=supporting if matched else [],
            reasoning='Existing sourced fact entails the exact required purpose clause.' if matched else 'No full entailment for this requirement.',
        ))
    missing = [c.obligation_id for c in evaluations if c.status != 'satisfied']
    return qac.ResearchCoverageReport(is_sufficient=not missing, missing_obligations=missing, coverage=evaluations)


def test_actual_v2_audit_misclassified_scope_not_coverage():
    audit = LIVE['minimality']['audit']
    necessary = {c['id'] for c in audit['components'] if c['strictly_necessary']}
    assert {'mass_flucht_als_ausloeser', 'west_berlin_als_fluchtweg',
            'abriegelung_zur_schliessung_des_fluchtwegs', 'sicherung_der_sed_herrschaft'} == necessary
    assert not audit['primary_contains_unnecessary_conjunction']
    assert all(c['correctly_required_or_optional'] for c in LIVE['minimality']['repair_check']['components'])
    assert 'Sicherung der SED-Herrschaft' in LIVE['contract_coverage']['coverage'][0]['reasoning']
    assert not LIVE['contract_coverage']['is_sufficient']


def test_saved_facts_genuinely_satisfy_corrected_minimum_not_bad_contract(monkeypatch):
    proposed, minimum, audit, repaired = berlin_review()
    facts = copy.deepcopy(LIVE['facts'])
    assert next(f for f in facts if f['id'] == 'fact_09')['claim'].find(GOAL) >= 0
    parse = raw_parser(monkeypatch, [proposed, minimum, audit, repaired, passed_check(audit),
                                    evidence_report(repaired, facts), evidence_report(proposed, facts),
                                    evidence_report(repaired, [f for f in facts if f['id'] in {'fact_07', 'fact_08'}])])
    final = qac.generate_contract(LIVE['prompt'], 'de', SETTINGS)
    assert final.primary_answer_obligation.description == CORE and final.primary_answer_obligation.is_required
    assert not final.required_supporting_obligations
    required = [CORE, final.historical_motive, *final.required_mechanism_concepts,
                *[step.description for step in final.causal_mechanistic_chain]]
    assert all('SED' not in text and 'entscheidende' not in text and 'Fortbestand' not in text for text in required)
    assert any('SED' in text for text in final.optional_context)
    assert any('entscheidende' in text for text in final.optional_context)
    assert final._minimality_review['proposed_contract'] == proposed.model_dump()
    assert final._minimality_review['question_minimum'] == minimum.model_dump()
    # Full production coverage, including grounding validation, no web research.
    report = qac.evaluate_research_coverage(final, facts, SETTINGS)
    assert report.is_sufficient and report.missing_obligations == []
    assert report.coverage[0].supporting_fact_ids == ['fact_09']
    assert not qac.evaluate_research_coverage(proposed, facts, SETTINGS).is_sufficient
    assert not qac.evaluate_research_coverage(final, [f for f in facts if f['id'] in {'fact_07', 'fact_08'}], SETTINGS).is_sufficient
    assert facts == LIVE['facts']
    assert parse.call_count == 8  # five bounded contract calls plus three coverage probes
    blind_payload = json.loads(parse.call_args_list[1].kwargs['messages'][1]['content'])
    assert blind_payload == {'original_question': LIVE['prompt'], 'target_language': 'de'}
    assert 'Facts:' in parse.call_args_list[5].kwargs['messages'][1]['content']


@pytest.mark.parametrize('error', ['old_scope', 'counterfactual', 'compound', 'lost_minimum', 'unknown_minimum'])
def test_inconsistent_or_non_atomic_audit_cannot_enter_research(monkeypatch, error):
    proposed, minimum, audit, _ = berlin_review()
    optional = next(c for c in audit.components if c.id == 'sicherung_der_sed_herrschaft')
    if error == 'old_scope':
        optional.strictly_necessary = True  # the actual V2 classification
        optional.omission_still_answers_question = False
    elif error == 'counterfactual':
        optional.strictly_necessary = True  # omission answer still resolves question
        optional.minimum_answer_component_ids = ['immediate_motive']
    elif error == 'compound':
        optional.atomic = False
    elif error == 'lost_minimum':
        audit.components[0].strictly_necessary = False
        audit.components[0].omission_still_answers_question = True
        audit.components[0].minimum_answer_component_ids = []
    else:
        optional.minimum_answer_component_ids = ['invented_scope']
    parse = raw_parser(monkeypatch, [proposed, minimum, audit])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['prompt'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_stage'] == 'necessity_audit'
    assert parse.call_count == 3


@pytest.mark.parametrize('question,description,language', [
    ('Warum hielt die DDR-Führung Massenflucht für eine Gefahr für den Fortbestand ihrer Herrschaft?',
     'Explain the threat to regime survival from mass emigration.', 'de'),
    ('Why did the leadership consider emigration a threat to regime survival?',
     'Explain the threat to regime survival from mass emigration.', 'en'),
    ('Warum war West-Berlin ein wichtiger Fluchtweg?', 'Explain escape-route geography and accessibility.', 'de'),
    ('Why was West Berlin an important escape route?', 'Explain escape-route geography and accessibility.', 'en'),
])
def test_explicit_objective_and_route_questions_remain_required(monkeypatch, question, description, language):
    model = contract(question, description)
    model.question_type = 'historical_motive'
    audit = approved_audit(model)
    minimum = minimum_for(model, audit)
    minimum.explicit_constraints = [description]
    parse = raw_parser(monkeypatch, [model, minimum, audit])
    final = qac.generate_contract(question, language, SETTINGS)
    assert final.primary_answer_obligation.description == description and final.primary_answer_obligation.is_required
    assert parse.call_count == 3


@pytest.mark.parametrize('question,chain,language', [
    ('Why did grain shortages and trade restrictions lead the council to introduce rationing?',
     ['Reduced supply', 'Restricted imports', 'Rationing intended to distribute scarce food'], 'en'),
    ('Warum führten Ernteausfälle und Handelsbeschränkungen zur Einführung von Rationierung?',
     ['Geringere Versorgung', 'Beschränkte Einfuhr', 'Rationierung zur Verteilung knapper Nahrung'], 'de'),
    ('How does an undersea earthquake create a tsunami that grows near shore?',
     ['Seafloor displacement', 'Water column displacement', 'Propagation', 'Shoaling'], 'en'),
])
def test_genuinely_necessary_multistep_answers_keep_their_chain(monkeypatch, question, chain, language):
    model = contract(question, '; '.join(chain))
    model.minimum_answer_depth = len(chain)
    model.causal_mechanistic_chain = [qac.CausalChainStep(id=str(i), description=text, depth_level=i)
                                    for i, text in enumerate(chain, 1)]
    audit = approved_audit(model)
    parse = raw_parser(monkeypatch, [model, minimum_for(model, audit), audit])
    final = qac.generate_contract(question, language, SETTINGS)
    assert final.causal_mechanistic_chain == model.causal_mechanistic_chain
    assert final.minimum_answer_depth == len(chain) and parse.call_count == 3


@pytest.mark.parametrize('failure', ['lost_motive', 'retained_objective', 'new_unnecessary_requirement'])
def test_independent_repair_check_rejects_semantic_failures(monkeypatch, failure):
    proposed, minimum, audit, repaired = berlin_review()
    check = passed_check(audit)
    if failure == 'lost_motive':
        check.preserves_primary_cause_or_motive = False
    elif failure == 'retained_objective':
        next(c for c in check.components if c.component_id == 'sicherung_der_sed_herrschaft').correctly_required_or_optional = False
    else:
        check.minimally_sufficient = False
    parse = raw_parser(monkeypatch, [proposed, minimum, audit, repaired, check])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['prompt'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_stage'] == 'repair_check' and parse.call_count == 5
    prompt = parse.call_args.kwargs['messages'][0]['content']
    assert 'Recheck necessity, not merely compliance' in prompt


@pytest.mark.parametrize('failure', [None, RuntimeError('review unavailable'), {'minimal_answer': 'incomplete'}])
def test_independent_minimum_failure_is_bounded_and_fail_closed(monkeypatch, failure):
    proposed, _, _, _ = berlin_review()
    parse = raw_parser(monkeypatch, [proposed, failure])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['prompt'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_stage'] == 'question_minimum' and parse.call_count == 2


@pytest.mark.parametrize('failure', ['duplicate_ids', 'semantics_lost', 'explicit_constraint_lost'])
def test_invalid_independent_minimum_is_rejected(monkeypatch, failure):
    proposed, minimum, _, _ = berlin_review()
    if failure == 'duplicate_ids':
        minimum.necessary_components.append(minimum.necessary_components[0].model_copy())
    elif failure == 'semantics_lost':
        minimum.preserves_question_semantics = False
    else:
        minimum.preserves_explicit_constraints = False
    parse = raw_parser(monkeypatch, [proposed, minimum])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['prompt'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_code'] == 'QUESTION_MINIMUM_REJECTED'
    assert parse.call_count == 2


def test_minimum_failure_persists_sanitized_diagnostics_and_blocks_readiness(monkeypatch):
    from test_question_answer_contract import install_pipeline

    from clipforge import pipeline
    from clipforge.config import Settings
    from clipforge.schemas import AdvancedOptions

    calls, _, _ = install_pipeline(monkeypatch)
    monkeypatch.setattr(pipeline, 'generate_contract', qac.generate_contract)
    proposed, _, _, _ = berlin_review()
    parse = raw_parser(monkeypatch, [proposed, RuntimeError('PRIVATE reviewer detail; api_key=sk-secret-scope')])
    state = pipeline.build_initial_state(LIVE['prompt'], AdvancedOptions(language='de', research='on'),
        Settings(clipforge_ai_mode='local', openai_api_key='fixture'))
    assert state['contract'] is None and not state['script']['readiness']['ready']
    diagnostic = state['research']['diagnostics']['contract_failure']
    assert diagnostic['failure_stage'] == 'question_minimum'
    assert 'sk-secret-scope' not in json.dumps(diagnostic)
    assert 'PRIVATE' not in state['script']['readiness']['message']
    assert parse.call_count == 2
    assert all('question_answer_contract' not in context for context in calls)
