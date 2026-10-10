"""Actual V5 failures plus bounded recovery of independently supported content."""
import copy
import json
from pathlib import Path

import pytest
from test_script_story_rewrite import FakeEditor, rewrite, verdict

from clipforge import pipeline
from clipforge.config import Settings
from clipforge.narration import split_sentences
from clipforge.readiness import content_readiness, not_ready_message
from clipforge.schemas import AdvancedOptions
from clipforge.script_grounding import evidence_key
from clipforge.script_story_quality import assess_script_story_quality, script_quality_signature
from clipforge.script_story_rewrite import (
    CriticResponse,
    _grounding_repair,
    build_brief,
    run_script_story_quality,
    verify_current_script,
)
from clipforge.script_writer import ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import editorial_payoff_plan, story_brief

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v5_recovery.json').read_text())


def context():
    result = copy.deepcopy({
        'prompt': FIXTURE['prompt'], 'intent': FIXTURE['intent'], 'facts': FIXTURE['facts'],
        'question_answer_contract': FIXTURE['contract'], 'research_coverage': FIXTURE['coverage'],
        'story_arc': FIXTURE['story_arc'], 'payoff_plan': FIXTURE['payoff_plan'],
        'word_budget': 250, 'script': {'triple_hook': FIXTURE['hook']},
    })
    result['script']['selected_hook'] = FIXTURE['selected_hook']['verbal_hook']
    result['script']['triple_hook']['verbal_hook'] = FIXTURE['selected_hook']['verbal_hook']
    return result


def initial_blocks():
    hook = FIXTURE['selected_hook']
    return pipeline._normalise_blocks(pipeline._hooked_blocks(
        copy.deepcopy(FIXTURE['writer']['reviewed_draft']), hook['verbal_hook'], FIXTURE['story_arc'],
        fact_ids=hook['supported_by_fact_ids'],
    ), 90, facts=FIXTURE['facts'])


def beats(blocks):
    return [(block['role'], block['text'], block['fact_ids']) for block in blocks]


def actual_verdict(attempt):
    # Retain every recorded flag and independent finding; do not manufacture
    # a successful verdict for any of the three real rejected candidates.
    flags = attempt['verification_flags']
    findings = [
        {key: item[key] for key in ('code', 'severity', 'beat_index', 'message')}
        for item in [*attempt['hard'], *attempt['major'], *attempt['minor']]
        if item['source'] == 'verifier' and item['code'] not in {'ungrounded', 'answer_payoff_duplicate', 'claim_unsupported', 'claim_unverifiable'}
    ]
    return verdict(beats(attempt['blocks']), **flags, findings=findings,
                   contract_evaluations=attempt['contract_evaluations'],
                   claim_grounding=attempt['claim_grounding']['evaluations'],
                   answer_payoff_duplicate=any(item['code'] == 'answer_payoff_duplicate' for item in attempt['hard']))


def supported_verdict(blocks):
    """Mock a repaired hook, retaining the V5 body reviews verbatim."""
    original = FIXTURE['recovery']['attempts'][0]
    reviews = [copy.deepcopy(item) for item in original['claim_grounding']['evaluations'] if item['beat_index'] != 1]
    reviews.extend(copy.deepcopy(item) for item in FIXTURE['recovery']['attempts'][1]['claim_grounding']['evaluations']
                   if item['beat_index'] == 1)
    assert {item['sentence'] for item in reviews} == {
        sentence for block in blocks for sentence in split_sentences(block['text'])
    }
    return verdict(beats(blocks), claim_grounding=reviews, contract_evaluations=original['contract_evaluations'])


def repaired_candidate():
    blocks = copy.deepcopy(FIXTURE['recovery']['attempts'][0]['blocks'])
    blocks[0] = copy.deepcopy(FIXTURE['recovery']['attempts'][1]['blocks'][0])
    return blocks


@pytest.mark.parametrize('index', [0, 1, 2])
def test_actual_v5_candidates_remain_rejected(index):
    attempt = FIXTURE['recovery']['attempts'][index]
    provider = FakeEditor(verdicts=[actual_verdict(attempt)])
    report = verify_current_script(attempt['blocks'], context(), provider)
    assert provider.stages() == ['verify']
    assert not report['gate']['ready']
    assert report['rewrite']['verified_by'] is None
    actual_codes = {item['code'] for item in attempt['hard']}
    assert actual_codes <= {item['code'] for item in report['verification_diagnostics']['findings']}
    assert all(item['status'] == 'satisfied' for item in attempt['contract_evaluations'])


def test_original_writer_output_and_legacy_wrong_endpoint_are_preserved_in_fixture():
    writer = FIXTURE['writer']
    assert writer['status'] == 'v2_success'
    assert writer['writer_draft'][-1]['fact_ids'] == ['fact_04']
    assert 'nur gelangen' in writer['writer_draft'][-1]['text']
    assert writer['review']['issues'][0]['code'] == 'OFF_TOPIC_PAYOFF'
    assert writer['reviewed_draft'][-1]['fact_ids'] == ['fact_01', 'fact_03']
    assert FIXTURE['story_arc']['question_contract']['final_resolution_id'] == 'fact_04'
    # Evidence about one escape is not evidence that every escape needed it.
    assert 'die Flucht' in next(f['claim'] for f in FIXTURE['facts'] if f['id'] == 'fact_04')


def test_qac_writer_request_does_not_require_the_wrong_legacy_payoff():
    class Writer:
        name = 'offline'

        def generate(self, request):
            self.request = request
            return ScriptWriterResult(draft=ScriptDraftV2(language='de', blocks=FIXTURE['writer']['writer_draft']), status='connected')

    writer = Writer()
    ctx = context()
    pipeline._generate_body_with_v2_or_fallback(
        FIXTURE['prompt'], ctx['intent'], AdvancedOptions(), Settings(openai_api_key='test-key'),
        ctx['facts'], [], payoff_plan=ctx['payoff_plan'], story_arc=ctx['story_arc'],
        question_answer_contract=ctx['question_answer_contract'], research_coverage=ctx['research_coverage'], provider=writer,
    )
    brief = writer.request.story_arc
    assert brief['requirement_authority'] == 'question_answer_contract'
    assert brief['final_payoff_id'] is None
    assert 'final_resolution_id' not in brief['question_contract']
    assert brief['question_contract']['minimum_answer_depth'] == 1
    assert brief['question_contract']['required_supporting_obligations'] == []
    assert brief['question_contract']['excluded_interpretations'] == FIXTURE['story_arc']['question_contract']['excluded_interpretations']
    assert all(item['may_be_omitted'] for item in brief['information_order'] if item['fact_id'] != 'fact_03')
    assert writer.request.required_answer_evidence[0]['supporting_fact_ids'] == ['fact_01', 'fact_03', 'fact_06']
    assert 'final_payoff_id' not in writer.request.payoff_plan
    assert ctx['story_arc'] == FIXTURE['story_arc']


def test_grounding_only_repair_preserves_supported_text_and_citations_and_reverifies():
    first = FIXTURE['recovery']['attempts'][0]
    candidate = repaired_candidate()
    proposed = copy.deepcopy(candidate)
    proposed[2].update(text='Die Regierung baute die Sperre im Jahr 9999.', fact_ids=['unknown'])
    provider = FakeEditor(critic_response=CriticResponse(**FIXTURE['recovery']['critic']),
                          rewrites=[rewrite(beats(first['blocks'])), rewrite(beats(proposed))],
                          verdicts=[actual_verdict(first), lambda brief: supported_verdict([
                              {'role': b['role'], 'text': b['text'], 'fact_ids': b['fact_ids']} for b in brief['candidate']['beats']])])
    final, report = run_script_story_quality(initial_blocks(), context(), provider)
    assert report['gate']['ready'], report['holistic']
    assert provider.stages() == ['critique', 'rewrite', 'verify', 'rewrite', 'verify']
    repair = provider.calls[3][1]['grounding_repair']
    assert repair['repair_beat_indices'] == [1]
    assert repair['preserve_beat_indices'] == list(range(2, 8))
    assert final[1:] == first['blocks'][1:]
    assert final[0]['fact_ids'] == ['fact_03']
    assert '9999' not in json.dumps(final)
    assert provider.calls[-1][1]['candidate']['beats'] == [
        {'index': index, 'role': b['role'], 'text': b['text'], 'fact_ids': b['fact_ids']} for index, b in enumerate(final, 1)]
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)
    assert report['rewrite']['verified_evidence_key'] == evidence_key(final, context()['facts'])
    state = {**context(), 'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage'],
             'explanation_audit': report['holistic']['explanation_audit'],
             'script': {'blocks': final, 'script_story_quality_v1': report}}
    assert content_readiness(state)['ready']


@pytest.mark.parametrize('defect', ['review_mismatch', 'essential_missing', 'repetition', 'incomplete', 'reveal'])
def test_scoped_repair_cannot_preserve_unestablished_or_incomplete_content(defect):
    attempt = copy.deepcopy(FIXTURE['recovery']['attempts'][0])
    if defect == 'review_mismatch':
        attempt['claim_grounding']['evaluations'][1]['sentence'] = 'Different text.'
    elif defect == 'essential_missing':
        attempt['failed_obligations'] = [{'id': 'primary_motive', 'status': 'missing'}]
    elif defect == 'repetition':
        attempt['hard'].append({'code': 'answer_payoff_duplicate', 'beat_index': None})
    elif defect == 'incomplete':
        attempt['claim_grounding']['evaluations'].pop()
    else:
        attempt['verification_flags']['premature_reveal'] = True
    assert _grounding_repair(attempt, context()) is None


def test_failed_scoped_repair_and_fresh_generation_remain_bounded_and_fail_closed():
    attempts = FIXTURE['recovery']['attempts']
    provider = FakeEditor(critic_response=CriticResponse(**FIXTURE['recovery']['critic']),
                          rewrites=[rewrite(beats(a['blocks'])) for a in attempts],
                          verdicts=[actual_verdict(attempts[0]), actual_verdict(attempts[2])])
    final, report = run_script_story_quality(initial_blocks(), context(), provider)
    assert not report['gate']['ready']
    assert report['holistic']['status'] == 'needs_fix'
    assert len(report['holistic']['attempts']) == 3
    assert any(f['code'] == 'grounding_repair_structure' for f in report['holistic']['attempts'][1]['hard'])
    assert 'answer_payoff_duplicate' in {f['code'] for f in report['holistic']['attempts'][2]['hard']}
    fresh = [brief for stage, brief in provider.calls if stage == 'rewrite'][2]
    assert fresh['recovery_constraints'] == ['claim_grounding', 'hook_grounding']
    assert 'previous_rewrite' not in fresh and 'verifier_findings' not in fresh and 'grounding_repair' not in fresh
    assert attempts[0]['blocks'][0]['text'] not in json.dumps(fresh, ensure_ascii=False)
    # Production finalization binds rejected reports to the selected script
    # too, without granting an independent verification signature.
    report['final_signature'] = script_quality_signature(final)
    state = {**context(), 'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage'],
             'script': {'blocks': final, 'script_story_quality_v1': report}}
    readiness = content_readiness(state)
    assert readiness['failure_category'] == 'script_recovery_failed' and not readiness['research_required']


@pytest.mark.parametrize('language', ['de', 'en'])
def test_script_error_copy_does_not_claim_that_research_is_missing(language):
    message = not_ready_message({'language': language, 'failure_category': 'script_recovery_failed'})
    assert 'Text' in message if language == 'de' else 'script' in message
    research = not_ready_message({'language': language, 'failure_category': 'research_required'})
    assert 'belegte Antwort' in research if language == 'de' else 'supported answer' in research
    assert FIXTURE['recovery']['attempts'][2]['hard'][0]['message'] not in message


def test_multistep_contract_and_reveal_protections_survive_provider_projection():
    contract = copy.deepcopy(FIXTURE['contract'])
    contract['question_type'] = 'scientific_causal'
    contract['minimum_answer_depth'] = 4
    contract['causal_mechanistic_chain'] = ['cause', 'process', 'intermediate effect', 'result']
    contract['required_supporting_obligations'] = [{'id': 'step', 'description': 'Explain the intermediate process.', 'is_required': True}]
    arc = copy.deepcopy(FIXTURE['story_arc'])
    arc['curiosity_gap']['withhold_answer'] = True
    projected = story_brief(arc, question_answer_contract=contract)
    assert projected['question_contract']['minimum_answer_depth'] == 4
    assert projected['question_contract']['causal_mechanistic_chain'] == contract['causal_mechanistic_chain']
    assert projected['question_contract']['required_supporting_obligations'] == contract['required_supporting_obligations']
    assert projected['curiosity_gap']['withhold_answer']
    payoff = editorial_payoff_plan({'hook_must_not_reveal': 'protected result', 'reveal_policy': 'late'}, contract)
    assert payoff['hook_must_not_reveal'] == 'protected result'
    assert payoff['reveal_policy'] == 'late'
    assert story_brief(arc) == story_brief(arc, question_answer_contract=None)
    assert editorial_payoff_plan(None, None) is None


def test_critic_and_fresh_briefs_use_qac_scope_instead_of_legacy_depth():
    ctx = context()
    brief = build_brief(FIXTURE['final_blocks'], ctx, assess_script_story_quality(FIXTURE['final_blocks'], ctx))
    assert brief['question']['intended'] == FIXTURE['prompt']
    assert brief['hook_intent']['hook_promise'] == FIXTURE['prompt']
    assert brief['hook_intent']['curiosity_target'] == FIXTURE['prompt']
    assert brief['payoff_intent']['final_payoff_id'] is None
    assert brief['grounding_policy']['positive_purpose_does_not_establish_an_exclusive_negative_contrast']
    assert brief['required_answer_evidence'][0]['is_required']


def test_missing_independent_verification_still_blocks():
    items = repaired_candidate()
    report = verify_current_script(items, context(), None)
    assert not report['rewrite']['verified_by']
    state = {**context(), 'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage'],
             'script': {'blocks': items, 'script_story_quality_v1': report}}
    assert not content_readiness(state)['ready']


def test_expanded_candidate_does_not_receive_an_impossible_same_count_repair():
    attempt = copy.deepcopy(FIXTURE['recovery']['attempts'][0])
    for index in range(8, 18):
        block = copy.deepcopy(attempt['blocks'][1])
        block['id'] = f'voice_block_{index:02d}'
        attempt['blocks'].append(block)
        review = copy.deepcopy(attempt['claim_grounding']['evaluations'][1])
        review['beat_index'] = index
        attempt['claim_grounding']['evaluations'].append(review)
    attempt['claim_grounding']['evidence_key'] = evidence_key(attempt['blocks'], context()['facts'])
    assert _grounding_repair(attempt, context()) is None
