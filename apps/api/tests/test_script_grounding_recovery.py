"""V4 production trace and generic grounded recovery; no live providers."""
import copy
import json
from pathlib import Path

import pytest
from test_question_answer_contract import contract, coverage
from test_script_story_rewrite import FakeEditor, fact, rewrite, verdict

from clipforge import pipeline
from clipforge.config import Settings
from clipforge.schemas import AdvancedOptions
from clipforge.script_story_quality import script_quality_signature
from clipforge.script_story_rewrite import (
    REWRITE_INSTRUCTIONS,
    ContractObligationEvaluation,
    EditorFinding,
    run_script_story_quality,
)
from clipforge.script_writer import (
    SCRIPT_WRITER_V2_INSTRUCTIONS,
    ScriptBlockV2,
    ScriptDraftV2,
    ScriptWriterResult,
    _request_for_model,
)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/mars_qac_v4_grounding.json').read_text())


def context(language='de', question=None, cause=None):
    model = contract(question or FIXTURE['prompt'], cause or 'Eisenoxide erzeugen die rote Erscheinung.')
    return {
        'prompt': model.core_question, 'intent': {'language': language}, 'facts': FIXTURE['facts'],
        'question_answer_contract': model.model_dump(), 'research_coverage': coverage(['fact_01','fact_09']).model_dump(),
        'word_budget': 160,
    }


def good_beats():
    # Use a factual hook's actual evidence; body retains the direct cause.
    return [
        ('hook', 'Wie kann Staub eine ganze Welt aus der Ferne rötlich wirken lassen?', ['fact_09']),
        ('answer', 'Der Mars erscheint rot, weil sein Boden hauptsächlich aus Eisenoxid besteht.', ['fact_01']),
        ('explanation', 'Eisen reagiert und bildet Oxide, ähnlich wie ein altes Fahrrad im Regen.', ['fact_02']),
        ('payoff', 'Feiner Eisenoxidstaub bedeckt weite Landschaften. Deshalb wirkt der ganze Planet rötlich.', ['fact_09']),
    ]


def checked(beats, status='satisfied', **kwargs):
    return verdict(beats, contract_evaluations=[ContractObligationEvaluation(
        id='primary', status=status, is_primary=True, is_required=True,
        reasoning='The actual direct cause is retained.' if status == 'satisfied' else 'A related precursor does not explain the appearance.',
    )], **kwargs)


def initial():
    return [{'id':'draft_hook','role':'hook','text':'Wie kommt diese Farbe zustande?','fact_ids':[]},
            {'id':'draft_payoff','role':'payoff','text':'Im Gestein steckt Eisen, darum ist der Planet rot.','fact_ids':['fact_03']}]


def test_v4_writer_request_now_runs_and_keeps_primary_support_with_eleven_facts():
    seen = []
    class Writer:
        name = 'fixture'
        def generate(self, request):
            seen.append(request)
            return ScriptWriterResult(status='success', draft=ScriptDraftV2(language='de', blocks=[
                ScriptBlockV2(role='answer', text=good_beats()[1][1], fact_ids=['fact_01']),
                ScriptBlockV2(role='payoff', text=good_beats()[-1][1], fact_ids=['fact_09']),
            ]))
    assert FIXTURE['writer_diagnostics']['reason'] == 'request_validation_failed'
    assert len(FIXTURE['facts']) == 11
    # Put required facts at the tail: selection must preserve them before capping.
    facts = sorted(copy.deepcopy(FIXTURE['facts']), key=lambda f:f['id'] in {'fact_01','fact_09'})
    ctx = context()
    blocks, diagnostics = pipeline._generate_body_with_v2_or_fallback(
        FIXTURE['prompt'], {'language':'de','content_type':'factual_explainer'}, AdvancedOptions(),
        Settings(openai_api_key='fixture'), facts, initial(), provider=Writer(),
        question_answer_contract=ctx['question_answer_contract'], research_coverage=ctx['research_coverage'],
    )
    assert diagnostics['status'] == 'v2_success'
    assert len(seen[0].facts) == 10
    assert {'fact_01','fact_09'} <= {f.id for f in seen[0].facts}
    payload = _request_for_model(seen[0])
    assert payload['required_answer_evidence'][0]['supporting_fact_ids'] == ['fact_01','fact_09']
    assert 'Eisenoxid' in blocks[0]['text']


def test_v4_rewrites_preserved_cause_but_uncited_hooks_were_rejected():
    for attempt in FIXTURE['attempts']:
        assert attempt['blocks'][0]['fact_ids'] == []
        assert any('Eisenoxid' in block['text'] for block in attempt['blocks'][1:])
        assert attempt['contract_evaluations'][0]['status'] == 'satisfied'
        assert any(finding['code'] == 'ungrounded' for finding in attempt['hard'])


def test_grounded_rewrite_replaces_precursor_with_actual_cause():
    beats = good_beats()
    editor = FakeEditor(rewrites=[rewrite(beats)], verdicts=[checked(beats)])
    final, report = run_script_story_quality(initial(), context(), provider=editor)
    assert report['gate']['ready']
    assert 'Eisenoxid' in ' '.join(b['text'] for b in final)
    assert final[0]['fact_ids'] == ['fact_09']
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)
    brief = editor.calls[1][1]
    assert brief['required_answer_evidence'][0]['supporting_fact_ids'] == ['fact_01','fact_09']
    assert brief['grounding_policy']['factual_hook_requires_supporting_fact_ids']


def test_dropped_direct_cause_is_rejected_then_repaired_within_budget():
    bad = [('hook', 'Wie kommt diese Farbe zustande?', []),
           ('payoff', 'Im Gestein steckt Eisen, darum ist der Planet rot.', ['fact_03'])]
    good = good_beats()
    editor = FakeEditor(rewrites=[rewrite(bad), rewrite(good)], verdicts=[checked(bad,'missing'), checked(good)])
    final, report = run_script_story_quality(initial(), context(), provider=editor)
    assert report['gate']['ready'] and report['rewrite']['attempt'] == 2
    assert 'Eisenoxid' in final[-1]['text']
    repair = [brief for stage,brief in editor.calls if stage == 'rewrite'][1]
    assert repair['failed_obligations'][0]['supporting_fact_ids'] == ['fact_01','fact_09']


@pytest.mark.parametrize('hook_ids', [[], ['fact_01']])
def test_missing_or_unrelated_hook_citations_cannot_authorize_unsupported_claim(hook_ids):
    beats = good_beats()
    beats[0] = ('hook', 'Mars produces its own red laser light.', hook_ids)
    rejected = checked(beats, grounded=False, findings=[EditorFinding(
        code='unsupported_hook', severity='hard', beat_index=1, message='The claim is not supported by any research fact.',
    )])
    editor = FakeEditor(rewrites=[rewrite(beats)]*3, verdicts=[rejected]*3)
    _final, report = run_script_story_quality(initial(), context(), provider=editor)
    assert not report['gate']['ready']
    assert 'rewrite' not in report
    assert 'Eisenoxid' in ' '.join(block['text'] for block in _final)
    assert report['holistic']['rejected_candidate_preserved']['attempt'] == 3
    assert len([stage for stage,_ in editor.calls if stage == 'rewrite']) == 3
    fresh = [brief for stage,brief in editor.calls if stage == 'rewrite'][-1]
    assert fresh['grounding_policy']['citations_must_support_the_actual_claim']
    assert fresh['required_answer_evidence'][0]['supporting_facts']


@pytest.mark.parametrize('language,question,cause,hook,payoff', [
    ('de', 'Warum ist der Mars rot?', 'Eisenoxide erzeugen die rote Erscheinung.', 'Wie entsteht diese Farbe?', 'Eisenoxid im Boden macht den Mars rot.'),
    ('en', 'Why does the material appear red?', 'The red compound causes the color.', 'What gives it this color?', 'The surface contains a red compound, which gives the material its color.'),
    ('de', 'Warum wurde die Berliner Mauer gebaut?', 'Die unmittelbare Absicht war, Abwanderung zu stoppen.', 'Welche Absicht steckte dahinter?', 'Die Mauer wurde errichtet, um die Abwanderung zu stoppen.'),
])
def test_short_complete_answer_and_historical_motive_survive(language, question, cause, hook, payoff):
    ctx = context(language,question,cause)
    ctx['facts'] = [fact('fact_01',payoff)]
    ctx['research_coverage'] = coverage(['fact_01']).model_dump()
    beats = [('hook',hook,[]),('payoff',payoff,['fact_01'])]
    editor = FakeEditor(rewrites=[rewrite(beats)], verdicts=[checked(beats)])
    final, report = run_script_story_quality(initial(),ctx,provider=editor)
    assert report['gate']['ready']
    assert final[-1]['text'] == payoff
    assert report['rewrite']['verified_by'] == 'ai_verifier'
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)


def test_hook_mapping_preserves_actual_block_citations_and_drops_stale_ids():
    blocks = [{'role':'hook','text':'A supported factual premise.','fact_ids':['premise']},
              {'role':'answer','text':'An actual supported cause.','fact_ids':['cause']}]
    unchanged = pipeline._apply_selected_hook(blocks, blocks[0]['text'])
    assert unchanged[0]['fact_ids'] == ['premise']
    changed = pipeline._apply_selected_hook(blocks, 'A different unsupported claim.')
    assert not changed[0].get('fact_ids')
    # General hook-plan/payoff support must not be invented as hook-claim citations.
    hooked = pipeline._hooked_blocks(blocks[1:], 'A nonfactual question?')
    assert not hooked[0].get('fact_ids')


def test_writer_and_rewriter_prompts_preserve_grounded_essentials():
    assert 'Factual claims or premises in the hook ALSO cite' in REWRITE_INSTRUCTIONS
    assert 'purely nonfactual rhetorical' in REWRITE_INSTRUCTIONS
    assert 'related precursor' in REWRITE_INSTRUCTIONS
    assert 'required_answer_evidence' in SCRIPT_WRITER_V2_INSTRUCTIONS
    assert 'related precursor' in SCRIPT_WRITER_V2_INSTRUCTIONS


def test_uncited_supported_factual_hook_is_repaired_with_its_actual_fact():
    bad = good_beats()
    bad[0] = (bad[0][0],bad[0][1],[])
    good = good_beats()
    rejected = checked(bad,grounded=False,findings=[EditorFinding(
        code='missing_hook_citation',severity='hard',beat_index=1,message='The factual dust premise needs fact_09.',
    )])
    editor = FakeEditor(rewrites=[rewrite(bad),rewrite(good)],verdicts=[rejected,checked(good)])
    final, report = run_script_story_quality(initial(),context(),provider=editor)
    assert report['gate']['ready'] and report['rewrite']['attempt'] == 2
    assert final[0]['fact_ids'] == ['fact_09']
    attempts = report['holistic']['attempts']
    assert not attempts[0]['verification_passed']
    assert attempts[0]['verification_flags']['grounded'] is False
    assert attempts[1]['verification_passed']
    verified_candidates = [brief['candidate']['beats'] for stage,brief in editor.calls if stage == 'verify']
    assert verified_candidates[0][0]['fact_ids'] == []
    assert verified_candidates[1][0]['fact_ids'] == ['fact_09']


def test_verifier_outage_never_creates_approval_or_signature():
    editor = FakeEditor(rewrites=[rewrite(good_beats())]*3,fail_on='verify')
    _final, report = run_script_story_quality(initial(),context(),provider=editor)
    assert not report['gate']['ready']
    assert not report.get('rewrite',{}).get('verified_script_signature')
    assert all(not attempt['verification_passed'] for attempt in report['holistic']['attempts'])
