from __future__ import annotations

import json
from types import SimpleNamespace

from clipforge.ai import (
    AIHookCandidate,
    AIHookGenerationResponse,
    AIVisualHook,
    generate_hook_candidates_with_openai,
)
from clipforge.config import Settings
from clipforge.models import Project
from clipforge.payoff import (
    build_payoff_plan,
    fallback_triple_hook,
    normalise_triple_hook,
    payoff_quality_issues,
    trim_post_payoff_fluff,
)
from clipforge.pipeline import build_initial_state
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.services import create_project
from clipforge.verbal_hook import assess_verbal, hook_context


def comparison_intent() -> dict:
    return {
        "question": "Which country has more pyramids, Egypt or Sudan?",
        "topic": "pyramids in Egypt and Sudan",
        "language": "en",
        "tone": "fast_documentary",
        "content_type": "factual_explainer",
    }


def test_comparison_plan_protects_winner_and_uses_content_based_reveal() -> None:
    blocks = [
        {"role": "support", "text": "Egypt has famous pyramid fields along the Nile."},
        {"role": "payoff", "text": "Sudan has more pyramids than Egypt."},
    ]
    plan = build_payoff_plan(comparison_intent(), blocks)
    assert plan["payoff_type"] == "comparison_winner"
    assert plan["hook_must_not_reveal"] == "Sudan"
    assert plan["reveal_policy"] == "after_supporting_information"
    assert "second" not in json.dumps(plan).casefold()


def test_verbal_authority_rejects_a_candidate_that_spoils_protected_payoff() -> None:
    body = [
        {"role": "support", "text": "Egypt has famous pyramids."},
        {"role": "payoff", "text": "Sudan has more pyramids than Egypt."},
    ]
    plan = build_payoff_plan(comparison_intent(), body)
    context = hook_context(comparison_intent(), [], story_arc=None, payoff_plan=plan, format_plan={"selected_format": "comparison"}, novelty_plan=None, body_blocks=body)
    spoiler = assess_verbal("Sudan has more pyramids than Egypt.", "evidence_insight", context)
    assert "verbal_names_protected_answer" in spoiler["hard_fail"]
    open_question = assess_verbal("Egypt or Sudan: who has more pyramids?", "curiosity_gap", context)
    assert not any("protected" in code for code in open_question["hard_fail"])


def test_triple_hook_is_complementary_and_visual_constraint_is_preserved() -> None:
    plan = build_payoff_plan(
        comparison_intent(), [{"role": "payoff", "text": "Sudan has more pyramids than Egypt."}]
    )
    fallback = fallback_triple_hook(
        comparison_intent(), plan, "Most people would bet on Egypt.", "expectation_reversal"
    )
    triple = normalise_triple_hook(
        {
            "on_screen_text_hook": "WHO HAS MORE?",
            "visual_hook": {
                "visual_goal": "pyramid skylines in visual contrast",
                "subjects_to_show": ["pyramids", "two landscapes"],
                "contrast": "Egypt versus Sudan without totals",
                "motion_or_change": "quick side-by-side reveal setup",
                "visual_priority": "make both places recognizable",
                "must_not_show": ["Sudan has more pyramids than Egypt"],
                "media_queries": ["Egypt pyramids", "Sudan pyramids"],
            },
        },
        fallback,
        plan,
    )
    assert triple["verbal_hook"] != triple["on_screen_text_hook"]
    assert triple["visual_hook"]["must_not_show"]
    assert any(item.rstrip(".") == "Sudan" for item in triple["visual_hook"]["must_not_show"])


def test_explanatory_topic_can_reveal_context_immediately() -> None:
    intent = {
        "question": "Why is the sky blue?",
        "topic": "why the sky is blue",
        "language": "en",
        "tone": "clear",
        "content_type": "factual_explainer",
    }
    plan = build_payoff_plan(intent, [{"role": "answer", "text": "Blue light scatters more strongly."}])
    assert plan["reveal_policy"] == "immediate_context_allowed"
    assert not plan["hook_must_not_reveal"]


def test_generic_post_payoff_outro_is_trimmed_without_rewriting_body() -> None:
    blocks = [
        {"role": "support", "text": "The setup makes the comparison meaningful."},
        {"role": "payoff", "text": "Sudan has more pyramids than Egypt."},
        {"role": "detail", "text": "Thanks for watching."},
    ]
    trimmed, changed = trim_post_payoff_fluff(blocks)
    assert changed is True
    assert trimmed == blocks[:2]


def test_quality_review_detects_early_payoff_repetition_and_visual_spoiler() -> None:
    state = {
        "payoff_plan": {
            "hook_must_not_reveal": "Sudan",
        },
        "script": {
            "blocks": [
                {"role": "hook", "text": "Sudan has more pyramids than Egypt."},
                {"role": "support", "text": "Sudan has more pyramids than Egypt."},
            ],
            "triple_hook": {
                "verbal_hook": "Sudan has more pyramids than Egypt.",
                "on_screen_text_hook": "Sudan has more pyramids than Egypt.",
                "visual_hook": {
                    "visual_goal": "Sudan has more pyramids than Egypt",
                    "subjects_to_show": [],
                    "media_queries": [],
                },
            },
        },
    }
    issues = payoff_quality_issues(state)
    assert "protected_payoff_revealed_in_hook" in issues
    assert "hook_repeats_first_body_sentence" in issues
    assert "text_hook_duplicates_verbal_hook" in issues
    assert "visual_hook_reveals_protected_payoff" in issues


def test_openai_hook_request_receives_payoff_plan_and_returns_structured_visuals(monkeypatch) -> None:
    captured: dict = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=AIHookGenerationResponse(
                    hook_candidates=[AIHookCandidate(strategy="curiosity_gap", text="Most people pick Egypt.")],
                    selected_hook_strategy="curiosity_gap",
                    on_screen_text_hook="WHO HAS MORE?",
                    visual_hook=AIVisualHook(
                        visual_goal="two pyramid landscapes in contrast",
                        subjects_to_show=["pyramids"],
                        must_not_show=["Sudan"],
                    ),
                )
            )

    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_kwargs: SimpleNamespace(responses=Responses()))
    plan = build_payoff_plan(
        comparison_intent(), [{"role": "payoff", "text": "Sudan has more pyramids than Egypt."}]
    )
    result = generate_hook_candidates_with_openai(
        comparison_intent()["question"], comparison_intent(), [], "Useful context first.",
        Settings(openai_api_key="not-a-real-key"), plan,
    )
    assert json.loads(captured["input"])["payoff_plan"] == plan
    assert result.triple_hook and result.triple_hook["visual_hook"]


def test_payoff_planning_failure_keeps_existing_generation_path_usable(monkeypatch) -> None:
    monkeypatch.setattr(
        "clipforge.pipeline.build_payoff_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    state = build_initial_state(
        "Tell a story about an astronaut on the moon",
        AdvancedOptions(language="en", research="off"),
        Settings(clipforge_ai_mode="local"),
    )
    assert state["payoff_plan"]["status"] == "fallback"
    assert state["script"]["text"]
    assert state["script"]["triple_hook"]["status"] == "fallback"


def test_pipeline_persists_triple_hook_and_uses_visual_hook_for_opening_scene() -> None:
    state = build_initial_state(
        "Tell a story about an astronaut on the moon",
        AdvancedOptions(language="en", research="off"),
        Settings(clipforge_ai_mode="local"),
    )
    triple = state["script"]["triple_hook"]
    assert state["payoff_plan"]["curiosity_question"]
    assert triple["verbal_hook"] == (state["script"]["selected_hook"] or "")
    # V2 omits the on-screen hook rather than showing generic filler text.
    assert not triple["on_screen_text_hook"] or triple["on_screen_text_hook"] != triple["verbal_hook"]
    assert triple["version"] == 2 and triple["on_screen_hook_status"] in {"shown", "omitted"}
    assert state["scenes"][0]["visual_intent"]["visual_goal"] == triple["visual_hook"]["visual_goal"]


def test_payoff_and_triple_hook_plan_persist_in_project_revision(db) -> None:
    project = create_project(
        db,
        ProjectCreate(prompt="Tell a story about an astronaut on the moon"),
        Settings(clipforge_ai_mode="local"),
    )
    project_id = project.id
    db.expire_all()
    restored = db.get(Project, project_id)
    assert restored is not None
    state = restored.revisions[0].state
    assert state["payoff_plan"]["curiosity_question"]
    assert "triple_hook" in state["script"]
