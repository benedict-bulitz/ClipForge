"""Replay the integrated live failure; no network, real providers or replacement evidence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_contract_minimality import SETTINGS, passed_check, response
from test_question_answer_contract import contract

from clipforge import question_answer_contract as qac
from clipforge.contract_diagnostics import ContractGenerationFailure
from clipforge.contract_minimality import (
    NecessityAudit,
    NecessityCorrection,
    QuestionMinimum,
    _validate_audit,
    requirement_ids,
)
from clipforge.research_v2.answer_relation import question_frame
from clipforge.research_v2.corroboration import group_claims, independence_clusters
from clipforge.research_v2.evidence import EvidenceUnit, question_terms, units_from_paragraphs
from clipforge.research_v2.models import SubQuestion
from clipforge.research_v2.package import (
    build_package,
    legacy_facts,
    select_claims,
    validate_synthesized,
)
from clipforge.research_v2.routing import route_question

LIVE = json.loads((Path(__file__).parent / 'fixtures/honey_post_integration_failure.json').read_text())


def sequence():
    diagnostic = LIVE['contract_failure']
    minimum = QuestionMinimum.model_validate(diagnostic['question_minimum'])
    # The rejected planner output was not persisted. Reconstruct only its
    # requirement structure from the real component source IDs, explicitly.
    proposed = contract(LIVE['question'], 'Hoher Zucker und wenig Wasser verhindern mikrobielles Wachstum.')
    proposed.required_supporting_obligations = [qac.AnswerObligation(id='acidity', description='Säure hemmt Mikroorganismen.', is_primary=False, is_required=True)]
    proposed.causal_mechanistic_chain = [qac.CausalChainStep(id=f'step{i}', description=text, depth_level=i) for i, text in enumerate([
        'Zucker bindet Wasser.', 'Wenig Wasser verhindert mikrobielles Wachstum.', 'Säure hemmt Mikroorganismen.'], 1)]
    proposed.required_mechanism_concepts = ['Hoher Zucker', 'Wenig verfügbares Wasser', 'Wachstumshemmung', 'Säure']
    proposed.minimum_answer_depth = 2
    audit = NecessityAudit(minimal_answer=minimum.minimal_answer, components=diagnostic['necessity_components'],
                           minimum_necessary_depth=minimum.minimum_necessary_depth, **diagnostic['verification_flags'])
    corrected = copy.deepcopy(audit)
    corrected.components[0].strictly_necessary = False
    corrected.components[0].minimum_answer_component_ids = []
    corrected.components[1].omission_still_answers_question = False
    corrected.components[1].answer_without_component = 'Mikroorganismen wachsen nicht, ohne Erklärung warum.'
    corrected.components[1].reason = 'Im gewählten Minimum erklärt wenig verfügbares Wasser die Wachstumshemmung; die alternative Zucker-Erklärung ist optional.'
    repaired = contract(LIVE['question'], 'Wenig verfügbares Wasser im Honig verhindert das Wachstum von Verderbniserregern.')
    repaired.optional_context = [c.description for c in corrected.components if not c.strictly_necessary]
    repaired.required_mechanism_concepts = ['Wenig verfügbares Wasser', 'Wachstumshemmung']
    return proposed, minimum, audit, NecessityCorrection(necessity_audit=corrected, repaired_contract=repaired)


def parser(monkeypatch, outputs):
    parse = Mock(side_effect=[value if isinstance(value, Exception) else response(value) for value in outputs])
    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))
    monkeypatch.setattr(qac, 'OpenAI', lambda **_kwargs: client)
    return parse


def test_actual_contradictory_audit_is_never_accepted():
    proposed, minimum, audit, _ = sequence()
    with pytest.raises(ContractGenerationFailure) as caught:
        _validate_audit(audit, requirement_ids(proposed), 'obligation:primary', minimum, SETTINGS)
    assert caught.value.diagnostics['failed_invariants'] == LIVE['contract_failure']['failed_invariants']


@pytest.mark.parametrize('language', ['de', 'en'])
def test_alternative_explanation_requires_bounded_correction_and_independent_check(monkeypatch, language):
    proposed, minimum, audit, correction = sequence()
    question = LIVE['question'] if language == 'de' else 'Why does honey not spoil?'
    if language == 'en':
        proposed.core_question = question
        correction.repaired_contract.core_question = question
    parse = parser(monkeypatch, [proposed, minimum, audit, correction, passed_check(correction.necessity_audit)])
    result = qac.generate_contract(question, language, SETTINGS)
    assert result.primary_answer_obligation.is_required
    assert result.primary_answer_obligation.description == correction.repaired_contract.primary_answer_obligation.description
    assert audit.components[0].description in result.optional_context
    assert result._minimality_review['audit_corrected']
    assert result._minimality_review['rejected_necessity_audit']['diagnostics']['failure_code'] == 'AUDIT_REJECTED'
    assert parse.call_count == 5
    assert parse.call_args_list[3].kwargs['response_format'] is NecessityCorrection
    assert 'rejected_necessity_audit' in parse.call_args_list[4].kwargs['messages'][1]['content']
    assert all('temperature' not in call.kwargs for call in parse.call_args_list)


@pytest.mark.parametrize('failure', ['repeat_contradiction', 'lost_cause', 'only_effect', 'changed_component', 'provider', 'missing_output'])
def test_invalid_correction_fails_closed_without_loop(monkeypatch, failure):
    proposed, minimum, audit, correction = sequence()
    if failure == 'repeat_contradiction':
        correction.necessity_audit = audit
    elif failure == 'lost_cause':
        for c in correction.necessity_audit.components:
            c.strictly_necessary = False
            c.omission_still_answers_question = True
            c.minimum_answer_component_ids = []
    elif failure == 'only_effect':
        c = correction.necessity_audit.components[1]
        c.strictly_necessary = False
        c.omission_still_answers_question = True
        c.minimum_answer_component_ids = []
    elif failure == 'changed_component':
        correction.necessity_audit.components.pop()
    elif failure == 'provider':
        correction = RuntimeError('Provider unavailable')
    else:
        correction = None
    parse = parser(monkeypatch, [proposed, minimum, audit, correction])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    assert parse.call_count == 4
    assert caught.value.diagnostics['rejected_necessity_audit']['diagnostics']['failure_code'] == 'AUDIT_REJECTED'


@pytest.mark.parametrize('failure', ['cause', 'chain', 'minimum', 'component', 'provider'])
def test_independent_check_can_reject_correction(monkeypatch, failure):
    proposed, minimum, audit, correction = sequence()
    check = passed_check(correction.necessity_audit)
    if failure == 'component':
        check.components[1].correctly_required_or_optional = False
    elif failure == 'provider':
        check = RuntimeError('Provider unavailable')
    else:
        setattr(check, {'cause': 'preserves_primary_cause_or_motive', 'chain': 'preserves_necessary_chain', 'minimum': 'minimally_sufficient'}[failure], False)
    parse = parser(monkeypatch, [proposed, minimum, audit, correction, check])
    with pytest.raises(ContractGenerationFailure):
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    assert parse.call_count == 5


def research_replay():
    frame = question_frame(LIVE['question'], LIVE['language'])
    subs = [SubQuestion(**row) for row in LIVE['sub_questions']]
    sources, texts, units = {}, {}, []
    for row in LIVE['sources']:
        source = copy.deepcopy(row['source'])
        sources[source['id']] = source
        page = row['page']
        texts[source['id']] = ' '.join(page['paragraphs'])
        units += units_from_paragraphs(page['paragraphs'] + (page['tables'][0] if page['tables'] else []),
                                       source_id=source['id'], sub_questions=subs,
                                       core_terms=question_terms(LIVE['question']), start=len(units) + 1, title=page['title'])
    clusters = independence_clusters(list(sources.values()), texts)
    groups = group_claims(units, clusters, frame.terms)
    claims = select_claims(groups, sources, route_question(LIVE['question']), frame=frame)
    return frame, sources, units, clusters, groups, claims


def test_live_authority_laundering_no_longer_suppresses_causal_evidence():
    frame, sources, units, clusters, groups, claims = research_replay()
    mixed = next(g for g in groups if {u.id for u in g.units} == {'ev_09', 'ev_18'})
    assert sources[mixed.units[0].source_id]['authority'] == 'medium'
    assert 'versiegeln' in mixed.units[0].text
    assert 'Wirkstoffe' in mixed.units[1].text
    # The failed run discarded both despite extracting them. No new evidence
    # or authority classification is introduced in this replay.
    old_ids = {eid for c in LIVE['observed_selected_claims'] for eid in c['evidence_ids']}
    assert not {'ev_19', 'ev_33'} & old_ids
    selected = {eid for c in claims for eid in c.evidence_ids}
    assert {'ev_19', 'ev_33'} <= selected
    core = next(c for c in claims if c.role == 'core_answer')
    assert core.evidence_ids == ['ev_33']
    assert core.best_tier == 3 and core.verification() == 'source_attributed'
    facts = legacy_facts(claims, sources, clusters)
    assert next(f for f in facts if f['research_role'] == 'core_answer')['confidence'] == 0.62
    package = build_package(question=LIVE['question'], language='de', route=route_question(LIVE['question']),
                            sub_questions=LIVE['sub_questions'], claims=claims,
                            evidence={u.id: u for u in units}, sources=sources, contradictions=[], frame=frame, rejected=[], synthesis_mode='deterministic')
    assert package['status'] == 'partial'  # legitimate source warnings remain
    assert 'core_answer_low_authority_source' in package['gaps']


def test_live_selection_preserves_specific_citations_and_excludes_wrong_answer():
    _, sources, units, clusters, _, claims = research_replay()
    evidence = {u.id: u for u in units}
    for fact in legacy_facts(claims, sources, clusters):
        assert all(identifier in evidence for identifier in fact['evidence_ids'])
        assert {source['source_id'] for source in fact['sources']} <= {evidence[i].source_id for i in fact['evidence_ids']}
    core = next(c for c in claims if c.role == 'core_answer')
    assert 'Geschmack' not in core.text
    assert validate_synthesized('Honig bleibt durch radioaktive Strahlung steril.', ['ev_33'], evidence)
    assert validate_synthesized('Honig ist für 9000 Jahre garantiert steril.', ['unknown'], evidence) == 'unknown_evidence_ids'


@pytest.mark.parametrize('authority,basis', [('medium', 'full_text'), ('high', 'snippet')])
def test_source_quality_is_evaluated_on_actual_answer_units(authority, basis):
    # A real better-source answer retains the original authority policy;
    # a high-authority snippet must not suppress a full-text explanation.
    question = 'Why does the material decay?'
    frame = question_frame(question, 'en')
    units = [EvidenceUnit(id='strong', source_id='a', text='The material decays because heat breaks its chemical bonds.',
                          excerpt='', sub_question='q_core', kinds=['mechanism'], relevance=1, matched=[], basis=basis),
             EvidenceUnit(id='low', source_id='b', text='The material decays because bacteria digest its organic structure.',
                          excerpt='', sub_question='q_core', kinds=['mechanism'], relevance=1, matched=[])]
    sources = {'a': {'authority': authority, 'source_type': 'institutional', 'title': question},
               'b': {'authority': 'low', 'source_type': 'generic_secondary', 'title': question}}
    groups = group_claims(units, {'a': {'cluster': 'a'}, 'b': {'cluster': 'b'}}, frame.terms)
    claims = select_claims(groups, sources, route_question(question), frame=frame)
    core = next(c for c in claims if c.role == 'core_answer')
    assert core.source_ids == (['a'] if basis == 'full_text' else ['b'])


@pytest.mark.parametrize('failure', ['question', 'depth', 'primary_id'])
def test_corrected_contract_cannot_change_protected_structure(monkeypatch, failure):
    proposed, minimum, audit, correction = sequence()
    if failure == 'question':
        correction.repaired_contract.core_question = 'Another question'
    elif failure == 'depth':
        correction.repaired_contract.minimum_answer_depth = 4
    else:
        correction.repaired_contract.primary_answer_obligation.id = 'replacement'
    parse = parser(monkeypatch, [proposed, minimum, audit, correction])
    with pytest.raises(ContractGenerationFailure) as caught:
        qac.generate_contract(LIVE['question'], 'de', SETTINGS)
    assert caught.value.diagnostics['failure_code'] == 'REPAIR_STRUCTURE_MISMATCH'
    assert caught.value.diagnostics['rejected_necessity_audit']['diagnostics']['failure_code'] == 'AUDIT_REJECTED'
    assert parse.call_count == 4
