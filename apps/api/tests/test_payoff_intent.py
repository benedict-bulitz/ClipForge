"""Payoff intent: a "why" video never closes on advice nobody asked for.

The real run "Warum essen wir weiter, obwohl wir schon satt sind?" completed
its causal explanation ("Darum kann besonders verlockendes Essen dein
Sättigungsgefühl überstimmen.") and then switched from *why it happens* to
*what to do about it* ("Achtsames Essen ... können helfen, aufzuhören").
The narration below is the run's script verbatim; the research facts are
reconstructed from what it says.  Production code has no topic vocabulary.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_triple_hook import Judge, ai_candidate, generation

from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import (
    assess_information_gain,
    build_novelty_plan,
    prune_redundant_information,
)
from clipforge.pipeline import _normalise_blocks, build_initial_state
from clipforge.question_intent import (
    advice_off_intent,
    answer_mode,
    asks_for_advice,
    gives_advice,
    interpret_question,
)
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import arc_units, build_story_arc

QUESTION = "Warum essen wir weiter, obwohl wir schon satt sind?"
HOOK = "Warum greifst du manchmal noch mal zu, obwohl du schon satt bist?"
E_ANSWER = "Wenn du etwas sehr Leckeres siehst, bekommst du oft wieder Lust auf Essen, obwohl du schon satt bist."
E_PROTEIN = "Dabei könnte ein Protein namens Nociceptin eine Rolle spielen."
E_SUSPECT = "Es steht im Verdacht, das Weiteressen trotz Sättigung zu fördern."
E_REWARD = "Außerdem schaltet sich dein Belohnungssystem ein."
E_SURVIVAL = "Es macht Essen angenehm, weil Essen unserem Körper früher beim Überleben geholfen hat."
E_OVERRIDE = "Darum kann besonders verlockendes Essen dein Sättigungsgefühl überstimmen."
E_ADVICE = "Achtsames Essen und frische, wenig verarbeitete Lebensmittel können helfen, aufzuhören, wenn du satt bist."
REAL_SCRIPT = [E_ANSWER, E_PROTEIN, E_SUSPECT, E_REWARD, E_SURVIVAL, E_OVERRIDE, E_ADVICE]
REAL_ROLES = ["answer", "support", "detail", "explanation", "detail", "detail", "payoff"]


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW" if importance >= 0.7 else "USEFUL",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


FACTS = [
    fact(1, "Der Anblick von sehr leckerem Essen weckt oft wieder Lust auf Essen, auch wenn man schon satt ist.", 0.9),
    fact(2, "Das Protein Nociceptin steht im Verdacht, das Weiteressen trotz Sättigung zu fördern.", 0.75),
    fact(3, "Das Belohnungssystem macht Essen angenehm, weil Essen früher beim Überleben half; deshalb kann verlockendes "
            "Essen das Sättigungsgefühl überstimmen.", 0.85),
    fact(4, "Achtsames Essen und frische, wenig verarbeitete Lebensmittel können helfen, mit dem Essen aufzuhören, wenn man satt ist.", 0.6),
]
# The Script Writer V2 blocks of the run (the pipeline splits them into sentences).
WRITER_BLOCKS = [
    {"role": "answer", "text": E_ANSWER, "fact_ids": ["fact_01"]},
    {"role": "support", "text": f"{E_PROTEIN} {E_SUSPECT}", "fact_ids": ["fact_02"]},
    {"role": "explanation", "text": f"{E_REWARD} {E_SURVIVAL} {E_OVERRIDE}", "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": E_ADVICE, "fact_ids": ["fact_04"]},
]


def intent(question: str = QUESTION) -> dict:
    return {
        "topic": question, "question": question, "content_type": "factual_explainer", "language": "de",
        "research_required": True, "question_intent": interpret_question(question, "de"),
    }


def state_for(blocks: list[dict], question: str = QUESTION, facts: list[dict] = FACTS) -> dict:
    novelty = build_novelty_plan(intent(question), facts)
    arc = build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty)
    return {"intent": intent(question), "facts": copy.deepcopy(facts), "story_arc": arc, "novelty_plan": novelty, "script": {"blocks": blocks}}


def normalised() -> list[dict]:
    return _normalise_blocks([{"role": "hook", "text": HOOK}, *copy.deepcopy(WRITER_BLOCKS)], 90)


def repaired(question: str = QUESTION) -> tuple[list[dict], list[dict], dict]:
    blocks = normalised()
    state = state_for(blocks, question)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return pruned, repairs, state


def body(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


def fact_ids(blocks: list[dict]) -> set[str]:
    return {fact_id for block in blocks for fact_id in block.get("fact_ids") or []}


# ---------------------------------------------------------------------------
# Question Intent: what kind of answer is owed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("question", "mode"), [
    (QUESTION, "explanation"),
    ("Warum vergeht die Zeit schneller, je älter man wird?", "explanation"),
    ("Warum öffnen wir TikTok, obwohl wir es nicht wollten?", "explanation"),
    ("Wie funktioniert ein Kühlschrank?", "explanation"),
    ("Warum essen wir weiter, obwohl wir schon satt sind, und was kann ich dagegen tun?", "advice"),
    ("Wie kann ich aufhören zu essen, wenn ich satt bin?", "advice"),
    ("Wie schaffe ich es, früher einzuschlafen?", "advice"),
    ("Welche Lebensmittel empfiehlt ihr gegen Heißhunger?", "advice"),
    ("Welches Öl ist am besten zum Braten?", "advice"),
    ("How do I stop snacking at night?", "advice"),
    ("Why do we keep eating when we are full?", "explanation"),
])
def test_the_question_says_whether_it_wants_an_explanation_or_advice(question, mode):
    assert answer_mode(question) == mode
    assert interpret_question(question)["answer_mode"] == mode


@pytest.mark.parametrize("text", [
    E_ADVICE,
    "Du solltest langsamer essen.",
    "Ein Tipp: Iss langsamer.",
    "Versuch es mal mit kleineren Tellern.",
    "Wasser vor dem Essen kann dir helfen, weniger zu essen.",
    "Mindful eating can help you stop when you are full.",
    "You should drink water first.",
])
def test_advice_recommendations_and_instructions_are_recognised(text):
    assert gives_advice(text)


@pytest.mark.parametrize("text", [
    E_SURVIVAL,  # "geholfen hat": a cause, not advice
    E_OVERRIDE,
    E_SUSPECT,
    "Das Hormon Leptin hilft dem Körper, den Energiehaushalt zu regeln.",  # a mechanism, nobody is told what to do
    "Insulin can help cells absorb sugar.",
    "Deshalb isst du weiter, obwohl du satt bist.",
])
def test_explanations_are_not_mistaken_for_advice(text):
    assert not gives_advice(text)


def test_advice_is_off_intent_only_for_an_explanation_question():
    assert advice_off_intent(QUESTION, E_ADVICE)
    assert not advice_off_intent(f"{QUESTION[:-1]} und was kann ich dagegen tun?", E_ADVICE)
    assert not advice_off_intent("Wie kann ich aufhören zu essen, wenn ich satt bin?", E_ADVICE)
    assert not advice_off_intent("Welche Lebensmittel helfen gegen Heißhunger?", E_ADVICE)
    assert not asks_for_advice(QUESTION)


# ---------------------------------------------------------------------------
# 1. WHY question + completed explanation + advice tail -> advice removed
# ---------------------------------------------------------------------------

def test_the_real_script_before_repair_ends_on_off_intent_advice():
    blocks = normalised()
    assert body(blocks) == REAL_SCRIPT
    assert [block["role"] for block in blocks[1:]] == REAL_ROLES
    report = assess_information_gain(state_for(blocks))
    units = {unit["text"]: unit for unit in report["units"]}
    assert units[E_ADVICE]["off_intent"] == "advice" and units[E_ADVICE]["is_payoff"]
    assert units[E_OVERRIDE]["explanatory_delta"] == "advances_explanation"
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "weak_resolution"
    assert "what to do" in report["payoff"]["reason"]


def test_the_story_arc_never_closes_a_why_video_on_advice():
    novelty = build_novelty_plan(intent(), FACTS)
    arc = build_story_arc(intent(), FACTS, plan_format(intent(), FACTS, [], novelty), novelty)
    units = arc_units(arc)
    assert units["fact_04"]["question_link"] == "off_intent_advice" and units["fact_04"]["off_question"]
    assert units["fact_04"]["may_be_omitted"] and arc["final_payoff_id"] != "fact_04"
    assert "fact_04" in arc["question_contract"]["off_question_ids"]


def test_the_advice_tail_is_removed_and_the_explanation_becomes_the_payoff():
    pruned, repairs, state = repaired()
    texts = body(pruned)
    assert E_ADVICE not in texts
    assert pruned[-1]["text"] == E_OVERRIDE and pruned[-1]["role"] == "payoff"
    removal = next(repair for repair in repairs if repair["text"] == E_ADVICE)
    assert removal["action"] == "remove_off_intent_payoff" and removal["category"] == "off_intent_advice"
    # The advice fact leaves with the advice; it is not pinned on the explanation.
    assert removal["dropped_fact_ids"] == ["fact_04"] and "fact_04" not in pruned[-1]["fact_ids"]
    report = assess_information_gain(state)
    assert report["payoff"]["status"] == "pass" and report["payoff"]["text"] == E_OVERRIDE
    assert not any(unit.get("off_intent") for unit in report["units"])


def test_nothing_is_invented_and_the_explanation_stays_intact():
    before = normalised()
    pruned, _repairs, state = repaired()
    texts = body(pruned)
    # 5. No text is invented, order is kept.
    assert texts == [text for text in REAL_SCRIPT if text in texts]
    # The causal path survives: observation, mechanism, consequence.
    assert {E_ANSWER, E_REWARD, E_SURVIVAL, E_OVERRIDE} <= set(texts)
    # 6. Protected evidence: the answer and the arc anchors stay told.
    arc = state["story_arc"]
    assert {arc["primary_answer_id"], arc["final_payoff_id"]} <= fact_ids(pruned)
    assert fact_ids(before) - fact_ids(pruned) == {"fact_04"}
    report = assess_information_gain(state)
    assert all(unit["evidence"]["status"] in {"supported", "derived"} for unit in report["units"] if unit["category"] != "hook")
    # Idempotent.
    again, more = prune_redundant_information(copy.deepcopy(pruned), state)
    assert again == pruned and more == []


def test_advice_in_the_middle_after_the_explanation_is_a_weak_tail():
    blocks = [
        {"id": "b1", "role": "hook", "text": HOOK},
        {"id": "b2", "role": "answer", "text": E_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "explanation", "text": E_SURVIVAL, "fact_ids": ["fact_03"]},
        {"id": "b4", "role": "detail", "text": E_ADVICE, "fact_ids": ["fact_04"]},
        {"id": "b5", "role": "payoff", "text": E_OVERRIDE, "fact_ids": ["fact_03"]},
    ]
    state = state_for(blocks)
    units = {unit["text"]: unit for unit in assess_information_gain(state)["units"]}
    assert units[E_ADVICE]["weak_tail"] and units[E_ADVICE]["explanatory_delta"] == "tangent"
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert E_ADVICE not in body(pruned) and pruned[-1]["text"] == E_OVERRIDE
    assert any(repair["action"] == "remove_off_intent" for repair in repairs)


def test_advice_without_a_completed_explanation_is_reported_not_hidden():
    # Nothing explanatory to inherit the payoff: the failure stays visible.
    blocks = [
        {"id": "b1", "role": "hook", "text": HOOK},
        {"id": "b2", "role": "answer", "text": E_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "payoff", "text": E_ADVICE, "fact_ids": ["fact_04"]},
    ]
    state = state_for(blocks)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert pruned == blocks and repairs == []
    assert assess_information_gain(state)["payoff"]["status"] == "fail"


# ---------------------------------------------------------------------------
# 2-4. Advice that was asked for stays
# ---------------------------------------------------------------------------

def test_an_explicit_advice_question_keeps_the_advice_payoff():
    question = "Warum essen wir weiter, obwohl wir schon satt sind, und was kann ich dagegen tun?"
    pruned, repairs, state = repaired(question)
    assert body(pruned)[-1] == E_ADVICE and pruned[-1]["role"] == "payoff"
    assert not any(repair["text"] == E_ADVICE for repair in repairs)
    report = assess_information_gain(state)
    assert not any(unit.get("off_intent") for unit in report["units"])
    assert "what to do" not in report["payoff"].get("reason", "")
    assert arc_units(state["story_arc"])["fact_04"]["question_link"] != "off_intent_advice"


@pytest.mark.parametrize(("question", "facts", "steps", "closing"), [
    (  # 3. HOW-TO: instructions are the answer
        "Wie kann ich aufhören zu essen, wenn ich satt bin?",
        [fact(1, "Langsames Essen gibt dem Sättigungssignal Zeit, das Gehirn zu erreichen.", 0.9),
         fact(2, "Achtsames Essen kann helfen, mit dem Essen aufzuhören, wenn man satt ist.", 0.85)],
        ["Iss langsamer, damit dein Sättigungssignal ankommt."],
        "Achtsames Essen kann dir helfen, aufzuhören, wenn du satt bist.",
    ),
    (  # 4. Recommendation: the recommendation is the answer
        "Welche Lebensmittel empfiehlt man gegen Heißhunger?",
        [fact(1, "Eiweißreiche Lebensmittel halten lange satt.", 0.9),
         fact(2, "Frische, wenig verarbeitete Lebensmittel können helfen, Heißhunger zu vermeiden.", 0.85)],
        ["Eiweißreiche Lebensmittel halten dich lange satt."],
        "Frische, wenig verarbeitete Lebensmittel können dir helfen, Heißhunger zu vermeiden.",
    ),
])
def test_how_to_and_recommendation_questions_may_close_on_advice(question, facts, steps, closing):
    blocks = [
        {"id": "b1", "role": "hook", "text": "Das kennst du bestimmt."},
        {"id": "b2", "role": "answer", "text": steps[0], "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "payoff", "text": closing, "fact_ids": ["fact_02"]},
    ]
    state = state_for(blocks, question, facts)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert pruned[-1]["text"] == closing and pruned[-1]["role"] == "payoff"
    assert not any(repair.get("action") in {"remove_off_intent", "remove_off_intent_payoff"} for repair in repairs)
    report = assess_information_gain(state)
    assert not any(unit.get("off_intent") for unit in report["units"])
    assert report["payoff"]["status"] == "pass"


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


def test_generation_removes_the_advice_tail(monkeypatch, tmp_path):
    source = {"label": "Quelle", "url": "https://source.test/satt"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    candidates = [ai_candidate("A", "curiosity_gap", HOOK, "person reaching for dessert", ["person reaching for dessert"],
                               action="reaching", detail="dessert", payoff_fact="fact_03")]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings)

    blocks = [block for block in state["script"]["blocks"] if block["role"] != "hook"]
    texts = [block["text"] for block in blocks]
    assert set(texts) <= set(REAL_SCRIPT)  # nothing invented
    assert E_ADVICE not in texts
    assert texts[-1] == E_OVERRIDE and blocks[-1]["role"] == "payoff"
    assert state["intent"]["question_intent"]["answer_mode"] == "explanation"
    gain = state["information_gain"]
    assert gain["payoff"]["status"] == "pass"
    assert any(repair["action"] == "remove_off_intent_payoff" for repair in gain.get("repairs") or [])
