"""Offline reproduction of the live causal-property selection failure."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from clipforge import question_answer_contract as qac
from clipforge.config import Settings
from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.research_v2.corroboration import ClaimGroup
from clipforge.research_v2.evidence import (
    EvidenceUnit,
    kinds_of,
    question_terms,
    units_from_paragraphs,
)
from clipforge.research_v2.models import SubQuestion
from clipforge.research_v2.package import (
    PackageClaim,
    build_package,
    claims_from_synthesis,
    legacy_facts,
    select_claims,
    sufficiency,
)
from clipforge.research_v2.routing import route_question

QUESTION = "Warum ist der Mars rot?"
COLD = "Uns Menschen würde es bei einem Besuch auf dem Mars daher ziemlich kalt vorkommen."
DIRECT = "Seine Farbe hat Mars von verrostetem Eisen, das die Oberfläche als Staub bedeckt."
REACTION = "Eisen reagiert und bildet Oxide."
DUST = "Feiner roter Staub bedeckt die Oberfläche des Mars und wird durch Winde verteilt."
CONTEXT = "Der Mars erscheint rot. " + DIRECT + " " + REACTION
LIVE_REACTION = ("Vereinfacht gesagt ist der Mars aus demselben grundlegenden Grund rot, aus dem ein altes Fahrrad, "
                 "das im Regen steht, rötlich-braun wird: Eisen reagiert und bildet Oxide.")


def bundle():
    frame = question_frame(QUESTION)
    sources = {
        "cold": {"id": "cold", "authority": "high", "source_type": "science", "title": "Mars", "url": "https://cold.test"},
        "answer": {"id": "answer", "authority": "medium", "source_type": "science", "title": "Mars", "url": "https://answer.test"},
    }
    records = [
        (COLD, "cold", COLD, ""),
        ("Der Mars ist kalt und besitzt eine sehr dünne Atmosphäre.", "cold", COLD, ""),
        ("Auf dem Mars herrschen niedrige Temperaturen, oft unter minus 60 Grad.", "cold", COLD, ""),
        (DIRECT, "answer", CONTEXT, "Der Mars erscheint rot."),
        (REACTION, "answer", CONTEXT, DIRECT),
        (DUST, "answer", CONTEXT, ""),
    ]
    records += [(f"Der Mars hat {number} bekannte Messwerte zur Temperatur, weil Messgeräte dort arbeiten.", "cold", COLD, "") for number in range(10, 20)]
    units = [EvidenceUnit(id=f"ev_{index}", text=text, source_id=source, excerpt=context,
                          sub_question="core", kinds=kinds_of(text), relevance=0.9,
                          matched=["mars"], antecedent=previous)
             for index, (text, source, context, previous) in enumerate(records)]
    groups = [ClaimGroup(key=f"claim_{index}", units=[unit], clusters={unit.source_id}) for index, unit in enumerate(units)]
    return frame, sources, units, groups


def package(frame, sources, units, claims):
    return build_package(question=frame.question, language=frame.language, route=route_question(frame.question),
                         sub_questions=[], claims=claims, evidence={unit.id: unit for unit in units},
                         sources=sources, contradictions=[], rejected=[], frame=frame, synthesis_mode="fixture")


def test_live_type_bundle_selects_direct_property_answer_before_authority():
    frame, sources, units, groups = bundle()
    assert "predicate_mismatch" in core_issues(frame, COLD)
    assert not core_issues(frame, DIRECT, "Der Mars erscheint rot.")
    assert not mechanism_issues(frame, REACTION, CONTEXT)
    assert not core_issues(frame, LIVE_REACTION)
    claims = select_claims(groups, sources, route_question(QUESTION), frame=frame)
    result = package(frame, sources, units, claims)
    assert DIRECT in result["core_answer"]["text"] and COLD not in result["core_answer"]["text"]
    assert result["status"] == "sufficient"
    assert result["explanation_spine"]["what_happens"]["text"] == DUST
    assert REACTION in [claim.text for claim in claims if claim.role == "mechanism"]
    facts = legacy_facts(claims, sources, {})
    assert any(DIRECT in fact["claim"] for fact in facts)
    assert any(REACTION == fact["claim"] for fact in facts)
    assert not core_issues(frame, result["core_answer"]["text"])


def test_unrelated_causal_evidence_alone_never_certifies_sufficient_high():
    frame, sources, units, groups = bundle()
    groups = [group for group in groups if group.units[0].source_id == "cold"]
    claims = select_claims(groups, sources, route_question(QUESTION), frame=frame)
    result = package(frame, sources, units, claims)
    assert result["core_answer"] is None and result["status"] != "sufficient"
    assert result["confidence"] != "high"


def test_package_rechecks_core_role_instead_of_trusting_high_authority_label():
    frame, sources, units, _ = bundle()
    bad = PackageClaim("bad", "core_answer", COLD, [units[0].id], ["cold"], {"cold"}, "full_text", 0)
    assert sufficiency([bad], frame=frame)["status"] != "sufficient"
    result = package(frame, sources, units, [bad])
    assert result["core_answer"] is None and result["confidence"] != "high"


def test_low_authority_direct_answer_not_removed_by_unrelated_high_authority():
    frame, sources, _, groups = bundle()
    sources["answer"]["authority"] = "low"
    claims = select_claims(groups, sources, route_question(QUESTION), frame=frame)
    assert DIRECT in next(claim.text for claim in claims if claim.role == "core_answer")


@pytest.mark.parametrize("text", [
    "Der Stoff reagiert und bildet eine neue Verbindung.", "Der Stoff bildet eine neue Verbindung.",
    "Der Stoff oxidiert an der feuchten Oberfläche.", "Der Stoff wird an der Oberfläche oxidiert.",
    "Oxidation erzeugt eine neue Verbindung an der Oberfläche.",
    "Seine Färbung stammt von einer Verbindung an der Oberfläche.",
    "Seine Färbung kommt von einer Verbindung an der Oberfläche.",
    "Seine Färbung ist auf die Verbindung zurückzuführen.",
    "Seine Färbung ist zurückzuführen auf die neue Verbindung.",
    "Seine Färbung liegt an einer Verbindung an der Oberfläche.",
])
def test_german_generic_mechanism_language(text):
    assert "mechanism" in kinds_of(text)
    frame = question_frame("Warum ist der Stoff dunkel?")
    assert not mechanism_issues(frame, text, "Der Stoff ist dunkel. " + text)


@pytest.mark.parametrize("text", [
    "The material reacts and forms a new compound.", "The material forms a new compound.",
    "The material produces a new compound.", "The material creates a new compound.",
    "The material oxidizes in humid air.", "The material is oxidized in humid air.",
    "Its color comes from a compound on the surface.", "Its color is due to a compound on the surface.",
    "Its color results from a compound on the surface.",
])
def test_english_generic_mechanism_language(text):
    assert "mechanism" in kinds_of(text)
    frame = question_frame("Why is the material dark?", "en")
    assert frame.predicate == frozenset({"dark"})
    assert not mechanism_issues(frame, text, "The material is dark. " + text)


@pytest.mark.parametrize("question,good,bad,language", [
    ("Warum ist der See trüb?", "Der See ist trüb, weil kleine Partikel das Wasser durchsetzen.",
     "Der See ist daher sehr kalt für einen Besucher.", "de"),
    ("Why is the lake cloudy?", "The lake looks cloudy because suspended particles scatter light.",
     "The lake is therefore very cold for a visitor.", "en"),
    ("Wie wird die Suppe dick?", "Die Suppe wird dick, weil ihre Stärke beim Erhitzen Wasser bindet.",
     "Die Suppe wird daher sehr heiß für einen Gast.", "de"),
])
def test_property_eligibility_is_generic(question, good, bad, language):
    frame = question_frame(question, language)
    assert not core_issues(frame, good)
    assert "predicate_mismatch" in core_issues(frame, bad)


def test_forms_alone_needs_entity_and_property_context():
    frame = question_frame("Warum ist der Stoff dunkel?")
    unrelated = "Das Unternehmen bildet neue Teams in seiner Verwaltung."
    assert "entity_mismatch" in mechanism_issues(frame, unrelated)
    assert "predicate_mismatch" in core_issues(frame, "Der Stoff bildet einen stabilen Block für das Gebäude.")
    assert "predicate_mismatch" in mechanism_issues(frame, unrelated, "Der Stoff wird zu einem Block geformt.")
    assert core_issues(frame, "Der Stoff ist dunkel, weil er dunkel ist.")


def test_property_reference_retains_actual_antecedent_not_just_entity():
    frame = question_frame(QUESTION)
    assert "predicate_mismatch" in core_issues(frame, DIRECT, "Der Mars ist ein Planet.")
    assert "predicate_mismatch" in core_issues(frame, COLD, "Der Mars erscheint rot.")


def test_short_reaction_extracted_and_direct_answer_survives_source_budget():
    sub = SubQuestion(id="core", question=QUESTION, query=QUESTION, kind="mechanism")
    paragraphs = [f"Auf dem Mars gibt es {number} bekannte Temperaturmessungen, daher sind dort viele Zahlen bekannt." for number in range(10, 22)]
    paragraphs += [CONTEXT, DUST]
    units = units_from_paragraphs(paragraphs, source_id="source", sub_questions=[sub], core_terms=question_terms(QUESTION), start=1)
    assert any(REACTION in unit.text for unit in units)
    assert any(DIRECT in unit.text for unit in units)
    assert any(DUST in unit.text for unit in units)
    assert len(units) <= 8


def test_berlin_purpose_does_not_need_to_repeat_action_predicate():
    frame = question_frame("Warum wurde die Berliner Mauer gebaut?")
    purpose = "Die Berliner Mauer sollte die Flucht in den Westen stoppen und die Abwanderung von Arbeitskräften verhindern."
    consequence = "Die Berliner Mauer trennte Familien, sodass Verwandte einander nicht mehr besuchen konnten."
    assert "gebaut" not in purpose
    assert not core_issues(frame, purpose)
    assert core_issues(frame, consequence) and mechanism_issues(frame, consequence)


def test_synthesis_cannot_promote_temperature_to_color_answer():
    frame, sources, units, _ = bundle()
    accepted, rejected = claims_from_synthesis({"core_answer": {"text": COLD, "evidence_ids": [units[0].id]}},
                                               {unit.id: unit for unit in units}, sources, {}, frame)
    assert accepted == [] and "predicate_mismatch" in rejected[0]["reason"]


def test_contract_planner_receives_minimum_depth_instructions(monkeypatch):
    from test_contract_minimality import approved_audit, minimum_for, response
    from test_question_answer_contract import contract
    model = contract()
    parse = Mock(side_effect=[response(model), response(minimum_for(model, approved_audit(model))), response(approved_audit(model))])
    monkeypatch.setattr(qac, "OpenAI", lambda **_kwargs: SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))))
    qac.generate_contract(QUESTION, "de", Settings(openai_api_key="fixture"))
    prompt = parse.call_args_list[0].kwargs["messages"][0]["content"]
    for instruction in ["STRICT MINIMUM", "first non-circular causal mechanism", "generic underlying physics or perception layer",
                        "photon-level reflection/scattering is optional", "independently necessary", "atomic enough to verify",
                        "necessary causal depth, not maximum possible depth", "Do not impose a universal depth cap"]:
        assert instruction in prompt


def test_temperature_connective_cannot_inherit_color_from_nearby_sentence():
    frame = question_frame(QUESTION)
    assert "predicate_mismatch" in mechanism_issues(frame, COLD, CONTEXT + " " + COLD)


@pytest.mark.parametrize("adjective", ["rötliche", "rötlichen", "rötlicher"])
def test_declined_property_adjectives_retain_the_asked_predicate(adjective):
    frame = question_frame("Warum ist der Stoff rot?")
    text = f"Der Stoff enthält {adjective} Partikel, weil eine chemische Reaktion neue Verbindungen bildet."
    assert not core_issues(frame, text)
