"""Replay the real topical-label/roadside false positive without topic/asset IDs."""

import copy
from dataclasses import replace

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, project, settings_for
from test_visual_director import FakeGenerator
from test_visual_director import Verifier as FallbackVerifier
from test_visual_sources_v2 import routed

from clipforge import media
from clipforge.visual_providers import AcquisitionBudget, ProviderAdapter, ProviderRegistry
from clipforge.visual_rights import MediaRights
from clipforge.visual_verifier import SCENE_VISUAL_THRESHOLD


def environment_project():
    return project({
        "narration": "Rain cannot wash these dust particles away on Venus.",
        "visual_goal": "Venus atmosphere and dusty surface",
        "visual_intent": {"objects": ["Venus atmosphere"], "context": ["actual planetary environment"],
                          "media_queries": ["Venus atmosphere dust"]},
        "search_queries": ["Venus atmosphere dust"], "preferred_media": "photo",
    }, intent={"topic": "Venus atmosphere"})


def themed_item(title="Costumed explorer hitchhiking with a Venus sign on a roadside", provider="pexels"):
    return replace(cand("themed", "Venus atmosphere dust", title,
                        provider=provider, kind="photo"), rank=100000)


@pytest.mark.parametrize("provider", ["pexels", "pixabay", "wikimedia", "openverse", "nasa"])
def test_actual_planetary_setting_rejects_themed_stock_despite_high_openclip(provider):
    state = environment_project(); scene = state["scenes"][0]
    item = themed_item(provider=provider)
    relevance = media.media_relevance(item, scene, state)
    assert relevance["query_provenance"] and relevance["scene_matches"]
    assert relevance["setting_evidence"]["required"] and relevance["setting_evidence"]["mismatch"]
    relevance["visual"] = {"status": "verified", "scene_score": .95}
    assert relevance["confidence"] == "rejected"
    assert media.real_media_quality_gate(item, relevance, "pass", .95) == (False, "semantic_mismatch")


@pytest.mark.parametrize("caption", [
    "Explorer cosplay beside a planetary landscape backdrop",
    "Venus-themed merchandise displayed in a planetary science convention",
    "A desert landscape resembling Venus",
    "Planetary explorer standing in grass beside a paved road",
    "A person holding a cardboard sign saying Venus",
])
def test_representation_conflicts_are_scene_specific(caption):
    state = environment_project()
    rel = media.media_relevance(themed_item(caption), state["scenes"][0], state)
    assert rel["setting_evidence"]["mismatch"] and rel["confidence"] == "rejected"


def test_source_page_caption_cannot_hide_the_conflicting_setting():
    state = environment_project()
    item = themed_item("Venus atmosphere")
    item = replace(item, source_url="https://www.pexels.com/photo/planetary-costume-beside-a-roadside-123456/",
                   description="")
    assert media.media_relevance(item, state["scenes"][0], state)["setting_evidence"]["mismatch"]


@pytest.mark.parametrize("provider", ["pexels", "nasa"])
def test_actor_and_planet_name_alone_cannot_establish_a_planetary_environment(provider):
    state = environment_project()
    item = themed_item("An astronaut walking on Venus", provider=provider)
    rel = media.media_relevance(item, state["scenes"][0], state)
    rel["visual"] = {"status": "verified", "scene_score": .95}
    assert "actor_without_environment_evidence" in rel["setting_evidence"]["markers"]
    assert media.real_media_quality_gate(item, rel) == (False, "semantic_mismatch")


def test_parent_fact_establishes_actual_setting_for_a_sentence_fragment():
    state = environment_project(); scene = state["scenes"][0]
    scene.update(narration="It stays suspended longer.", visual_goal="dust particles",
                 visual_intent={"objects": ["dust particles"]}, block_id="mechanism")
    state["script"] = {"blocks": [{"id": "mechanism", "text": "Dust floats in the Venus atmosphere."}]}
    rel = media.media_relevance(themed_item(), scene, state)
    assert rel["setting_evidence"]["required"] and rel["confidence"] == "rejected"


@pytest.mark.parametrize("placement", ["query", "exclusion", "negative_context"])
def test_query_or_negative_instruction_cannot_grant_a_cosplay_exception(placement):
    state = environment_project(); scene = state["scenes"][0]
    if placement == "query":
        scene["visual_intent"]["media_queries"] = ["Venus costume"]
        scene["search_queries"] = ["Venus costume"]
    elif placement == "exclusion":
        scene["visual_intent"]["must_not_show"] = ["cosplay themed Earth imagery"]
    else:
        scene["visual_intent"]["context"].append("no costumes or cosplay")
    relevance = media.media_relevance(themed_item(), scene, state)
    assert relevance["setting_evidence"]["required"] and relevance["confidence"] == "rejected"


@pytest.mark.parametrize("title", [
    "Lunar crater landscape captured by a surface mission",
    "Apollo astronaut collecting lunar soil on the Moon",
    "Lunar surface without grass or vegetation",
    "Lunar rocks resembling faces on a crater landscape",
])
def test_actual_domain_capture_remains_possible_with_people_or_caption_contrasts(title):
    state = project({"narration": "The lunar surface is rocky.", "visual_goal": "lunar surface soil rocks landscape",
                     "visual_intent": {"objects": ["lunar surface soil rocks landscape"],
                                       "media_queries": ["lunar surface"]}})
    item = cand("capture", "lunar surface", title, kind="photo", provider="wikimedia")
    rel = media.media_relevance(item, state["scenes"][0], state)
    assert rel["setting_evidence"]["required"] and not rel["setting_evidence"]["mismatch"]
    rel["visual"] = {"status": "verified", "scene_score": .31}
    assert media.real_media_quality_gate(item, rel)[0]


def test_explicitly_requested_cosplay_visual_is_still_eligible():
    state = project({"narration": "A costume lets visitors imagine another planet.",
        "visual_goal": "Venus-themed costume demonstration on a roadside",
        "visual_intent": {"objects": ["costumed explorer"], "context": ["themed demonstration"],
                          "media_queries": ["Venus-themed costume demonstration"]}})
    item = themed_item()
    rel = media.media_relevance(item, state["scenes"][0], state)
    assert not rel["setting_evidence"]["required"]
    rel["visual"] = {"status": "verified", "scene_score": .31}
    assert media.real_media_quality_gate(item, rel)[0]


def test_explicit_earth_comparison_can_use_earth_surroundings():
    state = environment_project(); scene = state["scenes"][0]
    scene.update(narration="On Earth, grass grows beside roads.", visual_goal="Earth landscape",
                 visual_intent={"objects": ["Earth landscape"]})
    state["script"] = {"blocks": [{"id": "compare", "text": "Earth and Venus have different atmospheres."}]}
    scene["block_id"] = "compare"
    item = themed_item("Grass beside a roadside in an Earth landscape")
    rel = media.media_relevance(item, scene, state)
    assert not rel["setting_evidence"]["required"] and media.real_media_quality_gate(item, rel)[0]


def test_domain_candidate_beats_richer_themed_stock_in_the_actual_primary_pool():
    state = environment_project(); query = "Venus atmosphere dust"
    real = replace(cand("domain", query, "Venus atmosphere dust above the rocky surface",
                        provider="nasa", kind="photo"), rank=-100)
    fake = themed_item()
    registry = ProviderRegistry([
        ProviderAdapter("nasa", Commons(photos={query: [real]})),
        ProviderAdapter("pexels", Provider(photos={query: [fake]})),
    ])
    verifier = Verifier(scores={"themed": (.95, .95), "domain": (.31, .31)})
    budget = AcquisitionBudget()
    result = routed(state, registry, verifier=verifier, budget=budget)
    assert result.ranked[0][0].identity == real.identity
    assert "themed" not in verifier.calls and budget.verifications == 1
    stage = result.provenance["stages"][0]
    assert [s["provider"] for s in stage["providers"]] == ["nasa", "pexels"]
    assert stage["providers"][1]["relevance_rejects"] == 1
    evidence = next(e for e in stage["candidate_evidence"] if e["identity"] == fake.identity)
    assert evidence["setting_evidence"]["mismatch"]
    assert not stage["widening_reasons"]


def test_relaxed_selection_and_previous_high_scoring_cache_cannot_rescue_themed_stock():
    state = environment_project(); scene = state["scenes"][0]; item = themed_item()
    verifier = Verifier(default=(.95, .95))
    assert media._relaxed_visual_verdict(item, None, verifier, scene, state)[0] == "rejected"
    assert not verifier.calls
    asset = {**media.candidate_evidence(item), "cache_path": "existing.jpg",
        "acceptance_scene_key": media.scene_acceptance_key(scene, state),
        "relevance": {"confidence": "acceptable", "visual": {"status": "verified", "scene_score": .95}}}
    assert not media.destination_asset_allowed(asset, scene, state)
    assert not media.destination_asset_allowed(asset, scene, state, reuse=True)


def test_only_themed_stock_uses_existing_verified_ai_fallback(tmp_path):
    state = environment_project(); generator = FakeGenerator(); verifier = FallbackVerifier(default=(.95, .95))
    # Return the same attractive themed pool for all bounded logical queries.
    from test_visual_director import Provider as PoolProvider

    media.prepare_project_media(state, "project", settings_for(tmp_path),
        client=PoolProvider(photos=[themed_item()]), fallback_client=Commons(),
        image_generator=generator, visual_verifier=verifier)
    scene = state["scenes"][0]
    assert scene["media"]["source"] == "generated_openai" and len(generator.prompts) == 1
    assert "themed" not in verifier.calls
    assert scene["media"]["generation"]["verification"]["accepted"]
    assert scene["media_search"]["logical_queries_executed"] <= 3
    assert state["visual_director"]["summary"]["auto_generated_images"] <= 3
    assert "generated_card_count" not in state["assets"]
    assert SCENE_VISUAL_THRESHOLD == .24


def test_new_setting_check_does_not_override_unknown_rights():
    state = environment_project()
    item = replace(themed_item("Venus rocky surface"), rights=MediaRights())
    rel = media.media_relevance(item, state["scenes"][0], state)
    rel["visual"] = {"status": "verified", "scene_score": .95}
    assert not media.real_media_quality_gate(item, rel)[0]


def test_fitting_same_block_continuity_still_passes_destination_acceptance():
    state = environment_project(); scene = state["scenes"][0]
    item = themed_item("Venus atmosphere dust above the rocky surface", provider="nasa")
    asset = {**media.candidate_evidence(item), "cache_path": "valid.jpg"}
    next_scene = copy.deepcopy(scene); next_scene.update(id="continuation", narration="Dust remains in the atmosphere.")
    assert media.destination_asset_allowed(asset, next_scene, state, reuse=True)
