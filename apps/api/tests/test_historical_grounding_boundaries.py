"""Berlin E2E V1 trace, exact citation audit and shared production segmentation."""
import copy
import json
from pathlib import Path

import pytest
from test_question_answer_contract import evaluated, install_pipeline, qac_context
from test_script_story_rewrite import DRAFT_EN, REWRITE_EN, FakeEditor, rewrite, verdict

from clipforge import pipeline
from clipforge.config import Settings
from clipforge.narration import clean_narration_text, split_sentences
from clipforge.novelty import _answer_sufficiency, _context
from clipforge.readiness import content_readiness
from clipforge.script_story_quality import script_quality_signature
from clipforge.script_story_rewrite import (
    VERIFIER_INSTRUCTIONS,
    ContractObligationEvaluation,
    EditorFinding,
    _verifier_findings,
    run_script_story_quality,
    verify_current_script,
)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v1_grounding.json').read_text())


def context():
    return {'prompt': FIXTURE['prompt'], 'intent': {'question': FIXTURE['prompt'], 'language': 'de'},
            'facts': copy.deepcopy(FIXTURE['facts']), 'question_answer_contract': FIXTURE['contract'],
            'research_coverage': FIXTURE['contract_coverage'], 'word_budget': 200}


def beats(hook_ids=None, hook=None):
    return [
        ('hook', hook or FIXTURE['blocks'][0]['text'], ['fact_08'] if hook_ids is None else hook_ids),
        ('answer', 'Der unmittelbare Zweck war: Fluchten aus der DDR in den Westen verhindern.', ['fact_03']),
        ('support', 'Am 13. August 1961 riegelte ihr Bau Ostberlin von allen Übergängen nach Westberlin ab.', ['fact_07']),
        ('payoff', 'Die DDR ließ die Mauer bauen, um den Flüchtlingsstrom zu stoppen.', ['fact_03', 'fact_06']),
    ]


def checked(items, **kwargs):
    return verdict(items, contract_evaluations=[ContractObligationEvaluation(
        id='primary_motive', is_primary=True, is_required=True, status='satisfied', reasoning='Immediate motive present.',
    )], **kwargs)


def blocks(items):
    return [{'id': f'beat_{i}', 'role': role, 'text': text, 'fact_ids': ids}
            for i, (role, text, ids) in enumerate(items, 1)]


@pytest.mark.parametrize('text,expected', [
    ('Am 13. August 1961 begann der Bau. Danach änderte sich die Stadt.',
     ['Am 13. August 1961 begann der Bau.', 'Danach änderte sich die Stadt.']),
    ('Der 3. Versuch gelang. Der 4. Versuch scheiterte.', ['Der 3. Versuch gelang.', 'Der 4. Versuch scheiterte.']),
    ('Es waren 13. Danach ging er.', ['Es waren 13.', 'Danach ging er.']),
    ('Es waren 3.14 Einheiten. Danach 2,5.', ['Es waren 3.14 Einheiten.', 'Danach 2,5.']),
    ('Dr. Meyer nennt z. B. drei Fälle. Das genügt.', ['Dr. Meyer nennt z. B. drei Fälle.', 'Das genügt.']),
    ('Mr. Smith arrived on Aug. 13, 1961. He left on August 14.',
     ['Mr. Smith arrived on Aug. 13, 1961.', 'He left on August 14.']),
    ('Is this true? Yes! It is.', ['Is this true?', 'Yes!', 'It is.']),
])
def test_shared_sentence_boundaries(text, expected):
    assert split_sentences(text) == expected
    assert clean_narration_text(text) == text
    actual = pipeline._normalise_blocks([{'role': 'support', 'text': text, 'fact_ids': ['fact_07']}], 60, 150)
    assert [b['text'] for b in actual] == expected
    assert all(b['fact_ids'] == ['fact_07'] for b in actual)


def test_live_fixture_locates_date_and_aggregate_grounding():
    original = FIXTURE['writer_diagnostics']['writer_draft']
    assert any('13. August' in b['text'] for b in original)
    assert 'Am 13.' in [b['text'] for b in FIXTURE['blocks']]
    diagnostics = FIXTURE['verification_diagnostics']
    assert {'grounding_hook_actor', 'fragmented_date', 'ungrounded'} <= {f['code'] for f in diagnostics['findings']}
    generic = _verifier_findings(checked(beats(), grounded=False))
    assert any(f['code'] == 'ungrounded' and f['severity'] == 'hard' for f in generic)


@pytest.mark.parametrize('ids', [[], ['fact_08']])
def test_hook_actor_without_agency_evidence_stays_blocked(ids):
    items = beats(ids)
    rejection = checked(items, grounded=False, findings=[EditorFinding(
        code='grounding_hook_actor', severity='hard', beat_index=1,
        message='Political context does not entail actor agency.',
    )])
    editor = FakeEditor(verdicts=[rejection])
    report = verify_current_script(blocks(items), context(), editor)
    assert not report['gate']['ready']
    assert report['rewrite']['verified_by'] is None
    audit = editor.calls[0][1]['hook_grounding'][0]
    assert audit['fact_ids'] == ids
    assert {f['id'] for f in audit['cited_facts']} == set(ids)
    assert 'fact_06' not in {f['id'] for f in audit['cited_facts']}
    assert 'agency' in audit['audit_claims']
    assert 'Political or other context does not establish who acted' in VERIFIER_INSTRUCTIONS


@pytest.mark.parametrize('hook,ids', [
    (FIXTURE['blocks'][0]['text'], ['fact_08', 'fact_06']),
    ('Welche Absicht steckte dahinter?', []),
])
def test_supported_agency_or_rhetorical_hook_can_be_verified(hook, ids):
    items = beats(ids, hook)
    report = verify_current_script(blocks(items), context(), FakeEditor(verdicts=[checked(items)]))
    assert report['rewrite']['verified_by'] == 'ai_verifier'
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(blocks(items))


def test_hook_rejection_repairs_inside_existing_budget_on_final_blocks():
    bad, good = beats(), beats(['fact_08', 'fact_06'])
    bad_verdict = checked(bad, grounded=False, findings=[EditorFinding(
        code='grounding_hook_actor', severity='hard', beat_index=1, message='Cite the exact actor evidence or remove the claim.',
    )])
    seen = []

    def check(brief):
        candidate = brief['candidate']['beats']
        assert any('13. August 1961' in b['text'] for b in candidate)
        assert not any(b['text'] == 'Am 13.' for b in candidate)
        seen.append(copy.deepcopy(candidate))
        return checked([(b['role'], b['text'], b['fact_ids']) for b in candidate])

    editor = FakeEditor(rewrites=[rewrite(bad), rewrite(good)], verdicts=[bad_verdict, check])
    final, report = run_script_story_quality(blocks(bad), context(), editor,
        finalize_candidate=lambda candidate: pipeline._normalise_blocks(candidate, 80, 150))
    assert report['rewrite']['attempt'] == 2
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)
    assert [b['text'] for b in final] == [b['text'] for b in seen[-1]]
    repair = [b for stage, b in editor.calls if stage == 'rewrite'][1]
    assert 'grounding_hook_actor' in json.dumps(repair)
    assert final[0]['fact_ids'] == ['fact_08', 'fact_06']
    assert 'Fluchten' in ' '.join(b['text'] for b in final)


@pytest.mark.parametrize('language,question,answer', [
    ('de', 'Warum wurde die Berliner Mauer gebaut?', 'Der unmittelbare Zweck war: Fluchten aus der DDR verhindern.'),
    ('en', 'Why was the city barrier built?', 'Its purpose was to prevent residents from leaving.'),
])
def test_supported_motive_does_not_require_action_verb(language, question, answer):
    state = {'intent': {'question': question, 'language': language},
             'contract': {'question_type': 'historical_motive'},
             'facts': [{'id': 'motive', 'claim': question + ' ' + answer, 'verification': 'source_attributed',
                        'confidence': .9, 'sources': [{'url': 'https://example.test'}]}]}
    unit = {'block_id': 'answer', 'category': 'new_fact', 'text': answer, 'explanatory_delta': 'advances_explanation',
            'evidence': {'fact_ids': ['motive'], 'status': 'supported'}}
    result = _answer_sufficiency([unit], _context(state), {'status': 'pass'})
    assert result['status'] == 'pass' and result['answer_relation'] == 'purpose'
    assert result['mechanism_block_ids'] == ['answer']
    # Topic context and action consequences are never enough.
    for text in ['The city developed two political systems.', 'The barrier divided families.', 'Die Stadt hatte zwei Systeme.']:
        result = _answer_sufficiency([{**unit, 'text': text}], _context(state), {'status': 'pass'})
        assert 'no_supported_motive_linked_to_question' in result['reasons']


def test_missing_motive_or_independent_verification_still_blocks():
    items = beats()
    response = checked(items, answers_question=False, contract_sufficient=False)
    response.contract_evaluations[0].status = 'missing'
    report = verify_current_script(blocks(items), context(), FakeEditor(verdicts=[response]))
    assert report['rewrite']['verified_by'] is None and not report['gate']['ready']
    report = verify_current_script(blocks(items), context(), None)
    assert report['rewrite']['verified_by'] is None


def test_final_reverify_preserves_generation_recovery_diagnostics(monkeypatch):
    state, _, _ = install_pipeline(monkeypatch)
    original = copy.deepcopy(state['script']['script_story_quality_v1']['holistic'])
    state['script']['blocks'][0]['text'] += ' Watch the wings.'
    provider = FakeEditor(verdicts=[evaluated()])
    pipeline._reverify_current_contract(state, Settings(openai_api_key='fixture'), provider)
    assert state['script']['script_story_quality_v1']['generation_recovery'] == original
    assert state['script']['script_story_quality_v1']['rewrite']['verified_script_signature'] == script_quality_signature(state['script']['blocks'])
    assert content_readiness(state)['ready']


def test_deterministic_finalization_precedes_independent_signature():
    seen = []
    def finalize(candidate):
        candidate[0]['text'] += ' Watch the wings.'
        candidate[0]['fact_ids'] = ['lift_02']
        seen.append(copy.deepcopy(candidate))
        return candidate
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)], verdicts=[evaluated()])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor, finalize_candidate=finalize)
    verifier = next(brief for stage, brief in editor.calls if stage == 'verify')
    assert verifier['hook_grounding'][0]['text'] == final[0]['text'] == seen[0][0]['text']
    assert verifier['hook_grounding'][0]['fact_ids'] == ['lift_02']
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)


def test_berlin_grounding_exhaustion_remains_bounded_and_unsigned():
    items = beats()
    rejected = checked(items, grounded=False, findings=[EditorFinding(
        code='grounding_hook_actor', severity='hard', beat_index=1, message='Actor agency not cited.',
    )])
    editor = FakeEditor(rewrites=[rewrite(items)] * 3, verdicts=[rejected] * 3)
    final, report = run_script_story_quality(blocks(items), context(), editor,
        finalize_candidate=lambda candidate: pipeline._normalise_blocks(candidate, 80, 150))
    assert not report['gate']['ready'] and report.get('rewrite', {}).get('verified_by') is None
    assert editor.stages().count('rewrite') == 3 and editor.stages().count('verify') == 3
    assert final[0]['fact_ids'] == ['fact_08']
    assert len(report['holistic']['attempts']) == 3
