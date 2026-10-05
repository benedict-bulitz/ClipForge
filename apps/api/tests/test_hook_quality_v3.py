"""Hook Quality V3: the first one to three seconds of the opening.

Topics are fixtures only; production logic has no topic vocabulary.  Provider
calls are replaced by fakes; V3 adds no provider call of its own.
"""
from __future__ import annotations

import pytest
from test_story_arc import ISLANDS, ISLANDS_Q
from test_triple_hook import (
    FINGER_FACTS,
    FINGERS_Q,
    PURR_FACTS,
    PURR_Q,
    SETTINGS,
    Judge,
    ai_candidate,
    by_id,
    channels,
    finger_pipeline_candidates,
    generation,
    islands_candidates,
    pipeline_state,
    plan_for,
    story,
)

from clipforge import hook_quality, triple_hook
from clipforge.ai import HOOK_GENERATION_INSTRUCTIONS, TRIPLE_HOOK_JUDGE_INSTRUCTIONS
from clipforge.config import Settings
from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.verbal_hook import assess_verbal, hook_context, select_verbal


def context_for(fixture: dict) -> dict:
    return hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
    )


def fingers() -> dict:
    return context_for(story(FINGERS_Q, [dict(item) for item in FINGER_FACTS]))


def purr() -> dict:
    return context_for(story(PURR_Q, [dict(item) for item in PURR_FACTS], language="en"))


# ---------------------------------------------------------------------------
# Gates on the spoken opening
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("make", "text", "strategy"), [
    (fingers, "Wusstest du, dass Finger im Wasser Falten bekommen?", "evidence_insight"),
    (fingers, "Hast du dich schon mal gefragt, warum deine Finger im Wasser schrumpelig werden?", "curiosity_gap"),
    (fingers, "Weißt du, warum deine Finger im Wasser Falten bekommen?", "curiosity_gap"),
    (purr, "Did you know cats purr when they are injured?", "evidence_insight"),
    (purr, "Many people wonder why cats purr when they are injured.", "evidence_insight"),
])
def test_generic_setup_opening_is_rejected(make, text, strategy):
    result = assess_verbal(text, strategy, make())
    assert "setup_opener" in result["hard_fail"]
    assert "generic_opener" in result["reason_codes"]
    assert result["dimensions"]["first_second_clarity"] <= 0.5


@pytest.mark.parametrize("text", [
    "Heute schauen wir uns an, warum Katzen schnurren.",
    "Viele Menschen fragen sich, warum Katzen schnurren.",
    "Hast du dich schon mal gefragt, warum?",
    "Today we look at why cats purr.",
    "Let's talk about why cats purr.",
    "Have you ever wondered why cats purr?",
])
def test_setup_formulas_are_recognised(text):
    assert hook_quality.setup_opener(text)


@pytest.mark.parametrize("text", [
    "Viele glauben, dass Katzen nur aus Freude schnurren.",  # a misconception hook, not setup
    "Stell dir vor, du liegst eine Stunde in der Wanne.",
    "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
    "Cats also purr when they are badly injured.",
])
def test_substantive_openings_are_not_setup(text):
    assert not hook_quality.setup_opener(text)


def test_question_restatement_without_added_value_is_rejected():
    context = fingers()
    for text, strategy in (
        ("Warum werden unsere Finger im Wasser schrumpelig?", "curiosity_gap"),
        ("Finger werden im Wasser schrumpelig.", "evidence_insight"),
        ("Finger werden im Wasser schrumpelig – und die Antwort wird dich überraschen.", "curiosity_gap"),
    ):
        result = assess_verbal(text, strategy, context)
        assert "question_restatement" in result["hard_fail"], text
        assert result["dimensions"]["insight"] <= 0.2
    teaser = assess_verbal("Finger werden im Wasser schrumpelig – und die Antwort wird dich überraschen.", "curiosity_gap", context)
    assert "empty_teaser" in teaser["hard_fail"] and teaser["dimensions"]["curiosity"] <= 0.1


def test_question_with_added_information_is_kept_but_marked_slow():
    result = assess_verbal("Finger schrumpeln im Wasser – aber nicht, weil sie Wasser aufsaugen.", "counterintuitive_insight", fingers())
    assert result["hard_fail"] == []
    assert "question_restatement" not in result["reason_codes"]
    # Denying the obvious explanation is new information, not a restatement.
    denial = assess_verbal("Wasser ist gar nicht der Grund für deine schrumpeligen Finger.", "counterintuitive_insight", fingers())
    assert "question_restatement" not in denial["hard_fail"]


def test_the_question_itself_stays_the_emergency_fallback():
    context = fingers()
    result = assess_verbal("Warum werden unsere Finger im Wasser schrumpelig?", "curiosity_gap", context, emergency=True)
    assert not {"question_restatement", "setup_opener", "empty_teaser", "vague_opening"} & set(result["hard_fail"])
    # Even when every researched hook is unusable, a hook always exists.
    assert select_verbal({**context, "allowed_facts": [], "body_sentences": []}, extra=[]) is not None


def test_specific_surprising_hook_is_accepted_with_a_strong_first_second():
    result = assess_verbal("Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", "counterintuitive_insight", fingers())
    assert result["hard_fail"] == []
    dims = result["dimensions"]
    assert dims["first_second_clarity"] == 1.0 and dims["scroll_stop"] >= 0.9 and dims["information_density"] >= 0.9
    assert result["opening_move"] == "counterintuitive"


def test_vague_opening_is_rejected_and_slow_specificity_costs_clarity():
    context = fingers()
    vague = assess_verbal("Etwas Seltsames passiert, wenn du lange badest.", "curiosity_gap", context)
    assert "vague_opening" in vague["hard_fail"] and vague["dimensions"]["first_second_clarity"] == 0.0
    # A placeholder start that later names the subject is only slower.
    late = assess_verbal("Something strange happens to injured cats: they purr.", "counterintuitive_insight", purr())
    assert "vague_opening" in late["reason_codes"] and "vague_opening" not in late["hard_fail"]
    direct = assess_verbal("Injured cats purr – not only happy ones.", "counterintuitive_insight", purr())
    assert direct["dimensions"]["first_second_clarity"] > late["dimensions"]["first_second_clarity"]


def test_weak_link_verbs_and_hard_words_lower_the_opening():
    context = fingers()
    weak = assess_verbal("Die Falten spielen eine Rolle beim Greifen.", "evidence_insight", context)
    assert "weak_verb" in weak["reason_codes"]
    strong = assess_verbal("Die Falten helfen beim Greifen nasser Steine.", "evidence_insight", context)
    assert strong["dimensions"]["information_density"] > weak["dimensions"]["information_density"]


def test_early_payoff_is_rejected_but_a_legitimate_reveal_hook_is_preserved():
    context = fingers()
    flat = assess_verbal("Finger schrumpeln, weil das Nervensystem die Blutgefäße verengt.", "evidence_insight", context)
    assert "early_payoff" in flat["hard_fail"]
    # A reveal that opens a new question keeps its place.
    reveal = assess_verbal("Nicht das Wasser macht die Falten, sondern deine Blutgefäße.", "direct_reframe", context)
    assert reveal["hard_fail"] == [] and reveal["opening_move"] == "contrast"


def test_protected_answer_is_still_never_revealed():
    fixture = story(ISLANDS_Q, [dict(item) for item in ISLANDS])
    plan = plan_for(fixture, islands_candidates(), judge=Judge(), protected_target="subject_a")
    assert plan["hook_id"] == "hook_c"
    assert "verbal_names_protected_answer" in by_id(plan)["hook_a"]["hard_fail"]
    assert "schwed" not in channels(plan) and "swed" not in channels(plan)
    report = plan["hook_quality"]
    assert report["version"] == 3 and report["payoff_integrity"] > 0 and report["opening_move"] == "number"


def test_first_second_window_follows_the_speaking_rate():
    assert hook_quality.window_words(hook_quality.FIRST_SECOND_SECONDS) == 4
    assert hook_quality.window_words(hook_quality.OPENING_SECONDS) == 8
    assert hook_quality.window_words(1.5, wpm=120) == 3


# ---------------------------------------------------------------------------
# Channels: on-screen text and first frame
# ---------------------------------------------------------------------------

def test_verbal_and_text_duplication_is_penalized_and_complementary_text_wins():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    verbal = "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen."
    kwargs = {"action": "gripping a wet stone", "detail": "deep ridges", "payoff_fact": "fact_01"}
    duplicate = ai_candidate("A", "counterintuitive_insight", verbal, "wet fingertips with wrinkles", ["wrinkled wet fingertips"],
                             on_screen="Finger schrumpeln nicht", **kwargs)
    overlap = ai_candidate("B", "counterintuitive_insight", verbal, "wet fingertips with wrinkles", ["wrinkled wet fingertips"],
                           on_screen="Finger schrumpeln ohne Wasser?", **kwargs)
    complementary = ai_candidate("C", "counterintuitive_insight", verbal, "wet fingertips with wrinkles", ["wrinkled wet fingertips"],
                                 on_screen="Dein Nervensystem steckt dahinter", **kwargs)
    context = context_for(fixture)
    results = {}
    for index, raw in enumerate((duplicate, overlap, complementary)):
        candidate = triple_hook.normalise_candidate(raw, index, context, origin="ai")
        results[raw["id"]] = (candidate, triple_hook.assess_candidate(candidate, context))
    dup_candidate, dup = results["A"]
    assert dup_candidate["on_screen_omitted_reason"] == "on_screen_duplicates_verbal"
    _over_candidate, over = results["B"]
    assert "on_screen_overlaps_verbal" in over["reason_codes"]
    comp_candidate, comp = results["C"]
    assert comp_candidate["on_screen_hook"] == "Dein Nervensystem steckt dahinter"
    assert comp["dimensions"]["complementarity"] > over["dimensions"]["complementarity"]
    assert comp["dimensions"]["complementarity"] > dup["dimensions"]["complementarity"]
    assert comp["dimensions"]["on_screen_quality"] > over["dimensions"]["on_screen_quality"]


def test_on_screen_roles_reward_numbers_contrast_and_clues_not_the_question():
    context = purr()
    verbal = "Injured cats purr – not only happy ones."
    assert hook_quality.on_screen_role("25 bis 150 Hertz", verbal, context)[0] == "number"
    assert hook_quality.on_screen_role("Not joy?", verbal, context)[0] == "contrast"
    assert hook_quality.on_screen_role("Healing sound?", verbal, context)[0] == "clue"
    role, codes = hook_quality.on_screen_role("Why do cats purr?", verbal, context)
    assert "on_screen_restates_question" in codes and role == "clue"


def test_first_frame_prefers_instant_motion_over_generic_or_cropped_stock():
    context = purr()
    moving = {"subject": "cat with a bandaged paw", "action_state": "purring while being stroked", "framing": "close-up",
              "key_detail": "throat vibrating", "media_queries": ["injured cat purring close up"]}
    generic = {"subject": "woman thinking about cats", "framing": "", "media_queries": ["woman thinking"]}
    wide = {"subject": "cats, dogs and birds in a shelter", "framing": "wide shot", "media_queries": ["animal shelter wide shot"]}
    still = {"subject": "cats", "framing": "", "media_queries": ["cats"]}
    frame = {name: hook_quality.assess_first_frame(visual, context) for name, visual in
             {"moving": moving, "generic": generic, "wide": wide, "still": still}.items()}
    assert frame["moving"]["reason_codes"] == []
    assert "visual_generic_stock" in frame["generic"]["reason_codes"]
    assert {"visual_crop_risk", "visual_cluttered"} <= set(frame["wide"]["reason_codes"])
    assert "visual_merely_illustrates" in frame["still"]["reason_codes"]
    best = frame["moving"]["visual_clarity"] + frame["moving"]["visual_intrigue"]
    assert all(best > item["visual_clarity"] + item["visual_intrigue"] for name, item in frame.items() if name != "moving")


def test_cross_language_visual_is_not_called_off_topic():
    # German project, English visual semantics: relation is not guessed.
    frame = hook_quality.assess_first_frame(
        {"subject": "Swedish archipelago from above", "media_queries": ["swedish archipelago"]},
        context_for(story(ISLANDS_Q, [dict(item) for item in ISLANDS])),
    )
    assert "visual_off_topic" not in frame["reason_codes"]


def test_planner_visual_for_the_opening_is_chosen_for_the_first_frame_not_for_repeating_the_voice():
    context = purr()
    illustrative = {"subject": "cats", "visual_goal": "cats", "media_queries": ["cats purring"]}
    striking = {"subject": "cat with a bandaged paw", "action_state": "purring on a vet table", "framing": "close-up",
                "key_detail": "throat vibrating", "media_queries": ["injured cat purring vet"]}
    chosen = triple_hook.best_first_frame([illustrative, striking], context, "Why do cats purr?")
    assert chosen is striking


# ---------------------------------------------------------------------------
# Candidate diversity
# ---------------------------------------------------------------------------

def test_paraphrases_with_different_strategy_labels_are_one_candidate():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    base = {"action": "gripping a wet stone", "detail": "deep ridges", "payoff_fact": "fact_01"}
    first = ai_candidate("A", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
                         "wet fingertips with wrinkles", ["wrinkled wet fingertips"], **base)
    paraphrase = ai_candidate("B", "direct_reframe", "Deine Finger schrumpeln nicht, weil sie das Wasser aufsaugen.",
                              "wet fingertips with wrinkles", ["wrinkled wet fingertips"], **base)
    different = ai_candidate("C", "direct_reframe", "Nicht das Wasser macht die Falten, sondern deine Blutgefäße.",
                             "wet fingertips with wrinkles", ["wrinkled wet fingertips"], **base)
    plan = plan_for(fixture, [first, paraphrase, different])
    texts = [item["verbal_hook"] for item in plan["selection"]["candidates"] if item["origin"] == "ai"]
    assert texts == [first["verbal_hook"], different["verbal_hook"]]
    moves = {item["opening_move"] for item in plan["selection"]["candidates"]}
    assert {"counterintuitive", "contrast"} <= moves
    assert set(plan["selection"]["opening_moves"]) == moves


def test_single_move_provider_pool_is_topped_up_with_a_different_approach():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    planner = [{"visual_goal": "wrinkled wet fingertips", "objects": ["fingertips"], "actions": ["gripping a wet stone"],
                "media_queries": ["wrinkled wet fingertips"], "source": "planner"}]
    raw = [
        ai_candidate(cid, "counterintuitive_insight", text, "wet fingertips with wrinkles", ["wrinkled wet fingertips"],
                     action="gripping a wet stone", detail="deep ridges", payoff_fact="fact_01")
        for cid, text in (
            ("A", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen."),
            ("B", "Nicht das Badewasser lässt deine Finger schrumpeln."),
            ("C", "Die Haut deiner Finger saugt beim Baden kein Wasser auf."),
            ("D", "Deine Fingerkuppen falten sich nicht durch aufgesaugtes Wasser."),
        )
    ]
    plan = triple_hook.plan_triple_hook(
        intent=fixture["intent"], facts=fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
        generation=generation(raw), judge=Judge(), settings=SETTINGS,
        planner_visuals=planner,
    )
    ai_moves = {item["opening_move"] for item in plan["selection"]["candidates"] if item["origin"] == "ai" and item["eligible"]}
    assert len(ai_moves) == 1
    # Deterministic research candidates (no provider call) add another approach.
    assert any(item["origin"] != "ai" for item in plan["selection"]["candidates"])
    assert len(plan["selection"]["opening_moves"]) >= 2
    assert plan["selection"]["candidate_count"] <= triple_hook.MAX_POOL


def test_opening_moves_cover_the_documented_approaches():
    assert hook_quality.opening_move("Rund 267.570 gegen etwa 17.000 Inseln?") == "number"
    assert hook_quality.opening_move("Kannst du das Tier erkennen?") == "challenge"
    assert hook_quality.opening_move("Schau auf diese Fingerkuppe.") == "visual_reveal"
    assert hook_quality.opening_move("Nicht das Wasser, sondern deine Nerven.") == "contrast"
    assert hook_quality.opening_move("Die Haut saugt kein Wasser auf – und trotzdem kommen Falten.") == "contradiction"
    assert hook_quality.opening_move("Ein Kratzer kann ein Glas zerstören.") == "consequence"
    assert hook_quality.opening_move("Warum schnurren verletzte Katzen?") == "mystery"


# ---------------------------------------------------------------------------
# Integration: pipeline, report and cost
# ---------------------------------------------------------------------------

def test_pipeline_persists_the_v3_report_and_makes_no_extra_provider_call(monkeypatch):
    calls = {"generation": 0}

    def provider(*_args, **_kwargs):
        calls["generation"] += 1
        return generation(finger_pipeline_candidates())

    judge = Judge()
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([{key: value for key, value in item.items() if key != "id"} for item in FINGER_FACTS], [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", provider)
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge)
    state = build_initial_state(FINGERS_Q, AdvancedOptions(), Settings(clipforge_ai_mode="local", openai_api_key=None))
    plan = state["script"]["triple_hook"]
    assert calls["generation"] == 1 and len(judge.calls) <= 1
    report = plan["hook_quality"]
    assert set(report) >= {
        "immediate_clarity", "curiosity_gap", "specificity", "novelty", "information_density",
        "complementarity", "payoff_integrity", "scroll_stop", "opening_move", "first_second",
    }
    # The selected hook still opens the narration (story/script pipeline intact).
    first = state["script"]["blocks"][0]
    assert first["role"] == "hook" and first["text"] == plan["verbal_hook"]
    assert not hook_quality.setup_opener(plan["verbal_hook"])


def test_pipeline_rejects_a_setup_provider_hook_in_favour_of_a_concrete_one(monkeypatch):
    setup = ai_candidate("A", "curiosity_gap", "Hast du dich schon mal gefragt, warum deine Finger im Wasser schrumpelig werden?",
                         "wet fingertips with wrinkles", ["wrinkled wet fingertips"], action="gripping a wet stone",
                         detail="deep ridges", payoff_fact="fact_01")
    concrete = ai_candidate("B", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
                            "wet fingertips with wrinkles", ["wrinkled wet fingertips"], action="gripping a wet stone",
                            detail="deep ridges", on_screen="Dein Nervensystem steckt dahinter", payoff_fact="fact_01")
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], [setup, concrete], Judge())
    plan = state["script"]["triple_hook"]
    assert plan["verbal_hook"] == concrete["verbal_hook"]
    assert "setup_opener" in by_id(plan)["hook_a"]["hard_fail"]


def test_prompts_carry_the_first_second_rules_without_a_new_call():
    assert "FIRST SECOND" in HOOK_GENERATION_INSTRUCTIONS and "DIFFERENT move" in HOOK_GENERATION_INSTRUCTIONS
    assert "9:16" in HOOK_GENERATION_INSTRUCTIONS and "cold viewer" in TRIPLE_HOOK_JUDGE_INSTRUCTIONS
