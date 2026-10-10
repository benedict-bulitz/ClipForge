"""QAC owns necessity; legacy arcs still own ordering of included facts."""
import copy
import json
from pathlib import Path

import pytest
from test_question_answer_contract import evaluated, qac_context
from test_script_story_rewrite import REWRITE_EN, FakeEditor

from clipforge import pipeline
from clipforge.config import Settings
from clipforge.script_story_quality import (
    _dependency_violations,
    _premature_reveal,
    assess_script_story_quality,
    script_quality_signature,
)
from clipforge.script_story_rewrite import build_brief, verify_current_script
from clipforge.story_arc import (
    annotate_story_roles,
    order_blocks_for_reveal,
    story_brief,
    story_script_issues,
)

V6 = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v6_dependencies.json').read_text())


def sample(language='en', *, qac=True):
    question, context, answer = (
        ('Why was the border closed?', 'The government called it a protective barrier.',
         'The border was closed to prevent people from leaving the country.')
        if language == 'en' else
        ('Warum wurde die Grenze geschlossen?', 'Die Regierung nannte sie eine Schutzbarriere.',
         'Die Grenze wurde geschlossen, um Menschen am Verlassen des Landes zu hindern.')
    )
    facts = [{'id': identifier, 'claim': text, 'confidence': .95, 'verification': 'supported',
              'sources': [{'url': f'https://{identifier}.test/source'}]}
             for identifier, text in [('context', context), ('answer', answer)]]
    arc = {
        'primary_question': question, 'primary_answer_id': 'answer', 'final_payoff_id': 'answer',
        'order': ['context', 'answer'], 'curiosity_gap': {'withhold_answer': False},
        'hook': {'protected_ids': ['answer'], 'allowed_ids': ['context']},
        'units': [{**fact, 'role': 'primary_answer' if fact['id'] == 'answer' else 'essential_context',
                   'may_be_omitted': False, 'may_appear_in_hook': fact['id'] == 'context',
                   'depends_on': ['context'] if fact['id'] == 'answer' else []}
                  for fact in facts],
    }
    contract = {
        'core_question': question, 'question_type': 'historical_motive',
        'primary_answer_obligation': {'id': 'purpose', 'description': answer, 'is_primary': True, 'is_required': True},
        'required_supporting_obligations': [], 'optional_context': [context],
        'minimum_answer_depth': 1, 'causal_mechanistic_chain': [],
    }
    return {'prompt': question, 'intent': {'question': question, 'language': language}, 'facts': facts,
            'story_arc': arc, 'question_answer_contract': contract if qac else None,
            'research_coverage': {'is_sufficient': True}, 'script': {}, 'word_budget': 100}


def block(role, text, ids):
    return {'id': role, 'role': role, 'text': text, 'fact_ids': ids}


@pytest.mark.parametrize('language', ['en', 'de'])
def test_optional_context_does_not_become_missing_required_context(language):
    ctx = sample(language)
    blocks = [block('hook', 'Why?' if language == 'en' else 'Warum?', []),
              block('answer', ctx['facts'][1]['claim'], ['answer'])]
    assert not _dependency_violations(blocks, ctx['story_arc'], ctx['question_answer_contract'])
    state = {**ctx, 'script': {'blocks': blocks}}
    assert not any('required_fact_missing:context' == issue for issue in story_script_issues(state))
    annotate_story_roles(state)
    assert state['story_arc']['completeness']['missing_required_ids'] == []
    report = assess_script_story_quality(blocks, ctx)
    assert not any(issue['issue_type'] == 'poor_fact_ordering' for issue in report['issues'])


@pytest.mark.parametrize('language', ['en', 'de'])
@pytest.mark.parametrize('qac', [False, True])
def test_actually_narrated_hook_context_satisfies_dependency(language, qac):
    ctx = sample(language, qac=qac)
    blocks = [block('hook', ctx['facts'][0]['claim'], ['context']),
              block('answer', ctx['facts'][1]['claim'], ['answer'])]
    assert not _dependency_violations(blocks, ctx['story_arc'], ctx['question_answer_contract'])
    assert not any(issue.startswith('fact_before_dependency:') for issue in
                   story_script_issues({**ctx, 'script': {'blocks': blocks}}))


def test_hook_citation_does_not_claim_the_whole_compound_source_was_narrated():
    ctx = sample(qac=False)
    ctx['story_arc']['units'][0]['claim'] += ' Construction required parliamentary approval.'
    blocks = [block('hook', ctx['facts'][0]['claim'], ['context']),
              block('answer', ctx['facts'][1]['claim'], ['answer'])]
    assert _dependency_violations(blocks, ctx['story_arc'])[0]['missing'] == ['context']


def test_legacy_required_dependency_without_qac_is_unchanged():
    ctx = sample(qac=False)
    blocks = [block('answer', ctx['facts'][1]['claim'], ['answer'])]
    assert _dependency_violations(blocks, ctx['story_arc'])[0]['missing'] == ['context']
    assert 'required_fact_missing:context' in story_script_issues({**ctx, 'script': {'blocks': blocks}})
    assert story_brief(ctx['story_arc'])['information_order'][1]['depends_on'] == ['context']


@pytest.mark.parametrize('language', ['en', 'de'])
def test_included_causal_or_chronological_dependencies_still_constrain_order(language):
    ctx = sample(language)
    blocks = [block('answer', ctx['facts'][1]['claim'], ['answer']),
              block('detail', ctx['facts'][0]['claim'], ['context'])]
    assert _dependency_violations(blocks, ctx['story_arc'], ctx['question_answer_contract'])[0]['missing'] == ['context']
    assert 'fact_before_dependency:answer' in story_script_issues({**ctx, 'script': {'blocks': blocks}})
    ctx['story_arc']['curiosity_gap']['withhold_answer'] = True
    assert _premature_reveal(blocks, ctx)
    assert assess_script_story_quality(blocks, ctx)['gate']['blocking'] == ['premature_reveal']
    ordered = order_blocks_for_reveal(blocks, ctx['story_arc'])
    assert [b['role'] for b in ordered] == ['detail', 'answer']
    assert not _premature_reveal(ordered, ctx)


def test_protected_answer_in_hook_remains_blocked_with_qac():
    ctx = sample()
    ctx['story_arc']['curiosity_gap']['withhold_answer'] = True
    blocks = [block('hook', ctx['facts'][1]['claim'], ['answer'])]
    assert _premature_reveal(blocks, ctx)
    assert 'premature_reveal' in assess_script_story_quality(blocks, ctx)['gate']['blocking']


def test_provider_dependencies_are_conditional_and_contract_is_unchanged():
    ctx = sample()
    original = copy.deepcopy(ctx)
    brief = story_brief(ctx['story_arc'], question_answer_contract=ctx['question_answer_contract'])
    context, answer = brief['information_order']
    assert context['may_be_omitted'] and not answer['may_be_omitted']
    assert answer['depends_on'] == [] and answer['order_after_if_included'] == ['context']
    assert brief['question_contract']['primary_answer_obligation']['is_required']
    assert brief['question_contract']['minimum_answer_depth'] == 1
    request = build_brief([], ctx, {})
    assert request['reveal_contract']['must_follow_fact_ids'] == []
    assert request['reveal_contract']['order_after_if_included_fact_ids'] == ['context']
    assert ctx == original


def test_hook_provider_receives_the_same_optional_dependency_policy(monkeypatch):
    ctx = sample()
    requests = []
    def provider(*args, **kwargs):
        requests.append(kwargs)
        return {'status': 'mocked'}
    monkeypatch.setattr(pipeline, 'generate_hook_candidates_with_openai', provider)
    blocks = [block('answer', ctx['facts'][1]['claim'], ['answer'])]
    result = pipeline._generate_hook_candidates(
        blocks, ctx['prompt'], ctx['intent'], ctx['facts'], Settings(), story_arc=ctx['story_arc'],
        question_answer_contract=ctx['question_answer_contract'],
    )
    assert result == {'status': 'mocked'} and len(requests) == 1
    brief = requests[0]['story_arc']
    assert brief['requirement_authority'] == 'question_answer_contract'
    assert brief['information_order'][1]['depends_on'] == []
    assert brief['information_order'][1]['order_after_if_included'] == ['context']


def test_actual_v6_script_loses_misleading_order_and_missing_context_diagnostics():
    ctx = copy.deepcopy(V6)
    ctx['question_answer_contract'] = ctx.pop('contract')
    ctx['research_coverage'] = ctx.pop('contract_coverage')
    ctx['script'] = {'blocks': ctx['blocks']}
    assert len(V6['recorded_order_warnings']) == 2
    assert 'required_fact_missing:fact_05' in V6['story_arc']['script_issues']
    assert not _dependency_violations(ctx['blocks'], ctx['story_arc'], ctx['question_answer_contract'])
    report = assess_script_story_quality(ctx['blocks'], ctx)
    assert not any(issue['issue_type'] == 'poor_fact_ordering' for issue in report['issues'])
    annotate_story_roles(ctx)
    assert ctx['story_arc']['completeness']['missing_required_ids'] == []
    assert not any(issue.startswith('required_fact_missing:') for issue in ctx['story_arc']['script_issues'])
    assert script_quality_signature(ctx['blocks']) == V6['recorded_verified_signature']


@pytest.mark.parametrize('essential', ['primary', 'supporting'])
def test_missing_essential_meaning_remains_blocking_under_independent_verification(essential):
    ctx = qac_context()
    if essential == 'supporting':
        ctx['question_answer_contract']['required_supporting_obligations'][0].update(is_required=True)
    response = evaluated('missing', contract_sufficient=True)
    if essential == 'supporting':
        response.contract_evaluations[0].status = 'satisfied'
        response.contract_evaluations[1].is_required = True
    blocks = [{'id': f'b{i}', 'role': role, 'text': text, 'fact_ids': ids}
              for i, (role, text, ids) in enumerate(REWRITE_EN)]
    provider = FakeEditor(verdicts=[response])
    report = verify_current_script(blocks, ctx, provider)
    assert provider.stages() == ['verify']
    assert not report['gate']['ready']
    assert report['rewrite']['verified_by'] is None
    assert any(item['code'] == ('primary_answer_missing' if essential == 'primary' else 'required_obligation_missing')
               for item in report['verification_diagnostics']['findings'])
