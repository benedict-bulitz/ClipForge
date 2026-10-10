"""Offline replay of Berlin V4 and claim/citation lifecycle regressions."""
import copy
import json
from pathlib import Path

import pytest
from test_script_story_rewrite import FakeEditor, critic, fact, rewrite, verdict

from clipforge import pipeline
from clipforge.novelty import assess_information_gain
from clipforge.readiness import ScriptNotReady, content_readiness, not_ready_message
from clipforge.script_grounding import (
    ClaimGrounding,
    check_claim_grounding,
    evidence_key,
    sentence_citations,
)
from clipforge.script_story_quality import assess_script_story_quality, script_quality_signature
from clipforge.script_story_rewrite import (
    EditorFinding,
    _restore_hook,
    answer_payoff_duplicate,
    deterministic_findings,
    run_script_story_quality,
    verify_current_script,
)
from clipforge.services import _require_ready

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/berlin_qac_v4_recovery.json').read_text())


def context():
    return {'prompt': FIXTURE['prompt'], 'intent': {'question': FIXTURE['prompt'], 'language': 'de', 'research_required': True},
            'facts': copy.deepcopy(FIXTURE['facts']), 'question_answer_contract': FIXTURE['contract'],
            'research_coverage': copy.deepcopy(FIXTURE['coverage']), 'word_budget': 250}


def blocks(text='Die Mauer sollte Menschen am Weggehen hindern.', ids=None):
    return [{'id': 'voice_block_01', 'role': 'hook', 'text': 'Welche Absicht steckte dahinter?', 'fact_ids': []},
            {'id': 'voice_block_02', 'role': 'payoff', 'text': text, 'fact_ids': ['fact_01'] if ids is None else ids}]


def checked(items, *, evaluations=None, **kwargs):
    from clipforge.narration import split_sentences
    from clipforge.script_story_rewrite import ContractObligationEvaluation
    audit = evaluations if evaluations is not None else [ClaimGrounding(
        beat_index=index, sentence=sentence, status='nonfactual' if not block['fact_ids'] else 'supported',
        supporting_fact_ids=block['fact_ids'], covers_all_claims=True, reasoning='Exact cited facts support every claim.',
    ) for index, block in enumerate(items, 1) for sentence in split_sentences(block['text'])]
    return verdict([(b['role'], b['text'], b['fact_ids']) for b in items],
                   contract_evaluations=[ContractObligationEvaluation(id=FIXTURE['contract']['primary_answer_obligation']['id'],
                       is_primary=True, is_required=True, status='satisfied', reasoning='The immediate motive is present.')],
                   claim_grounding=audit, **kwargs)


def tuples(items):
    return [(b['role'], b['text'], b['fact_ids']) for b in items]


def test_hook_provenance_survives_selection_normalization_and_replacement():
    ids = FIXTURE['hook']['supported_by_fact_ids']
    assert ids
    original = pipeline._hooked_blocks(blocks()[1:], FIXTURE['blocks'][0]['text'], fact_ids=ids)
    normalized = pipeline._normalise_blocks(original, 80, facts=FIXTURE['facts'])
    assert normalized[0]['fact_ids'] == ids
    assert pipeline._apply_selected_hook(normalized, normalized[0]['text'])[0]['fact_ids'] == ids
    changed = pipeline._apply_selected_hook(normalized, 'Eine belegte neue Aussage.', fact_ids=['fact_02'])
    assert changed[0]['fact_ids'] == ['fact_02']
    assert not pipeline._apply_selected_hook(normalized, 'Eine unbelegte neue Aussage.')[0].get('fact_ids')


def test_compound_sentences_get_only_their_own_native_citations():
    facts = [fact('a', 'The gate closes at noon.'), fact('b', 'The lamp uses five watts.')]
    block = {'role': 'explanation', 'text': 'The gate closes at noon. The lamp uses five watts.', 'fact_ids': ['a', 'b']}
    assert sentence_citations(block['text'], block['fact_ids'], facts) == [['a'], ['b']]
    result = pipeline._normalise_blocks([block], 60, facts=facts)
    assert [b['fact_ids'] for b in result] == [['a'], ['b']]
    # A paraphrase without a proven per-sentence mapping stays together for review.
    block['text'] = 'At midday the gate shuts. Five watts power the lamp.'
    result = pipeline._normalise_blocks([block], 60, facts=facts)
    assert len(result) == 1 and result[0]['text'] == block['text']


@pytest.mark.parametrize('language,question,text,claim', [
    ('de', FIXTURE['prompt'], 'Die Mauer sollte Menschen am Weggehen hindern.', FIXTURE['facts'][0]['claim']),
    ('en', 'Why was the city barrier built?', 'The barrier kept inhabitants from departing.', 'The city barrier was built to stop emigration.'),
    ('de', 'Warum schweben Wolken?', 'Winzige Wasserteilchen schweben, weil bewegte Luft sie trägt.', 'Sehr kleine Tropfen werden durch Luftströmungen in der Wolke gehalten.'),
])
def test_exact_independent_support_resolves_paraphrases(language, question, text, claim):
    ctx = context()
    ctx['intent'].update(question=question, language=language)
    ctx['prompt'] = question
    ctx['facts'] = [fact('fact_01', claim)]
    items = blocks(text)
    response = checked(items)
    provider = FakeEditor(verdicts=[response])
    report = verify_current_script(items, ctx, provider)
    assert report['gate']['ready'], report
    audit = report['holistic']['explanation_audit']
    assert audit['claim_grounding']['approved']
    result = assess_information_gain({**ctx, 'script': {'blocks': items}, 'explanation_audit': audit})
    assert result['units'][1]['evidence']['kind'] == 'semantic'
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(items)
    assert provider.stages() == ['verify']


def test_generic_positive_verdict_cannot_release_a_lexical_grounding_disagreement():
    items = blocks('Tatsächlich sollte diese Sperre vor allem Menschen am Weggehen hindern.')
    response = checked(items, evaluations=[])
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready']
    assert report['rewrite']['verified_by'] is None


@pytest.mark.parametrize('negative', ['unsupported', 'uncertain'])
def test_unsupported_or_ambiguous_claim_remains_blocked(negative):
    items = blocks('Die Führung wollte die Menschen aus wirtschaftlichen Gründen aufhalten.')
    response = checked(items)
    response.claim_grounding[1].status = negative
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready'] and not report['rewrite']['verified_script_signature']


@pytest.mark.parametrize('ids', [['unknown'], ['fact_05'], ['fact_01', 'unknown']])
def test_invalid_or_incorrect_citations_remain_blocked(ids):
    items = blocks(ids=ids)
    response = checked(items)
    if 'unknown' not in ids:
        response.claim_grounding[1].supporting_fact_ids = ['fact_01']
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready']
    assert not report['rewrite']['verified_script_signature']


def test_numbers_must_be_supported_by_the_actual_citation():
    items = blocks('Die Mauer wurde am 13. August 1961 gebaut.', ['fact_01'])
    report = verify_current_script(items, context(), FakeEditor(verdicts=[checked(items)]))
    assert any(f['code'] == 'unsupported_number' for f in report['verification_diagnostics']['findings'])
    assert not report['gate']['ready']


def test_unsupported_agency_cannot_be_overridden_by_a_positive_grounded_flag():
    items = blocks()
    items[0].update(text=FIXTURE['blocks'][0]['text'], fact_ids=['fact_02'])
    response = checked(items, findings=[EditorFinding(code='unsupported_agency', severity='hard', beat_index=1,
                                                      message='The cited action evidence does not establish this actor.')])
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready'] and not report['rewrite']['verified_by']
    assert not report['holistic']['explanation_audit']['claim_grounding']['approved']


def test_known_invalid_original_hook_is_not_restored():
    original = FIXTURE['blocks'][0]
    candidate = blocks()
    restored, changed = _restore_hook(candidate, context(), original, {original['text']})
    assert not changed and restored == candidate
    restored, changed = _restore_hook(candidate, context(), original)
    assert not changed  # It also lacks citations independent of the rejection history.


def test_recovery_replaces_a_rejected_hook_without_extra_calls():
    bad, good = blocks(), blocks()
    bad[0].update(text=FIXTURE['blocks'][0]['text'], fact_ids=['fact_02'])
    rejection = checked(bad, grounded=False, findings=[EditorFinding(code='unsupported_agency', severity='hard', beat_index=1,
                                                                    message='This citation does not support actor agency.')])
    provider = FakeEditor(critic_response=critic(findings=[EditorFinding(code='unsupported_hook', severity='hard', beat_index=1,
                                                                        message='Original hook rejected.')]),
                          rewrites=[rewrite(tuples(bad)), rewrite(tuples(good))], verdicts=[rejection, checked(good)])
    final, report = run_script_story_quality(bad, context(), provider)
    assert report['gate']['ready'] and final[0]['text'] == good[0]['text']
    assert not report['rewrite']['hook_restored']
    assert provider.stages() == ['critique', 'rewrite', 'verify', 'rewrite', 'verify']
    assert report['rewrite']['verified_script_signature'] == script_quality_signature(final)


def test_exhaustion_remains_bounded_and_classified_as_script_failure():
    items = blocks()
    negative = checked(items, grounded=False, findings=[EditorFinding(code='unsupported_claim', severity='hard', beat_index=2,
                                                                      message='The claim is not supported.')])
    provider = FakeEditor(rewrites=[rewrite(tuples(items))] * 3, verdicts=[negative] * 3)
    final, report = run_script_story_quality(items, context(), provider)
    state = {**context(), 'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage'],
             'script': {'blocks': final, 'script_story_quality_v1': report}}
    ready = content_readiness(state)
    assert not ready['ready'] and not ready['research_required']
    assert ready['failure_category'] == 'script_recovery_failed'
    assert provider.stages().count('rewrite') == provider.stages().count('verify') == 3
    assert not report.get('rewrite', {}).get('verified_by')
    with pytest.raises(ScriptNotReady) as caught:
        _require_ready(state)
    assert caught.value.category == 'script_recovery_failed'
    assert str(caught.value) == not_ready_message(ready)


def test_edited_text_citations_or_evidence_cannot_reuse_semantic_approval():
    items, ctx = blocks(), context()
    report = verify_current_script(items, ctx, FakeEditor(verdicts=[checked(items)]))
    audit = report['holistic']['explanation_audit']
    key = audit['claim_grounding']['evidence_key']
    for kind in ('text', 'citations', 'evidence'):
        changed, dossier = copy.deepcopy(items), copy.deepcopy(ctx['facts'])
        if kind == 'text':
            changed[1]['text'] += ' Und das rettete die Herrschaft.'
        elif kind == 'citations':
            changed[1]['fact_ids'] = ['fact_05']
        else:
            dossier[0]['claim'] = 'Unrelated historical context.'
        assert evidence_key(changed, dossier) != key
        result = assess_information_gain({**ctx, 'facts': dossier, 'script': {'blocks': changed}, 'explanation_audit': audit})
        assert result['units'][1]['evidence'].get('kind') != 'semantic'


def test_unused_citation_and_partial_compound_review_are_hard_failures():
    items = blocks(ids=['fact_01', 'fact_05'])
    response = checked(items)
    response.claim_grounding[1].supporting_fact_ids = ['fact_01']
    failures = check_claim_grounding(response.claim_grounding, items, {f['id']: f for f in FIXTURE['facts']})
    assert any(f['code'] == 'claim_citation_mismatch' for f in failures)
    response.claim_grounding[1].covers_all_claims = False
    assert any(f['code'] == 'claim_grounding_incomplete' for f in check_claim_grounding(
        response.claim_grounding, items, {f['id']: f for f in FIXTURE['facts']}))


def test_optional_historical_context_stays_optional_and_primary_stays_required():
    ctx, items = context(), blocks()
    ctx['story_arc'] = {'question_contract': {'essential_explanation_chain': ['fact_05'],
        'explanation_spine': {'status': 'missing_mechanism', 'mechanism': ['fact_05']}}}
    assert verify_current_script(items, ctx, FakeEditor(verdicts=[checked(items)]))['gate']['ready']
    response = checked(items)
    response.contract_evaluations[0].status = 'missing'
    response.contract_sufficient = False
    assert not verify_current_script(items, ctx, FakeEditor(verdicts=[response]))['gate']['ready']


def test_genuine_repetition_remains_detectable_but_progression_can_pass():
    repeated = blocks()
    repeated.insert(1, {'role': 'answer', 'text': repeated[-1]['text'], 'fact_ids': ['fact_01']})
    assert answer_payoff_duplicate(repeated)
    progressed = blocks('Die Sperre blockierte die Übergänge zwischen den Stadtteilen.', ['fact_02'])
    progressed.insert(1, {'role': 'answer', 'text': 'Die Mauer sollte Fluchten verhindern.', 'fact_ids': ['fact_01']})
    assert not answer_payoff_duplicate(progressed)


def test_real_v4_replay_keeps_its_actual_negative_verifier_findings():
    for attempt in FIXTURE['recovery']['attempts']:
        items = attempt['blocks']
        actual_findings = [EditorFinding(**{k: f[k] for k in ('code', 'severity', 'beat_index', 'message')})
                           for f in [*attempt['hard'], *attempt['major']] if f['source'] == 'verifier']
        response = checked(items, grounded=attempt['verification_flags']['grounded'], findings=actual_findings)
        report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
        assert not report['gate']['ready']
        assert not report['rewrite']['verified_by']
    first = FIXTURE['recovery']['attempts'][0]
    assert first['verification_flags']['grounded'] and any(f['code'] == 'unsupported_claim' for f in first['hard'])
    assert any(f['code'] == 'answer_payoff_duplicate' for f in first['major'])


def test_research_missing_still_has_its_original_category():
    ctx = context()
    ctx['research_coverage']['is_sufficient'] = False
    ctx['research_coverage']['missing_obligations'] = [FIXTURE['contract']['primary_answer_obligation']['id']]
    state = {**ctx, 'contract': ctx['question_answer_contract'], 'contract_coverage': ctx['research_coverage'],
             'script': {'blocks': blocks(), 'script_story_quality_v1': assess_script_story_quality(blocks(), ctx)}}
    result = content_readiness(state)
    assert not result['ready'] and result['research_required']
    assert result['failure_category'] == 'research_required'


def test_nonfactual_hook_needs_no_invented_citations_and_factual_hook_does():
    items = blocks()
    assert not any(f['code'] == 'uncited_hook' for f in deterministic_findings(items, context()))
    items[0]['text'] = 'Mitten durch die Stadt wurden Übergänge geschlossen. Warum?'
    assert any(f['code'] == 'uncited_hook' for f in deterministic_findings(items, context()))


def test_semantic_positive_cannot_override_an_explicit_citation_contradiction():
    ctx = context()
    ctx['intent']['language'] = 'en'
    ctx['facts'] = [fact('fact_01', 'The device opens the gate automatically.')]
    items = blocks('The device does not open the gate automatically.')
    report = verify_current_script(items, ctx, FakeEditor(verdicts=[checked(items)]))
    assert not report['gate']['ready']
    assert any(f['code'] == 'citation_contradiction' for f in report['verification_diagnostics']['findings'])


def test_genuine_verified_repetition_cannot_pass_after_recovery_exhaustion():
    items = blocks()
    items.insert(1, {'id': 'answer', 'role': 'answer', 'text': items[1]['text'], 'fact_ids': ['fact_01']})
    provider = FakeEditor(rewrites=[rewrite(tuples(items))] * 3,
                          verdicts=[checked(items, answer_payoff_duplicate=True)] * 3)
    _, report = run_script_story_quality(items, context(), provider)
    assert not report['gate']['ready']
    assert all(any(f['code'] == 'answer_payoff_duplicate' for f in attempt['hard']) for attempt in report['holistic']['attempts'])


def test_manual_reverification_grounding_failure_has_an_accurate_category():
    ctx, items = context(), blocks()
    response = checked(items, grounded=False)
    report = verify_current_script(items, ctx, FakeEditor(verdicts=[response]))
    state = {**ctx, 'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage'],
             'script': {'blocks': items, 'script_story_quality_v1': report}}
    assert content_readiness(state)['failure_category'] == 'script_grounding_failed'


def test_provider_diagnostics_are_sanitized_without_an_approval():
    class Offline:
        def verify(self, _brief):
            raise RuntimeError('Authorization: Bearer sk-do-not-persist-this-secret')
    report = verify_current_script(blocks(), context(), Offline())
    assert 'sk-do-not-persist-this-secret' not in json.dumps(report)
    assert not report['rewrite']['verified_by']


def test_invalid_claim_review_does_not_become_an_approval():
    items = blocks()
    response = checked(items)
    response.claim_grounding[1].sentence = 'Different wording never inspected.'
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready']
    assert any(f['code'] == 'claim_grounding_mismatch' for f in report['verification_diagnostics']['findings'])


@pytest.mark.parametrize('supported', [True, False])
def test_hook_lexical_disagreement_requires_current_claim_specific_verification(monkeypatch, supported):
    from clipforge.triple_hook import verbal_still_valid
    monkeypatch.setattr('clipforge.verbal_hook.assess_verbal', lambda *_a, **_k: {'hard_fail': ['unsupported_cause']})
    items = blocks()
    items[0].update(text='Die Barriere wirkte, weil sie Menschen am Weggehen hinderte.', fact_ids=['fact_01'])
    response = checked(items, grounded=supported)
    if not supported:
        response.claim_grounding[0].status = 'unsupported'
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert report['gate']['ready'] is supported
    assert (not any(f['code'] == 'hook_lexical_grounding' for f in report['verification_diagnostics']['findings'])) is supported
    state = {**context(), 'script': {'blocks': items, 'script_story_quality_v1': report},
             'explanation_audit': report['holistic']['explanation_audit'],
             'contract': FIXTURE['contract'], 'contract_coverage': FIXTURE['coverage']}
    assert verbal_still_valid(state, items[0]['text'], 'evidence_insight') is supported
    assert content_readiness(state)['ready'] is supported


def test_hook_citations_do_not_claim_that_the_full_answer_was_narrated():
    from clipforge.script_grounding import narrated_fact_ids
    facts = [fact('a', 'The northern region has 200 islands and wins the comparison.'),
             fact('b', 'The southern region has 100 islands.')]
    hook = {'role': 'hook', 'text': '200 against 100 islands: which region wins?', 'fact_ids': ['a', 'b']}
    assert narrated_fact_ids(hook, facts) == []
    # The source IDs remain available for grounding without resolving the question.
    assert hook['fact_ids'] == ['a', 'b']
    hook['text'] = facts[0]['claim']
    assert narrated_fact_ids(hook, facts) == ['a']



def test_native_hook_does_not_inherit_unrelated_dossier_citations():
    facts = [fact('a', 'The gate closes at noon.'), fact('b', 'The lamp uses five watts.')]
    result = pipeline._normalise_blocks([{'role': 'hook', 'text': facts[0]['claim'], 'fact_ids': ['a', 'b']}], 60, facts=facts)
    assert result[0]['fact_ids'] == ['a']
    matching = pipeline._hooked_blocks([{'role': 'answer', 'text': facts[0]['claim'], 'fact_ids': ['a']}], facts[0]['claim'], {'status': 'planned'}, fact_ids=[])
    assert matching[0]['fact_ids'] == ['a']


def test_single_original_citation_can_split_but_cannot_certify_an_unsupported_child():
    ctx = context()
    ctx['facts'] = [fact('a', 'The gate closes at noon.')]
    items = pipeline._normalise_blocks([blocks()[0], {'role': 'payoff', 'text': 'The gate closes at noon. The governor ordered it.', 'fact_ids': ['a']}], 60, facts=ctx['facts'])
    assert len(items) == 3 and all(block['fact_ids'] == ['a'] for block in items[1:])
    response = checked(items)
    response.claim_grounding[-1].status = 'unsupported'
    report = verify_current_script(items, ctx, FakeEditor(verdicts=[response]))
    assert not report['gate']['ready'] and not report['rewrite']['verified_by']
    assert any(f['code'] == 'claim_unsupported' for f in report['verification_diagnostics']['findings'])


def test_hook_reselection_excludes_all_known_rejected_wording(monkeypatch):
    ctx = context()
    items = blocks()
    rejected = 'A previously rejected hook.'
    state = {**ctx, 'script': {'blocks': items, 'selected_hook': items[0]['text'],
             'script_story_quality_v1': {'holistic': {'rejected_hooks': [rejected]}},
             'triple_hook': {'version': 2, 'verbal_hook': items[0]['text'], 'selected_strategy': 'evidence_insight'}}}
    observed = []
    monkeypatch.setattr('clipforge.triple_hook.verbal_still_valid', lambda *_a: False)
    def no_alternative(*_a, **kwargs):
        observed.append(kwargs['exclude'])
    monkeypatch.setattr('clipforge.triple_hook.reselect_verbal', no_alternative)
    monkeypatch.setattr(pipeline, 'ensure_hook_advances', lambda *_a: 'ok')
    assert pipeline.enforce_selected_hook(state) == 'no_alternative'
    assert rejected in observed[0]
    assert not state['script']['script_story_quality_v1'].get('rewrite', {}).get('verified_by')



def test_independent_repetition_finding_remains_hard_despite_conflicting_boolean():
    items = blocks()
    response = checked(items, answer_payoff_duplicate=False, findings=[
        EditorFinding(code='answer_payoff_duplicate', severity='major', beat_index=2, message='The payoff repeats the answer.')])
    report = verify_current_script(items, context(), FakeEditor(verdicts=[response]))
    assert not report['gate']['ready']
    assert any(f['code'] == 'answer_payoff_duplicate' and f['severity'] == 'hard'
               for f in report['verification_diagnostics']['findings'])
