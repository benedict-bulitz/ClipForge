"""Generic replay of empty video fields, OCR overlap and lost story context."""
import copy
from dataclasses import replace

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, project, settings_for
from test_visual_director import FakeGenerator
from test_visual_sources_v2 import registry_all, routed

from clipforge import media
from clipforge.media_candidates import _ordered
from clipforge.source_router import route_sources
from clipforge.visual_providers import AcquisitionBudget, ProviderAdapter, ProviderRegistry
from clipforge.visual_rights import MediaRights
from clipforge.visual_verifier import SCENE_VISUAL_THRESHOLD, visual_intent_text


def planetary_project():
    scene = {"id": "detail", "block_id": "material", "narration": "This is a familiar mineral.",
             "visual_goal": "mineral dust", "visual_intent": {"objects": ["mineral dust"],
             "media_queries": ["mineral dust"]}, "search_queries": ["mineral dust"], "preferred_media": "photo"}
    state = project(scene, intent={"topic": "planetary atmosphere"})
    state["script"] = {"blocks": [{"id": "material", "text": "Mineral particles color the planetary atmosphere.", "fact_ids": ["material-fact"]}]}
    state["story_arc"] = {"primary_answer_id": "answer", "units": [
        {"id": "answer", "claim": "Particles in a planetary atmosphere scatter sunlight."},
        {"id": "material-fact", "claim": "Mineral particles float in the atmosphere."}]}
    return state


def unrelated_video():
    return replace(cand("ordinary", "mineral dust", "", kind="video",
        source_url="https://www.pexels.com/video/coastal-town-shopping-street-891234/"), rank=10000)


@pytest.mark.parametrize("relaxed", [False, True])
def test_empty_video_fields_do_not_hide_source_page_mismatch(relaxed):
    state = planetary_project(); scene = state["scenes"][0]; item = unrelated_video()
    relevance = media.media_relevance(item, scene, state)
    assert relevance["query_provenance"] and relevance["confidence"] == "rejected"
    assert "shopping street" in relevance["metadata_evidence"]
    relevance["visual"] = {"status": "verified", "score": 0.2324, "scene_score": 0.2431, "subject_score": 0.1692}
    assert media.real_media_quality_gate(item, relevance) == (False, "semantic_mismatch")
    if relaxed:
        verifier = Verifier(default=(0.8, 0.8))
        assert media._relaxed_visual_verdict(item, None, verifier, scene, state)[0] == "rejected"
        assert not verifier.calls


def test_primary_planetary_pool_rejects_terrestrial_stock_with_larger_rank():
    state = planetary_project(); query = "mineral dust"
    domain = replace(cand("domain", query, "Mineral dust in a planetary atmosphere", provider="nasa", kind="photo"), rank=1)
    stock = replace(unrelated_video(), kind="photo", duration=None)
    registry = ProviderRegistry([ProviderAdapter("nasa", Commons(photos={query: [domain]})),
        ProviderAdapter("pexels", Provider(photos={query: [stock]}))])
    verifier = Verifier(scores={"ordinary": (0.8, 0.8), "domain": (0.31, 0.31)})
    result = routed(state, registry, verifier=verifier)
    assert result.ranked[0][0].identity == domain.identity and "ordinary" not in verifier.calls
    assert result.provenance["stages"][0]["providers"][1]["relevance_rejects"] == 1
    assert not result.provenance["stages"][0]["widening_reasons"]


def test_primary_answer_routes_domain_source_for_non_domain_query():
    state = planetary_project(); scene = state["scenes"][0]
    routes = route_sources(registry_all(), scene, state, "mineral dust", "video")
    assert [s.adapter.provider for s in routes[0]] == ["nasa", "wikimedia"]
    assert routes[0][0].kind == "photo"


@pytest.mark.parametrize("negative", ["archival history 1888", "spacecraft astronomy", "medical anatomy"])
def test_exclusions_and_unrelated_story_units_do_not_route_sources(negative):
    scene = {"block_id": "daily", "narration": "A person checks a kitchen cupboard.",
             "visual_intent": {"objects": ["cupboard"], "actions": ["checking"], "must_not_show": [negative]}}
    state = {"script": {"blocks": [{"id": "daily", "text": scene["narration"], "fact_ids": ["today"]}]},
             "story_arc": {"units": [{"id": "unrelated", "claim": "Archival city streets in 1888."}]}}
    routes = route_sources(registry_all(), scene, state, "checking kitchen cupboard", "video")
    assert [s.adapter.provider for s in routes[0]] == ["pexels", "pixabay"]
    sources = {s.adapter.provider for group in routes for s in group}
    assert not {"loc", "europeana", "nasa"} & sources and "wikimedia" in sources


@pytest.mark.parametrize("provider", ["wikimedia", "openverse", "loc", "europeana"])
def test_long_document_transcription_is_not_visual_corroboration(provider):
    scene = {"narration": "The feeling of acting brings immediate comfort.", "visual_goal": "feeling immediate comfort",
             "visual_intent": {"source": "narration_fallback", "objects": ["feeling", "immediate", "comfort"],
                               "media_queries": ["feeling immediate comfort"]}, "search_queries": ["feeling immediate comfort"]}
    state = project(scene)
    body = "Shipping report: cargo arrivals and port departures. " + "Harbor freight cargo schedules. " * 50
    body += " The feeling of acting brings immediate comfort."
    item = replace(cand("document", "feeling immediate comfort", "Trade Gazette shipping report 1898", provider=provider, kind="photo"), description=body, rank=1e6)
    relevance = media.media_relevance(item, scene, state)
    assert relevance["confidence"] == "rejected"
    relevance["visual"] = {"status": "unavailable_preview"}
    assert not media.real_media_quality_gate(item, relevance)[0]
    assert media.verify_media_shortlist([item], scene, state, Verifier()) == []


def historical_project():
    scene = {"id": "historical", "block_id": "works", "narration": "It separated the districts.",
             "visual_goal": "archival city barrier construction", "search_queries": ["city barrier construction"],
             "visual_intent": {"objects": ["city barrier"], "actions": ["construction"], "context": ["archival"], "media_queries": ["city barrier construction"]}, "preferred_media": "photo"}
    state = project(scene)
    state["script"] = {"blocks": [{"id": "works", "text": "A city barrier was constructed between 1958 and 1960.", "fact_ids": ["construction"]}]}
    state["story_arc"] = {"units": [{"id": "construction", "claim": "Workers erected the barrier between 1958 and 1960."}]}
    return state


def test_historical_context_does_not_accept_merely_current_location():
    state = historical_project(); scene = state["scenes"][0]
    current = cand("current", "city barrier construction", "City barrier construction street view", kind="photo")
    old = replace(cand("old", current.query, "Archival city barrier construction", kind="photo"), origin={"date": "1959-07-01"})
    wrong_period = replace(old, provider_id="later", origin={"date": "1987-01-01"})
    for item in [current, wrong_period]:
        relevance = media.media_relevance(item, scene, state)
        relevance["visual"] = {"status": "verified", "score": 0.8, "scene_score": 0.8}
        assert relevance["temporal_evidence"]["mismatch"]
        assert media.real_media_quality_gate(item, relevance) == (False, "semantic_mismatch")
    accepted = media.media_relevance(old, scene, state)
    assert accepted["temporal_evidence"]["matched_years"] == [1959]
    assert media.real_media_quality_gate(old, accepted)[0]


def test_date_from_parent_block_routes_twentieth_century_archives():
    state = historical_project(); scene = state["scenes"][0]; scene["visual_intent"].pop("context")
    routes = route_sources(registry_all(), scene, state, "city barrier construction", "photo")
    assert [s.adapter.provider for s in routes[0]] == ["loc", "europeana"]


def test_explicit_present_day_broll_remains_possible_in_history():
    state = historical_project(); scene = state["scenes"][0]
    scene["visual_goal"] = "current city barrier remains"
    scene["visual_intent"] = {"objects": ["city barrier"], "context": ["present-day remains"], "media_queries": ["current city barrier remains"]}
    item = cand("today", "city barrier", "Current city barrier remains", kind="photo")
    relevance = media.media_relevance(item, scene, state)
    assert not relevance["temporal_evidence"]["required"] and media.real_media_quality_gate(item, relevance)[0]


def test_wrong_period_cannot_gain_legitimacy_through_cached_or_critic_reuse():
    state = historical_project(); scene = state["scenes"][0]
    item = cand("current", "city barrier construction", "City barrier construction street view", kind="photo")
    asset = {**media.candidate_evidence(item), "cache_path": "current.jpg",
             "acceptance_scene_key": media.scene_acceptance_key(scene, state),
             "relevance": {"visual": {"status": "verified", "score": 0.8, "scene_score": 0.8, "provenance": "final_critic_destination_frames"}}}
    assert not media.destination_asset_allowed(asset, scene, state)
    assert not media.destination_asset_allowed(asset, scene, state, reuse=True)


def test_fitting_historical_same_block_continuity_remains_possible():
    state = historical_project(); scene = state["scenes"][0]
    item = cand("old", "city barrier construction", "Archival city barrier construction 1959", kind="photo")
    asset = {**media.candidate_evidence(item), "cache_path": "old.jpg"}
    continuation = copy.deepcopy(scene); continuation.update(id="continued", narration="Workers closed the crossing.")
    assert media.destination_asset_allowed(asset, continuation, state, reuse=True)


def test_query_provenance_cannot_add_absent_scene_concepts():
    scene = {"narration": "Rubber bends under pressure.", "visual_goal": "rubber bending", "search_queries": ["summer street"]}
    state = project(scene); item = cand("wrong", "summer street", "Summer street photograph", kind="photo")
    relevance = media.media_relevance(item, scene, state)
    assert relevance["query_provenance"] and not relevance["scene_matches"] and relevance["confidence"] == "rejected"


@pytest.mark.parametrize("provider", ["pexels", "wikimedia", "nasa"])
def test_absolute_rank_scale_does_not_starve_verification(provider):
    state = planetary_project(); scene = state["scenes"][0]
    items = [replace(cand(f"stock-{i}", "mineral dust", "Mineral dust in planetary atmosphere", provider=provider, kind="photo"), rank=100000-i) for i in range(8)]
    items.append(replace(cand("other-source", "mineral dust", "Mineral dust in planetary atmosphere", provider="openverse", kind="photo"), rank=-200))
    verifier = Verifier(); budget = AcquisitionBudget(max_verifications=6)
    media.verify_media_shortlist(items, scene, state, verifier, acquisition_budget=budget)
    assert "other-source" in verifier.calls and len(verifier.calls) == 6
    assert _ordered(items, "photo", set(), scene, state).index(items[-1]) < 6


def test_widened_wrong_metadata_cannot_bypass_thresholds_or_rights():
    state = planetary_project(); query = "mineral dust"
    wrong = replace(unrelated_video(), kind="photo", duration=None)
    unknown = replace(cand("unknown", query, "Mineral dust in planetary atmosphere", provider="nasa", kind="photo"), rights=MediaRights())
    registry = ProviderRegistry([ProviderAdapter("nasa", Commons(photos={query: [unknown]})), ProviderAdapter("pexels", Provider(photos={query: [wrong]}))])
    result = routed(state, registry); stats = result.provenance["stages"][0]["providers"]
    assert not result.ranked and stats[0]["rights_rejects"] == 1 and stats[1]["relevance_rejects"] == 1
    assert result.provenance["logical_queries_executed"] <= 3


def test_no_good_stock_reaches_existing_ai_director_fallback(tmp_path):
    state = planetary_project(); state["scenes"][0].update(start=0, end=4)
    generator = FakeGenerator(); query = "mineral dust"
    media.prepare_project_media(state, "project", settings_for(tmp_path),
        client=Provider(photos={query: [replace(unrelated_video(), kind="photo", duration=None)]}),
        fallback_client=Commons(), visual_verifier=Verifier(), image_generator=generator)
    assert state["scenes"][0]["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1 and state["scenes"][0]["visual_director"]["decision"] == "GENERATE_FALLBACK"


def test_fitting_generic_lifestyle_keeps_existing_authority_and_threshold():
    assert SCENE_VISUAL_THRESHOLD == 0.24
    scene = {"narration": "A person checks a kitchen cupboard.", "visual_goal": "person checking cupboard",
             "visual_intent": {"objects": ["cupboard"], "actions": ["checking"], "context": ["kitchen"]}}
    state = project(scene); item = cand("fit", "cupboard", "Person checking a kitchen cupboard")
    r = media.media_relevance(item, scene, state)
    assert media.real_media_quality_gate(item, r)[0]
    r["visual"] = {"status": "verified", "scene_score": 0.239}
    assert media.real_media_quality_gate(item, r) == (False, "visual_rejected")


def test_scene_prompt_keeps_canonical_action_ahead_of_generic_query():
    scene = {"narration": "This changed the crossing.", "visual_goal": "Archival workers build a barrier in 1959",
             "visual_intent": {"objects": ["workers"], "actions": ["building a barrier"], "context": ["1959"]}, "search_queries": ["city street"]}
    texts = visual_intent_text(scene, {})
    assert texts.scene[0] == scene["narration"]
    assert any("building a barrier" in t and "1959" in t for t in texts.scene)
    assert "city street" not in texts.scene


def test_candidate_diagnostics_are_bounded_with_actual_gate_evidence():
    state = planetary_project(); query = "mineral dust"
    items = [cand(f"image-{i}", query, "Mineral dust in planetary atmosphere", provider="nasa", kind="photo") for i in range(30)]
    result = routed(state, ProviderRegistry([ProviderAdapter("nasa", Commons(photos={query: items}))]))
    evidence = result.provenance["stages"][0]["candidate_evidence"]
    assert len(evidence) <= 24 and all(len(e["metadata_evidence"]) <= 320 for e in evidence)
    assert any(e.get("acceptance", {}).get("accepted") and e["visual"]["scene_score"] >= 0.24 for e in evidence)


def test_negative_modern_exclusion_cannot_allow_current_stock_in_history():
    state = historical_project(); scene = state["scenes"][0]
    scene["visual_intent"]["must_not_show"] = ["modern current city traffic"]
    item = cand("now", "city barrier construction", "City barrier construction street", kind="photo")
    relevance = media.media_relevance(item, scene, state)
    assert relevance["temporal_evidence"]["required"]
    assert relevance["confidence"] == "rejected"


def test_parent_fact_changes_invalidate_cached_destination_verification():
    state = historical_project(); scene = state["scenes"][0]
    before = media.scene_acceptance_key(scene, state)
    state["story_arc"]["units"][0]["claim"] = "Workers erected the barrier in 1987."
    assert media.scene_acceptance_key(scene, state) != before


def test_full_canonical_goal_is_not_truncated_by_redundant_subject_phrases():
    scene = {"narration": "The skin folds.", "visual_intent": {"visual_goal": "wrinkled fingertip skin after immersion",
             "objects": ["wrinkled"], "media_queries": ["wrinkled fingertip skin"]}}
    prompts = visual_intent_text(scene, {})
    assert any("fingertip skin after immersion" in text for text in prompts.scene)
    assert len(prompts.scene) <= 3


def test_calm_lifestyle_atmosphere_is_not_an_earth_observation_scene():
    scene = {"narration": "A person relaxes in a calm atmosphere.",
             "visual_intent": {"objects": ["person"], "actions": ["relaxing"], "context": ["calm atmosphere at home"]}}
    routes = route_sources(registry_all(), scene, {}, "person relaxing at home", "photo")
    assert [s.adapter.provider for s in routes[0]] == ["pexels", "pixabay"]
    assert all(s.adapter.provider != "nasa" for group in routes for s in group)
