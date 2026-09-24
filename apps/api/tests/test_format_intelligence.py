from __future__ import annotations

from clipforge.config import Settings
from clipforge.format_intelligence import format_quality_issues, plan_format
from clipforge.pipeline import _build_scenes, build_initial_state
from clipforge.schemas import AdvancedOptions


def intent(question: str, content_type: str = "factual_explainer") -> dict:
    return {
        "question": question,
        "topic": question,
        "language": "en",
        "tone": "clear",
        "content_type": content_type,
    }


def test_comparison_questions_select_comparison_reveal_guidance() -> None:
    for question in (
        "Which country has more pyramids, Egypt or Sudan?",
        "Which has more: trees on Earth or stars in the Milky Way?",
    ):
        plan = plan_format(intent(question))
        assert plan["selected_format"] == "comparison"
        assert plan["payoff_structure"] == "contrast_then_result"
        assert "fixed" not in plan["pacing_guidance"]


def test_explanation_and_ranking_are_selected_from_content() -> None:
    assert plan_format(intent("Why do airplanes leave white trails?"))["selected_format"] == "explanation"
    assert plan_format(intent("Top 5 fastest animals"))["selected_format"] == "ranking"
    assert plan_format(intent("What causes tides?"))["selected_format"] != "quiz"


def test_format_reaches_payoff_reaction_pacing_and_visual_planning() -> None:
    state = build_initial_state(
        "Which country has more pyramids, Egypt or Sudan?",
        AdvancedOptions(language="en", research="off"),
        Settings(clipforge_ai_mode="local"),
    )
    assert state["format_plan"]["selected_format"] == "comparison"
    assert state["payoff_plan"]["format"] == "comparison"
    assert state["reaction_plan"]["format"] == "comparison"
    assert state["pacing_analysis"]["format"] == "comparison"
    assert state["scenes"][0]["visual_intent"]["format"] == "comparison"
    assert state["scenes"][0]["visual_intent"]["format_guidance"]


def test_format_guidance_survives_scene_rebuild() -> None:
    plan = plan_format(intent("Which country has more pyramids, Egypt or Sudan?"))
    blocks = [{"id": "voice_block_01", "role": "hook", "text": "Which country has more pyramids?"}]
    rebuilt = _build_scenes(blocks, 3.0, format_plan=plan)
    assert rebuilt[0]["visual_intent"]["format"] == "comparison"
    assert rebuilt[0]["visual_intent"]["format_guidance"] == plan["visual_structure"]


def test_invalid_ranking_basis_is_flagged_without_blocking() -> None:
    state = {
        "prompt": "A list of animals",
        "intent": intent("A list of animals"),
        "facts": [],
        "format_plan": plan_format(intent("Top 5 animals")),
        "payoff_plan": {},
        "script": {"blocks": []},
    }
    assert "ranking_without_ordering_basis" in format_quality_issues(state)


def test_format_planning_failure_uses_generic_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "clipforge.format_intelligence.select_format",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("planner failure")),
    )
    plan = plan_format(intent("Why is the sky blue?"))
    assert plan["status"] == "fallback"
    assert plan["selected_format"] == "explanation"
