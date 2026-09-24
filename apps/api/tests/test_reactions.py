from __future__ import annotations

from clipforge.pacing import analyze_pacing
from clipforge.reactions import plan_viewer_reactions, reaction_arc, reaction_quality_issues


def scene(identifier: str, block_id: str, narration: str) -> dict:
    return {
        "id": identifier,
        "block_id": block_id,
        "narration": narration,
        "start": 0,
        "end": 3,
        "visual_goal": narration,
        "visual_intent": {"visual_goal": narration},
        "search_queries": [],
    }


def project(*, payoff_type: str = "comparison_winner", protected: bool = True) -> dict:
    blocks = [
        {"id": "hook", "role": "hook", "text": "Most people would bet on Egypt."},
        {"id": "setup", "role": "support", "text": "Egypt is famous for pyramids, which frames the comparison."},
        {"id": "payoff", "role": "payoff", "text": "Sudan has more pyramids than Egypt."},
    ]
    return {
        "intent": {"content_type": "factual_explainer", "question": "Which country has more pyramids?"},
        "payoff_plan": {
            "payoff": "Sudan has more pyramids than Egypt.",
            "payoff_type": payoff_type,
            "reveal_policy": "after_supporting_information" if protected else "immediate_context_allowed",
            "hook_must_not_reveal": "Sudan" if protected else "",
            "desired_viewer_reaction": "surprise",
        },
        "script": {
            "blocks": blocks,
            "text": " ".join(block["text"] for block in blocks),
            "triple_hook": {
                "verbal_hook": "Most people would bet on Egypt.",
                "on_screen_text_hook": "WHO HAS MORE?",
                "visual_hook": {"must_not_show": ["Sudan"]},
            },
        },
        "scenes": [
            scene("scene_hook", "hook", blocks[0]["text"]),
            scene("scene_setup", "setup", blocks[1]["text"]),
            scene("scene_payoff", "payoff", blocks[2]["text"]),
        ],
        "captions": {"items": []},
        "duration": {"speaking_rate_wpm": 150},
        "timeline": {"scene_ids": ["scene_hook", "scene_setup", "scene_payoff"]},
        "facts": [{"claim": "Sudan has more pyramids than Egypt."}],
    }


def test_comparison_arc_is_curiosity_anticipation_surprise_and_satisfaction() -> None:
    state = project()
    analyze_pacing(state)
    plan = plan_viewer_reactions(state)
    assert plan["hook_reaction"] == "curiosity"
    assert plan["buildup_reaction"] == "anticipation"
    assert plan["payoff_reaction"] == "surprise"
    assert plan["ending_reaction"] == "satisfaction"


def test_explanation_prefers_insight_over_forced_surprise() -> None:
    arc = reaction_arc(
        {"content_type": "factual_explainer"},
        {"payoff_type": "explanation", "desired_viewer_reaction": "surprise"},
    )
    assert arc["primary_reaction"] == "insight"
    assert arc["payoff_reaction"] == "insight"


def test_mundane_content_is_not_escalated_to_shock() -> None:
    arc = reaction_arc(
        {"content_type": "factual_explainer"},
        {"payoff_type": "answer", "desired_viewer_reaction": "shock"},
    )
    assert "shock" not in arc.values()
    assert arc["primary_reaction"] == "insight"


def test_hook_and_visual_guidance_integrate_without_leaking_protected_payoff() -> None:
    state = project()
    plan_viewer_reactions(state)
    triple = state["script"]["triple_hook"]
    assert triple["intended_reaction"] == "curiosity"
    assert "protected answer" in triple["visual_hook"]["reaction_direction"]
    assert triple["visual_hook"]["must_not_show"] == ["Sudan"]
    assert "protected answer" in state["scenes"][0]["visual_intent"]["reaction_direction"]


def test_scene_reactions_reuse_existing_scenes_without_filler_or_forced_switches() -> None:
    state = project()
    original_scene_ids = [scene["id"] for scene in state["scenes"]]
    original_text = state["script"]["text"]
    plan = plan_viewer_reactions(state)
    assert [item["scene_id"] for item in plan["scene_reactions"]] == original_scene_ids
    assert len(plan["scene_reactions"]) == len(state["scenes"])
    assert state["script"]["text"] == original_text

    # Consecutive setup scenes are allowed to share anticipation when both earn it.
    state["scenes"].insert(2, scene("scene_setup_two", "setup", "The locations make the result easier to understand."))
    same_reaction = plan_viewer_reactions(state)
    setup_reactions = [item["intended_reaction"] for item in same_reaction["scene_reactions"] if item["scene_id"].startswith("scene_setup")]
    assert setup_reactions == ["anticipation", "anticipation"]


def test_unsupported_emotional_framing_and_fake_clickbait_are_flagged() -> None:
    state = project()
    plan_viewer_reactions(state)
    state["facts"] = [{"claim": "The comparison is documented."}]
    state["script"]["triple_hook"].update(
        {"verbal_hook": "This is shocking!", "intended_reaction": "shock"}
    )
    issues = reaction_quality_issues(state)
    assert "unsupported_emotional_framing" in issues
    assert "fake_clickbait_reaction" in issues


def test_payoff_mismatch_visual_conflict_and_fallback_are_detected(monkeypatch) -> None:
    state = project(payoff_type="explanation", protected=False)
    plan_viewer_reactions(state)
    state["reaction_plan"]["payoff_reaction"] = "surprise"
    state["reaction_plan"]["scene_reactions"][0]["pacing_context"]["recommendation"] = "REPLACE_VISUAL"
    issues = reaction_quality_issues(state)
    assert "payoff_reaction_mismatch" in issues
    assert "visual_direction_conflicts_with_reaction" in issues

    monkeypatch.setattr(
        "clipforge.reactions.build_reaction_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    fallback = plan_viewer_reactions(state)
    assert fallback["status"] == "fallback"
    assert state["script"]["text"]


def test_hook_cannot_promise_more_reaction_than_an_explanatory_payoff_delivers() -> None:
    state = project(payoff_type="explanation", protected=False)
    plan_viewer_reactions(state)
    state["script"]["triple_hook"]["intended_reaction"] = "surprise"
    assert "hook_reaction_exceeds_payoff" in reaction_quality_issues(state)
