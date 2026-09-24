from __future__ import annotations

from clipforge.config import Settings
from clipforge.pacing import analyze_pacing, analyze_scene_quality, apply_safe_pacing_fixes
from clipforge.pipeline import build_initial_state
from clipforge.schemas import AdvancedOptions


def state_for(scenes: list[dict], roles: list[str] | None = None) -> dict:
    roles = roles or ["explanation"] * len(scenes)
    blocks = [
        {"id": scene["block_id"], "role": role, "text": scene["narration"]}
        for scene, role in zip(scenes, roles, strict=True)
    ]
    return {
        "script": {"blocks": blocks, "text": " ".join(block["text"] for block in blocks)},
        "scenes": scenes,
        "captions": {"items": []},
        "duration": {"speaking_rate_wpm": 150},
        "timeline": {"scene_ids": [scene["id"] for scene in scenes]},
    }


def scene(identifier: str, narration: str, start: float, end: float, visual: str | None = None) -> dict:
    return {
        "id": identifier,
        "block_id": f"block_{identifier}",
        "narration": narration,
        "start": start,
        "end": end,
        "visual_goal": visual or narration,
        "visual_intent": {"visual_goal": visual or narration, "objects": [], "media_queries": []},
        "search_queries": [],
    }


def assessment(analysis: dict, scene_id: str) -> dict:
    return next(item for item in analysis["scene_assessments"] if item["scene_id"] == scene_id)


def test_duplicated_scene_is_flagged_but_new_information_is_kept() -> None:
    repeated = "Blue light scatters more strongly in the atmosphere."
    project = state_for([
        scene("one", repeated, 0, 3),
        scene("two", repeated, 3, 6),
        scene("three", "That is why the sky looks blue from the ground.", 6, 9),
    ])
    analysis = analyze_scene_quality(project)
    assert assessment(analysis, "two")["recommendation"] == "MERGE_WITH_PREVIOUS"
    assert assessment(analysis, "three")["information_gain"] > 0.4
    assert assessment(analysis, "three")["recommendation"] == "KEEP"


def test_long_information_dense_scene_is_not_penalized_by_universal_duration() -> None:
    narration = (
        "Cabin pressure stays higher than outside air at altitude, so the tiny hole balances pressure "
        "between the window panes and spreads the load safely."
    )
    project = state_for([scene("dense", narration, 0, 11)])
    result = assessment(analyze_scene_quality(project), "dense")
    assert result["information_gain"] > 0.8
    assert result["recommendation"] == "KEEP"
    assert result["pacing_quality"] == "earned"


def test_short_chain_that_cuts_faster_than_its_meaning_is_flagged() -> None:
    project = state_for([
        scene("one", "Pressure changes between the panes during flight.", 0, 0.7),
        scene("two", "The hole lets air move slowly between them.", 0.7, 1.4),
        scene("three", "That reduces strain on the window structure.", 1.4, 2.1),
    ])
    result = assessment(analyze_scene_quality(project), "three")
    assert result["pacing_quality"] == "compressed"
    assert result["recommendation"] == "MERGE_WITH_NEXT"


def test_post_payoff_generic_filler_is_detected_while_setup_is_preserved() -> None:
    project = state_for(
        [
            scene("setup", "Egypt is famous for pyramids, which makes the comparison surprising.", 0, 4),
            scene("payoff", "Sudan has more pyramids than Egypt.", 4, 6),
            scene("outro", "Thanks for watching.", 6, 8),
        ],
        ["support", "payoff", "detail"],
    )
    analysis = analyze_scene_quality(project)
    assert assessment(analysis, "setup")["recommendation"] == "KEEP"
    assert assessment(analysis, "outro")["recommendation"] == "SHORTEN_POST_PAYOFF"


def test_hook_is_protected_even_when_its_words_overlap_following_setup() -> None:
    project = state_for(
        [
            scene("hook", "Most people would bet on Egypt.", 0, 2),
            scene("setup", "Egypt is famous for pyramids, but the comparison has another side.", 2, 5),
        ],
        ["hook", "support"],
    )
    assert assessment(analyze_scene_quality(project), "hook")["recommendation"] == "KEEP"


def test_visual_mismatch_requests_replacement_and_information_gain_is_internal() -> None:
    project = state_for([
        scene("one", "The hole balances air pressure between window panes.", 0, 4, "tropical beach sunset"),
        scene("two", "It also spreads the pressure load across the panes.", 4, 8, "aircraft window panes pressure"),
    ])
    analysis = analyze_scene_quality(project)
    assert assessment(analysis, "one")["recommendation"] == "REPLACE_VISUAL"
    assert assessment(analysis, "two")["information_gain"] > 0.4


def test_safe_fix_removes_only_empty_scene_artifacts_without_changing_script() -> None:
    project = state_for([
        scene("kept", "Air pressure changes during flight.", 0, 3),
        scene("empty", "", 3, 4),
    ])
    original_script = project["script"]["text"]
    analysis = analyze_scene_quality(project)
    apply_safe_pacing_fixes(project, analysis)
    assert [item["id"] for item in project["scenes"]] == ["kept"]
    assert project["script"]["text"] == original_script
    assert analysis["applied_safe_fixes"] == [
        {"action": "REMOVE", "scene_id": "empty", "reason": "Removed an empty scene artifact."}
    ]


def test_analyzer_failure_is_persisted_as_a_safe_fallback(monkeypatch) -> None:
    project = state_for([scene("one", "Useful information remains available.", 0, 3)])
    monkeypatch.setattr(
        "clipforge.pacing.analyze_scene_quality",
        lambda _state: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    analysis = analyze_pacing(project)
    assert analysis["status"] == "fallback"
    assert project["scenes"][0]["narration"] == "Useful information remains available."


def test_generation_persists_scene_quality_analysis_without_altering_the_script() -> None:
    project = build_initial_state(
        "Tell a story about an astronaut on the moon",
        AdvancedOptions(language="en", research="off"),
        Settings(clipforge_ai_mode="local"),
    )
    assert project["pacing_analysis"]["status"] == "ready"
    assert project["pacing_analysis"]["scene_assessments"]
    assert project["script"]["text"]
