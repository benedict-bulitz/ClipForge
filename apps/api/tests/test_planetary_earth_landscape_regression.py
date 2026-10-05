"""Terrestrial setting evidence outranks red-rock resemblance, CLIP and novelty.

Replay the retained landscape/location mechanism with generic scene facts and
assets. No provider calls, real model weights or asset-specific exceptions.
"""
import copy
from dataclasses import replace

import pytest
from test_planetary_setting_regression import environment_project
from test_staged_media_search import Commons, Provider, Verifier, cand
from test_visual_director import FakeGenerator, settings_for
from test_visual_director import Provider as PoolProvider
from test_visual_director import Verifier as FallbackVerifier

from clipforge import media
from clipforge.visual_providers import AcquisitionBudget, ProviderAdapter, ProviderRegistry
from clipforge.visual_verifier import SCENE_VISUAL_THRESHOLD


def item(caption, *, provider="pexels", source_url=None):
    return cand("landscape", "Venus atmosphere dust", caption, kind="photo", provider=provider,
                source_url=source_url)


@pytest.mark.parametrize("caption", [
    "Red desert landscape in Silver City, Nevada.",
    "Red dusty terrain near Albuquerque, New Mexico.",
    "Rocky desert landscape in Namibia beneath a blue sky with white clouds.",
    "Dusty canyon in Spain.",
    "Barren rocky landscape near Iceland.",
    "Rote Landschaft in Namibia.",
    "Red desert landscape beneath an Earth-like blue sky with white clouds.",
    "Rocky terrain beneath terrestrial white clouds.",
])
def test_planetary_environment_rejects_affirmed_earth_landscape(caption):
    state = environment_project()
    candidate = item(caption)
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    assert relevance["setting_evidence"]["required"]
    assert relevance["setting_evidence"]["mismatch"]
    assert relevance["confidence"] == "rejected"
    relevance["visual"] = {"status": "verified", "scene_score": .99}
    assert media.real_media_quality_gate(candidate, relevance, "pass", .99) == (False, "semantic_mismatch")


@pytest.mark.parametrize("provider", ["pexels", "pixabay", "wikimedia", "openverse", "nasa"])
def test_source_identity_and_rich_topic_caption_cannot_grant_earth_exception(provider):
    state = environment_project()
    candidate = replace(item("Venus dust atmosphere, red rocky landscape in Reno, Nevada.", provider=provider), rank=1000000)
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    assert relevance["query_provenance"] and relevance["scene_matches"]
    assert "earth_geography" in relevance["setting_evidence"]["markers"]
    assert media.real_media_quality_gate(candidate, relevance, "pass", .99) == (False, "semantic_mismatch")


def test_source_page_geography_is_evidence_even_when_title_has_only_planet():
    state = environment_project()
    candidate = item("Venus dusty landscape", source_url="https://www.pexels.com/photo/red-desert-landscape-in-oregon-999/")
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    assert "earth_geography" in relevance["setting_evidence"]["markers"]
    assert relevance["confidence"] == "rejected"


@pytest.mark.parametrize("caption", [
    "Venus rocky surface with red dust captured by a surface mission.",
    "Mars rocky landscape with a blue sky and thin white clouds observed by a rover.",
    "Lunar terrain photographed by an Apollo astronaut.",
    "Venus red dusty landscape processed by a laboratory in California.",
    "Venus red terrain, without a terrestrial sky.",
    "Venus red landscape, not a desert landscape in Nevada.",
])
def test_valid_planetary_captures_and_caption_contrasts_remain_possible(caption):
    state = environment_project()
    scene = state["scenes"][0]
    if "Mars" in caption:
        scene.update(narration="Clouds appear over the Mars surface.", visual_goal="Mars surface sky clouds",
                     visual_intent={"objects": ["Mars surface sky clouds"], "media_queries": ["Mars surface"]})
    elif "Lunar" in caption:
        scene.update(narration="The lunar surface is rocky.", visual_goal="lunar surface terrain",
                     visual_intent={"objects": ["lunar surface terrain"], "media_queries": ["lunar surface"]})
    candidate = item(caption, provider="wikimedia")
    relevance = media.media_relevance(candidate, scene, state)
    assert not relevance["setting_evidence"]["mismatch"]
    relevance["visual"] = {"status": "verified", "scene_score": .31}
    assert media.real_media_quality_gate(candidate, relevance)[0]


@pytest.mark.parametrize("caption", [
    "Red desert landscape under a blue sky with white clouds.",
    "Dusty landscape beneath a hazy sky.",
    "Rocky terrain in an unidentified region.",
    "Red landscape in Utopia Planitia on Mars.",
])
def test_ambiguous_colour_clouds_and_places_do_not_become_a_blanket_ban(caption):
    state = environment_project()
    relevance = media.media_relevance(item(caption), state["scenes"][0], state)
    assert relevance["setting_evidence"]["required"]
    assert not relevance["setting_evidence"]["mismatch"]


def test_earth_comparison_and_deliberate_analogue_keep_the_existing_exception():
    state = environment_project()
    scene = state["scenes"][0]
    candidate = item("Red desert landscape in Oregon under a blue sky.")
    for direction in ["Earth desert landscape", "Venus analogue desert landscape demonstration"]:
        scene.update(visual_goal=direction, visual_intent={"objects": [direction]})
        relevance = media.media_relevance(candidate, scene, state)
        assert not relevance["setting_evidence"]["required"]
        assert not relevance["setting_evidence"]["mismatch"]


def test_earth_contradiction_never_enters_novelty_ranking_or_expensive_verification():
    state = environment_project()
    scene = state["scenes"][0]
    good = replace(item("Venus atmosphere dust above a rocky surface.", provider="wikimedia"), provider_id="valid")
    wrong = replace(item("Venus red dust, desert landscape in Namibia."), provider_id="wrong", rank=100000)
    state["scenes"].insert(0, {"id": "prior", "media": media.candidate_evidence(good)})
    registry = ProviderRegistry([
        ProviderAdapter("wikimedia", Commons(photos={good.query: [good]})),
        ProviderAdapter("pexels", Provider(photos={wrong.query: [wrong]})),
    ])
    verifier = Verifier(scores={"wrong": (.99, .99), "valid": (.31, .31)})
    budget = AcquisitionBudget()
    result = media.run_staged_scene_search(
        scene["search_queries"], scene, state, media.build_visual_query_plan(scene, state),
        pexels=registry.get("pexels"), wikimedia=registry.get("wikimedia"), registry=registry,
        preferred_kind="photo", portrait=True, scene_duration=4, used=set(),
        verifier=verifier, acquisition_budget=budget,
    )
    assert [c.provider_id for c, _ in result.ranked] == ["valid"]
    assert "wrong" not in verifier.calls
    assert budget.verifications == 1
    assert not result.ranked[0][1]["setting_evidence"]["mismatch"]
    assert scene is state["scenes"][1]


def test_cache_reuse_and_relaxation_recompute_the_same_contradiction():
    state = environment_project()
    scene = state["scenes"][0]
    candidate = item("Venus red rocks, desert landscape in Silver City, Nevada.")
    old = {**media.candidate_evidence(candidate), "cache_path": "exists.jpg",
           "acceptance_scene_key": media.scene_acceptance_key(scene, state),
           "relevance": {"confidence": "high", "acceptance": {"accepted": True},
                         "visual": {"status": "verified", "scene_score": .99}}}
    assert not media.destination_asset_allowed(old, scene, state)
    next_scene = copy.deepcopy(scene)
    next_scene.update(id="continuation", block_id=scene.get("block_id"))
    assert not media.destination_asset_allowed(old, next_scene, state, reuse=True)
    verifier = Verifier(default=(.99, .99))
    assert media._relaxed_visual_verdict(candidate, None, verifier, scene, state)[0] == "rejected"
    assert not verifier.calls


def test_all_real_earth_candidates_rejected_complete_existing_ai_fallback(tmp_path):
    state = environment_project()
    generator = FakeGenerator()
    verifier = FallbackVerifier(default=(.99, .99))
    media.prepare_project_media(
        state, "project", settings_for(tmp_path),
        client=PoolProvider(photos=[item("Red dusty landscape in Oregon.")]),
        fallback_client=PoolProvider(photos=[]), extra_clients=[],
        image_generator=generator, visual_verifier=verifier,
    )
    scene = state["scenes"][0]
    assert scene["media"]["source"] == "generated_openai"
    assert scene["media"]["generation"]["verification"]["accepted"]
    assert len(generator.prompts) == 1
    assert "landscape" not in verifier.calls
    assert scene["media_search"]["logical_queries_executed"] <= 3
    assert "generated_card_count" not in state["assets"]
    assert SCENE_VISUAL_THRESHOLD == .24
