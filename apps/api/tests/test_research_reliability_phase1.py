"""Offline regressions for the regular-app evidence selection failures.

Live-shaped quotations are fixtures, never production vocabulary. Each case
exercises extraction, classification, selection and the persisted fact package.
"""
import pytest
from research_semantic_fixtures import Case
from research_v2_support import research_settings

from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import (
    core_issues,
    entity_coverage,
    mechanism_issues,
    question_frame,
)
from clipforge.research_v2.synthesis import SynthesisOut

BATTERY = "Warum verlieren Handyakkus mit der Zeit an Kapazität?"
ANNOUNCEMENT = (
    "Doch nach einiger Zeit verlieren sie an Kapazität. Daher untersuchte ein "
    "deutsch-amerikanisches Forschungsteam den Aufbau und die Funktionsweise dieser Akkus mit Neutronenbeugung."
)
DEGRADATION = (
    "Die Ursache der nachlassenden Kapazität sind kleinste Lithium-Ablagerungen, "
    "die beim Entladevorgang von der Anode abbrechen und die direkte Verbindung zur Anode verlieren."
)
SPECIFICATION = (
    "Häufig geben Gerätehersteller daher eine Anzahl an Zyklen an, die der verbaute Akku "
    "üblicherweise übersteht, bevor er merklich an Kapazität verliert."
)
ANC = "Wie funktioniert Noise Cancellation bei Kopfhörern?"
CANCELLATION = (
    "Diesen Antischall gibt der Kopfhörer anschließend an Ihr Ohr aus und überlagert damit "
    "die Hintergrundgeräusche derart, dass es zur Auslöschung der zugehörigen Schallwellen kommt."
)
PASSIVE = "PNC kommt bei mehr Kopfhörern zum Einsatz als ANC, weil die passive Variante in der Herstellung günstiger ist."
CLOUD = "Warum fallen Wolken nicht vom Himmel, obwohl sie große Mengen Wasser enthalten?"
SUSPENSION = "Da sie so klein sind, fallen sie nicht vom Himmel, sondern werden von Luftströmungen durcheinandergewirbelt und in der Wolke gehalten."
TOUCH = "Wie merkt ein Touchscreen, wo der Finger ist?"
DETECTION = "Wird ein kapazitiver Touchscreen mit dem Finger berührt, verändert sich die elektrische Kapazität an dieser Stelle. Dadurch bestimmt die Elektronik die Position der Berührung."
HEART = "Warum schlägt das Herz auch ohne Befehl vom Gehirn?"
CIRCULATION = "So beeinflusst der Herzschlag die Durchblutung des Gehirns und damit die Verarbeitung aller Wahrnehmungen."
RHYTHM = "Das Herz schlägt ohne Befehl vom Gehirn, weil sein eigener Taktgeber selbst elektrische Impulse erzeugt. Diese Impulse lösen die Kontraktion des Herzmuskels aus."


def run(question, paragraphs, *, language="de", title=None, llm=None):
    case = Case(question, "", pages=[
        ("https://www.uni-beispiel.de/erklaerung", title or question, paragraphs, {}),
    ])
    return run_research(question, language, research_settings(brave_search_api_key="fixture"),
                        context={"question": question}, transport=case.web().transport(),
                        llm=llm, cache_root=None)


def core(result):
    return (result.package.get("core_answer") or {}).get("text", "")


def test_battery_direct_mechanism_survives_peripheral_evidence():
    result = run(BATTERY, [ANNOUNCEMENT, SPECIFICATION, DEGRADATION], title="Handyakkus verlieren mit der Zeit Kapazität")
    assert DEGRADATION in core(result)
    assert result.package["status"] == "sufficient"
    assert any(DEGRADATION in fact["claim"] for fact in result.facts)
    assert ANNOUNCEMENT not in core(result) and SPECIFICATION not in core(result)


@pytest.mark.parametrize("text", [ANNOUNCEMENT, SPECIFICATION,
    "Forscher erklären erstmals, wie der Kapazitätsverlust von Handyakkus chemisch abläuft."])
def test_battery_announcements_and_specifications_are_not_explanations(text):
    result = run(BATTERY, [text], title=BATTERY)
    assert not core(result) and result.package["status"] != "sufficient"


@pytest.mark.parametrize("question", [ANC, "Wie funktioniert Noise cancellation bei Kopfhörern?"])
def test_anc_contextual_antischall_is_selected_instead_of_cost(question):
    result = run(question, [
        "Active Noise Cancelling bei Kopfhörern arbeitet mit einem Gegensignal. " + CANCELLATION,
        PASSIVE,
        "Passive Noise Cancellation hängt von der Bauweise des Kopfhörers ab.",
    ], title=question)
    assert CANCELLATION in core(result)
    assert result.package["status"] == "sufficient"
    assert PASSIVE not in core(result)


def test_passive_isolation_cannot_answer_active_cancellation():
    result = run("Wie funktioniert aktive Geräuschunterdrückung bei Kopfhörern?", [
        "Passive Geräuschunterdrückung bei Kopfhörern funktioniert, weil ihre Polster Schall absorbieren.", PASSIVE,
    ])
    assert not core(result) and result.package["status"] != "sufficient"


def test_cloud_direct_suspension_does_not_require_rain_context():
    result = run(CLOUD, ["Wolken enthalten große Mengen Wasser in winzigen Tröpfchen. " + SUSPENSION])
    assert SUSPENSION in core(result)
    assert result.package["status"] == "sufficient"
    assert not result.package["gaps"] or "no_mechanism_evidence" not in result.package["gaps"]


def test_touchscreen_question_and_detection_survive_paraphrase():
    frame = question_frame(TOUCH)
    assert frame.qtype == "how" and frame.asks_observation
    result = run(TOUCH, [DETECTION])
    assert result.package["status"] == "sufficient"
    assert "Kapazität" in " ".join(fact["claim"] for fact in result.facts)


@pytest.mark.parametrize("question,text,language", [
    ("Wie erkennt ein Sensor die Position?", "Ein Sensor bestimmt die Position, indem er Änderungen im elektrischen Feld misst.", "de"),
    ("Wie arbeitet eine Pumpe?", "Eine Pumpe bewegt die Flüssigkeit durch den Druckunterschied zwischen Einlass und Auslass.", "de"),
    ("How does a sensor detect position?", "A sensor determines position by measuring changes in the electric field.", "en"),
])
def test_generic_operational_and_detection_questions(question, text, language):
    frame = question_frame(question, language)
    assert frame.qtype == "how"
    assert not core_issues(frame, text)


def test_heart_circulation_effect_cannot_replace_rhythm_mechanism():
    result = run(HEART, [CIRCULATION, RHYTHM])
    assert "eigener Taktgeber" in core(result)
    assert result.package["status"] == "sufficient"
    missing = run(HEART, [CIRCULATION])
    assert not core(missing) and missing.package["status"] != "sufficient"


@pytest.mark.parametrize("question,text", [
    ("Warum ist der See trüb?", "Der See ist kalt, weil die Sonne im Winter kaum scheint."),
    ("Wie erkennt ein Sensor die Position?", "Ein Sensor kostet weniger, weil seine Herstellung günstiger ist."),
    ("Warum nimmt die Leistung des Motors ab?", "Ein Forscher untersucht den Motor, weil seine Leistung abnimmt."),
])
def test_topical_causality_does_not_answer_the_question(question, text):
    assert core_issues(question_frame(question), text)


def test_context_cannot_license_an_unrelated_entity_or_property():
    frame = question_frame("Warum ist der See trüb?")
    unrelated = "Der Fluss ist trüb, weil aufgewirbelte Partikel das Licht streuen."
    assert core_issues(frame, unrelated, "Der See ist trüb.")
    assert mechanism_issues(frame, "Das Unternehmen bildet neue Teams.", "Der See ist trüb.")


def test_fact_provenance_resolves_to_selected_evidence_and_sources():
    result = run(BATTERY, [ANNOUNCEMENT, DEGRADATION])
    evidence = {item["id"]: item for item in result.package["evidence"]}
    sources = {item["id"] for item in result.package["source_summary"]["sources"]}
    for fact in result.facts:
        assert fact["evidence_ids"] and all(item in evidence for item in fact["evidence_ids"])
        assert all(source["source_id"] in sources for source in fact["sources"])
    assert result.diagnostics["budget"]["used"]["llm_calls"] == 0


def test_electrolyte_mechanism_remains_traceable_without_repeating_the_question():
    process = "Die dabei entstehenden Zersetzungsprodukte des Elektrolyten lagern Lithiumatome ein, welche dann nicht mehr als bewegliches Lithium zur Verfügung stehen, um zwischen den beiden Elektroden ausgetauscht zu werden."
    result = run(BATTERY, [
        "Im Handy verwenden wir Lithium-Ionen-Akkus. Doch nach einiger Zeit verlieren sie an Kapazität. " + process,
        DEGRADATION,
    ], title="Warum Lithium-Ionen-Akkus an Kapazität verlieren")
    assert any(process in fact["claim"] for fact in result.facts)
    assert result.package["status"] == "sufficient"


@pytest.mark.parametrize("question,answer", [
    ("Warum verlieren Motoren an Leistung?", "Die Leistung der Motoren nimmt ab, weil ihre Lager verschleißen."),
    ("Warum nimmt die Leistung der Motoren ab?", "Motoren verlieren Leistung, weil ihre Lager verschleißen."),
])
def test_change_predicate_equivalents_preserve_the_actual_cause(question, answer):
    assert not core_issues(question_frame(question), answer)


def test_compound_entity_requires_both_parts_of_its_reference():
    frame = question_frame(BATTERY)
    assert "verlieren" in frame.predicate
    assert "Handyakkus" in entity_coverage(frame, "Im Handy verwenden wir Akkus.")[1]
    assert core_issues(frame, "Akkus verlieren Kapazität, weil die aktiven Bestandteile chemisch reagieren.")


def test_adjectival_name_still_resolves_its_acronym():
    frame = question_frame("Kann künstliche Intelligenz Schmerz empfinden?")
    assert "Intelligenz" in entity_coverage(frame, "KI kann Schmerz empfinden.")[1]


def test_context_cannot_promote_a_company_process_even_on_a_matching_page():
    frame = question_frame("Warum ist der See trüb?")
    unrelated = "Das Unternehmen bildet neue Teams in der Verwaltung."
    assert core_issues(frame, unrelated, context="Der See ist trüb. " + unrelated)
    assert mechanism_issues(frame, unrelated, "Der See ist trüb. " + unrelated)


def test_english_heartbeat_question_rejects_a_circulation_consequence():
    frame = question_frame("Why does the heart beat without orders from the brain?", "en")
    assert core_issues(frame, "The heart therefore influences circulation in the brain and the processing of perceptions.")
    assert not core_issues(frame, "The heart beats without orders from the brain because its own pacemaker generates electrical impulses.")


@pytest.mark.parametrize("question", ["Wie viele Sensoren gibt es?", "How many sensors are there?"])
def test_quantity_questions_are_not_mistaken_for_process_questions(question):
    assert question_frame(question, "de" if question.startswith("Wie") else "en").qtype != "how"


def test_reported_finding_can_still_explain_the_actual_cause():
    frame = question_frame("Warum verlieren Motoren an Leistung?")
    text = "Forscher fanden heraus, dass Motoren Leistung verlieren, weil ihre Lager sich zersetzen."
    assert not core_issues(frame, text)


@pytest.mark.parametrize("valid_ids", [True, False])
def test_existing_synthesis_budget_and_citation_checks_are_preserved(valid_ids):
    class Provider:
        calls = 0

        def decompose(self, *_args):
            self.calls += 1

        def synthesize(self, payload):
            self.calls += 1
            item = next(item for item in payload["evidence"] if DEGRADATION in item["text"])
            return SynthesisOut(core_answer={"text": DEGRADATION, "evidence_ids": [item["id"] if valid_ids else "invented"]})

    provider = Provider()
    result = run(BATTERY, [ANNOUNCEMENT, DEGRADATION], llm=provider)
    assert provider.calls == result.diagnostics["budget"]["used"]["llm_calls"] == 2
    assert result.package["status"] == "sufficient" and DEGRADATION in core(result)
    if not valid_ids:
        assert result.package["synthesis"] == "llm_rejected_fallback_deterministic"
        assert any("unknown_evidence_id" in item.get("reason", "") for item in result.package["rejected_claims"])


def test_minimal_native_answer_satisfies_required_qac_with_optional_context_absent():
    from test_question_answer_contract import contract, coverage

    from clipforge.question_answer_contract import checked_coverage

    result = run(BATTERY, [DEGRADATION], title="Handyakkus verlieren mit der Zeit Kapazität")
    model = contract(BATTERY, "Explain capacity loss through disconnected lithium deposits")
    ids = [fact["id"] for fact in result.facts if DEGRADATION in fact["claim"]]
    assert ids
    checked = checked_coverage(model, coverage(ids), result.facts)
    assert checked.is_sufficient and checked.missing_obligations == []
    assert not model.required_supporting_obligations[0].is_required
    missing = checked_coverage(model, coverage(ids), [])
    assert not missing.is_sufficient and "primary" in missing.missing_obligations
