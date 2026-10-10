"""Real cached pages plus negative contracts; no network or provider calls."""
import copy
import json
from pathlib import Path

import pytest

from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.research_v2.corroboration import (
    find_contradictions,
    group_claims,
    independence_clusters,
)
from clipforge.research_v2.evidence import (
    EvidenceUnit,
    kinds_of,
    question_terms,
    units_from_paragraphs,
)
from clipforge.research_v2.models import SubQuestion
from clipforge.research_v2.package import (
    build_package,
    legacy_facts,
    select_claims,
    validate_synthesized,
)
from clipforge.research_v2.routing import route_question

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/research_honey_everest_cached.json').read_text())


def replay(name):
    data = FIXTURE[name]
    frame = question_frame(data['question'], data['language'])
    subs = [SubQuestion(**sub) for sub in data['sub_questions']]
    sources, texts, units = {}, {}, []
    for row in data['sources']:
        source = copy.deepcopy(row['source'])
        sources[source['id']] = source
        if 'paragraphs' in row:
            paragraphs = row['paragraphs'] + (row['tables'][0] if row['tables'] else [])
            found = units_from_paragraphs(paragraphs, source_id=source['id'], sub_questions=subs,
                                          core_terms=question_terms(data['question']), start=len(units) + 1,
                                          title=source['title'])
            texts[source['id']] = ' '.join(row['paragraphs'])
        else:
            # Only the saved snippet units are available for the denied page.
            found = [EvidenceUnit(**{key: unit[key] for key in (
                'id', 'text', 'excerpt', 'source_id', 'sub_question', 'kinds', 'relevance',
                'basis', 'provenance', 'numbers', 'time_sensitive')}, matched=[])
                for unit in row['saved_units']]
            texts[source['id']] = ' '.join(unit.text for unit in found)
        units.extend(found)
    for index, unit in enumerate(units, 1):
        unit.id = f'ev_{index:02d}'
    clusters = independence_clusters(list(sources.values()), texts)
    groups = group_claims(units, clusters, frame.terms)
    contradictions = find_contradictions(groups, sources, frame.terms)
    rejected = []
    route = route_question(data['question'])
    claims = select_claims(groups, sources, route, frame=frame, rejected=rejected)
    package = build_package(question=data['question'], language=data['language'], route=route,
                            sub_questions=data['sub_questions'], claims=claims,
                            evidence={unit.id: unit for unit in units}, sources=sources,
                            contradictions=contradictions, rejected=rejected, frame=frame,
                            synthesis_mode='deterministic')
    return package, legacy_facts(claims, sources, clusters), units


def test_cached_honey_selects_actual_inhibitory_explanation():
    package, facts, units = replay('honey')
    assert 'Bakterien und Keime nicht überleben' in package['core_answer']['text']
    assert any('Wachstum von Mikroorganismen hemmt' in fact['claim'] for fact in facts)
    assert all('schlechten Geschmack' not in fact['claim'] for fact in facts)
    assert package['sufficiency']['checks']['direct_answer']
    assert package['sufficiency']['checks']['mechanism']
    # Real source authority stays unchanged; this is not a fabricated high-confidence PASS.
    assert package['status'] == 'partial'
    assert 'core_answer_low_authority_source' in package['gaps']
    assert all(sum(unit.source_id == source['source']['id'] for unit in units) <= 8
               for source in FIXTURE['honey']['sources'])


def test_cached_everest_retains_pressure_and_erosion():
    package, facts, _ = replay('everest')
    assert package['status'] == 'sufficient'
    assert package['core_answer']
    assert any('kontinuierlichen Druck' in fact['claim'] for fact in facts)
    assert any('geringeren Last' in fact['claim'] for fact in facts)
    assert 'no_mechanism_evidence' not in package['gaps']
    assert all('garantiert' not in fact['claim'] for fact in facts)


def test_cached_dog_mechanism_remains_sufficient():
    package, _, _ = replay('dog')
    assert package['status'] == 'sufficient'
    assert 'Verdunstung' in package['core_answer']['text']


@pytest.mark.parametrize('name', ['honey', 'everest', 'dog'])
def test_cached_facts_keep_exact_source_and_evidence_provenance(name):
    package, facts, units = replay(name)
    evidence = {unit.id: unit for unit in units}
    urls = {row['source']['id']: row['source']['url'] for row in FIXTURE[name]['sources']}
    for fact in facts:
        assert fact['evidence_ids'] and fact['sources'] and fact['research_key']
        assert all(identifier in evidence for identifier in fact['evidence_ids'])
        assert all(source['url'] == urls[source['source_id']] for source in fact['sources'])
        # Corroborating quotations may use different wording. The emitted
        # native claim must have an exact origin, and every ID must remain
        # attached to its actual source, not borrowed from another claim.
        assert any(evidence[identifier].text in fact['claim'] or fact['claim'] in evidence[identifier].text
                   for identifier in fact['evidence_ids'])
        # Legacy facts intentionally show one source per independence
        # cluster, capped at three. All remaining evidence is traceable in
        # the package's full source inventory.
        assert {evidence[identifier].source_id for identifier in fact['evidence_ids']} <= {
            source['id'] for source in package['source_summary']['sources']}


@pytest.mark.parametrize('language,question', [
    ('de', FIXTURE['everest']['question']),
    ('en', 'Why does Mount Everest grow a little higher every year?'),
    ('de', 'Warum wird der Mont Blanc jeden Tag ein kleines Stück höher?'),
])
def test_time_and_degree_are_not_entities(language, question):
    frame = question_frame(question, language)
    assert len(frame.entities) == 1
    assert frame.entities[0].name in {'Mount Everest', 'Mont Blanc'}
    assert not frame.extra['condition_terms']


@pytest.mark.parametrize('question,language,subject', [
    ('Why does a bit change?', 'en', 'bit'),
    ('Why does a little computer overheat?', 'en', 'computer'),
    ('Warum brennt ein Stück Papier?', 'de', 'Stück'),
])
def test_degree_filter_does_not_remove_subject_nouns(question, language, subject):
    assert any(subject in entity.name for entity in question_frame(question, language).entities)


@pytest.mark.parametrize('question,text,language', [
    ('Warum wird Honig nie schlecht?', 'Honig verdirbt nicht, weil der geringe Wassergehalt das Wachstum von Keimen hemmt.', 'de'),
    ('Why does honey never spoil?', 'Honey does not spoil because its low water content inhibits microbial growth.', 'en'),
    ('Why does bread not rot?', 'Bread does not rot because freezing prevents microbial growth.', 'en'),
    ('Warum wird der Berg höher?', 'Der Berg wird angehoben, weil die Platten gegeneinander drücken.', 'de'),
    ('Why does the mountain grow?', 'The mountain grows because the colliding plates push it upward.', 'en'),
])
def test_equivalent_events_and_causal_processes(question, text, language):
    assert not core_issues(question_frame(question, language), text)


@pytest.mark.parametrize('text', [
    'Honig nimmt Gerüche auf und kann dadurch einen schlechten Geschmack erhalten.',
    'Honig wird heiß, weil sein Glas in der Sonne steht.',
    'Die richtige Lagerung von Honig ist wichtig, um seine Haltbarkeit zu gewährleisten.',
    'Honig ist lange haltbar. Das liegt an seinen besonderen Eigenschaften.',
    'Honig verdirbt, weil Hefezellen den Zucker abbauen.',
])
def test_wrong_event_polarity_and_generic_advice_cannot_answer_resistance(text):
    assert core_issues(question_frame(FIXTURE['honey']['question']), text)


@pytest.mark.parametrize('text', [
    'Durch den niedrigen Wassergehalt können Bakterien nicht überleben.',
    'Low water content inhibits microbial growth.',
    'Der Druck hebt die Gesteinsmassen langsam an.',
    'The pressure raises the rock gradually.',
])
def test_inhibition_and_force_are_mechanism_kinds(text):
    assert 'mechanism' in kinds_of(text)


def test_title_does_not_turn_unrelated_inhibition_into_answer():
    frame = question_frame(FIXTURE['honey']['question'])
    text = 'Die Hülle verhindert die Ausbreitung von Schallwellen.'
    assert core_issues(frame, text, context=frame.question)
    assert mechanism_issues(frame, text, frame.question)


def test_nearby_durability_cannot_supply_the_target_of_unrelated_inhibition():
    frame = question_frame(FIXTURE['honey']['question'])
    text = 'Honig verhindert die Ausbreitung lauter Geräusche.'
    assert 'predicate_mismatch' in core_issues(frame, text, context='Honig besitzt eine lange Haltbarkeit. ' + text)


def test_prevention_does_not_answer_why_deterioration_occurs():
    frame = question_frame('Warum verdirbt Honig?')
    text = 'Honig verdirbt nicht, weil der geringe Wassergehalt das Wachstum von Keimen hemmt.'
    assert 'predicate_mismatch' in core_issues(frame, text)


@pytest.mark.parametrize('question,text,language', [
    ('Warum fallen Wolken nicht vom Himmel?', 'Wolken fallen vom Himmel, weil die Schwerkraft auf das Wasser wirkt.', 'de'),
    ('Why does the coating not crack?', 'The coating cracks because pressure deforms its surface.', 'en'),
    ('Warum ist der Stoff nicht giftig?', 'Der Stoff ist giftig, weil seine Moleküle Zellen schädigen.', 'de'),
])
def test_opposite_effect_polarity_is_not_a_core_answer(question, text, language):
    assert 'polarity_mismatch' in core_issues(question_frame(question, language), text)


def test_bounded_extraction_preserves_cause_not_repeated_quality_context():
    data = FIXTURE['honey']
    row = next(row for row in data['sources'] if 'rund-um-die-biene' in row['source']['url'])
    units = units_from_paragraphs(row['paragraphs'], source_id='actual_source',
                                  sub_questions=[SubQuestion(**sub) for sub in data['sub_questions']],
                                  core_terms=question_terms(data['question']), start=1, title=row['source']['title'])
    assert len(units) <= 8
    assert any('Wachstum von Mikroorganismen hemmt' in unit.text for unit in units)


@pytest.mark.parametrize('text', [
    'Der Mount Everest ist mit einer Höhe von 8.849 Metern der höchste Berg, wenn die Höhe bestimmt wird.',
    'Der Mount Everest wächst weiter. Forscher untersuchen deshalb seine Höhe.',
    'Ein anderer Berg wächst, weil sich die Kontinentalplatten gegeneinander drücken.',
])
def test_static_dimensions_announcements_and_wrong_subject_do_not_answer_growth(text):
    assert core_issues(question_frame(FIXTURE['everest']['question']), text)


def unit(text, kinds=None):
    return EvidenceUnit('ev_01', text, text, 'src_01', 'core', kinds or kinds_of(text), 1, [])


@pytest.mark.parametrize('text,reason', [
    ('The mountain continues to rise every year because pressure raises the rock.', 'recurrence_not_in_evidence'),
    ('The mountain necessarily continues to rise because pressure raises the rock.', 'certainty_not_in_evidence'),
    ('Der Berg wird jedes Jahr angehoben, weil Druck das Gestein hebt.', 'recurrence_not_in_evidence'),
    ('Der Berg wird garantiert angehoben, weil Druck das Gestein hebt.', 'certainty_not_in_evidence'),
])
def test_ongoing_process_does_not_support_guaranteed_annual_measurement(text, reason):
    evidence = unit('The mountain continues to rise because pressure raises the rock. Der Berg wird angehoben, weil Druck das Gestein hebt.')
    assert validate_synthesized(text, ['ev_01'], {'ev_01': evidence}) == reason


def test_supported_annual_wording_keeps_source_qualification():
    text = 'The mountain may rise annually because pressure raises the rock.'
    evidence = unit(text)
    assert validate_synthesized(text, ['ev_01'], {'ev_01': evidence}) is None
    assert validate_synthesized(text.replace('may ', ''), ['ev_01'], {'ev_01': evidence}) == 'uncertainty_not_preserved'


@pytest.mark.parametrize('original,paraphrase', [
    ('Druck hebt den Berg pro Jahr um 2 Meter an.', 'Druck hebt den Berg jährlich um 2 Meter an.'),
    ('Pressure raises the mountain by 2 meters per year.', 'Pressure raises the mountain by 2 meters annually.'),
])
def test_supported_cadence_survives_equivalent_wording(original, paraphrase):
    assert validate_synthesized(paraphrase, ['ev_01'], {'ev_01': unit(original)}) is None


def test_unknown_ids_and_unsupported_numbers_remain_rejected():
    evidence = unit('Pressure raises the mountain by 2 meters.')
    assert validate_synthesized(evidence.text, ['unknown'], {'ev_01': evidence}) == 'unknown_evidence_ids'
    assert validate_synthesized(evidence.text.replace('2', '20'), ['ev_01'], {'ev_01': evidence}).startswith('number_not_in_evidence')


def test_observation_cannot_become_synthesized_cause():
    evidence = unit('The mountain is high and has steep sides.', ['observation'])
    assert validate_synthesized('The mountain is high because it has steep sides.', ['ev_01'], {'ev_01': evidence}) == 'causal_claim_without_causal_evidence'


def test_conflicting_evidence_is_not_selected_as_verified_answer():
    frame = question_frame('How much does a mountain rise?', 'en')
    first = unit('The mountain rises by 2 meters per year.')
    second = unit('The mountain rises by 20 meters per year.')
    second.id, second.source_id = 'ev_02', 'src_02'
    sources = {sid: {'id': sid, 'authority': 'high', 'source_type': 'academic'} for sid in ['src_01', 'src_02']}
    groups = group_claims([first, second], {}, frame.terms)
    contradictions = find_contradictions(groups, sources, frame.terms)
    assert contradictions
    assert all(group.status != 'ok' for group in groups)
    assert not select_claims(groups, sources, route_question(frame.question), frame=frame)


@pytest.mark.parametrize('question,text', [
    (FIXTURE['honey']['question'], 'Honig nimmt Gerüche auf und kann dadurch einen schlechten Geschmack erhalten.'),
    (FIXTURE['everest']['question'], 'Der Mount Everest ist der höchste Berg der Erde mit einer Höhe von 8.849 Metern.'),
])
def test_peripheral_evidence_without_mechanism_cannot_make_package_sufficient(question, text):
    frame = question_frame(question)
    evidence = unit(text)
    sources = {'src_01': {'id': 'src_01', 'title': question, 'authority': 'high', 'source_type': 'academic'}}
    route = route_question(question)
    claims = select_claims(group_claims([evidence], {}, frame.terms), sources, route, frame=frame)
    package = build_package(question=question, language='de', route=route, sub_questions=[], claims=claims,
                            evidence={'ev_01': evidence}, sources=sources, contradictions=[], rejected=[],
                            frame=frame, synthesis_mode='deterministic')
    assert not package['core_answer']
    assert package['status'] != 'sufficient'
