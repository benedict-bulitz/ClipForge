"""Fresh-base integration guards for the selectively ported visual work."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_shared_visual_intent_regression import provisional_state, settings_for, translation

from clipforge import ai, media, renderer, visual_director, visual_translation
from clipforge.visual_context import scene_story_context


def test_non_planetary_acceptance_key_keeps_legacy_shape():
    scene = {
        "id": "ordinary",
        "narration": "A person opens a refrigerator.",
        "visual_goal": "person opening refrigerator",
        "visual_intent": {"objects": ["person", "refrigerator"]},
    }
    state = {"intent": {"topic": "habits"}, "scenes": [scene]}
    evidence = {key: scene.get(key) for key in ("narration", "visual_goal", "visual_intent")}
    evidence["intent"] = state["intent"]
    evidence["story_context"] = scene_story_context(scene, state)
    legacy = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert media.scene_acceptance_key(scene, state) == legacy
    assert "required_environment" not in scene


def test_planetary_acceptance_key_tracks_applicable_environment(monkeypatch):
    scene = {"narration": "Actual planetary surface", "visual_goal": "actual surface", "visual_intent": {}}
    state = {"intent": {"topic": "planet"}, "scenes": [scene]}
    first = {"domain": "planetary", "entity": "mars", "representation": "actual", "incompatible": ["open_water"]}
    second = {**first, "entity": "moon"}
    monkeypatch.setattr(media, "environment_requirement", lambda *_: first)
    mars_key = media.scene_acceptance_key(scene, state)
    monkeypatch.setattr(media, "environment_requirement", lambda *_: second)
    assert media.scene_acceptance_key(scene, state) != mars_key


def test_hook_owned_fallback_is_never_translated(tmp_path, monkeypatch):
    state = provisional_state()
    scene = state["scenes"][0]
    state["script"]["blocks"][0]["role"] = "hook"
    before = copy.deepcopy(scene)
    monkeypatch.setattr(
        visual_translation,
        "translate_statement",
        lambda *args, **kwargs: pytest.fail("Triple Hook owns the opening visual"),
    )
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert scene == before


def test_body_fallback_translation_is_cached_and_bounded(tmp_path, monkeypatch):
    state = provisional_state()
    scene = state["scenes"][0]
    calls = []

    def translate(*args, **kwargs):
        calls.append((args, kwargs))
        return translation()

    monkeypatch.setattr(visual_translation, "translate_statement", translate)
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    converted = copy.deepcopy(scene)
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert scene == converted
    assert len(calls) == 1
    assert scene["visual_intent"]["source"] == "fact_translation"
    assert len(scene["search_queries"]) <= 3


def test_caption_words_do_not_trigger_generic_letterboxing():
    scene = {"media": {"title": "Newspaper map document manuscript chart"}}
    plan = renderer.still_motion_plan(scene, 0, "dynamic", 0.5, 100)
    assert plan["type"] == "push_in"
    assert "framing" not in plan


def test_single_frame_still_is_static():
    plan = renderer.still_motion_plan({}, 0, "dynamic", 0.5, 1)
    assert plan["type"] == "static"
    assert plan["max_zoom"] == 1.0


def test_hook_v3_and_progression_instructions_coexist():
    assert "hook_playbook is the canonical ClipForge hook manifest" in ai.DIRECTOR_INSTRUCTIONS
    assert "what should the viewer see NOW" in ai.DIRECTOR_INSTRUCTIONS
    assert "relevant repetition is better than unrelated variety" in ai.DIRECTOR_INSTRUCTIONS


def test_acquisition_budget_is_restored_before_cache_admission(tmp_path, monkeypatch):
    state = provisional_state()
    scene = state["scenes"][0]
    scene["visual_intent"]["source"] = "authored"
    scene.update(
        media={"identity": "pexels:photo:cached", "cache_path": "cached.jpg"},
        asset_status="photo_ready",
        media_search={
            "acquisition_budget": {
                "limits": {"search_requests": 9, "verifications": 7, "downloads": 5},
                "used": {"search_requests": 4, "verifications": 3, "downloads": 2},
            }
        },
    )
    observed = {}

    def allowed(_asset, _scene, _state, **kwargs):
        budget = kwargs["acquisition_budget"]
        observed.update(
            search_requests=budget.search_requests,
            verifications=budget.verifications,
            downloads=budget.downloads,
        )
        return True

    monkeypatch.setattr(media, "cached_scene_asset_path", lambda *_: Path("cached.jpg"))
    monkeypatch.setattr(media, "destination_asset_allowed", allowed)
    monkeypatch.setattr(media, "refresh_rights_acceptance", lambda value: value)
    media.complete_project_visuals(
        state,
        "project",
        settings_for(tmp_path),
        image_generator=object(),
        visual_verifier=object(),
    )
    assert observed == {"search_requests": 4, "verifications": 3, "downloads": 2}


def test_broad_linked_fact_graphics_were_not_ported():
    assert "linked_fact_overlay" not in visual_director.__dict__
