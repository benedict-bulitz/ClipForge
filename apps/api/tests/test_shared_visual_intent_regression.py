"""Real-run mechanisms, with generic facts, mocked providers and no paid calls."""
import copy
import hashlib
from dataclasses import replace

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, project, settings_for
from test_visual_director import FakeGenerator
from test_visual_director import Verifier as ImageVerifier

from clipforge import media, overlay_copy, visual_director, visual_translation
from clipforge.visual_rights import MediaRights


def provisional_state():
    text = "Fine pale particles drift through the lunar environment."
    scene = {
        "id": "particles", "block_id": "particles", "narration": text,
        "visual_goal": text, "preferred_media": "photo", "start": 0, "end": 4,
        "visual_intent": {"source": "narration_fallback", "visual_goal": text,
                          "objects": ["Fine", "pale", "particles"], "media_queries": [text]},
    }
    state = project(scene, intent={"topic": "lunar environment", "visual_subject": "lunar environment"})
    state["script"] = {"blocks": [{"id": "particles", "text": text, "fact_ids": ["dust"]}]}
    return state


def portrait():
    return cand("portrait", "fine pale", "Portrait wearing fine pale silk clothing", kind="photo", provider="wikimedia")


@pytest.mark.parametrize("query", ["fine pale", "fine pale particles drift lunar environment"])
def test_two_incidental_words_are_not_an_independent_metadata_pass(query):
    state = provisional_state()
    scene = state["scenes"][0]
    candidate = replace(portrait(), query=query)
    scene["search_queries"] = [query]
    relevance = media.media_relevance(candidate, scene, state)
    relevance["visual"] = {"status": "unavailable_preview", "scene_score": None}
    assert {"fine", "pale"} <= set(relevance["scene_matches"])
    assert relevance["provisional_intent"]
    assert not media.real_media_quality_gate(candidate, relevance)[0]
    assert not media.destination_asset_allowed(media.candidate_evidence(candidate), scene, state, reuse=True)


def translation():
    return {"main_subject": "Lunar dust particles", "visible_state_or_action": "floating above rocky terrain",
            "setting": "Moon surface", "details": ["gray regolith illuminated by sunlight"]}


def cache_translation(state):
    statement = visual_director.full_statement(state["scenes"][0], state)
    key = hashlib.sha256(statement.encode()).hexdigest()[:16]
    state["visual_director"] = {"visual_translations": {key: translation()}}


def test_cached_translation_drives_queries_and_generation_without_extra_call(tmp_path, monkeypatch):
    state = provisional_state()
    cache_translation(state)
    monkeypatch.setattr(visual_translation, "translate_statement", lambda *a, **kw: pytest.fail("already translated"))
    scene = state["scenes"][0]
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert scene["visual_intent"]["source"] == "fact_translation"
    plan = media.build_visual_query_plan(scene, state)
    assert len(plan["queries"]) <= 3
    assert "lunar dust" in plan["queries"][0]
    strategy = visual_director.plan_scene_strategy(scene, state, plan)
    prompt = visual_director.build_generation_prompt(scene, state, strategy, plan, settings=settings_for(tmp_path))
    assert "Lunar dust particles" in prompt["prompt"]
    assert media.media_relevance(portrait(), scene, state)["confidence"] == "rejected"


def test_translation_precedes_real_search_and_cache_admission(tmp_path, monkeypatch):
    state = provisional_state()
    scene = state["scenes"][0]
    cache_translation(state)
    cached = tmp_path / "legacy.jpg"
    cached.write_bytes(b"old irrelevant cache")
    scene.update(media={**media.candidate_evidence(portrait()), "cache_path": cached.name}, asset_status="photo_ready")
    copy_state = copy.deepcopy(state)
    visual_director.resolve_acquisition_intent(copy_state["scenes"][0], copy_state, settings_for(tmp_path))
    query = media.build_visual_query_plan(copy_state["scenes"][0], copy_state)["queries"][0]
    valid = cand("physical", query, "Lunar dust particles floating above rocky terrain on the Moon surface", kind="photo")
    provider = Provider(photos={query: [valid]})
    media.prepare_project_media(state, "project", settings_for(tmp_path), client=provider, fallback_client=Commons(),
                                extra_clients=[], visual_verifier=Verifier(), image_generator=FakeGenerator())
    assert scene["media"]["provider_id"] == "physical"
    assert scene["visual_intent"]["source"] == "fact_translation"
    assert scene["media_search"]["logical_queries_executed"] <= 3


def test_translation_once_for_split_statement_and_same_intent(tmp_path, monkeypatch):
    state = provisional_state()
    other = copy.deepcopy(state["scenes"][0])
    other.update(id="second", narration="through the lunar environment.")
    state["scenes"].append(other)
    calls = []
    def translate(statement, **kwargs):
        calls.append(statement)
        return translation()
    monkeypatch.setattr(visual_translation, "translate_statement", translate)
    for scene in state["scenes"]:
        visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert len(calls) == 1
    assert state["scenes"][0]["visual_intent"] == other["visual_intent"]


@pytest.mark.parametrize("edit", ["authored", "legacy_authored", "locked", "manual_selection", "manual_edit"])
def test_authored_and_user_owned_intent_is_unchanged(tmp_path, monkeypatch, edit):
    state = provisional_state()
    scene = state["scenes"][0]
    if edit == "authored":
        scene["visual_intent"].update(source="triple_hook_v2", actions=["floating"])
    elif edit == "legacy_authored":
        scene["visual_intent"].pop("source")
    elif edit == "manual_selection":
        scene["media"] = {"manually_selected": True}
    elif edit == "locked":
        scene["user_locked_visual"] = True
    else:
        scene["edit_instruction"] = "Show the surface"
    before = copy.deepcopy(scene)
    monkeypatch.setattr(visual_translation, "translate_statement", lambda *a, **kw: pytest.fail("not provisional"))
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert scene == before


def test_missing_translation_does_not_turn_words_into_permission(tmp_path, monkeypatch):
    state = provisional_state()
    monkeypatch.setattr(visual_translation, "translate_statement", lambda *a, **kw: None)
    scene = state["scenes"][0]
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    assert scene["visual_intent"]["source"] == "narration_fallback"
    assert not media.real_media_quality_gate(portrait(), media.media_relevance(portrait(), scene, state))[0]


def relation_state():
    state = provisional_state()
    scene = state["scenes"][0]
    scene.update(id="effect", block_id="effect", narration="Production slows down.", story_role="evidence", story_stage="open", story_unit_ids=["fact"])
    state["intent"] = {"topic": "industrial production"}
    state["script"]["blocks"] = [
        {"id": "cause", "text": "Workers leave the factory.", "fact_ids": ["fact"]},
        {"id": "effect", "text": "Production slows down.", "fact_ids": ["fact"]},
    ]
    scene["visual_intent"] = {"source": "narration_fallback", "visual_goal": "Production slows down",
                              "objects": ["Production", "slows", "down"], "media_queries": ["Production slows down"]}
    scene["visual_goal"] = "Production slows down"
    return state


def test_evidence_role_does_not_hide_valid_linked_fact_relation():
    state = relation_state()
    scene = state["scenes"][0]
    strategy = visual_director.plan_scene_strategy(scene, state, media.build_visual_query_plan(scene, state))
    assert strategy["visual_role"] == "evidence"
    assert strategy["fallback_chain"][-1] == "simple_graphic"
    assert strategy["graphic"]["steps"] == ["Workers leave the factory", "Production slows down"]
    assert not overlay_copy.assess_overlay(strategy["overlay_spec"], source=overlay_copy.fact_statement(scene, state))


@pytest.mark.parametrize("legacy", [False, True])
def test_budget_exhaustion_finishes_linked_relation_graphic(tmp_path, legacy):
    state = relation_state()
    scene = state["scenes"][0]
    visual_director.generation_policy(state, settings_for(tmp_path))
    state["visual_director"]["generations"] = [
        {"billed": True, "trigger": "auto", "status": "accepted", "scene_id": f"previous-{i}"}
        for i in range(3)
    ]
    if legacy:
        scene["visual_director"] = {"reason": "concrete_subject_real_media", "planned_type": "stock_video",
                                    "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual"]}
    generator = FakeGenerator()
    media.complete_project_visuals(state, "project", settings_for(tmp_path), image_generator=generator, visual_verifier=ImageVerifier())
    assert not generator.prompts
    assert scene["asset_status"] == "graphic_ready"
    assert scene["media"]["source"] == "simple_graphic"
    assert media.cached_scene_asset_path(scene["media"], settings_for(tmp_path))
    assert "generated_card_count" not in state["assets"]


def test_relation_cannot_join_unrelated_facts_or_override_explicit_chain(tmp_path):
    state = relation_state()
    state["script"]["blocks"][0]["fact_ids"] = ["other"]
    scene = state["scenes"][0]
    assert overlay_copy.linked_fact_overlay(scene, state) is None
    scene["visual_director"] = {"fallback_chain": ["real_media", "reuse_previous_visual"]}
    media.complete_project_visuals(state, "project", settings_for(tmp_path), image_generator=FakeGenerator(), visual_verifier=ImageVerifier())
    assert scene["asset_status"] == "real_media_unavailable"


def test_rights_still_fail_closed_after_translation(tmp_path):
    state = provisional_state()
    cache_translation(state)
    scene = state["scenes"][0]
    visual_director.resolve_acquisition_intent(scene, state, settings_for(tmp_path))
    candidate = replace(cand("dust", "lunar dust", "Lunar dust particles floating above rocky terrain", kind="photo"), rights=MediaRights())
    assert not media.real_media_quality_gate(candidate, media.media_relevance(candidate, scene, state))[0]


def test_concise_authored_intent_is_not_inferred_to_be_an_emergency_fragment():
    state = provisional_state()
    scene = state["scenes"][0]
    scene.update(narration="Hier sehen wir das Objekt.", visual_goal="lighthouse")
    scene["visual_intent"] = {"visual_goal": "lighthouse", "objects": ["lighthouse"], "media_queries": ["lighthouse"]}
    candidate = cand("tower", "lighthouse", "Lighthouse above the cliffs", kind="photo")
    relevance = media.media_relevance(candidate, scene, state)
    assert not relevance["provisional_intent"]
    assert media.real_media_quality_gate(candidate, relevance)[0]


def test_incidental_metadata_with_unavailable_preview_reaches_existing_ai_fallback(tmp_path):
    from test_visual_director import Provider as ImageProvider

    from clipforge.visual_verifier import VisualVerification

    class UnavailablePreview(ImageVerifier):
        def verify_candidate(self, candidate, texts):
            return VisualVerification(None, "unavailable_preview")

    state = provisional_state()
    generator = FakeGenerator()
    media.prepare_project_media(
        state, "project", settings_for(tmp_path), client=ImageProvider(),
        fallback_client=ImageProvider(photos=[portrait()]), extra_clients=[],
        visual_verifier=UnavailablePreview(), image_generator=generator,
    )
    scene = state["scenes"][0]
    assert scene["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1
    assert scene["media_search"]["logical_queries_executed"] <= 3
    assert "generated_card_count" not in state["assets"]
