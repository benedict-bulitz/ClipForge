"""Verbal hook document authority: the spoken hook comes only from the
documented Hook Strategy framework, chosen by what the research supports.

Topics are fixtures only; production logic has no topic vocabulary.
"""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError
from test_story_arc import ISLANDS, ISLANDS_Q, fact
from test_triple_hook import (
    FINGER_FACTS,
    FINGERS_Q,
    PURR_FACTS,
    PURR_Q,
    Judge,
    ai_candidate,
    by_id,
    generation,
    plan_for,
    story,
)

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge import triple_hook
from clipforge.ai import AIHookGenerationResult, AITripleHookCandidate
from clipforge.config import Settings
from clipforge.hooks import CANONICAL_STRATEGIES, canonical_strategy
from clipforge.pipeline import build_initial_state, enforce_selected_hook
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.verbal_hook import (
    assess_verbal,
    deterministic_candidates,
    hook_context,
    rank_verbal,
    select_verbal,
    strategy_signals,
)

LOCAL = Settings(clipforge_ai_mode="local", openai_api_key=None)


def context_for(fixture: dict) -> dict:
    return hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
    )


def islands() -> dict:
    return story(ISLANDS_Q, [dict(item) for item in ISLANDS])


def fingers() -> dict:
    return story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])


def run_pipeline(monkeypatch, question: str, facts: list[dict], *, provider=None, language: str = "de", writer=None, judge=None) -> dict:
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([{key: value for key, value in item.items() if key != "id"} for item in facts], [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    if provider is not None:
        monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", provider)
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge or Judge())
    settings = Settings(clipforge_ai_mode="local", openai_api_key="test-key" if writer else None)
    return build_initial_state(question, AdvancedOptions(language=language), settings, script_writer_provider=writer)


class Writer:
    """Script Writer V2 double that returns a fixed body."""

    name = "fixture-v2"

    def __init__(self, blocks: list[ScriptBlockV2]):
        self.result = ScriptWriterResult(ScriptDraftV2(language="de", blocks=blocks), "connected")

    def generate(self, _request):
        return self.result


# 1, 2 — the taxonomy -------------------------------------------------------------

def test_every_strategy_that_can_be_selected_is_a_documented_one(monkeypatch):
    for fixture in (islands(), fingers(), story(PURR_Q, [dict(item) for item in PURR_FACTS], language="en")):
        context = context_for(fixture)
        assert set(context["signals"]) == set(CANONICAL_STRATEGIES)
        assert all(item["strategy"] in CANONICAL_STRATEGIES for item in deterministic_candidates(context))
    state = run_pipeline(monkeypatch, ISLANDS_Q, ISLANDS)
    plan = state["script"]["triple_hook"]
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert state["script"]["selected_hook_strategy"] == plan["selected_strategy"]
    assert all(item["strategy"] in CANONICAL_STRATEGIES for item in plan["selection"]["candidates"])
    assert all(item["strategy"] in CANONICAL_STRATEGIES for item in state["script"]["hook_candidates"])


def test_invented_model_strategies_are_rejected_and_document_aliases_normalised():
    with pytest.raises(ValidationError):
        AITripleHookCandidate.model_validate({**ai_candidate("A", "evidence_insight", "x", "wet fingertips", ["wet fingertips"]), "strategy": "comparison_tension"})
    fixture = fingers()
    good = ai_candidate("B", "counterintuitive_insight", "Diese Falten sind kein Wasserschaden.", "wet fingertips with deep wrinkles",
                        ["wrinkled wet fingertips"], action="gripping a wet stone", detail="deep ridges")
    invented = {**good, "id": "A", "strategy": "viral_hook", "verbal_hook": "Das ist der krasseste Hook der Welt."}
    alias = {**good, "id": "C", "strategy": "hot_take", "verbal_hook": "Nicht das Wasser macht die Falten, sondern deine Nerven."}
    plan = plan_for(fixture, [invented, good, alias])
    assert plan["selection"]["generation"]["rejected_non_document_strategies"] == ["viral_hook"]
    assert {item["strategy"] for item in plan["selection"]["candidates"]} == {"counterintuitive_insight"}
    assert canonical_strategy("shock_number") == "verified_statistic" and canonical_strategy("mystery") is None
    assert "non_document_strategy" in assess_verbal("Das ist ein Hook.", "attention_hook", context_for(fixture))["hard_fail"]


# 3, 7 — verified statistics ---------------------------------------------------------

def test_strong_sourced_numbers_make_verified_statistic_competitive():
    context = context_for(islands())
    entry = context["signals"]["verified_statistic"]
    assert entry["viable"] and {"fact_01", "fact_02"} <= set(entry["fact_ids"])
    assert "comparable_sourced_numbers" in entry["signals"]
    ranked = rank_verbal(context, [
        *deterministic_candidates(context),
        {"strategy": "curiosity_gap", "text": "Welche Antwort überrascht dich hier?", "origin": "ai"},
    ])
    assert ranked[0]["strategy"] == "verified_statistic"
    assert "strong_sourced_number" in ranked[0]["positive_codes"]


def test_unsupported_statistic_fails():
    context = context_for(islands())
    result = assess_verbal("Schweden hat über 300.000 Inseln – oder doch Indonesien?", "verified_statistic", context)
    assert "unsupported_statistic" in result["hard_fail"]
    result = assess_verbal("Rund 267.570 gegen etwa 17.000 Inseln – welche Zahl gehört zu Indonesien, welche zu Schweden?", "verified_statistic", context)
    assert not result["hard_fail"]


# 4, 18 — Sweden / Indonesia ----------------------------------------------------------

def test_islands_winner_protected_question_loses_and_statistic_hook_can_win():
    fixture = islands()
    context = context_for(fixture)
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": ISLANDS_Q, "origin": "ai"},
        {"strategy": "evidence_insight", "text": "Schweden hat mehr Inseln als Indonesien.", "origin": "ai"},
        {"strategy": "verified_statistic", "text": "Rund 267.570 gegen etwa 17.000 Inseln – welche Zahl gehört zu Indonesien, welche zu Schweden?", "origin": "ai"},
    ])
    by_text = {item["text"]: item for item in ranked}
    assert "question_echo" in by_text[ISLANDS_Q]["hard_fail"]
    assert any("protected" in code for code in by_text["Schweden hat mehr Inseln als Indonesien."]["hard_fail"])
    winner = ranked[0]
    assert winner["strategy"] == "verified_statistic" and winner["eligible"]
    assert {"protected_answer_safe", "strong_sourced_number"} <= set(winner["positive_codes"])
    # Without a provider the same kind of hook comes from the documented strategies.
    chosen = select_verbal(context)
    # The long figure is spoken as a true rounding a 14-year-old can follow.
    assert chosen["strategy"] == "verified_statistic" and "270.000" in chosen["text"] and "17.000" in chosen["text"]
    assert "267.570" not in chosen["text"] and "easy_to_follow" in chosen["positive_codes"]
    assert triple_hook.leaks(chosen["text"], context) is None


def test_islands_pipeline_hook_precedes_the_answer_even_when_the_writer_opens_with_it(monkeypatch):
    writer = Writer([
        ScriptBlockV2(role="answer", text="Schweden hat mehr Inseln als Indonesien.", fact_ids=["fact_02"]),
        ScriptBlockV2(role="support", text="Indonesien hat etwa 17.000 Inseln.", fact_ids=["fact_01"]),
        ScriptBlockV2(role="explanation", text="Gletscher haben Schwedens Küste zerklüftet.", fact_ids=["fact_03"]),
        ScriptBlockV2(role="payoff", text="Indonesien bleibt trotzdem der größte Inselstaat der Welt.", fact_ids=["fact_04"]),
    ])
    state = run_pipeline(monkeypatch, ISLANDS_Q, ISLANDS, writer=writer)
    blocks = state["script"]["blocks"]
    hook = blocks[0]
    assert hook["role"] == "hook" and hook["text"] == state["script"]["selected_hook"]
    assert triple_hook.hook_text_leaks(state, hook["text"]) is None
    assert hook["text"] != ISLANDS_Q
    roles = [block["role"] for block in blocks]
    answer = next(index for index, block in enumerate(blocks) if "fact_02" in (block.get("fact_ids") or []))
    evidence = next(index for index, block in enumerate(blocks) if "fact_01" in (block.get("fact_ids") or []))
    assert roles[0] == "hook" and evidence < answer  # the reveal comes after its evidence
    assert state["scenes"][0]["story_stage"] == "before_reveal"
    assert state["script"]["triple_hook"]["selected_strategy"] == "verified_statistic"


# 5, 6, 9 — quality decides -----------------------------------------------------------

def test_question_repetition_loses_when_useful_evidence_exists():
    context = context_for(fingers())
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": FINGERS_Q, "origin": "ai"},
        {"strategy": "curiosity_gap", "text": "Warum werden deine Finger im Wasser eigentlich schrumpelig?", "origin": "ai"},
        {"strategy": "counterintuitive_insight", "text": "Diese Falten sind kein Wasserschaden.", "origin": "ai"},
    ])
    assert ranked[0]["text"] == "Diese Falten sind kein Wasserschaden."
    assert all("question_echo" in item["hard_fail"] for item in ranked[1:])


def test_empty_curiosity_loses_to_a_content_rich_evidence_hook():
    context = context_for(fingers())
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": "Was steckt wirklich dahinter?", "origin": "ai"},
        {"strategy": "counterintuitive_insight", "text": "Nicht das Wasser macht die Falten, sondern deine Nerven.", "origin": "ai"},
    ])
    assert ranked[0]["strategy"] == "counterintuitive_insight"
    empty = next(item for item in ranked if item["text"].startswith("Was steckt"))
    assert "empty_curiosity" in empty["reason_codes"] and empty["score"] < ranked[0]["score"]


def test_counterintuitive_researched_insight_beats_generic_curiosity():
    fixture = story(PURR_Q, [dict(item) for item in PURR_FACTS], language="en")
    context = context_for(fixture)
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": "Have you ever wondered why cats purr?", "origin": "ai"},
        {"strategy": "counterintuitive_insight", "text": "A purring cat is not always a happy cat.", "origin": "ai"},
    ])
    assert ranked[0]["text"] == "A purring cat is not always a happy cat."
    assert "researched_contrast" in ranked[0]["positive_codes"]


# 8, 10, 11 — strategies need their evidence -------------------------------------------

def test_unsupported_trend_or_social_proof_fails():
    context = context_for(fingers())
    assert not context["signals"]["social_proof_or_trend"]["viable"]
    result = assess_verbal("Immer mehr Menschen fragen sich, warum ihre Finger schrumpeln.", "social_proof_or_trend", context)
    assert {"unsupported_trend", "strategy_not_supported_by_research"} & set(result["hard_fail"])
    trend = story("Warum greifen Leser zu digitalen Büchern?", [fact(1, "Leser greifen zunehmend zu digitalen Büchern, weil sie leichter sind.")])
    result = assess_verbal("Leser greifen zunehmend zu digitalen Büchern.", "social_proof_or_trend", context_for(trend))
    assert not {"unsupported_trend", "strategy_not_supported_by_research", "strategy_evidence_not_used"} & set(result["hard_fail"])


def test_direct_reframe_wins_only_when_its_correcting_side_is_researched():
    context = context_for(fingers())
    supported = assess_verbal("Nicht das Wasser macht die Falten, sondern deine Blutgefäße.", "direct_reframe", context)
    unsupported = assess_verbal("Nicht das Wasser macht die Falten, sondern ein Pilz.", "direct_reframe", context)
    assert not supported["hard_fail"] and supported["supported_by_fact_ids"]
    assert "strategy_evidence_not_used" in unsupported["hard_fail"]
    no_reframe = story("Wie groß ist der Mond?", [fact(1, "Der Mond hat einen Durchmesser von etwa 3.474 Kilometern.")])
    assert "strategy_not_supported_by_research" in assess_verbal("Der Mond ist nicht klein, sondern riesig.", "direct_reframe", context_for(no_reframe))["hard_fail"]


def test_common_mistake_must_be_grounded_in_a_researched_misconception():
    plain = story("Wie groß ist der Mond?", [fact(1, "Der Mond hat einen Durchmesser von etwa 3.474 Kilometern.")])
    assert not context_for(plain)["signals"]["common_mistake"]["viable"]
    assert "strategy_not_supported_by_research" in assess_verbal("Die meisten machen hier einen Fehler beim Mond.", "common_mistake", context_for(plain))["hard_fail"]
    grounded = story("Warum ist der Mond am Horizont größer?", [
        fact(1, "Dass der Mond am Horizont größer ist, ist ein Irrtum: Er erscheint nur größer, weil das Gehirn ihn mit Bäumen und Häusern vergleicht."),
    ])
    result = assess_verbal("Der große Mond am Horizont ist ein Irrtum deines Gehirns.", "common_mistake", context_for(grounded))
    assert not result["hard_fail"] and "researched_misconception" in result["positive_codes"]


# 12, 13 — no provider ------------------------------------------------------------------

def test_deterministic_fallback_uses_documented_strategies_and_research():
    context = context_for(fingers())
    candidates = deterministic_candidates(context)
    assert candidates and all(item["strategy"] in CANONICAL_STRATEGIES and item["supported_by_fact_ids"] for item in candidates)
    chosen = select_verbal(context)
    assert chosen["strategy"] in CANONICAL_STRATEGIES and chosen["text"] != FINGERS_Q and chosen["supported_by_fact_ids"]


def test_no_provider_never_produces_a_non_document_strategy(monkeypatch):
    failing = lambda *_a, **_k: AIHookGenerationResult([], None, "missing_key", "no key")
    for question, facts in ((ISLANDS_Q, ISLANDS), (FINGERS_Q, FINGER_FACTS)):
        state = run_pipeline(monkeypatch, question, facts, provider=failing)
        plan = state["script"]["triple_hook"]
        assert plan["selected_strategy"] in CANONICAL_STRATEGIES
        assert plan["verbal_hook"] and plan["verbal_hook"] != question
        assert plan["selection"]["judge"]["status"] == "not_called"


def test_question_is_only_an_emergency_fallback():
    no_research = story("Warum ist der Himmel blau?", [])
    chosen = select_verbal(context_for(no_research))
    assert chosen["strategy"] == "curiosity_gap" and "question_fallback" in chosen["reason_codes"]
    # A story instruction never becomes the spoken hook, but a hook still exists.
    instruction = {**no_research, "intent": {**no_research["intent"], "question": "Erzähle eine Geschichte über den Mond", "topic": "Erzähle eine Geschichte über den Mond", "content_type": "fictional_story"}}
    chosen = select_verbal(context_for(instruction))
    assert chosen is not None and chosen["strategy"] in CANONICAL_STRATEGIES
    assert not chosen["text"].casefold().startswith("erzähle") and "mond" in chosen["text"].casefold()


# 14, 15, 16, 17 — one hook from narration to TTS and captions ------------------------

def finger_state(monkeypatch, *, writer: bool = False) -> dict:
    candidates = [
        ai_candidate("A", "counterintuitive_insight", "Diese Falten sind kein Wasserschaden.", "wet fingertips with deep wrinkles",
                     ["wrinkled wet fingertips"], action="gripping a smooth wet stone", detail="deep ridges on the fingertip pads",
                     on_screen="Dein Nervensystem steckt dahinter"),
        ai_candidate("B", "evidence_insight", "Schrumpelige Finger entstehen, weil das Nervensystem die Blutgefäße in den Fingerkuppen verengt.",
                     "wrinkled fingertips", ["wrinkled fingertips"], action="in a bathtub", detail="wrinkles"),
        ai_candidate("C", "curiosity_gap", FINGERS_Q, "wrinkled fingertips", ["wrinkled fingertips"], action="in water", detail="wrinkles"),
        ai_candidate("D", "curiosity_gap", "Was steckt wirklich dahinter?", "wrinkled fingertips", ["wrinkled fingertips"], action="in water", detail="wrinkles"),
    ]
    return run_pipeline(monkeypatch, FINGERS_Q, FINGER_FACTS, provider=lambda *_a, **_k: generation(candidates),
                        judge=Judge({"hook_a": {"attention_value": 9, "curiosity": 9}}))


def test_finger_topic_keeps_a_strong_content_rich_documented_hook(monkeypatch):
    state = finger_state(monkeypatch)
    plan = state["script"]["triple_hook"]
    assert plan["verbal_hook"] == "Diese Falten sind kein Wasserschaden."
    assert plan["selected_strategy"] in {"counterintuitive_insight", "direct_reframe", "common_mistake"}
    assert plan["supported_by_fact_ids"]
    candidates = by_id(plan)
    assert "question_echo" in candidates["hook_c"]["hard_fail"]
    assert "empty_curiosity" in candidates["hook_d"]["reason_codes"]


def test_selected_hook_is_the_first_narration_tts_and_caption_content(monkeypatch, tmp_path):
    from clipforge.renderer import _create_voice

    state = finger_state(monkeypatch)
    hook = state["script"]["triple_hook"]["verbal_hook"]
    blocks = state["script"]["blocks"]
    assert blocks[0]["role"] == "hook" and blocks[0]["text"] == hook == state["script"]["selected_hook"]
    assert sum(block["role"] == "hook" for block in blocks) == 1
    assert state["script"]["text"].startswith(hook)
    assert blocks[1]["text"] != hook
    assert " ".join(item["text"] for item in state["captions"]["items"]).startswith(hook.rstrip(".!?").split()[0])
    assert state["captions"]["items"][0]["text"] in hook
    captured: dict = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"w" * 5000})()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = type("Audio", (), {"speech": Speech()})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", FakeOpenAI)
    state["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(state, tmp_path, Settings(openai_api_key="test-key", render_root=tmp_path))
    assert str(captured["input"]).startswith(hook)


def test_rerender_without_changes_preserves_the_hook(monkeypatch, tmp_path):
    from clipforge.renderer import RenderResult
    from clipforge.services import _render_state

    state = finger_state(monkeypatch)
    hook = state["script"]["selected_hook"]
    rendered_inputs: list[str] = []
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services.run_ai_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "clipforge.services.render_video",
        lambda render_state, *_a, **_k: rendered_inputs.append(render_state["script"]["text"]) or RenderResult("/media/t.mp4", 9.0, "openai", 100),
    )
    settings = Settings(openai_api_key="test-key", render_root=tmp_path)
    first = _render_state(state, "p", 2, settings)
    second = _render_state(first, "p", 3, settings)
    for rendered in (first, second):
        assert rendered["script"]["blocks"][0]["text"] == hook == rendered["script"]["selected_hook"]
        assert rendered["script"]["triple_hook"]["verbal_hook"] == hook
    assert all(text.startswith(hook) for text in rendered_inputs) and len(rendered_inputs) == 2


def test_no_later_stage_replaces_the_hook_with_the_question_or_an_answer(monkeypatch):
    state = finger_state(monkeypatch)
    hook = state["script"]["selected_hook"]
    answer = state["script"]["blocks"][1]
    # A stage that dropped the hook and led with the answer, or put the question first.
    for broken in (
        [dict(answer), *copy.deepcopy(state["script"]["blocks"][2:])],
        [{"role": "hook", "text": FINGERS_Q}, *copy.deepcopy(state["script"]["blocks"][1:])],
    ):
        trial = copy.deepcopy(state)
        trial["script"]["blocks"] = broken
        assert enforce_selected_hook(trial) in {"kept", "restored"}
        assert trial["script"]["blocks"][0]["text"] == hook and trial["script"]["blocks"][0]["role"] == "hook"


def test_invalid_hook_after_a_content_change_is_reselected_from_documented_candidates(monkeypatch):
    state = finger_state(monkeypatch)
    state["script"]["triple_hook"]["verbal_hook"] = "Du wurdest belogen: Finger schrumpeln nie."
    state["script"]["selected_hook"] = state["script"]["triple_hook"]["verbal_hook"]
    state["script"]["blocks"][0]["text"] = state["script"]["selected_hook"]
    assert enforce_selected_hook(state) == "reselected"
    plan = state["script"]["triple_hook"]
    assert plan["verbal_hook"] != "Du wurdest belogen: Finger schrumpeln nie."
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert state["script"]["blocks"][0]["text"] == plan["verbal_hook"] == state["script"]["selected_hook"]


# 20 — English --------------------------------------------------------------------------

def test_english_project_uses_the_same_document_authority(monkeypatch):
    candidates = [
        ai_candidate("A", "curiosity_gap", "Have you ever wondered why cats purr?", "cat on a sofa", ["cat purring sofa"], action="purring", detail="closed eyes"),
        ai_candidate("B", "counterintuitive_insight", "A purring cat is not always a happy cat.", "injured cat at the vet", ["cat at vet"],
                     action="purring on the examination table", detail="bandaged paw", on_screen="Purring can mean pain"),
    ]
    state = run_pipeline(monkeypatch, PURR_Q, PURR_FACTS, language="en", provider=lambda *_a, **_k: generation(candidates))
    plan = state["script"]["triple_hook"]
    assert plan["verbal_hook"] == "A purring cat is not always a happy cat."
    assert plan["selected_strategy"] == "counterintuitive_insight"
    assert state["script"]["blocks"][0]["text"] == plan["verbal_hook"]
    assert state["intent"]["language"] == "en"


def test_strategy_signals_follow_the_evidence_not_the_topic():
    signals = strategy_signals(context_for(story("Wie groß ist der Mond?", [fact(1, "Der Mond hat einen Durchmesser von etwa 3.474 Kilometern.")])))
    assert signals["verified_statistic"]["viable"] and not signals["common_mistake"]["viable"]
    assert not signals["social_proof_or_trend"]["viable"] and not signals["direct_reframe"]["viable"]


def test_tight_duration_keeps_answer_and_payoff_and_reselects_a_shorter_documented_hook(monkeypatch):
    from clipforge.pipeline import _refresh_script_derivatives

    state = run_pipeline(monkeypatch, ISLANDS_Q, ISLANDS)
    long_hook = state["script"]["selected_hook"]
    state["duration"]["max_seconds"] = 10
    _refresh_script_derivatives(state, old_scenes=copy.deepcopy(state["scenes"]))
    carried = {fact_id for block in state["script"]["blocks"] for fact_id in block.get("fact_ids") or []}
    assert {"fact_02", "fact_04"} <= carried  # the Story Arc outranks the hook
    hook = state["script"]["blocks"][0]
    assert hook["role"] == "hook" and hook["text"] != long_hook and len(hook["text"].split()) < len(long_hook.split())
    plan = state["script"]["triple_hook"]
    assert hook["text"] == plan["verbal_hook"] == state["script"]["selected_hook"]
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES and "reselected_for_duration" in plan["reason_codes"]
    assert triple_hook.hook_text_leaks(state, hook["text"]) is None


def test_hook_window_carries_only_the_hook_text_channel(monkeypatch):
    from clipforge.media import build_visual_query_plan
    from clipforge.visual_director import TEXT_NUMBER_VISUAL, plan_scene_strategy

    state = run_pipeline(monkeypatch, ISLANDS_Q, ISLANDS)
    assert state["script"]["triple_hook"]["on_screen_text_hook"] == ""
    opening = state["scenes"][0]
    strategy = plan_scene_strategy(opening, state, build_visual_query_plan(opening, state))
    # The spoken numbers are not repeated as a number graphic in the opening.
    assert strategy["planned_type"] != TEXT_NUMBER_VISUAL and strategy["graphic"] is None and strategy["overlay_spec"] is None


def test_stronger_hook_request_reselects_a_documented_alternative(monkeypatch):
    from clipforge.pipeline import apply_edit

    state = finger_state(monkeypatch)
    before = state["script"]["selected_hook"]
    edited, _ = apply_edit(state, "Make the hook stronger", LOCAL)
    plan = edited["script"]["triple_hook"]
    assert edited["script"]["blocks"][0]["text"] != before
    assert edited["script"]["blocks"][0]["text"] == plan["verbal_hook"] == edited["script"]["selected_hook"]
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert "half the story" not in edited["script"]["text"] and "halbe Wahrheit" not in edited["script"]["text"]


# --- 14-year-old comprehension, closest documented strategy, always a hook ------

ISLANDS_EN_Q = "Which country has more islands – Sweden or Indonesia?"
ISLANDS_EN = [
    fact(1, "Indonesia has about 17,000 islands."),
    fact(2, "Sweden has around 267,570 islands – more than any other country in the world.", 0.95),
    fact(3, "During the Ice Age, glaciers carved Sweden's coast into countless small islands."),
    fact(4, "Indonesia is still the biggest island nation in the world.", 0.7),
]


def test_clear_wording_beats_a_harder_equivalent_for_the_same_idea():
    context = context_for(islands())
    hard = "Der offensichtlichere Inselstaat ist hier nicht der mit den meisten Inseln."
    clear = "Eines der beiden Länder besteht komplett aus Inseln – und hat trotzdem nicht die meisten."
    ranked = rank_verbal(context, [
        {"strategy": "counterintuitive_insight", "text": hard, "origin": "ai"},
        {"strategy": "counterintuitive_insight", "text": clear, "origin": "ai"},
    ])
    by_text = {item["text"]: item for item in ranked}
    assert by_text[hard]["eligible"] and by_text[clear]["eligible"]  # both truthful and reveal-safe
    assert ranked[0]["text"] == clear
    assert by_text[clear]["dimensions"]["spoken_simplicity"] > by_text[hard]["dimensions"]["spoken_simplicity"]
    assert {"long_words", "unfamiliar_term"} <= set(by_text[hard]["reason_codes"])
    assert "protected_answer_safe" in by_text[clear]["positive_codes"]


def test_english_clear_wording_beats_a_harder_equivalent():
    fixture = story(ISLANDS_EN_Q, [dict(item) for item in ISLANDS_EN], language="en")
    context = context_for(fixture)
    hard = "The more conspicuous archipelagic nation is nevertheless not the one with the most islands."
    clear = "One of the two countries is an island nation – and still doesn't have the most."
    ranked = rank_verbal(context, [
        {"strategy": "counterintuitive_insight", "text": hard, "origin": "ai"},
        {"strategy": "counterintuitive_insight", "text": clear, "origin": "ai"},
    ])
    assert all(item["eligible"] for item in ranked) and ranked[0]["text"] == clear
    chosen = select_verbal(context)
    assert chosen["strategy"] in CANONICAL_STRATEGIES and triple_hook.leaks(chosen["text"], context) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Hinsichtlich der Inselanzahl ergibt sich eine Überraschung.", "bureaucratic_wording"),
        ("Rund 267.570 Inseln – so viele?", "long_number"),
        ("Das Land, das die Forscher, die dort waren, zählten, dass es viele sind, überrascht.", "nested_clauses"),
        ("Die Fragmentierung der Küstenformation erklärt die Inselentstehung.", "abstract_nouns"),
    ],
)
def test_spoken_simplicity_flags_what_is_hard_to_follow_by_ear(text, code):
    from clipforge.verbal_hook import spoken_simplicity

    score, codes = spoken_simplicity(text, context_for(islands()))
    assert code in codes and score < 1.0
    assert spoken_simplicity("Ein Land hat viel mehr Inseln, als du denkst.", context_for(islands()))[0] == 1.0


def test_truthful_hook_with_an_imperfect_label_gets_the_closest_documented_strategy():
    context = context_for(fingers())
    # Labelled as a statistic, but it has no number: the wording is still true.
    chosen = select_verbal(context, extra=[{"strategy": "verified_statistic", "text": "Diese Falten sind kein Wasserschaden.", "origin": "ai"}])
    assert chosen["strategy"] in CANONICAL_STRATEGIES
    ranked = rank_verbal(context, [{"strategy": "verified_statistic", "text": "Diese Falten sind kein Wasserschaden.", "origin": "ai"}])
    assert {"strategy_not_in_wording", "strategy_not_supported_by_research"} & set(ranked[0]["hard_fail"])
    thin = story("Wie heißt der höchste Berg?", [fact(1, "Der Mount Everest ist der höchste Berg der Erde.")])
    approximate = select_verbal(context_for(thin), extra=[{"strategy": "common_mistake", "text": "Der Mount Everest ist der höchste Berg der Erde – aber wie hoch?", "origin": "ai"}])
    assert approximate["strategy"] in CANONICAL_STRATEGIES and approximate["strategy"] != "common_mistake"


@pytest.mark.parametrize(
    ("question", "claims"),
    [
        ("Wie heißt der höchste Berg?", ["Der Mount Everest ist der höchste Berg der Erde."]),  # thin research
        ("Was ist Photosynthese?", ["Pflanzen nutzen Licht."]),  # very thin
        ("Welche Stadt ist älter – Rom oder Athen?", ["Athen wurde vor Rom besiedelt."]),  # withheld, no numbers
        ("Why is the sky blue?", []),  # no research at all
    ],
)
def test_thin_or_unusual_research_still_produces_a_documented_hook(question, claims):
    facts = [fact(index, claim) for index, claim in enumerate(claims, 1)]
    context = context_for(story(question, facts, language="en" if question.startswith("Why") else "de"))
    chosen = select_verbal(context)
    assert chosen is not None and chosen["text"].strip()
    assert chosen["strategy"] in CANONICAL_STRATEGIES
    assert triple_hook.leaks(chosen["text"], context) is None


def test_fallback_tiers_never_admit_unsupported_claims():
    context = context_for(fingers())
    bad = [
        {"strategy": "verified_statistic", "text": "83 % aller Menschen haben schrumpelige Finger.", "origin": "ai"},
        {"strategy": "social_proof_or_trend", "text": "Immer mehr Menschen haben schrumpelige Finger.", "origin": "ai"},
        {"strategy": "common_mistake", "text": "Die meisten Menschen denken falsch über Finger.", "origin": "ai"},
        {"strategy": "high_stakes_consequence", "text": "Du wurdest belogen: Schrumpelfinger sind gefährlich.", "origin": "ai"},
    ]
    chosen = select_verbal(context, extra=bad)
    assert chosen["text"] not in {item["text"] for item in bad}
    assert chosen["strategy"] in CANONICAL_STRATEGIES


@pytest.mark.parametrize(
    ("prompt", "options"),
    [
        (ISLANDS_Q, {}),
        (FINGERS_Q, {}),
        ("Tell a story about an astronaut on the moon", {"language": "en", "research": "off"}),
        ("Write a fictional story about a lighthouse keeper", {"max_duration": 30}),
    ],
)
def test_normal_generation_never_ends_without_a_documented_hook(monkeypatch, prompt, options):
    failing = lambda *_a, **_k: AIHookGenerationResult([], None, "missing_key", "no key")
    facts = ISLANDS if prompt == ISLANDS_Q else FINGER_FACTS if prompt == FINGERS_Q else []
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([{key: value for key, value in item.items() if key != "id"} for item in facts], [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", failing)
    state = build_initial_state(prompt, AdvancedOptions(**options), LOCAL)
    blocks = state["script"]["blocks"]
    plan = state["script"]["triple_hook"]
    assert blocks[0]["role"] == "hook" and blocks[0]["text"] == plan["verbal_hook"] == state["script"]["selected_hook"]
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert state["script"]["selected_hook_strategy"] in CANONICAL_STRATEGIES
    assert not blocks[0]["text"].casefold().startswith(("tell a story", "write a fictional"))


# --- the 14-year-old rule does not depend on how the user phrased the question ---

def _technical(question: str, claim: str, language: str) -> dict:
    return context_for(story(question, [fact(1, claim)], language=language))


def test_technical_german_question_still_gets_the_simpler_spoken_hook():
    from clipforge.verbal_hook import spoken_simplicity

    context = _technical(
        "Warum kommt es bei Kälte zur Piloerektion?",
        "Bei Kälte ziehen sich winzige Muskeln an den Haarwurzeln zusammen und stellen die Haare auf – das nennt man Piloerektion.",
        "de",
    )
    technical = "Was löst die Piloerektion an deinen Haarwurzeln aus?"
    simple = "Was stellt bei Kälte deine Haare auf?"
    # The user's own term is not "known" to a 14-year-old: it is evaluated.
    score, codes = spoken_simplicity("Das ist Piloerektion.", context)
    assert score < 1.0 and "unfamiliar_term" in codes
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": technical, "origin": "ai"},
        {"strategy": "curiosity_gap", "text": simple, "origin": "ai"},
    ])
    by_text = {item["text"]: item for item in ranked}
    assert by_text[technical]["eligible"] and by_text[simple]["eligible"]
    assert ranked[0]["text"] == simple
    assert by_text[simple]["dimensions"]["spoken_simplicity"] > by_text[technical]["dimensions"]["spoken_simplicity"]


def test_technical_english_question_still_gets_the_simpler_spoken_hook():
    context = _technical(
        "What causes piloerection in cold weather?",
        "In the cold, tiny muscles at the hair roots contract and make hairs stand up – this is called piloerection.",
        "en",
    )
    # Same content, same strategy – only the wording differs.
    technical = "What pulls your hairs into piloerection when you get cold?"
    simple = "What pulls your hairs upright when you get cold?"
    ranked = rank_verbal(context, [
        {"strategy": "curiosity_gap", "text": technical, "origin": "ai"},
        {"strategy": "curiosity_gap", "text": simple, "origin": "ai"},
    ])
    by_text = {item["text"]: item for item in ranked}
    assert by_text[technical]["eligible"] and by_text[simple]["eligible"]
    assert ranked[0]["text"] == simple
    assert "unfamiliar_term" in by_text[technical]["reason_codes"]
    assert by_text[technical]["dimensions"]["spoken_simplicity"] < by_text[simple]["dimensions"]["spoken_simplicity"]


def test_a_necessary_technical_term_may_stay_but_is_still_counted():
    from clipforge.verbal_hook import spoken_simplicity

    context = _technical(
        "Was ist Photosynthese?",
        "Bei der Photosynthese machen Pflanzen aus Licht, Wasser und Luft ihren eigenen Zucker.",
        "de",
    )
    # Every candidate needs the term: it is not banned and a hook is selected ...
    candidates = [
        {"strategy": "curiosity_gap", "text": "Photosynthese: Wie machen Pflanzen Zucker aus Licht?", "origin": "ai"},
        {"strategy": "curiosity_gap", "text": "Was passiert bei der Photosynthese mit dem Licht?", "origin": "ai"},
    ]
    chosen = select_verbal(context, extra=candidates)
    assert "Photosynthese" in chosen["text"] and chosen["strategy"] in CANONICAL_STRATEGIES
    # ... but it still costs spoken-comprehension points like any long word,
    # even though the user's question used it.
    score, codes = spoken_simplicity(chosen["text"], context)
    assert score < 1.0 and "long_words" in codes
    assert chosen["dimensions"]["spoken_simplicity"] == score
    # Compared subjects stay exempt: they are the topic, not a wording choice.
    islands_context = context_for(islands())
    assert spoken_simplicity("Schweden oder Indonesien?", islands_context) == (1.0, [])
