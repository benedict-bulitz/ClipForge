"""Regression from a real run: "Warum bin ich nach einem Mittagsschlaf manchmal noch müder?".

The provider's curiosity gap ("Du schläfst kurz ein – und wachst trotzdem
völlig matschig auf.") was hard-failed for not containing a question word,
and an understandable but unnatural hook ("Bei einem Mittagsschlaf kann kurz
besser sein.") won.  A strategy is a rhetorical function, not a template;
natural spoken German matters as much as simple words.  The research and
body are fixtures shaped like that run; the pipeline is the real one.
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from test_story_arc import fact
from test_triple_hook import Judge, ai_candidate, generation, story

from clipforge.config import Settings
from clipforge.hooks import CANONICAL_STRATEGIES, strategy_function
from clipforge.narration import clean_narration_text
from clipforge.pipeline import build_initial_state
from clipforge.renderer import RenderResult, _create_voice
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state
from clipforge.verbal_hook import assess_verbal, fluency_issues, hook_context, rank_verbal

QUESTION = "Warum bin ich nach einem Mittagsschlaf manchmal noch müder?"
FACTS = [
    fact(1, "Wer nach mehr als etwa 30 Minuten aus dem Tiefschlaf geweckt wird, fühlt sich oft benommen – das nennt man Schlafträgheit.", 0.95),
    fact(2, "Beim Mittagsschlaf ist kurz besser: 10 bis 20 Minuten machen wacher, ein langer Schlaf macht aber oft noch müder."),
    fact(3, "Nach etwa 30 Minuten gleitet der Körper in den Tiefschlaf."),
]
BODY = [
    ScriptBlockV2(role="answer", text="Nach dem Mittagsschlaf bist du nicht unbedingt müder als vorher – es kommt auf die Länge an.", fact_ids=["fact_02"]),
    ScriptBlockV2(role="explanation", text="Nach etwa 30 Minuten gleitet dein Körper in den Tiefschlaf.", fact_ids=["fact_03"]),
    ScriptBlockV2(role="explanation", text="Wirst du daraus geweckt, fühlst du dich benommen – das nennt man Schlafträgheit.", fact_ids=["fact_01"]),
    ScriptBlockV2(role="payoff", text="Ein kurzer Mittagsschlaf von 10 bis 20 Minuten macht dich dagegen wacher.", fact_ids=["fact_02"]),
]
# The three candidates of the real run.
DUPLICATE = "Nach dem Mittagsschlaf bist du nicht unbedingt müder als vorher."
AWKWARD = "Bei einem Mittagsschlaf kann kurz besser sein."
IMPLICIT_GAP = "Du schläfst kurz ein – und wachst trotzdem völlig matschig auf."
# A natural equivalent of the awkward hook, with the same facts.
NATURAL = "Ein kurzer Mittagsschlaf macht wacher als ein langer."


def context() -> dict:
    fixture = story(QUESTION, [dict(item) for item in FACTS])
    body = [{"role": block.role, "text": block.text, "fact_ids": block.fact_ids} for block in BODY]
    return hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=body,
    )


def ranked(candidates: list[tuple[str, str]]) -> dict[str, dict]:
    return {item["text"]: item for item in rank_verbal(context(), [{"strategy": strategy, "text": text, "origin": "ai"} for strategy, text in candidates])}


# ---------------------------------------------------------------------------
# 1. Strategy semantics, not template wording
# ---------------------------------------------------------------------------

def test_a_semantic_curiosity_gap_passes_without_question_wording():
    assert "?" not in IMPLICIT_GAP
    result = assess_verbal(IMPLICIT_GAP, "curiosity_gap", context())
    assert result["hard_fail"] == []
    assert result["dimensions"]["strategy_fit"] > 0
    assert "strategy_implicit" in result["reason_codes"]  # performed, though less explicitly than a question


@pytest.mark.parametrize("text", [
    IMPLICIT_GAP,
    "You nap for twenty minutes – and still wake up groggy.",
    "Zwanzig Minuten Schlaf, und trotzdem fühlst du dich wie gerädert.",
    "Der Grund liegt nicht an zu wenig Schlaf.",
    "Rate mal, welcher Fluss ganz oben steht.",
])
def test_strategy_fit_is_the_rhetorical_function_not_a_lexical_template(text):
    assert strategy_function("curiosity_gap", text) > 0


@pytest.mark.parametrize(("strategy", "text"), [
    ("curiosity_gap", "Ein kurzer Mittagsschlaf macht wacher."),
    ("curiosity_gap", "Plants use sunlight to make sugar."),
    ("counterintuitive_insight", "Plants use sunlight to make sugar."),
    ("verified_statistic", "Ein kurzer Mittagsschlaf macht wacher."),
])
def test_a_candidate_that_does_not_perform_its_strategy_is_still_rejected(strategy, text):
    assert strategy_function(strategy, text) == 0


def test_a_genuine_strategy_mismatch_still_hard_fails():
    result = assess_verbal("Ein kurzer Mittagsschlaf macht wacher als ein langer.", "curiosity_gap", context())
    assert "strategy_not_in_wording" in result["hard_fail"]


# ---------------------------------------------------------------------------
# 2. Natural spoken language
# ---------------------------------------------------------------------------

def test_unnatural_wording_loses_to_a_natural_equivalent_of_similar_quality():
    results = ranked([("evidence_insight", AWKWARD), ("evidence_insight", NATURAL)])
    awkward, natural = results[AWKWARD], results[NATURAL]
    assert awkward["eligible"] and natural["eligible"]  # understandable and true: not a hard failure
    # Similar factual quality and clarity ...
    for key in ("factual_defensibility", "topic_relevance", "spoken_simplicity"):
        assert abs(awkward["dimensions"][key] - natural["dimensions"][key]) <= 0.15
    # ... but only one of them is how a person would say it.
    assert {"missing_subject", "vague_comparison"} <= set(awkward["reason_codes"])
    assert natural["dimensions"]["natural_language"] > awkward["dimensions"]["natural_language"]
    assert natural["score"] > awkward["score"]


@pytest.mark.parametrize(("text", "issue"), [
    ("Bei einem Mittagsschlaf kann kurz besser sein.", "missing_subject"),
    ("Beim Joggen kann langsam besser sein.", "missing_subject"),
    ("In a short nap can be better.", "missing_subject"),
    ("Kurz kann besser sein.", "vague_comparison"),
    ("A short nap can be better.", "vague_comparison"),
])
def test_structural_fluency_signals(text, issue):
    assert issue in fluency_issues(text)


@pytest.mark.parametrize("text", [
    NATURAL, IMPLICIT_GAP, "In Schweden ist das Wetter kalt.", "Bei Kälte ist Gänsehaut normal.", "Nach 30 Minuten wird es kritisch.",
    "Bei Hitze kann sein Körper schneller schwitzen.", "A short nap can be better than a long one.", "Viele wissen das nicht.",
    "Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein.",
])
def test_natural_sentences_are_not_flagged(text):
    assert fluency_issues(text) == []


# ---------------------------------------------------------------------------
# 3. Existing hard rules stay hard
# ---------------------------------------------------------------------------

def test_body_duplication_still_fails():
    result = assess_verbal(DUPLICATE, "counterintuitive_insight", context())
    assert "body_duplication" in result["hard_fail"]


def test_real_candidates_rank_by_function_and_naturalness():
    results = ranked([("counterintuitive_insight", DUPLICATE), ("evidence_insight", AWKWARD), ("curiosity_gap", IMPLICIT_GAP)])
    assert not results[DUPLICATE]["eligible"]
    assert results[IMPLICIT_GAP]["eligible"] and results[IMPLICIT_GAP]["score"] > results[AWKWARD]["score"]


# ---------------------------------------------------------------------------
# 4. The real pipeline: selection and persistence through narration/TTS/captions
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


def provider_candidates() -> list[dict]:
    return [
        ai_candidate("A", "counterintuitive_insight", DUPLICATE, "person yawning on a sofa after a nap", ["tired person sofa nap"], payoff_fact="fact_02"),
        ai_candidate("B", "evidence_insight", AWKWARD, "alarm clock next to a pillow", ["alarm clock pillow"], payoff_fact="fact_02"),
        ai_candidate("C", "curiosity_gap", IMPLICIT_GAP, "groggy person waking up on a couch", ["groggy person waking up couch"],
                     action="rubbing eyes", detail="messy hair", payoff_fact="fact_01"),
    ]


def test_nap_pipeline_selects_by_function_and_the_hook_persists(monkeypatch, tmp_path: pathlib.Path):
    source = {"label": "Quelle", "url": "https://source.test/nap"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(provider_candidates()))
    judge = Judge()  # neutral: every candidate rated alike, the rubric decides
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge)
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings)
    plan = state["script"]["triple_hook"]
    by_text = {item["verbal_hook"]: item for item in plan["selection"]["candidates"]}

    assert state["script"]["hook_generation"]["status"] == "connected"
    assert "body_duplication" in by_text[DUPLICATE]["hard_fail"]
    assert "strategy_not_in_wording" not in by_text[IMPLICIT_GAP]["hard_fail"] and by_text[IMPLICIT_GAP]["eligible"]
    assert by_text[IMPLICIT_GAP]["score"] > by_text[AWKWARD]["score"]
    hook = plan["verbal_hook"]
    assert hook not in {DUPLICATE, AWKWARD} and plan["selected_strategy"] in CANONICAL_STRATEGIES

    def assert_everywhere(current: dict) -> None:
        blocks = current["script"]["blocks"]
        assert [block["role"] for block in blocks].count("hook") == 1 and blocks[0]["role"] == "hook"
        assert blocks[0]["text"] == hook == current["script"]["selected_hook"] == current["script"]["triple_hook"]["verbal_hook"]
        assert current["script"]["text"].startswith(hook)
        assert " ".join(item["text"] for item in current["captions"]["items"]).startswith(hook)

    assert_everywhere(state)
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services.render_video", lambda *_a, **_k: RenderResult("/media/t.mp4", 12.0, "openai", 100))
    rendered = _render_state(state, "project-nap", 2, settings)
    assert_everywhere(rendered)
    captured: dict[str, object] = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=b"w" * 5000)

    monkeypatch.setattr("clipforge.renderer.OpenAI", lambda **_k: SimpleNamespace(audio=SimpleNamespace(speech=Speech())))
    rendered["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(rendered, tmp_path, settings)
    assert str(captured["input"]) == clean_narration_text(rendered["script"]["text"])
    assert str(captured["input"]).startswith(hook)
