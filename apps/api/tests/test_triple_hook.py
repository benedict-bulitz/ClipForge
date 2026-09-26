"""Triple Hook V2: verbal, visual and on-screen hooks selected together.

Topics are fixtures only; production logic has no topic vocabulary.  Provider
calls are replaced by fakes (one generation call, one judge call).
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_story_arc import ISLANDS, ISLANDS_Q, RANKING, RANKING_Q, fact

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge import final_critic, triple_hook
from clipforge.ai import (
    AIHookGenerationResponse,
    AIHookGenerationResult,
    AITripleHookCandidate,
    AITripleHookJudgement,
    AITripleHookJudgeResponse,
    AITripleHookJudgeResult,
    AITripleHookVisual,
    generate_hook_candidates_with_openai,
    judge_triple_hooks_with_openai,
)
from clipforge.attention import replan_attention
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.hooks import HookCandidate
from clipforge.media import attach_project_overlays, build_visual_query_plan
from clipforge.models import Project
from clipforge.novelty import build_novelty_plan
from clipforge.payoff import build_payoff_plan
from clipforge.pipeline import build_initial_state
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.services import create_project
from clipforge.simple_graphics import render_overlay
from clipforge.story_arc import build_story_arc
from clipforge.visual_director import (
    build_generation_prompt,
    generation_policy,
    plan_scene_strategy,
)
from clipforge.visual_verifier import visual_intent_text

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FINGERS_Q = "Warum werden unsere Finger im Wasser schrumpelig?"
FINGER_FACTS = [
    fact(1, "Schrumpelige Finger entstehen, weil das Nervensystem die Blutgefäße in den Fingerkuppen verengt.", 0.95),
    fact(2, "Die Falten entstehen nicht, weil die Haut Wasser aufsaugt, sondern weil sich Blutgefäße unter der Haut zusammenziehen."),
    fact(3, "Bei durchtrennten Fingernerven bleiben die Falten aus."),
    fact(4, "Die Falten helfen vermutlich dabei, nasse Gegenstände besser zu greifen.", 0.7),
]
QUIZ_Q = "Quiz: Welches Tier kann nicht rückwärts laufen?"
QUIZ_FACTS = [
    fact(1, "Das Känguru kann nicht rückwärts laufen.", 0.95),
    fact(2, "Sein kräftiger Schwanz und die großen Hinterbeine sind für Vorwärtssprünge gebaut."),
]
PURR_Q = "Why do cats purr?"
PURR_FACTS = [
    fact(1, "Cats purr when muscles in the larynx twitch rapidly as they breathe.", 0.95),
    fact(2, "Cats also purr when they are injured or stressed, not only when they are happy."),
    fact(3, "Purring vibrations between 25 and 150 hertz may help bones and tissue heal."),
]


def story(question: str, facts: list[dict], *, language: str = "de") -> dict:
    """Intent, Story Arc, format and payoff plan exactly as the pipeline derives them."""
    intent = {"question": question, "topic": question, "language": language, "content_type": "factual_explainer", "tone": "documentary"}
    novelty = build_novelty_plan(intent, facts)
    format_plan = plan_format(intent, facts, [], novelty)
    protected = format_plan["selected_format"] in {"comparison", "quiz"}
    arc = build_story_arc(intent, facts, format_plan, novelty, protected=protected)
    units = {unit["id"]: unit for unit in arc["units"]}
    body = [{"role": "answer" if fact_id == arc["primary_answer_id"] else "support", "text": units[fact_id]["claim"], "fact_ids": [fact_id]} for fact_id in arc["order"]]
    payoff = build_payoff_plan(intent, body, format_plan=format_plan, novelty_plan=novelty, story_arc=arc)
    return {"intent": intent, "facts": facts, "arc": arc, "format": format_plan, "novelty": novelty, "payoff": payoff, "body": body}


def ai_candidate(
    cid: str, strategy: str, verbal: str, subject: str, queries: list[str], *, on_screen: str = "", action: str = "",
    framing: str = "close-up", detail: str = "", tension: str = "", targets: list[str] | None = None,
    payoff: str = "", payoff_fact: str = "", protected: list[str] | None = None, feasibility: str = "real_media_likely",
) -> dict:
    return AITripleHookCandidate(
        id=cid, strategy=strategy, verbal_hook=verbal,
        visual=AITripleHookVisual(
            subject=subject, action_state=action, framing=framing, key_detail=detail, tension=tension,
            media_queries=queries, media_query_targets=targets or [],
        ),
        on_screen_hook=on_screen, promised_payoff=payoff, payoff_fact_id=payoff_fact,
        protected_information=protected or [], production_feasibility=feasibility, rationale="fixture",
    ).model_dump(mode="json")


def generation(candidates: list[dict]) -> AIHookGenerationResult:
    return AIHookGenerationResult(
        [{"strategy": "evidence_insight", "text": item["verbal_hook"]} for item in candidates], None, "connected",
        triple_candidates=candidates,
    )


class Judge:
    """Records the one judge call; scores every candidate from a table (default 7)."""

    def __init__(self, scores: dict[str, dict] | None = None):
        self.scores = scores or {}
        self.calls: list[tuple[dict, list[dict]]] = []

    def __call__(self, story_payload, items, _settings):
        self.calls.append((story_payload, items))
        judgements = []
        for item in items:
            values = {key: 7 for key in triple_hook.DIMENSIONS}
            values.update(self.scores.get(item["candidate_id"], {}))
            judgements.append({"candidate_id": item["candidate_id"], "veto": "none", "reason_codes": [], **values})
        return AITripleHookJudgeResult(judgements, None, "connected")


SETTINGS = Settings(clipforge_ai_mode="local", openai_api_key=None)


def plan_for(fixture: dict, candidates: list[dict], *, judge: Judge | None = None, baseline: HookCandidate | None = None, protected_target: str | None = None) -> dict:
    return triple_hook.plan_triple_hook(
        intent=fixture["intent"], facts=fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"], baseline=baseline,
        generation=generation(candidates), judge=judge or Judge(), settings=SETTINGS, protected_target=protected_target,
    )


def by_id(plan: dict) -> dict[str, dict]:
    return {item["id"]: item for item in plan["selection"]["candidates"]}


def channels(plan: dict) -> str:
    visual = plan["visual_hook"]
    return " ".join([plan["verbal_hook"], plan["on_screen_text_hook"], visual["subject"], visual["visual_goal"], *visual["media_queries"]]).casefold()


# ---------------------------------------------------------------------------
# A) Sweden / Indonesia — comparison, protected winner
# ---------------------------------------------------------------------------

def islands_candidates() -> list[dict]:
    return [
        ai_candidate("A", "verified_statistic", "Schweden hat die meisten Inseln der Welt.", "Swedish archipelago from above", ["swedish archipelago"],
                     targets=["subject_a"], payoff_fact="fact_02"),
        ai_candidate("B", "counterintuitive_insight", "Indonesien hat etwa 17.000 Inseln – und liegt trotzdem nicht vorn.", "Indonesian islands from the air",
                     ["indonesian islands aerial"], targets=["subject_b"], payoff_fact="fact_02"),
        ai_candidate("C", "verified_statistic", "Indonesien hat etwa 17.000 Inseln. Reicht das für Platz eins?", "countless green islands in a turquoise sea",
                     ["indonesian islands aerial", "tropical islands from above"], targets=["subject_b", "context"], framing="aerial shot",
                     action="drone glides over the island chain", detail="islands up to the horizon", tension="looks unbeatable",
                     on_screen="Reicht das für Platz 1?", payoff="Schweden hat rund 267.570 Inseln", payoff_fact="fact_02"),
        ai_candidate("D", "curiosity_gap", "Vulkane formen die schönsten Inseln im Pazifik.", "smoking volcano on an island",
                     ["volcano island smoke"], payoff="Warum Vulkane Inselketten bilden"),
    ]


def test_islands_four_distinct_candidates_and_protected_winner_never_leaks():
    fixture = story(ISLANDS_Q, [dict(item) for item in ISLANDS])
    assert fixture["payoff"]["hook_must_not_reveal"].rstrip(".") == "Schweden"
    judge = Judge({"C": {"complementarity": 9, "curiosity": 9}})
    plan = plan_for(fixture, islands_candidates(), judge=judge, protected_target="subject_a")

    candidates = by_id(plan)
    assert plan["selection"]["candidate_count"] == 4 and len({item["strategy"] for item in candidates.values()}) >= 3
    assert all(item["strategy"] in triple_hook.STRATEGIES for item in candidates.values())
    assert "verbal_names_protected_answer" in candidates["hook_a"]["hard_fail"]
    assert "visual_queries_protected_target" in candidates["hook_a"]["hard_fail"]
    assert "verbal_implies_protected_answer" in candidates["hook_b"]["hard_fail"]
    assert "payoff_mismatch" in candidates["hook_d"]["hard_fail"]
    assert plan["hook_id"] == "hook_c" and plan["status"] == "connected"
    # Complete: all three channels, strategy, promise and reasons persisted.
    assert plan["verbal_hook"] and plan["visual_hook"]["subject"] and plan["visual_hook"]["media_queries"]
    assert plan["on_screen_text_hook"] == "Reicht das für Platz 1?" and plan["on_screen_hook_status"] == "shown"
    assert plan["payoff_fact_id"] == "fact_02" and plan["selected_strategy"] == "verified_statistic"
    # Only one complete candidate survived the checks: no judge call is spent on it.
    assert isinstance(plan["score"], float) and plan["selection"]["judge"]["status"] == "not_called"
    # Sweden never appears in any selected channel; Indonesia (evidence) may.
    assert "schwed" not in channels(plan) and "swed" not in channels(plan)
    assert "indonesi" in channels(plan)
    assert "Schweden" in plan["visual_hook"]["must_not_show"]
    assert judge.calls == []
    # With two complete, safe candidates the judge sees only those (a leaking
    # hook is never "scored up").
    second = ai_candidate("E", "verified_statistic", "17.000 Inseln – und Indonesien ist trotzdem nicht allein an der Spitze der Rekordliste?",
                          "tropical island chain from above", ["tropical islands from above"], targets=["context"], action="waves around the islands",
                          detail="white beaches", payoff_fact="fact_02")
    plan = plan_for(fixture, [*islands_candidates()[:3], second], judge=judge, protected_target="subject_a")
    assert len(judge.calls) == 1
    assert {item["candidate_id"] for item in judge.calls[0][1]} == {"hook_c", "hook_d"}
    assert "hook_c" in {item["candidate_id"] for item in judge.calls[0][1]}
    assert judge.calls[0][0]["hook_must_not_reveal"].rstrip(".") == "Schweden"


def test_islands_protected_subject_may_be_an_open_option_only_when_spoken():
    fixture = story(ISLANDS_Q, [dict(item) for item in ISLANDS])
    context = triple_hook.hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"],
    )
    assert triple_hook.leaks("Schweden oder Indonesien – wer hat mehr Inseln?", context) is None
    assert triple_hook.leaks("Schweden oder Indonesien?", context, strict=True) == "names_protected_answer"
    assert triple_hook.leaks("Es gibt mehr Inseln in Schweden als in Indonesien.", context) == "names_protected_answer"
    assert triple_hook.leaks("Indonesien verliert dieses Duell.", context) == "implies_protected_answer"
    assert triple_hook.leaks("Indonesien hat mehr als 17.000 Inseln.", context) is None
    # Story Arc is authoritative: the protected answer is the primary answer
    # (Sweden), not the last payoff block (Indonesia, secondary insight).
    assert context["primary_answer_id"] == "fact_02" and context["final_payoff_id"] == "fact_04"
    assert context["forbidden"] == {"schweden"}


# ---------------------------------------------------------------------------
# B) Fingers — concrete visual, complementarity, clickbait, vague visual
# ---------------------------------------------------------------------------

def finger_candidates() -> list[dict]:
    return [
        ai_candidate("A", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", "wet fingertips with deep wrinkles",
                     ["wrinkled wet fingertips", "wrinkled fingers gripping wet stone"], framing="extreme close-up",
                     action="gripping a smooth wet stone", detail="deep ridges on the fingertip pads",
                     on_screen="Dein Nervensystem steckt dahinter", payoff_fact="fact_01"),
        ai_candidate("B", "evidence_insight", "Finger werden im Wasser schrumpelig.", "fingers wrinkling in water", ["wrinkled fingers water"],
                     framing="", on_screen="Finger werden schrumpelig", payoff_fact="fact_01"),
        ai_candidate("C", "high_stakes_consequence", "Das wirst du nicht glauben: Deine Finger!", "wet hand in a bathtub", ["wet hand bathtub"],
                     action="rising out of the water", detail="water droplets", payoff_fact="fact_01"),
        ai_candidate("D", "curiosity_gap", "Nach langem Baden verändert sich deine Haut an einer ganz bestimmten Stelle.",
                     "Interesting science concept about why skin changes", ["science concept"], payoff_fact="fact_01"),
    ]


def test_fingers_selects_a_concrete_complementary_opening():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    plan = plan_for(fixture, finger_candidates())
    candidates = by_id(plan)

    assert plan["hook_id"] == "hook_a"
    assert "redundant_channels" in candidates["hook_b"]["hard_fail"]  # voice, image and text say the same
    assert candidates["hook_b"]["on_screen_omitted_reason"] == "on_screen_duplicates_verbal"
    assert "cheap_clickbait" in candidates["hook_c"]["hard_fail"]
    assert "visual_vague" in candidates["hook_d"]["hard_fail"]
    visual = plan["visual_hook"]
    assert visual["source"] == triple_hook.SOURCE and visual["framing"] == "extreme close-up"
    assert visual["media_queries"] == ["wrinkled wet fingertips", "wrinkled fingers gripping wet stone"]
    assert plan["on_screen_text_hook"] == "Dein Nervensystem steckt dahinter"
    assert candidates["hook_a"]["dimensions"]["complementarity"] > candidates["hook_b"]["dimensions"]["complementarity"]


def test_impossible_visual_loses_even_with_a_great_verbal_hook():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    great = ai_candidate("A", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.",
                         "nerve signal travelling live through the blood vessels of a finger", ["nerve signal inside finger blood vessel"],
                         feasibility="impossible", payoff_fact="fact_01")
    plain = ai_candidate("B", "evidence_insight", "Nach zehn Minuten in der Wanne verengt dein Nervensystem die Blutgefäße in deinen Fingerkuppen.",
                         "wet fingertips with wrinkles", ["wrinkled wet fingertips"], action="pressing on a smooth tile",
                         detail="deep wrinkles", payoff_fact="fact_01")
    plan = plan_for(fixture, [great, plain], judge=Judge({"hook_a": {"curiosity": 10, "verbal_quality": 10}}))
    assert "visual_infeasible" in by_id(plan)["hook_a"]["hard_fail"]
    assert plan["hook_id"] == "hook_b"


def test_judge_prefers_the_complementary_triple_and_can_veto():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    first = ai_candidate("A", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", "wet fingertips with deep wrinkles",
                         ["wrinkled wet fingertips"], action="gripping a wet stone", detail="deep ridges", on_screen="Dein Nervensystem steckt dahinter")
    second = ai_candidate("B", "evidence_insight", "Nach langem Baden legen sich deine Fingerkuppen in Falten.", "fingertips in a bathtub",
                          ["wrinkled fingertips bath"], action="rising out of the water", detail="wrinkles")
    judge = Judge({"hook_a": {"complementarity": 10, "curiosity": 9}, "hook_b": {"complementarity": 2, "curiosity": 5}})
    assert plan_for(fixture, [first, second], judge=judge)["hook_id"] == "hook_a"
    judge = Judge({"hook_a": {"complementarity": 2}, "hook_b": {"complementarity": 10}})
    assert plan_for(fixture, [first, second], judge=judge)["hook_id"] == "hook_b"

    class Veto(Judge):
        def __call__(self, story_payload, items, settings):
            result = super().__call__(story_payload, items, settings)
            result.judgements[0]["veto"] = "leaks_answer"
            return result

    vetoed = plan_for(fixture, [first, second], judge=Veto({"hook_a": {"curiosity": 10}}))
    assert vetoed["hook_id"] == "hook_b" and "judge_leaks_answer" in by_id(vetoed)["hook_a"]["hard_fail"]


def test_generic_opener_and_duplicate_on_screen_text_are_penalized():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    generic = ai_candidate("A", "evidence_insight", "Wusstest du, dass Finger im Wasser Falten bekommen?", "wet fingertips with wrinkles",
                           ["wrinkled wet fingertips"], action="gripping a stone", detail="wrinkles")
    concrete = ai_candidate("B", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", "wet fingertips with wrinkles",
                            ["wrinkled wet fingertips"], action="gripping a stone", detail="wrinkles",
                            on_screen="Deine Finger schrumpeln nicht")
    plan = plan_for(fixture, [generic, concrete])
    candidates = by_id(plan)
    assert "generic_opener" in candidates["hook_a"]["reason_codes"]
    assert candidates["hook_b"]["on_screen_omitted_reason"] == "on_screen_duplicates_verbal"
    assert "on_screen_duplicates_verbal" in candidates["hook_b"]["reason_codes"]
    assert plan["hook_id"] == "hook_b" and plan["on_screen_text_hook"] == ""


# ---------------------------------------------------------------------------
# F) On-screen text: complete, short and new — or omitted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Weil das Nervensystem", "on_screen_fragment"),
        ("Das Nervensystem und", "on_screen_fragment"),
        ("Dein Nervensystem steckt da…", "on_screen_fragment"),
        ("Dein Nervensystem steckt hinter diesem erstaunlichen Effekt", "on_screen_too_long"),
        ("Das ist verrückt!", "on_screen_generic"),
        ("", "no_supplementary_text"),
    ],
)
def test_broken_or_useless_on_screen_hook_is_omitted_never_truncated(text, reason):
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    context = triple_hook.hook_context(
        fixture["intent"], fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=None, body_blocks=fixture["body"],
    )
    assert triple_hook.validate_on_screen(text, "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", context) == ("", reason)
    assert triple_hook.validate_on_screen("Dein Nervensystem steckt dahinter", "Deine Finger schrumpeln nicht.", context) == (
        "Dein Nervensystem steckt dahinter", None,
    )


def test_topic_without_useful_supplementary_text_omits_the_overlay():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    candidate = ai_candidate("A", "counterintuitive_insight", "Deine Finger schrumpeln nicht, weil sie Wasser aufsaugen.", "wet fingertips with wrinkles",
                             ["wrinkled wet fingertips"], action="gripping a stone", detail="wrinkles", on_screen="Das ist verrückt!")
    plan = plan_for(fixture, [candidate])
    assert plan["hook_id"] == "hook_a" and plan["on_screen_text_hook"] == ""
    assert plan["on_screen_hook_status"] == "omitted" and plan["on_screen_omitted_reason"] == "on_screen_generic"
    state = {"script": {"triple_hook": plan, "blocks": [{"id": "voice_block_01", "role": "hook", "text": plan["verbal_hook"]}]}}
    assert triple_hook.hook_overlay_spec(state) is None


# ---------------------------------------------------------------------------
# C) English, D) ranking, E) quiz — format-aware reveal rules
# ---------------------------------------------------------------------------

def test_english_project_keeps_language_and_prefers_concrete_anomaly_over_trivia():
    fixture = story(PURR_Q, [dict(item) for item in PURR_FACTS], language="en")
    trivia = ai_candidate("A", "evidence_insight", "Did you know cats purr?", "Cat lying on a sofa", ["cat purring sofa"], action="purring", detail="closed eyes")
    anomaly = ai_candidate("B", "counterintuitive_insight", "A purring cat is not always a happy cat.", "Injured cat at the vet", ["cat at vet"],
                           action="purring on the examination table", detail="bandaged paw", on_screen="Purring can mean pain", payoff_fact="fact_02")
    plan = plan_for(fixture, [trivia, anomaly])
    assert plan["hook_id"] == "hook_b" and plan["verbal_hook"].startswith("A purring cat")
    assert plan["on_screen_text_hook"] == "Purring can mean pain"
    assert "generic_opener" in by_id(plan)["hook_a"]["reason_codes"]
    assert by_id(plan)["hook_a"]["dimensions"]["format_fit"] < by_id(plan)["hook_b"]["dimensions"]["format_fit"]


def test_ranking_never_leaks_the_top_item():
    fixture = story(RANKING_Q, [dict(item) for item in RANKING])
    assert fixture["arc"]["structure"] == "ranked_progression" and fixture["arc"]["primary_answer_id"] == "fact_01"
    leak = ai_candidate("A", "verified_statistic", "Der Nil ist der längste Fluss der Welt.", "Nile river from the air", ["nile river aerial"])
    tease = ai_candidate("B", "curiosity_gap", "Rate mal, welcher Fluss ganz oben steht.", "wide river winding through rainforest",
                         ["river winding through rainforest aerial"], action="winding to the horizon", detail="brown water",
                         on_screen="Platz 1 bleibt noch geheim")
    plan = plan_for(fixture, [leak, tease])
    assert "verbal_names_protected_answer" in by_id(plan)["hook_a"]["hard_fail"]
    assert plan["hook_id"] == "hook_b" and "nil" not in channels(plan).split()


def test_quiz_answer_never_appears_in_any_channel():
    fixture = story(QUIZ_Q, [dict(item) for item in QUIZ_FACTS])
    assert fixture["format"]["selected_format"] == "quiz" and fixture["arc"]["curiosity_gap"]["withhold_answer"]
    spoken = ai_candidate("A", "ego_challenge", "Das Känguru kann nicht rückwärts laufen – wusstest du das?", "animal hopping across a meadow",
                          ["animal hopping meadow"], protected=["Känguru", "kangaroo"])
    shown = ai_candidate("B", "curiosity_gap", "Dieses Tier kommt nur in eine Richtung voran.", "kangaroo hopping across a meadow",
                         ["kangaroo hopping"], protected=["Känguru", "kangaroo"])
    english = ai_candidate("C", "counterintuitive_insight", "Rückwärts? Für dieses Tier unmöglich.", "powerful hind legs mid-jump",
                           ["kangaroo legs jumping"], protected=["Känguru", "kangaroo"])
    good = ai_candidate("D", "ego_challenge", "Ein Tier kann nur vorwärts – errätst du welches?", "powerful hind legs and a long tail",
                        ["powerful hind legs jumping animal"], action="pushing off the ground", detail="long tail used for balance",
                        on_screen="Nur vorwärts möglich?", protected=["Känguru", "kangaroo"])
    plan = plan_for(fixture, [spoken, shown, english, good])
    candidates = by_id(plan)
    assert candidates["hook_a"]["hard_fail"] and candidates["hook_b"]["hard_fail"]
    assert "visual_names_protected_answer" in candidates["hook_c"]["hard_fail"]  # English name, cross-checked
    assert plan["hook_id"] == "hook_d" and "känguru" not in channels(plan) and "kangaroo" not in channels(plan)


# ---------------------------------------------------------------------------
# Fallbacks, compatibility, bounded calls
# ---------------------------------------------------------------------------

def test_paraphrased_candidates_are_not_counted_as_distinct():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    base = finger_candidates()[0]
    paraphrase = dict(base, id="E", verbal_hook="Deine Finger schrumpeln nicht, weil sie das Wasser aufsaugen.")
    plan = plan_for(fixture, [*finger_candidates(), paraphrase])
    assert plan["selection"]["generation"]["candidate_count"] == 5
    assert plan["selection"]["candidate_count"] == 4


def test_without_a_provider_the_plan_is_deterministic_and_never_invents_text():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    baseline = HookCandidate("counterintuitive_insight", "Nicht das Wasser macht die Falten, sondern deine Nerven.", 40.0, "fixture")
    plan = triple_hook.plan_triple_hook(
        intent=fixture["intent"], facts=fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"], baseline=baseline,
        generation=AIHookGenerationResult([], None, "missing_key", "no key"), judge=Judge(), settings=SETTINGS,
        planner_visuals=[{"visual_goal": "wrinkled wet fingertips close-up", "objects": ["fingertips"], "actions": ["gripping"],
                          "context": ["bathtub"], "media_queries": ["wrinkled wet fingertips"]}],
    )
    assert plan["version"] == 2 and plan["status"] == "deterministic" and plan["selection"]["judge"]["status"] == "not_called"
    # A documented strategy with research provenance (the planner's hook only competes).
    assert plan["selected_strategy"] in triple_hook.STRATEGIES and plan["supported_by_fact_ids"]
    assert plan["selected_strategy"] in {"counterintuitive_insight", "direct_reframe"}
    assert baseline.text in {item["verbal_hook"] for item in plan["selection"]["candidates"]}
    assert plan["visual_hook"]["media_queries"] == ["wrinkled wet fingertips"]
    assert plan["visual_hook"]["objects"] == ["fingertips"]  # the planner's own structure is kept
    no_visual = triple_hook.plan_triple_hook(
        intent=fixture["intent"], facts=fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=None, body_blocks=fixture["body"], baseline=baseline,
        generation=None, judge=None, settings=None,
    )
    assert no_visual["status"] == "fallback" and no_visual["selected_strategy"] in triple_hook.STRATEGIES
    assert no_visual["verbal_hook"] and no_visual["verbal_origin"] in {"planner", "deterministic"}
    assert no_visual["visual_hook"]["source"] == f"{triple_hook.SOURCE}_fallback"


def test_generation_and_judge_are_single_bounded_structured_calls(monkeypatch):
    calls: list[dict] = []

    class Responses:
        def parse(self, **kwargs):
            calls.append(kwargs)
            if kwargs["text_format"] is AIHookGenerationResponse:
                return SimpleNamespace(output_parsed=AIHookGenerationResponse(
                    triple_hook_candidates=[AITripleHookCandidate.model_validate(item) for item in finger_candidates()]
                ))
            return SimpleNamespace(output_parsed=AITripleHookJudgeResponse(judgements=[
                AITripleHookJudgement(candidate_id="hook_a", **{key: 8 for key in triple_hook.DIMENSIONS}),
            ], selected_candidate_id="hook_a"))

    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_kwargs: SimpleNamespace(responses=Responses()))
    settings = Settings(openai_api_key="not-a-real-key")
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    result = generate_hook_candidates_with_openai(FINGERS_Q, fixture["intent"], fixture["facts"], "Body.", settings, fixture["payoff"], story_arc={"primary_question": FINGERS_Q})
    assert result.status == "connected" and len(result.triple_candidates) == 4
    request = json.loads(calls[0]["input"])
    assert request["story_arc"]["primary_question"] == FINGERS_Q and request["facts"][0]["id"] == "fact_01"
    assert calls[0]["max_output_tokens"] <= 3200 and len(calls) == 1  # one bounded call when the answer is complete
    verdict = judge_triple_hooks_with_openai({"primary_question": FINGERS_Q}, [{"candidate_id": "hook_a"}], settings)
    assert verdict.status == "connected" and verdict.judgements[0]["candidate_id"] == "hook_a"
    assert len(calls) == 2 and calls[1]["max_output_tokens"] <= 1600


def test_old_projects_without_v2_plan_keep_their_behaviour():
    legacy = {"verbal_hook": "Most people pick Egypt.", "on_screen_text_hook": "WHO HAS MORE?", "visual_hook": {"visual_goal": "pyramids"}}
    state = {
        "script": {"triple_hook": legacy, "blocks": [{"id": "voice_block_01", "role": "hook", "text": "Most people pick Egypt."}]},
        "scenes": [{"id": "scene_01_01", "block_id": "voice_block_01", "narration": "Most people pick Egypt.", "visual_intent": {"visual_goal": "pyramids in the desert", "media_queries": ["pyramids desert"]}}],
        "intent": {"topic": "pyramids", "language": "en"},
        "payoff_plan": {},
    }
    assert triple_hook.state_plan(state) is None and triple_hook.hook_overlay_spec(state) is None
    strategy = plan_scene_strategy(state["scenes"][0], state, build_visual_query_plan(state["scenes"][0], state))
    assert "hook" not in strategy and strategy["overlay_spec"] is None


# ---------------------------------------------------------------------------
# Pipeline: the selected triple drives the real opening
# ---------------------------------------------------------------------------

def pipeline_state(monkeypatch, question: str, facts: list[dict], candidates: list[dict], judge: Judge | None = None) -> dict:
    from clipforge.research import ResearchResult

    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([{key: value for key, value in item.items() if key != "id"} for item in facts], [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge or Judge())
    return build_initial_state(question, AdvancedOptions(), Settings(clipforge_ai_mode="local", openai_api_key=None))


def finger_pipeline_candidates() -> list[dict]:
    plain = ai_candidate("E", "evidence_insight", "Nach zehn Minuten in der Wanne verengt dein Nervensystem die Blutgefäße in deinen Fingerkuppen.",
                         "wet fingertips with wrinkles", ["wrinkled wet fingertips"], action="pressing on a smooth tile",
                         detail="deep wrinkles", payoff_fact="fact_01")
    return [*finger_candidates()[:3], plain]


def test_pipeline_selected_hook_drives_narration_visual_and_overlay(monkeypatch):
    judge = Judge({"hook_a": {"complementarity": 10}, "hook_e": {"complementarity": 5}})
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_pipeline_candidates(), judge)
    plan = state["script"]["triple_hook"]
    blocks = state["script"]["blocks"]

    assert len(judge.calls) == 1  # one bounded judge call
    assert plan["hook_id"] == "hook_a" and plan["version"] == 2
    # Verbal hook is the actual opening narration, with no immediate repetition.
    assert blocks[0]["role"] == "hook" and blocks[0]["text"] == plan["verbal_hook"] == state["script"]["selected_hook"]
    assert state["script"]["text"].startswith(plan["verbal_hook"])
    assert blocks[1]["text"] != blocks[0]["text"]
    # Visual hook is the opening scene's visual intent, with provenance.
    opening = [scene for scene in state["scenes"] if scene["block_id"] == blocks[0]["id"]]
    intent = opening[0]["visual_intent"]
    assert intent["source"] == triple_hook.SOURCE and intent["hook_id"] == "hook_a"
    query_plan = build_visual_query_plan(opening[0], state)
    assert query_plan["queries"][:2] == ["wrinkled wet fingertips", "wrinkled fingers gripping wet stone"]
    assert any("wet fingertips with deep wrinkles" in text for text in visual_intent_text(opening[0], state))
    strategy = plan_scene_strategy(opening[0], state, query_plan)
    assert strategy["hook"]["visual_from_hook"] and strategy["visual_source"] == triple_hook.SOURCE
    assert strategy["overlay_spec"] == {"kind": "label", "text": "Dein Nervensystem steckt dahinter", "source": triple_hook.SOURCE, "hook_id": "hook_a"}
    prompt = build_generation_prompt(opening[0], state, strategy, query_plan)
    assert prompt is not None and "wet fingertips with deep wrinkles" in prompt["prompt"] and prompt["visual_source"] == "visual_intent"
    # Later scenes are untouched by the hook.
    later = next(scene for scene in state["scenes"] if scene["block_id"] != blocks[0]["id"])
    assert "hook" not in plan_scene_strategy(later, state, build_visual_query_plan(later, state))


def test_renderer_receives_the_hook_overlay_and_attention_stays_out_of_it(monkeypatch, tmp_path):
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_candidates())
    opening = state["scenes"][0]
    strategy = plan_scene_strategy(opening, state, build_visual_query_plan(opening, state))
    opening["visual_director"] = strategy
    opening["media"] = {"identity": "pexels:photo:1", "source": "pexels", "kind": "photo"}
    attach_project_overlays(state["scenes"])
    assert opening["overlays"][0]["spec"] == {"kind": "label", "text": "Dein Nervensystem steckt dahinter"}
    path = render_overlay(opening["overlays"][0]["spec"], tmp_path / "hook.png", width=270, height=480)
    assert path.is_file()
    state["captions"]["items"] = [{"start": 0.1, "end": 0.9, "text": "7 Prozent"}, *state["captions"]["items"]]
    replan_attention(state)
    assert not any(event["scene_index"] == 0 for event in state["attention_events"])


def test_selected_hook_persists_in_the_project_revision(monkeypatch, db):
    from clipforge.research import ResearchResult

    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([{key: value for key, value in item.items() if key != "id"} for item in FINGER_FACTS], [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(finger_candidates()))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    project = create_project(db, ProjectCreate(prompt=FINGERS_Q), Settings(clipforge_ai_mode="local", openai_api_key=None))
    project_id = project.id
    db.expire_all()
    restored = db.get(Project, project_id)
    plan = restored.revisions[0].state["script"]["triple_hook"]
    assert plan["hook_id"] == "hook_a" and len(plan["selection"]["candidates"]) == 4
    assert all({"score", "eligible", "hard_fail", "reason_codes", "dimensions"} <= set(item) for item in plan["selection"]["candidates"])


def test_islands_pipeline_opening_is_reveal_safe(monkeypatch):
    state = pipeline_state(monkeypatch, ISLANDS_Q, [dict(item) for item in ISLANDS], islands_candidates(), Judge())
    plan = state["script"]["triple_hook"]
    assert plan["hook_id"] == "hook_c"
    assert "schwed" not in state["script"]["blocks"][0]["text"].casefold()
    assert state["scenes"][0]["story_stage"] == "before_reveal"
    strategy = plan_scene_strategy(state["scenes"][0], state, build_visual_query_plan(state["scenes"][0], state))
    assert strategy["overlay_spec"]["text"] == "Reicht das für Platz 1?" and not strategy["reveal_allowed"]


# ---------------------------------------------------------------------------
# Final Video Critic reads the same persisted plan
# ---------------------------------------------------------------------------

class _NoVerifier:
    status = "unavailable"


def critic_review(state: dict, rows: list[tuple[dict, list[dict]]]) -> final_critic._Review:
    review = final_critic._Review(state, project_id="p", revision=1, settings=SETTINGS, verifier=_NoVerifier(), pass_index=0, vision_critic=None)
    scenes = {scene["id"]: scene for scene in state["scenes"]}
    for number, (entry, overlays) in enumerate(rows, 1):
        row = final_critic._Row({**entry, "overlays": overlays}, scenes.get(entry["scene_id"]), number)
        row.dimensions["_story"] = review._story_context(row)
        row.dimensions["semantic_match"] = {"rating": final_critic.GOOD}
        review.rows.append(row)
    review._hook()
    return review


def test_final_critic_checks_the_rendered_hook_against_the_plan(monkeypatch):
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_candidates())
    opening = state["scenes"][0]
    entry = {"scene_id": opening["id"], "start": opening["start"], "end": opening["end"]}
    label = {"spec": {"kind": "label", "text": "Dein Nervensystem steckt dahinter"}}

    good = critic_review(state, [(entry, [label])])
    assert good.hook_summary["hook_id"] == "hook_a" and good.hook_summary["rating"] == final_critic.GOOD
    assert good.hook_summary["planned"]["on_screen_hook"] == "Dein Nervensystem steckt dahinter"
    assert good.hook_summary["rendered"]["overlay_text"] == ["Dein Nervensystem steckt dahinter"]

    missing = critic_review(state, [(entry, [])])
    assert "hook_overlay_missing" in {issue["code"] for issue in missing.issues()}

    duplicate = {"spec": {"kind": "label", "text": "Deine Finger schrumpeln nicht"}}
    review = critic_review(state, [(entry, [duplicate])])
    assert "hook_overlay_duplicates_narration" in {issue["code"] for issue in review.issues()}
    opening["visual_director"] = plan_scene_strategy(opening, state, build_visual_query_plan(opening, state))
    actions = final_critic.plan_repairs(review)
    assert actions[0]["action"] == "adjust_composition" and actions[0]["adjustments"]["overlay"]["mode"] == "remove"
    assert not actions[0].get("rewrite_overlay")


def test_final_critic_reports_a_leaking_spoken_hook_for_regeneration(monkeypatch):
    state = pipeline_state(monkeypatch, ISLANDS_Q, [dict(item) for item in ISLANDS], islands_candidates(), Judge())
    state["script"]["blocks"][0]["text"] = "Schweden hat die meisten Inseln der Welt."
    opening = state["scenes"][0]
    review = critic_review(state, [({"scene_id": opening["id"], "start": opening["start"], "end": opening["end"]}, [])])
    codes = {issue["code"] for issue in review.issues()}
    assert {"hook_verbal_reveals_answer", "hook_verbal_drift"} <= codes
    actions = final_critic.plan_repairs(review)
    # Never re-voiced automatically: reported, no repair render for it.
    assert all(action["status"] == "blocked" for action in actions)


def test_generated_image_budget_and_repair_cap_are_unchanged(monkeypatch):
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_candidates())
    settings = Settings(clipforge_ai_mode="local", openai_api_key=None)
    policy = generation_policy(copy.deepcopy(state), settings)
    assert policy["max_auto_generated_images_per_project"] == settings.max_auto_generated_images_per_project
    strategy = plan_scene_strategy(state["scenes"][0], state, build_visual_query_plan(state["scenes"][0], state))
    assert strategy["fallback_chain"].count("generated_image") <= 1 and strategy["fallback_chain"][0] == "real_media"
    assert final_critic.MAX_REPAIR_PASSES_LIMIT == 2 and final_critic.DEFAULT_MAX_REPAIR_PASSES == 1


def test_rendered_opening_shows_the_hook_and_the_critic_reviews_the_real_frames(monkeypatch, tmp_path):
    """Real MP4: the hook label is drawn over the hook visual and the critic reads it back."""
    from critic_support import media_pass, silent_voice, small_timeline
    from test_final_critic import detect_only, finger_provider, row
    from test_story_visual_director import FINGERS, FINGERS_Q
    from test_story_visual_integration import generate as planner_generate

    silent_voice(monkeypatch)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(finger_pipeline_candidates()))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge({"hook_a": {"complementarity": 10}}))
    from test_story_visual_integration import fact as planner_fact
    from test_story_visual_integration import visual as planner_visual

    contrast = (planner_fact("Die Falten entstehen nicht, weil die Haut Wasser aufsaugt, sondern weil sich Blutgefäße zusammenziehen."), "explanation",
                planner_visual("wrinkled fingertip skin close-up", ["wrinkled fingertip skin"], ["shared"]))
    state = small_timeline(planner_generate(monkeypatch, tmp_path, FINGERS_Q, [FINGERS[0], contrast, *FINGERS[1:]], planner_target=""))
    plan = state["script"]["triple_hook"]
    assert plan["hook_id"] == "hook_a" and state["script"]["blocks"][0]["text"] == plan["verbal_hook"]
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    opening = state["scenes"][0]
    assert opening["visual_director"]["hook"]["visual_from_hook"]
    assert opening["media"]["provider_id"] == "answer"  # the hook's own query found the wrinkled fingertips

    review = detect_only(state, tmp_path, provider)
    drawn = [item for item in state["render"]["layout"][0]["overlays"] if item.get("kind") == "label"]
    assert drawn and drawn[0]["bbox"]
    hook = review["hook"]
    assert hook["hook_id"] == "hook_a" and hook["planned"]["on_screen_hook"] == "Dein Nervensystem steckt dahinter"
    assert hook["rendered"]["overlay_text"] == ["Dein Nervensystem steckt dahinter"]
    assert hook["rendered"]["spoken_opening"] == plan["verbal_hook"]
    assert row(review, opening["id"])["dimensions"]["hook_alignment"]["rating"] in {"good", "warning"}
    assert not {issue["code"] for issue in review["issues"]} & {"hook_overlay_duplicates_narration", "hook_overlay_missing", "dead_opening_frame", "hook_verbal_reveals_answer"}


def test_hook_paraphrasing_the_first_body_sentence_loses_but_an_identical_one_is_folded(monkeypatch):
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    first = fixture["body"][0]["text"]
    paraphrase = ai_candidate("A", "evidence_insight", first.replace("das Nervensystem", "dein Nervensystem"), "wet fingertips with wrinkles",
                              ["wrinkled wet fingertips"], action="gripping a stone", detail="wrinkles")
    plan = plan_for(fixture, [paraphrase])
    assert "body_duplication" in by_id(plan)["hook_a"]["hard_fail"]
    # Pipeline: the body after the hook never starts by restating the hook.
    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_pipeline_candidates())
    hook, body = state["script"]["blocks"][0], state["script"]["blocks"][1]
    words = set(hook["text"].casefold().split())
    assert len(words & set(body["text"].casefold().split())) / len(words) < 0.75


def test_v1_shaped_provider_answer_still_yields_a_complete_v2_plan():
    fixture = story(FINGERS_Q, [dict(item) for item in FINGER_FACTS])
    legacy = AIHookGenerationResult(
        [{"strategy": "counterintuitive_insight", "text": "Nicht das Wasser macht die Falten, sondern deine Nerven."}], "counterintuitive_insight", "connected",
        triple_hook={"visual_hook": {"visual_goal": "wrinkled wet fingertips close-up", "subjects_to_show": ["wrinkled wet fingertips"],
                                     "media_queries": ["wrinkled wet fingertips"], "contrast": "smooth palm next to wrinkled fingertips"},
                     "on_screen_text_hook": "Ein Schutz beim Greifen?"},
    )
    baseline = HookCandidate("counterintuitive_insight", "Nicht das Wasser macht die Falten, sondern deine Nerven.", 40.0, "fixture")
    plan = triple_hook.plan_triple_hook(
        intent=fixture["intent"], facts=fixture["facts"], story_arc=fixture["arc"], payoff_plan=fixture["payoff"],
        format_plan=fixture["format"], novelty_plan=fixture["novelty"], body_blocks=fixture["body"], baseline=baseline,
        generation=legacy, judge=Judge(), settings=SETTINGS,
    )
    assert plan["version"] == 2 and plan["status"] == "connected" and plan["verbal_hook"] == baseline.text
    assert plan["visual_hook"]["media_queries"] == ["wrinkled wet fingertips"] and plan["on_screen_text_hook"] == "Ein Schutz beim Greifen?"


def test_retimed_opening_keeps_the_hook_visual_in_every_hook_scene(monkeypatch):
    from clipforge.pipeline import _build_scenes

    state = pipeline_state(monkeypatch, FINGERS_Q, [dict(item) for item in FINGER_FACTS], finger_pipeline_candidates())
    blocks = state["script"]["blocks"]
    # A much longer measured narration splits the hook block into several scenes.
    scenes = _build_scenes(blocks, 120.0, state["scenes"], "fast")
    opening = [scene for scene in scenes if scene["block_id"] == blocks[0]["id"]]
    assert len(opening) >= 2
    assert all(scene["visual_intent"].get("source") == triple_hook.SOURCE for scene in opening)
