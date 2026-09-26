"""Hook -> body continuity: the sentence after the hook must advance the story.

Real run for "Warum bin ich nach einem Mittagsschlaf manchmal noch müder?":
HOOK "Ein Mittagsschlaf macht dich nicht automatisch müder als vorher." was
followed by ANSWER "Nach einem Mittagsschlaf bist du nicht automatisch
müder." - the same proposition twice - and only then by the new information
"Es kommt darauf an, in welcher Schlafphase du aufwachst.".  The research
and body are fixtures shaped like that run; the pipeline is the real one.
"""
from __future__ import annotations

import copy
import pathlib
from types import SimpleNamespace

import pytest
from test_story_arc import fact
from test_triple_hook import Judge, ai_candidate, generation

from clipforge.config import Settings
from clipforge.hooks import CANONICAL_STRATEGIES
from clipforge.narration import clean_narration_text
from clipforge.pipeline import _advance_after_hook, build_initial_state
from clipforge.renderer import RenderResult, _create_voice
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state
from clipforge.triple_hook import verbal_still_valid
from clipforge.verbal_hook import information_gain

QUESTION = "Warum bin ich nach einem Mittagsschlaf manchmal noch müder?"
HOOK = "Ein Mittagsschlaf macht dich nicht automatisch müder als vorher."
RESTATES = "Nach einem Mittagsschlaf bist du nicht automatisch müder."
ADVANCES = "Es kommt darauf an, in welcher Schlafphase du aufwachst."
FACTS = [
    fact(1, "Ein Mittagsschlaf macht nicht automatisch müder – entscheidend ist, in welcher Schlafphase man aufwacht.", 0.95),
    fact(2, "Nach etwa 30 Minuten gleitet der Körper in den Tiefschlaf."),
    fact(3, "Wer aus dem Tiefschlaf geweckt wird, fühlt sich oft benommen – das nennt man Schlafträgheit."),
    fact(4, "Ein kurzer Mittagsschlaf von 10 bis 20 Minuten macht wacher."),
]
BODY = [
    ScriptBlockV2(role="answer", text=f"{RESTATES} {ADVANCES}", fact_ids=["fact_01"]),
    ScriptBlockV2(role="explanation", text="Nach etwa 30 Minuten gleitet dein Körper in den Tiefschlaf.", fact_ids=["fact_02"]),
    ScriptBlockV2(role="explanation", text="Wirst du daraus geweckt, fühlst du dich benommen – das nennt man Schlafträgheit.", fact_ids=["fact_03"]),
    ScriptBlockV2(role="payoff", text="Ein kurzer Mittagsschlaf von 10 bis 20 Minuten macht dich dagegen wacher.", fact_ids=["fact_04"]),
]


# ---------------------------------------------------------------------------
# Semantic duplication and information gain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("hook", "sentence"), [
    (HOOK, RESTATES),
    ("Schweden hat mehr Inseln.", "Mehr Inseln gibt es in Schweden."),
    ("Rund 270.000 gegen etwa 17.000 Inseln – welche Zahl gehört zu wem?", "Es sind rund 270.000 gegen etwa 17.000 Inseln."),
])
def test_same_proposition_in_other_words_has_no_information_gain(hook, sentence):
    assert information_gain(hook, sentence) == []


@pytest.mark.parametrize(("hook", "sentence"), [
    (HOOK, ADVANCES),  # mechanism
    ("Ein Haus wiegt Tonnen – warum wird die Erde dadurch trotzdem nicht schwerer?", "Weil das Baumaterial bereits Teil der Erde war."),  # early answer
    ("Rund 270.000 gegen etwa 17.000 Inseln – welche Zahl gehört zu wem?", "Indonesien kommt auf etwa 17.000 Inseln."),  # next reveal step
    ("Deine Haare stellen sich auf – aber warum eigentlich?", "Kleine Muskeln an den Haarwurzeln stellen deine Haare auf."),  # cause
    ("Schweden hat mehr Inseln.", "Schweden hat nicht mehr Inseln."),  # contrast
])
def test_new_information_is_positive_gain(hook, sentence):
    assert information_gain(hook, sentence)


def test_an_answer_that_adds_information_may_follow_the_hook_directly():
    blocks = [
        {"role": "hook", "text": "Ein Haus wiegt Tonnen – warum wird die Erde dadurch trotzdem nicht schwerer?"},
        {"role": "answer", "text": "Weil das Baumaterial bereits Teil der Erde war.", "fact_ids": ["fact_01"]},
        {"role": "support", "text": "Es wird nur umgeschichtet.", "fact_ids": ["fact_02"]},
    ]
    assert _advance_after_hook(copy.deepcopy(blocks), None) == (blocks, {"action": "ok"})


# ---------------------------------------------------------------------------
# Repair order on the final blocks
# ---------------------------------------------------------------------------

def _blocks(*rows: tuple[str, str, list[str]]) -> list[dict]:
    return [{"role": role, "text": text, "fact_ids": ids} for role, text, ids in rows]


def test_the_next_advancing_block_moves_up_and_a_redundant_restatement_goes():
    blocks = _blocks(("hook", HOOK, []), ("answer", RESTATES, ["fact_01"]), ("detail", ADVANCES, ["fact_01"]), ("explanation", "Nach etwa 30 Minuten gleitet dein Körper in den Tiefschlaf.", ["fact_02"]))
    fixed, report = _advance_after_hook(blocks, None)
    assert [block["text"] for block in fixed] == [HOOK, ADVANCES, "Nach etwa 30 Minuten gleitet dein Körper in den Tiefschlaf."]
    assert report == {"action": "reordered", "moved_up": ADVANCES, "restating": RESTATES, "restating_kept": False}
    # No fact is lost: every fact id is still told.
    assert {fact_id for block in fixed for fact_id in block["fact_ids"]} == {"fact_01", "fact_02"}


def test_a_restatement_carrying_a_fact_nobody_else_tells_is_kept_later():
    blocks = _blocks(("hook", HOOK, []), ("answer", RESTATES, ["fact_09"]), ("detail", ADVANCES, ["fact_01"]))
    fixed, report = _advance_after_hook(blocks, None)
    assert [block["text"] for block in fixed] == [HOOK, ADVANCES, RESTATES]
    assert report["restating_kept"] is True


def test_dependencies_and_reveal_protection_stay_authoritative():
    arc = {
        "curiosity_gap": {"withhold_answer": True}, "primary_answer_id": "fact_03", "final_payoff_id": "fact_03",
        "hook": {"protected_ids": ["fact_03"]},
        "units": [
            {"id": "fact_01", "depends_on": []}, {"id": "fact_02", "depends_on": ["fact_01"]}, {"id": "fact_03", "depends_on": ["fact_01"]},
        ],
    }
    # fact_02 needs fact_01, which only the restating block tells; fact_03 is the protected reveal.
    blocks = _blocks(("hook", HOOK, []), ("support", RESTATES, ["fact_01"]), ("detail", ADVANCES, ["fact_02"]), ("answer", "Schweden hat die meisten Inseln.", ["fact_03"]))
    fixed, report = _advance_after_hook(copy.deepcopy(blocks), arc)
    assert report["action"] == "no_safe_reorder" and fixed == blocks


# ---------------------------------------------------------------------------
# The real run, end to end
# ---------------------------------------------------------------------------

class Writer:
    name = "fixture-v2"

    def __init__(self, _settings=None):
        pass

    def generate(self, _request):
        return ScriptWriterResult(ScriptDraftV2(language="de", blocks=BODY), "connected")


class OfflineReviewer:
    name = "fixture-review"

    def __init__(self, _settings=None):
        pass

    def review(self, _request):
        raise RuntimeError("offline")


def test_nap_run_advances_after_the_hook_through_every_stage(monkeypatch, tmp_path: pathlib.Path):
    source = {"label": "Quelle", "url": "https://source.test/nap"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    candidates = [ai_candidate("A", "counterintuitive_insight", HOOK, "person waking up on a sofa after a nap", ["person waking up sofa nap"],
                               action="stretching", detail="afternoon light", payoff_fact="fact_01")]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings)
    plan = state["script"]["triple_hook"]
    hook = plan["verbal_hook"]
    body_facts = {fact_id for block in state["script"]["blocks"][1:] for fact_id in block.get("fact_ids") or []}

    def assert_opening(current: dict) -> None:
        blocks = current["script"]["blocks"]
        texts = [block["text"] for block in blocks]
        # The selected hook stays and remains valid.
        assert blocks[0]["role"] == "hook" and texts[0] == hook == current["script"]["selected_hook"] == current["script"]["triple_hook"]["verbal_hook"]
        assert verbal_still_valid(current, hook, current["script"]["triple_hook"]["selected_strategy"])
        # The next sentence adds information; the restatement is not next.
        assert texts[1] != RESTATES and information_gain(hook, texts[1])
        assert texts[1] == ADVANCES  # the existing next information, not a new sentence
        # No invented content: every sentence is the writer's own.
        writer_sentences = {sentence for block in BODY for sentence in block.text.replace(". ", ".\n").split("\n")}
        assert set(texts[1:]) <= writer_sentences
        # No fact is dropped; the arc and payoff are intact.
        assert {fact_id for block in blocks[1:] for fact_id in block.get("fact_ids") or []} == body_facts
        assert texts[-1] == BODY[-1].text
        # Narration and captions follow the corrected order.
        assert current["script"]["text"].startswith(f"{hook} {ADVANCES}")
        assert " ".join(item["text"] for item in current["captions"]["items"]).startswith(f"{hook} {ADVANCES}")

    assert hook == HOOK and plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert state["script"]["hook_transition"]["action"] == "reordered"
    assert_opening(state)
    arc = state["story_arc"]
    assert arc["primary_answer_id"] == "fact_01" and state["payoff_plan"]

    # Review / hook enforcement / duration refit (the render stage) keep it.
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services.render_video", lambda *_a, **_k: RenderResult("/media/t.mp4", 12.0, "openai", 100))
    rendered = _render_state(state, "project-nap", 2, settings)
    assert_opening(rendered)
    captured: dict[str, object] = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=b"w" * 5000)

    monkeypatch.setattr("clipforge.renderer.OpenAI", lambda **_k: SimpleNamespace(audio=SimpleNamespace(speech=Speech())))
    rendered["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(rendered, tmp_path, settings)
    assert str(captured["input"]) == clean_narration_text(rendered["script"]["text"])
    assert str(captured["input"]).startswith(f"{hook} {ADVANCES}")
