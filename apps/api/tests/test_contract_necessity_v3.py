"""Native V3 responses/rejection; corrected examples are explicitly mocked.

The fixture preserves all five responses and full failure diagnostics. Repeated
request prompts are omitted. No live AI is used or native verdict overwritten.
"""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_contract_minimality import SETTINGS, approved_audit, minimum_for, passed_check
from test_contract_necessity_v2 import raw_parser
from test_question_answer_contract import contract

from clipforge import question_answer_contract as qac
from clipforge.contract_diagnostics import ContractGenerationFailure
from clipforge.contract_minimality import NecessityAudit, QuestionMinimum, RepairCheck

LIVE = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v3_necessity.json').read_text())
CAUSE = 'Die Berliner Mauer wurde gebaut, um die Flucht von DDR-Bürgern zu stoppen.'


def proposed_contract():
    return qac.QuestionAnswerContract.model_validate(LIVE['calls'][0]['response'])


def minimum_with_counterfactuals():
    """Use actual audit omission answers, classified by final live check reasoning."""
    data = copy.deepcopy(LIVE['calls'][1]['response'])
    audit = LIVE['calls'][2]['response']
    for component in data['necessary_components']:
        counterpart = next(c for c in audit['components'] if component['id'] in c['minimum_answer_component_ids'])
        component['answer_without_component'] = counterpart['answer_without_component']
        component['omission_still_answers_question'] = component['id'] != 'C3'
        component['omission_failure'] = 'unanswered' if component['id'] == 'C3' else 'none'
    data['explanatory_context'] = []
    return QuestionMinimum.model_validate(data)


def corrected_sequence():
    proposed = proposed_contract()
    minimum = minimum_with_counterfactuals()
    optional = [c.description for c in minimum.necessary_components if c.id != 'C3']
    minimum.necessary_components = [c for c in minimum.necessary_components if c.id == 'C3']
    minimum.minimal_answer = CAUSE
    minimum.question_intent = 'Unmittelbaren Zweck des Mauerbaus erklären.'
    minimum.explicit_constraints = []  # actual ':' was not a user constraint
    minimum.explanatory_context = optional
    audit = NecessityAudit.model_validate(LIVE['calls'][2]['response'])
    audit.minimal_answer = CAUSE
    for component in audit.components:
        if component.id in {'N1', 'N2', 'N4'}:
            component.strictly_necessary = False
            component.semantic_role = 'background'
            component.minimum_answer_component_ids = []
            component.omission_still_answers_question = True
            component.reason = 'Die unmittelbare Absicht beantwortet die Warum-Frage ohne diese Präzisierung.'
    repaired = qac.QuestionAnswerContract.model_validate(LIVE['calls'][3]['response'])
    repaired.optional_context += [c.description for c in repaired.required_supporting_obligations]
    repaired.required_supporting_obligations = []
    repaired.historical_motive = CAUSE
    repaired.required_mechanism_concepts = ['Verhindern der Flucht von DDR-Bürgern']
    return proposed, minimum, audit, repaired


def test_native_v3_trace_confirms_earliest_scope_error_and_correct_rejection():
    assert LIVE['status'] == 'FAIL' and LIVE['openai_calls'] == 5
    assert LIVE['research_coverage'] == 'NOT_RUN'
    assert [c['stage'] for c in LIVE['calls']] == [
        'contract_planner', 'question_minimum', 'necessity_audit', 'contract_repair', 'repair_check']
    assert all(c['status'] == 'returned' for c in LIVE['calls'])
    assert {c['id'] for c in LIVE['calls'][1]['response']['necessary_components']} == {'C1', 'C2', 'C3', 'C4'}
    assert {c['id'] for c in LIVE['calls'][2]['response']['components'] if c['strictly_necessary']} == {'N1', 'N2', 'N3', 'N4'}
    check = LIVE['calls'][4]['response']
    assert not check['minimally_sufficient']
    assert {c['component_id'] for c in check['components'] if not c['correctly_required_or_optional']} == {'N1', 'N2', 'N4'}
    assert LIVE['error']['diagnostics']['failure_code'] == 'REPAIR_CHECK_REJECTED'


def test_actor_date_route_omission_answers_question_rejected_before_audit(monkeypatch):
    minimum = minimum_with_counterfactuals()
    parse = raw_parser(monkeypatch, [proposed_contract(), minimum])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    diagnostics = caught.value.diagnostics
    assert diagnostics['failure_stage'] == 'question_minimum'
    assert diagnostics['failure_code'] == 'QUESTION_MINIMUM_REJECTED'
    assert {i['component_ids'][0] for i in diagnostics['failed_invariants']} == {'C1', 'C2', 'C4'}
    assert all(i['code'] == 'MINIMUM_COUNTERFACTUAL_NOT_NECESSARY' for i in diagnostics['failed_invariants'])
    assert diagnostics['question_minimum'] == minimum.model_dump()
    assert parse.call_count == 2


def test_corrected_minimum_preserves_purpose_required_and_context_optional(monkeypatch):
    proposed, minimum, audit, repaired = corrected_sequence()
    parse = raw_parser(monkeypatch, [proposed, minimum, audit, repaired, passed_check(audit)])
    result = qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    assert result.primary_answer_obligation.is_required
    assert 'Flucht' in result.primary_answer_obligation.description
    assert result.required_supporting_obligations == []
    assert result.minimum_answer_depth == 1
    assert result.optional_context == repaired.optional_context
    assert minimum.explanatory_context == [c['description'] for c in LIVE['calls'][1]['response']['necessary_components'] if c['id'] != 'C3']
    assert result._minimality_review['question_minimum'] == minimum.model_dump()
    assert parse.call_count == 5
    request = parse.call_args_list[1].kwargs
    assert json.loads(request['messages'][1]['content']) == {'original_question': LIVE['question'], 'target_language': 'de'}
    assert 'temperature' not in request
    instructions = request['messages'][0]['content']
    for phrase in ('explanatory_context', 'Only A', 'unasked who/when/where', 'answer_without_component',
                   'sufficiency, not necessity', 'optional, not forbidden', 'No global depth cap'):
        assert phrase in instructions
    assert request['response_format'] == QuestionMinimum


def test_native_negative_check_still_rejected_after_corrected_minimum(monkeypatch):
    proposed, minimum, audit, repaired = corrected_sequence()
    check = RepairCheck.model_validate(LIVE['calls'][4]['response'])
    parse = raw_parser(monkeypatch, [proposed, minimum, audit, repaired, check])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_code'] == 'REPAIR_CHECK_REJECTED'
    assert not caught.value.diagnostics['verification_flags']['minimally_sufficient']
    assert parse.call_count == 5


@pytest.mark.parametrize('answers,failure', [(True, 'none'), (True, 'incorrect'), (False, 'none')])
def test_inconsistent_minimum_counterfactual_fails_closed(monkeypatch, answers, failure):
    model = contract()
    audit = approved_audit(model)
    minimum = minimum_for(model, audit)
    minimum.necessary_components[0].omission_still_answers_question = answers
    minimum.necessary_components[0].omission_failure = failure
    parse = raw_parser(monkeypatch, [model, minimum])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(model.core_question, 'en', SETTINGS)
    assert caught.value.diagnostics['failed_invariants'][0]['code'] == 'MINIMUM_COUNTERFACTUAL_NOT_NECESSARY'
    assert parse.call_count == 2


@pytest.mark.parametrize('question,answer,remaining,language', [
    ('Wer ordnete den Bau der Berliner Mauer an?', 'Die DDR- und sowjetische Führung ordneten den Bau an.', 'Die Mauer wurde 1961 gebaut.', 'de'),
    ('Who ordered the Berlin Wall construction?', 'The East German and Soviet leadership ordered construction.', 'The wall was built in 1961.', 'en'),
    ('In welchem Jahr wurde die Berliner Mauer gebaut?', 'Sie wurde 1961 gebaut.', 'Sie wurde gebaut, um Flucht zu verhindern.', 'de'),
    ('In which year was the Berlin Wall built?', 'It was built in 1961.', 'It was built to prevent emigration.', 'en'),
    ('Warum wurde die Grenze speziell um West-Berlin geschlossen?', 'Weil diese Grenze den zugänglichen Fluchtweg in den Westen bot.', 'Die Grenze sollte Flucht verhindern.', 'de'),
    ('Why was the border closed specifically around West Berlin?', 'That accessible border offered the escape route to the West.', 'The border was closed to stop emigration.', 'en'),
    ('Welches breitere politische Ziel verfolgte die DDR-Führung?', 'Sie wollte ihre Herrschaft sichern.', 'Sie baute eine Grenze.', 'de'),
    ('What broader political objective did the DDR leadership pursue?', 'It aimed to preserve its rule.', 'It closed a border.', 'en'),
])
def test_explicit_requested_dimensions_remain_necessary(monkeypatch, question, answer, remaining, language):
    model = contract(question, answer)
    audit = approved_audit(model)
    minimum = minimum_for(model, audit)
    minimum.explicit_constraints = [question]
    for component in minimum.necessary_components:
        component.answer_without_component = remaining
        component.necessity_reason = 'The remaining answer omits the explicitly requested dimension.'
    parse = raw_parser(monkeypatch, [model, minimum, audit])
    result = qac.generate_contract(question, language, SETTINGS)
    assert result.primary_answer_obligation.description == answer
    assert result.primary_answer_obligation.is_required
    assert parse.call_count == 3


def test_immediate_motive_cannot_become_optional(monkeypatch):
    proposed, minimum, audit, _ = corrected_sequence()
    essential = next(c for c in audit.components if c.id == 'N3')
    essential.strictly_necessary = False
    essential.minimum_answer_component_ids = []
    essential.omission_still_answers_question = True
    parse = raw_parser(monkeypatch, [proposed, minimum, audit])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    codes = {i['code'] for i in caught.value.diagnostics['failed_invariants']}
    assert {'NO_NECESSARY_PRIMARY_COMPONENT', 'MINIMUM_COMPONENT_NOT_PRESERVED'} <= codes
    assert parse.call_count == 3


def test_missing_new_minimum_fields_cannot_silently_pass():
    with pytest.raises(ValidationError):
        QuestionMinimum.model_validate(LIVE['calls'][1]['response'])
