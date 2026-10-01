"""Semantic (proposition-level) redundancy: the real "Fotos vs. Spiegel" run.

The first real Mac generation of "Warum sehe ich auf Fotos schlechter aus als
im Spiegel?" said the mirror-reversal fact four times in different words
("spiegelverkehrt", "vertauscht die Seiten", "fehlt diese Umkehrung", "nicht
so herum") and labelled an intermediate sentence as the payoff.  The script
below is that run's text; the research facts are shaped like its evidence.
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

QUESTION = "Warum sehe ich auf Fotos schlechter aus als im Spiegel?"
HOOK = "Auf Fotos siehst du nicht schlechter aus – du siehst nur anders aus als im Spiegel."
S_REVERSED = "Im Spiegel siehst du dein Gesicht spiegelverkehrt."
S_PHOTO_DIFFERS = "Auf einem Foto wirkt es deshalb anders als das Bild, an das du dich gewöhnt hast."
S_SWAPS_SIDES = "Der Spiegel vertauscht gewissermaßen die Seiten deines Gesichts."
S_NO_REVERSAL = "Auf Fotos fehlt diese Umkehrung – und kleine Unterschiede fallen dir dadurch stärker auf."
S_OTHERS = "Für andere Menschen ist dagegen dein Spiegelbild die ungewohnte Version."
S_NOT_THAT_WAY = "Sie sehen dich normalerweise nicht so herum wie du selbst im Spiegel."
S_MORE_OFTEN = "Du schaust dich viel öfter im Spiegel an als andere."
S_RESOLUTION = (
    "Darum fühlt sich genau dieses Bild vertraut an, während ein Foto dich in einer Version zeigt, "
    "die dir fremder vorkommt."
)
REAL_SCRIPT = [S_REVERSED, S_PHOTO_DIFFERS, S_SWAPS_SIDES, S_NO_REVERSAL, S_OTHERS, S_NOT_THAT_WAY, S_MORE_OFTEN, S_RESOLUTION]
# Pure restatements of the mirror-orientation fact (no consequence of their own).
ORIENTATION_PARAPHRASES = {S_REVERSED, S_SWAPS_SIDES, S_NOT_THAT_WAY}


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW" if importance >= 0.7 else "USEFUL",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


FACTS = [
    fact(1, "Ein Spiegel zeigt das eigene Gesicht seitenverkehrt; auf Fotos fehlt diese Umkehrung.", 0.9),
    fact(2, "Andere Menschen sehen dich so wie auf Fotos, für sie ist dein Spiegelbild die ungewohnte Version.", 0.85),
    fact(3, "Menschen sehen ihr eigenes Spiegelbild viel öfter als andere Menschen.", 0.8),
    fact(4, "Durch den Mere-Exposure-Effekt wirkt das vertraute Spiegelbild angenehmer, deshalb kommt einem das Foto fremder vor.", 0.8),
]
# The Script Writer V2 blocks of the run (the pipeline splits them into sentences).
WRITER_BLOCKS = [
    {"role": "answer", "text": f"{S_REVERSED} {S_PHOTO_DIFFERS}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{S_SWAPS_SIDES} {S_NO_REVERSAL}", "fact_ids": ["fact_01"]},
    {"role": "support", "text": f"{S_OTHERS} {S_NOT_THAT_WAY}", "fact_ids": ["fact_02"]},
    {"role": "payoff", "text": f"{S_MORE_OFTEN} {S_RESOLUTION}", "fact_ids": ["fact_03", "fact_04"]},
]
INTENT = {"topic": QUESTION, "question": QUESTION, "content_type": "factual_explainer", "language": "de", "research_required": True}


def arc_state(blocks: list[dict]) -> dict:
    novelty = build_novelty_plan(INTENT, FACTS)
    arc = build_story_arc(INTENT, FACTS, plan_format(INTENT, FACTS, [], novelty), novelty)
    return {
        "intent": INTENT, "facts": copy.deepcopy(FACTS), "story_arc": arc, "novelty_plan": novelty,
        "script": {"blocks": blocks},
    }


def normalised() -> list[dict]:
    return _normalise_blocks([{"role": "hook", "text": HOOK}, *copy.deepcopy(WRITER_BLOCKS)], 90)


def processed() -> tuple[list[dict], list[dict], dict]:
    blocks = normalised()
    state = arc_state(blocks)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return pruned, repairs, state


# ---------------------------------------------------------------------------
# The real script
# ---------------------------------------------------------------------------

def test_the_real_script_before_repair_is_diagnosed_as_semantically_repetitive():
    blocks = normalised()
    assert [block["text"] for block in blocks[1:]] == REAL_SCRIPT
    report = assess_information_gain(arc_state(blocks))
    by_text = {unit["text"]: unit for unit in report["units"]}
    # Neither shares a content word with the first telling, yet both repeat it.
    assert by_text[S_SWAPS_SIDES]["category"] in REDUNDANT_CATEGORIES
    assert by_text[S_SWAPS_SIDES]["repeats_block_id"] == by_text[S_REVERSED]["block_id"]
    assert by_text[S_NOT_THAT_WAY]["category"] in REDUNDANT_CATEGORIES
    assert report["status"] == "diluted" and report["summary"]["redundant_units"] == 2


def test_mirror_reversal_paraphrases_do_not_all_survive():
    pruned, repairs, _state = processed()
    texts = [block["text"] for block in pruned[1:]]
    surviving = ORIENTATION_PARAPHRASES & set(texts)
    # At most one sentence establishes the orientation fact ...
    assert surviving == {S_REVERSED}
    # ... another may mention it only while adding a distinct consequence.
    assert S_NO_REVERSAL in texts
    assert classify_gain(f"{HOOK} {S_REVERSED} {S_PHOTO_DIFFERS}", S_NO_REVERSAL)["category"] not in REDUNDANT_CATEGORIES
    assert {repair["text"] for repair in repairs} == {S_SWAPS_SIDES, S_NOT_THAT_WAY}
    # Compressed to fewer beats (no fixed count), nothing invented, order kept.
    assert len(texts) < len(REAL_SCRIPT)
    assert texts == [text for text in REAL_SCRIPT if text in texts]


def test_perspective_and_familiarity_mechanism_survive():
    pruned, _repairs, state = processed()
    texts = [block["text"] for block in pruned]
    assert S_OTHERS in texts  # other people see the non-mirrored version: perspective
    assert S_MORE_OFTEN in texts  # you see the mirror image far more often: mechanism
    report = assess_information_gain(state)
    units = {unit["text"]: unit for unit in report["units"]}
    assert units[S_MORE_OFTEN]["counts_as_gain"] and "+frequent" in units[S_MORE_OFTEN]["gain_terms"]
    assert units[S_OTHERS]["counts_as_gain"] and "-familiar" in units[S_OTHERS]["gain_terms"]


def test_the_final_payoff_resolves_why_the_photo_feels_wrong():
    pruned, _repairs, state = processed()
    assert pruned[-1]["text"] == S_RESOLUTION and pruned[-1]["role"] == "payoff"
    report = assess_information_gain(state)
    assert report["payoff"]["status"] == "pass" and report["payoff"]["result"] == "strong"
    assert report["payoff"]["text"] == S_RESOLUTION and report["payoff"]["category"] == "resolution"
    assert report["summary"]["redundant_units"] == 0 and report["status"] == "dense"


def test_protected_facts_and_evidence_remain_intact():
    before = normalised()
    pruned, _repairs, state = processed()
    def ids(blocks: list[dict]) -> set[str]:
        return {fact_id for block in blocks for fact_id in block.get("fact_ids") or []}

    assert ids(pruned) == ids(before)
    arc = state["story_arc"]
    for anchor in (arc["primary_answer_id"], arc["final_payoff_id"]):
        assert anchor in ids(pruned)
    report = assess_information_gain(state)
    assert all(unit["evidence"]["status"] in {"supported", "derived"} for unit in report["units"] if unit["category"] != "hook")
    # Idempotent: a second pass finds nothing more to remove.
    again, more = prune_redundant_information(copy.deepcopy(pruned), state)
    assert again == pruned and more == []


# ---------------------------------------------------------------------------
# Payoff role
# ---------------------------------------------------------------------------

def test_a_split_payoff_block_puts_the_payoff_role_on_its_resolution():
    blocks = normalised()
    roles = {block["text"]: block["role"] for block in blocks}
    assert roles[S_RESOLUTION] == "payoff" and roles[S_MORE_OFTEN] == "detail"
    # Other blocks still lead with their first sentence.
    assert roles[S_REVERSED] == "answer" and roles[S_PHOTO_DIFFERS] == "detail"


def test_an_old_state_with_the_payoff_on_the_first_sentence_still_keeps_the_resolution():
    blocks = normalised()
    for block in blocks:
        if block["text"] == S_MORE_OFTEN:
            block["role"] = "payoff"
        elif block["text"] == S_RESOLUTION:
            block["role"] = "detail"
    pruned, _repairs = prune_redundant_information(copy.deepcopy(blocks), arc_state(blocks))
    assert pruned[-1]["text"] == S_RESOLUTION
    report = assess_information_gain(arc_state(pruned))
    assert report["payoff"]["text"] == S_RESOLUTION and report["payoff"]["status"] == "pass"


def test_the_story_arc_never_closes_on_a_restatement_of_its_answer():
    facts = [
        fact(1, "Auf Fotos sehen wir unser Gesicht nicht seitenverkehrt, so wie andere Menschen es sehen.", 0.95),
        fact(2, "Im Spiegel siehst du dein Gesicht seitenverkehrt.", 0.8),
        fact(3, "Vertraute Gesichter wirken auf uns angenehmer als ungewohnte.", 0.7),
        fact(4, "Auf Fotos sehen wir uns so, wie andere uns sehen - nicht seitenverkehrt.", 0.6),
    ]
    novelty = build_novelty_plan(INTENT, facts)
    assert "fact_04" in novelty["redundant_candidates"]  # the same proposition, other words
    arc = build_story_arc(INTENT, facts, plan_format(INTENT, facts, [], novelty), novelty)
    assert arc["final_payoff_id"] != arc["primary_answer_id"]
    assert arc["order"][-1] == arc["final_payoff_id"]  # the closing beat closes
    final_claim = next(item["claim"] for item in facts if item["id"] == arc["final_payoff_id"])
    primary_claim = next(item["claim"] for item in facts if item["id"] == arc["primary_answer_id"])
    assert information_gain(primary_claim, final_claim)


def test_a_payoff_that_only_restates_the_hook_is_still_not_a_resolution():
    blocks = [
        {"id": "b1", "role": "hook", "text": HOOK},
        {"id": "b2", "role": "answer", "text": S_REVERSED, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "payoff", "text": "Darum siehst du auf Fotos einfach anders aus als im Spiegel.", "fact_ids": ["fact_04"]},
    ]
    report = assess_information_gain(arc_state(blocks))
    assert report["payoff"]["status"] == "fail" and report["payoff"]["category"] != "resolution"


# ---------------------------------------------------------------------------
# Negation and adversarial German paraphrases
# ---------------------------------------------------------------------------

def test_a_negated_hook_does_not_make_every_later_sentence_new():
    assert "negation" not in information_gain(HOOK, S_REVERSED)
    assert information_gain(f"{HOOK} {S_REVERSED}", S_SWAPS_SIDES) == []
    # A denial of the one statement it repeats is still news.
    assert information_gain("Schweden hat mehr Inseln.", "Schweden hat nicht mehr Inseln.") == ["negation"]


def test_a_missing_or_denied_relation_is_a_different_proposition():
    assert "+reverse" in proposition_words(S_REVERSED)
    assert "-reverse" in proposition_words("Auf Fotos fehlt diese Umkehrung.")
    assert "-reverse" in proposition_words(S_NOT_THAT_WAY)
    assert "-familiar" in proposition_words("Das ist die ungewohnte Version.")
    assert "+familiar" in proposition_words("Das Bild ist nicht ungewohnt.")  # double negation


@pytest.mark.parametrize(("earlier", "later"), [
    (S_REVERSED, S_SWAPS_SIDES),
    (S_REVERSED, "Dein Spiegelbild ist seitenverkehrt."),
    (S_REVERSED, "Der Spiegel zeigt dein Gesicht umgekehrt."),
    ("Auf Fotos fehlt die Umkehrung.", "Ein Foto zeigt dich nicht spiegelverkehrt."),
    ("Du hast dich an dein Spiegelbild gewöhnt.", "Dein Spiegelbild ist dir vertraut."),
    ("Auf Fotos siehst du die ungewohnte Version.", "Auf Fotos wirkst du fremd."),
    ("Du siehst dein Spiegelbild oft.", "Dein Spiegelbild schaust du häufig an."),
    ("Fotos und Spiegel zeigen dich unterschiedlich.", "Im Spiegel wirkst du anders als auf Fotos."),
])
def test_german_paraphrases_in_other_words_are_redundant(earlier, later):
    assert classify_gain(earlier, later)["category"] in REDUNDANT_CATEGORIES


@pytest.mark.parametrize(("earlier", "later"), [
    (S_REVERSED, "Du schaust dich viel öfter im Spiegel an als andere."),  # frequency
    (S_REVERSED, "Auf Fotos fehlt diese Umkehrung."),  # denied for photos
    (S_REVERSED, "Dein Gesicht ist nicht perfekt symmetrisch."),  # new cause
    (S_REVERSED, "Deshalb wirkt dein Foto ungewohnt."),  # consequence
    ("Du hast dich an dein Spiegelbild gewöhnt.", "Diesen Effekt nennt man Mere-Exposure-Effekt."),  # named mechanism
    ("Der Spiegel vertauscht links und rechts.", "Oben und unten vertauscht er dagegen nicht."),  # distinction
    ("Ein Spiegel zeigt dich seitenverkehrt.", "Eine Kamera speichert dich in etwa 12 Millisekunden."),  # number
])
def test_german_sentences_with_new_content_are_not_over_pruned(earlier, later):
    result = classify_gain(earlier, later)
    assert result["category"] not in REDUNDANT_CATEGORIES, result


def test_synonym_rewording_of_new_content_is_not_mistaken_for_repetition():
    # Different words, different facts: only shared concepts are merged.
    said = f"{HOOK} {S_REVERSED}"
    assert classify_gain(said, "Andere Menschen kennen vor allem dein nicht gespiegeltes Gesicht.")["category"] not in REDUNDANT_CATEGORIES


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
    source = {"label": "Quelle", "url": "https://source.test/spiegel"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    candidates = [ai_candidate("A", "direct_reframe", HOOK, "person comparing a photo with a mirror", ["person looking at mirror"],
                               action="looking", detail="mirror", payoff_fact="fact_04")]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings)

    body = [block for block in state["script"]["blocks"] if block["role"] != "hook"]
    texts = [block["text"] for block in body]
    assert set(texts) <= set(REAL_SCRIPT)  # nothing invented
    assert len(texts) < len(REAL_SCRIPT)
    assert len(ORIENTATION_PARAPHRASES & set(texts)) <= 1
    assert {S_OTHERS, S_MORE_OFTEN} <= set(texts)
    assert texts[-1] == S_RESOLUTION and body[-1]["role"] == "payoff"
    gain = state["information_gain"]
    assert gain["payoff"]["status"] == "pass" and gain["summary"]["redundant_units"] == 0
    assert {fact_id for block in body for fact_id in block.get("fact_ids") or []} == {"fact_01", "fact_02", "fact_03", "fact_04"}
