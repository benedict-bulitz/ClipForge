from __future__ import annotations

from copy import deepcopy

from clipforge.pacing import analyze_pacing
from clipforge.pipeline import _build_scenes
from clipforge.reactions import plan_viewer_reactions
from clipforge.review import pre_render_quality_gate


def integrated_state() -> dict:
    blocks = [
        {"id": "b1", "role": "hook", "text": "Most people would bet on Egypt."},
        {"id": "b2", "role": "support", "text": "Egypt is famous for pyramids, which frames the comparison."},
        {"id": "b3", "role": "payoff", "text": "Sudan has more pyramids than Egypt."},
    ]
    scenes = [
        {"id": "s1", "block_id": "b1", "narration": blocks[0]["text"], "start": 0, "end": 2, "visual_goal": "pyramid comparison setup", "visual_intent": {"visual_goal": "pyramid comparison setup"}, "search_queries": ["Egypt pyramids", "Sudan pyramids"]},
        {"id": "s2", "block_id": "b2", "narration": blocks[1]["text"], "start": 2, "end": 5, "visual_goal": "pyramids along the Nile", "visual_intent": {"visual_goal": "pyramids along the Nile"}, "search_queries": ["Egypt pyramids"]},
        {"id": "s3", "block_id": "b3", "narration": blocks[2]["text"], "start": 5, "end": 7, "visual_goal": "resolved pyramid comparison", "visual_intent": {"visual_goal": "resolved pyramid comparison"}, "search_queries": ["Sudan pyramids"]},
    ]
    return {
        "intent": {"content_type": "factual_explainer", "question": "Which country has more pyramids?"},
        "payoff_plan": {"payoff": "Sudan has more pyramids than Egypt.", "payoff_type": "comparison_winner", "reveal_policy": "after_supporting_information", "hook_must_not_reveal": "Sudan", "desired_viewer_reaction": "surprise"},
        "script": {"blocks": blocks, "selected_hook": blocks[0]["text"], "text": " ".join(block["text"] for block in blocks), "triple_hook": {"verbal_hook": blocks[0]["text"], "on_screen_text_hook": "WHO HAS MORE?", "visual_hook": {"visual_goal": "pyramid comparison setup", "must_not_show": ["Sudan"]}}},
        "scenes": scenes,
        "captions": {"items": []},
        "duration": {"speaking_rate_wpm": 150},
        "timeline": {"scene_ids": [scene["id"] for scene in scenes]},
        "facts": [{"claim": "Sudan has more pyramids than Egypt."}],
    }


def test_payoff_hook_scene_review_and_reaction_data_reach_one_pre_render_state() -> None:
    state = integrated_state()
    analyze_pacing(state)
    plan_viewer_reactions(state)
    gate = pre_render_quality_gate(state)
    assert state["reaction_plan"]["scene_reactions"]
    assert state["reaction_plan"]["scene_reactions"][0]["scene_id"] == "s1"
    assert state["scenes"][0]["visual_intent"]["reaction_direction"]
    assert gate["ai_calls"] == 0
    assert gate["status"] in {"passed", "passed_with_warnings"}


def test_visual_hook_payoff_leak_is_caught_before_render() -> None:
    state = integrated_state()
    state["script"]["triple_hook"]["visual_hook"]["visual_goal"] = "Sudan has more pyramids than Egypt"
    analyze_pacing(state)
    plan_viewer_reactions(state)
    gate = pre_render_quality_gate(state)
    assert "visual_hook_reveals_protected_payoff" in gate["severe_issues"]
    assert gate["status"] == "fallback"


def test_safe_pacing_fix_is_reflected_in_quality_gate_scene_input() -> None:
    state = integrated_state()
    state["scenes"].append({"id": "empty", "block_id": "none", "narration": "", "start": 7, "end": 8, "visual_intent": {}})
    state["timeline"]["scene_ids"].append("empty")
    analyze_pacing(state)
    assert [scene["id"] for scene in state["scenes"]] == ["s1", "s2", "s3"]
    gate = pre_render_quality_gate(state)
    assert gate["status"] != "fallback"
    assert not gate["severe_issues"]


def test_measured_scene_rebuild_preserves_visual_and_reaction_guidance() -> None:
    state = integrated_state()
    analyze_pacing(state)
    plan_viewer_reactions(state)
    rebuilt = _build_scenes(state["script"]["blocks"], 7.5, state["scenes"], visual_intents=None)
    assert rebuilt[0]["visual_intent"]["reaction_direction"] == state["scenes"][0]["visual_intent"]["reaction_direction"]
    assert rebuilt[0]["visual_goal"] == state["scenes"][0]["visual_goal"]


def test_old_revision_without_optional_planners_still_passes_with_fallbacks() -> None:
    old = {"script": {"blocks": [], "text": "An old project."}, "scenes": [], "facts": []}
    snapshot = deepcopy(old)
    gate = pre_render_quality_gate(old)
    assert gate["ai_calls"] == 0
    assert old["script"] == snapshot["script"]
    assert gate["status"] in {"passed", "passed_with_warnings", "fallback"}
