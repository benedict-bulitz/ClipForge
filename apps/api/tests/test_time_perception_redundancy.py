"""Second real regression: "Zeit vergeht schneller, je älter man wird".

A second real Mac generation still repeated itself in other words: the
explanation restated the hook's mechanism ("weniger neue Erlebnisse hängen
bleiben" / "weniger neue Erlebnisse, die einen starken Eindruck
hinterlassen"), the support sentence restated the answer as survey evidence
("Ältere erleben Zeit schneller" / "Ältere wählen häufiger die Antwort, Zeit
vergehe schneller") and the payoff restated the age correlation once more
("mit zunehmendem Alter wird dieser Effekt stärker").  The script below is
that run's text verbatim; the question and the research facts are shaped
like its evidence.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_triple_hook import Judge, ai_candidate, generation

from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import (
    REDUNDANT_CATEGORIES,
    assess_information_gain,
    build_novelty_plan,
    classify_gain,
    prune_redundant_information,
)
from clipforge.pipeline import _normalise_blocks, build_initial_state
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import build_story_arc
from clipforge.verbal_hook import information_gain, proposition_words

QUESTION = "Warum vergeht die Zeit schneller, je älter man wird?"
HOOK = "Je weniger neue Erlebnisse hängen bleiben, desto schneller kann sich Zeit anfühlen."
S_ANSWER = (
    "Ältere Erwachsene erleben die letzten zehn Jahre und die Zeit insgesamt im Durchschnitt eher als "
    "schneller vergangen als jüngere."
)
S_NUANCE = "Die Unterschiede sind aber eher klein."
S_EXPLANATION = "Ein Grund: Mit dem Alter gibt es oft weniger neue Erlebnisse, die einen starken Eindruck hinterlassen."
S_ROUTINE = "Mehr Abläufe werden vertraut und laufen fast automatisch."
S_SUPPORT = "In Befragungen wählen ältere Menschen deshalb häufiger die Antwort, dass Zeit immer schneller vergeht."
S_PAYOFF = "Und mit zunehmendem Alter wird dieser Effekt stärker."
REAL_SCRIPT = [S_ANSWER, S_NUANCE, S_EXPLANATION, S_ROUTINE, S_SUPPORT, S_PAYOFF]
REAL_ROLES = ["answer", "detail", "explanation", "detail", "support", "payoff"]


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW" if importance >= 0.7 else "USEFUL",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


FACTS = [
    fact(1, "Ältere Erwachsene erleben die vergangenen zehn Jahre und die Zeit insgesamt im Durchschnitt als schneller "
            "vergangen als jüngere Erwachsene; die Unterschiede sind klein.", 0.9),
    fact(2, "Mit zunehmendem Alter gibt es weniger neue, einprägsame Erlebnisse, weil mehr Abläufe zur Routine werden "
            "und automatisch laufen.", 0.85),
    fact(3, "In Befragungen geben ältere Menschen häufiger an, dass die Zeit immer schneller vergeht.", 0.75),
    fact(4, "Wenige einprägsame Erinnerungen lassen einen Zeitraum im Rückblick kürzer erscheinen, deshalb wirkt die "
            "Zeit schneller vergangen.", 0.8),
]
# The Script Writer V2 blocks of the run (the pipeline splits them into sentences).
WRITER_BLOCKS = [
    {"role": "answer", "text": f"{S_ANSWER} {S_NUANCE}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{S_EXPLANATION} {S_ROUTINE}", "fact_ids": ["fact_02"]},
    {"role": "support", "text": S_SUPPORT, "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": S_PAYOFF, "fact_ids": ["fact_04"]},
]
INTENT = {"topic": QUESTION, "question": QUESTION, "content_type": "factual_explainer", "language": "de", "research_required": True}


def arc_state(blocks: list[dict], facts: list[dict] = FACTS) -> dict:
    novelty = build_novelty_plan(INTENT, facts)
    arc = build_story_arc(INTENT, facts, plan_format(INTENT, facts, [], novelty), novelty)
    return {
        "intent": INTENT, "facts": copy.deepcopy(facts), "story_arc": arc, "novelty_plan": novelty,
        "script": {"blocks": blocks},
    }


def normalised() -> list[dict]:
    return _normalise_blocks([{"role": "hook", "text": HOOK}, *copy.deepcopy(WRITER_BLOCKS)], 90)


def processed(state: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    blocks = normalised()
    state = state or arc_state(blocks)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return pruned, repairs, state


def texts(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


# ---------------------------------------------------------------------------
# The real script
# ---------------------------------------------------------------------------

def test_the_real_script_before_repair_is_diagnosed_as_semantically_repetitive():
    blocks = normalised()
    assert texts(blocks) == REAL_SCRIPT
    assert [block["role"] for block in blocks[1:]] == REAL_ROLES
    report = assess_information_gain(arc_state(blocks))
    units = {unit["text"]: unit for unit in report["units"]}
    hook_id = units[HOOK]["block_id"]
    # The explanation restates the hook's mechanism in other words.
    assert units[S_EXPLANATION]["category"] in REDUNDANT_CATEGORIES
    assert units[S_EXPLANATION]["redundancy"] == "restates_hook" and units[S_EXPLANATION]["repeats_block_id"] == hook_id
    # The survey sentence and the payoff restate the age correlation.
    assert units[S_SUPPORT]["category"] in REDUNDANT_CATEGORIES
    assert units[S_PAYOFF]["category"] in REDUNDANT_CATEGORIES
    # Nuance and routine are real information.
    assert units[S_NUANCE]["counts_as_gain"] and units[S_ROUTINE]["counts_as_gain"]
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "repeats_earlier"
    assert report["status"] == "diluted" and report["summary"]["redundant_units"] == 3


def test_hook_and_explanation_do_not_both_survive():
    pruned, repairs, _state = processed()
    assert HOOK in [block["text"] for block in pruned]  # the hook is owned by hook selection
    assert S_EXPLANATION not in texts(pruned)
    assert any(repair["text"] == S_EXPLANATION for repair in repairs)


def test_answer_and_its_survey_restatement_do_not_both_survive():
    pruned, repairs, _state = processed()
    body = texts(pruned)
    assert S_ANSWER in body and S_SUPPORT not in body
    merged = next(repair for repair in repairs if repair["text"] == S_SUPPORT)
    # The survey fact moves to the answer sentence, which already says it.
    answer = next(block for block in pruned if block["text"] == S_ANSWER)
    assert merged["moved_fact_ids"] == ["fact_03"] and "fact_03" in answer["fact_ids"]


def test_the_payoff_is_the_final_explanatory_beat_not_another_age_correlation():
    pruned, repairs, state = processed()
    assert S_PAYOFF not in texts(pruned)
    handed = next(repair for repair in repairs if repair["action"] == "hand_over_payoff")
    assert handed["text"] == S_PAYOFF and handed["category"] == "restatement"
    assert pruned[-1]["text"] == S_ROUTINE and pruned[-1]["role"] == "payoff"
    # The arc's final payoff fact stays on the closing unit.
    assert state["story_arc"]["final_payoff_id"] in pruned[-1]["fact_ids"]
    report = assess_information_gain(state)
    assert report["payoff"]["status"] == "pass" and report["payoff"]["text"] == S_ROUTINE
    assert report["summary"]["redundant_units"] == 0 and report["status"] == "dense"


def test_the_processed_script_is_fact_nuance_mechanism_without_paraphrases():
    before = normalised()
    pruned, _repairs, state = processed()
    body = texts(pruned)
    # fact -> nuance -> mechanism (which closes on the hook's consequence).
    assert body == [S_ANSWER, S_NUANCE, S_ROUTINE]
    # Compressed (no fixed count), nothing invented, order kept.
    assert len(body) < len(REAL_SCRIPT) and body == [text for text in REAL_SCRIPT if text in body]
    report = assess_information_gain(state)
    assert report["hook_transition"]["status"] == "pass"
    assert all(unit["evidence"]["status"] in {"supported", "derived"} for unit in report["units"] if unit["category"] != "hook")

    def ids(blocks: list[dict]) -> set[str]:
        return {fact_id for block in blocks for fact_id in block.get("fact_ids") or []}

    assert ids(pruned) == ids(before)
    # Idempotent: a second pass finds nothing more to remove.
    again, more = prune_redundant_information(copy.deepcopy(pruned), state)
    assert again == pruned and more == []


def test_an_arc_that_anchors_the_answer_on_the_survey_fact_still_merges_it():
    # A planner may name the restating evidence as the primary answer; it is
    # then told by the earlier answer sentence it repeats.
    blocks = normalised()
    state = arc_state(blocks)
    state["story_arc"]["primary_answer_id"] = "fact_03"
    pruned, _repairs, _state = processed(state)
    assert S_SUPPORT not in texts(pruned)
    assert "fact_03" in next(block for block in pruned if block["text"] == S_ANSWER)["fact_ids"]


# ---------------------------------------------------------------------------
# Novelty plan and Story Arc on the research
# ---------------------------------------------------------------------------

def test_survey_evidence_restating_the_answer_is_a_redundant_fact():
    novelty = build_novelty_plan(INTENT, FACTS)
    assert novelty["redundant_candidates"] == ["fact_03"]
    arc = build_story_arc(INTENT, FACTS, plan_format(INTENT, FACTS, [], novelty), novelty)
    assert arc["primary_answer_id"] == "fact_01"
    assert arc["final_payoff_id"] != arc["primary_answer_id"] and arc["order"][-1] == arc["final_payoff_id"]


def test_survey_evidence_with_its_own_figure_is_not_redundant():
    facts = [*FACTS[:2], fact(3, "In einer Befragung von 500 Menschen gaben 60 Prozent der Älteren an, dass die Zeit schneller vergeht.", 0.75), FACTS[3]]
    assert "fact_03" not in build_novelty_plan(INTENT, facts)["redundant_candidates"]


# ---------------------------------------------------------------------------
# Causal paraphrases, reported conclusions and trends (any topic)
# ---------------------------------------------------------------------------

def test_memory_paraphrases_are_one_concept():
    for text in (HOOK, S_EXPLANATION, "Neue Erlebnisse bleiben im Gedächtnis.", "Das hinterlässt einen bleibenden Eindruck.",
                 "New experiences stick in your memory.", "New experiences leave a lasting impression."):
        assert "+memorable" in proposition_words(text), text
    assert "+novel" in proposition_words(HOOK) and "+novel" in proposition_words(S_EXPLANATION)


def test_a_frequency_word_that_only_hedges_is_not_a_frequency_claim():
    assert "+frequent" not in proposition_words(S_EXPLANATION)  # "oft weniger"
    assert "+frequent" not in proposition_words(S_SUPPORT)  # "wählen häufiger die Antwort"
    assert "+frequent" in proposition_words("Du schaust dich viel öfter im Spiegel an als andere.")


def test_the_explanation_says_the_hooks_mechanism_again():
    # Other words, one proposition: fewer new experiences are remembered.
    mechanism = proposition_words(S_EXPLANATION) - {"+age"}
    assert mechanism == {"+novel", "+memorable", "erlebnisse"} and mechanism <= proposition_words(HOOK)
    # Its only other content ("mit dem Alter") is the answer's.
    assert information_gain(f"{HOOK} {S_ANSWER}", S_EXPLANATION) == []


@pytest.mark.parametrize(("earlier", "later"), [
    ("Je weniger neue Erlebnisse hängen bleiben, desto schneller vergeht die Zeit.",
     "Es gibt weniger neue Erlebnisse, die einen starken Eindruck hinterlassen."),
    ("Neue Erlebnisse bleiben im Gedächtnis hängen.", "Neue Erlebnisse hinterlassen einen starken Eindruck."),
    ("Neue Erlebnisse bleiben uns in Erinnerung.", "Neue Erlebnisse sind einprägsam."),
    ("New experiences stick in your memory.", "New experiences leave a strong impression."),
])
def test_german_causal_paraphrases_are_the_same_proposition(earlier, later):
    assert classify_gain(earlier, later)["category"] in REDUNDANT_CATEGORIES


@pytest.mark.parametrize(("earlier", "later"), [
    (S_ANSWER, S_SUPPORT),
    ("Ältere erleben die Zeit schneller als Jüngere.", "In Befragungen wählen Ältere häufiger die Antwort, dass die Zeit schneller vergeht."),
    ("Ältere erleben die Zeit schneller als Jüngere.", "Studien zeigen: Ältere empfinden, dass die Zeit schneller vergeht."),
    ("Older adults feel that time passes faster than younger adults do.",
     "In surveys, older people more often answer that time passes ever faster."),
])
def test_evidence_that_only_reports_the_conclusion_is_redundant(earlier, later):
    assert classify_gain(earlier, later)["category"] in REDUNDANT_CATEGORIES


@pytest.mark.parametrize(("earlier", "later"), [
    (S_ANSWER, S_PAYOFF),
    ("Ältere erleben die Zeit schneller als Jüngere.", "Mit zunehmendem Alter wird dieser Effekt stärker."),
    ("Older adults feel time passes faster than younger adults.", "With age, this effect grows stronger."),
])
def test_a_trend_along_an_already_compared_axis_is_redundant(earlier, later):
    assert classify_gain(earlier, later)["category"] in REDUNDANT_CATEGORIES


@pytest.mark.parametrize(("earlier", "later"), [
    (HOOK, S_ROUTINE),  # routine: the mechanism behind fewer new experiences
    (S_ANSWER, S_NUANCE),  # the size of the effect
    (S_ANSWER, "In einer Befragung von 500 Menschen sagten 60 Prozent der Älteren, die Zeit vergehe schneller."),  # figures
    (S_ANSWER, "Eine Studie der Universität Basel bestätigt das."),  # a named source
    (S_ANSWER, "Mit zunehmendem Alter wird dieser Effekt schwächer."),  # the opposite trend
    ("Ältere erleben die Zeit schneller.", "Mit zunehmendem Alter wird dieser Effekt stärker."),  # no comparison yet
    (HOOK, "Im Rückblick wirkt ein Zeitraum mit wenigen Erinnerungen deshalb kürzer."),  # retrospective compression
    ("Neue Erlebnisse bleiben im Gedächtnis.", "Routine hinterlässt kaum Erinnerungen."),
])
def test_sentences_with_new_content_are_not_over_pruned(earlier, later):
    result = classify_gain(earlier, later)
    assert result["category"] not in REDUNDANT_CATEGORIES, result


def test_a_negated_impression_is_not_absorbed_into_the_memory_phrase():
    assert "-memorable" in proposition_words("Routine hinterlässt keinen Eindruck.")
    assert "-memorable" in proposition_words("Routine bleibt nicht hängen.")
    assert classify_gain("Neue Erlebnisse hinterlassen einen Eindruck.", "Neue Erlebnisse hinterlassen keinen Eindruck.")["category"] not in REDUNDANT_CATEGORIES


# ---------------------------------------------------------------------------
# Payoff hand-over is conservative
# ---------------------------------------------------------------------------

def test_a_closing_restatement_after_the_answer_alone_is_reported_not_removed():
    blocks = [
        {"id": "b1", "role": "hook", "text": HOOK},
        {"id": "b2", "role": "answer", "text": S_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "payoff", "text": S_PAYOFF, "fact_ids": ["fact_04"]},
    ]
    state = arc_state(blocks)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    # The answer never becomes the payoff: nothing is removed, the report fails.
    assert pruned == blocks and repairs == []
    assert assess_information_gain(state)["payoff"]["status"] == "fail"


def test_the_answers_own_continuation_never_becomes_the_payoff():
    blocks = [
        {"id": "b1", "role": "hook", "text": HOOK},
        {"id": "b2", "role": "answer", "text": S_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "detail", "text": S_NUANCE, "fact_ids": ["fact_01"]},
        {"id": "b4", "role": "payoff", "text": S_PAYOFF, "fact_ids": ["fact_04"]},
    ]
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), arc_state(blocks))
    assert pruned == blocks and repairs == []


def test_a_closing_sentence_with_one_new_word_keeps_the_payoff():
    blocks = [
        {"id": "b1", "role": "hook", "text": "Rund 270.000 gegen etwa 17.000 Inseln – welches Land ist welches?"},
        {"id": "b2", "role": "answer", "text": "Schweden hat rund 267.570 Inseln.", "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "explanation", "text": "Gletscher haben Schwedens Küste zerklüftet.", "fact_ids": ["fact_02"]},
        {"id": "b4", "role": "payoff", "text": "Schweden ist trotzdem das größte Inselland.", "fact_ids": ["fact_03"]},
    ]
    facts = [fact(1, "Schweden hat rund 267.570 Inseln.", 0.9), fact(2, "Gletscher haben Schwedens Küste zerklüftet."),
             fact(3, "Schweden ist das größte Inselland.")]
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), arc_state(blocks, facts))
    assert pruned[-1]["text"] == blocks[-1]["text"] and not any(repair["action"] == "hand_over_payoff" for repair in repairs)


# ---------------------------------------------------------------------------
# End to end: the real writer output through generation
# ---------------------------------------------------------------------------

class Writer:
    name = "fixture-v2"

    def __init__(self, _settings=None):
        pass

    def generate(self, _request):
        return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[ScriptBlockV2(**block) for block in WRITER_BLOCKS]), "connected")


class OfflineReviewer:
    name = "fixture-review"

    def __init__(self, _settings=None):
        pass

    def review(self, _request):
        raise RuntimeError("offline")


def test_generation_compresses_the_real_script(monkeypatch, tmp_path):
    source = {"label": "Quelle", "url": "https://source.test/zeit"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    candidates = [ai_candidate("A", "direct_reframe", HOOK, "older person looking at a calendar", ["older person calendar"],
                               action="looking", detail="calendar", payoff_fact="fact_04")]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings)

    blocks = state["script"]["blocks"]
    body = [block for block in blocks if block["role"] != "hook"]
    body_texts = [block["text"] for block in body]
    assert set(body_texts) <= set(REAL_SCRIPT)  # nothing invented
    assert len(body_texts) < len(REAL_SCRIPT)
    assert S_EXPLANATION not in body_texts and S_SUPPORT not in body_texts and S_PAYOFF not in body_texts
    assert {S_ANSWER, S_NUANCE, S_ROUTINE} <= set(body_texts)
    assert body[-1]["text"] == S_ROUTINE and body[-1]["role"] == "payoff"
    gain = state["information_gain"]
    assert gain["payoff"]["status"] == "pass" and gain["summary"]["redundant_units"] == 0
    assert {fact_id for block in body for fact_id in block.get("fact_ids") or []} == {"fact_01", "fact_02", "fact_03", "fact_04"}
