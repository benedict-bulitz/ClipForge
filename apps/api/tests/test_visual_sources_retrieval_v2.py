"""Visual Sources & Retrieval V2: planner, router, judge, fallback rules, gate.

Deterministic and offline: providers, OpenCLIP, the optional VLM judge and the
image generator are all fakes. No live third-party API is contacted.
"""

import copy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_staged_media_search import Commons, cand, project
from test_visual_director import STRONG, WEAK_PASS, FakeGenerator, Provider, Verifier
from test_visual_director import settings_for as director_settings
from test_visual_sources_v2 import registry_all
from visual_rights_support import TEST_REUSE_RIGHTS

from clipforge import media, visual_judge
from clipforge.media import (
    MediaCandidate,
    MediaProviderError,
    build_visual_query_plan,
    complete_project_visuals,
    prepare_project_media,
)
from clipforge.open_media import FLICKR_LICENSE_URIS, FlickrProvider
from clipforge.renderer import still_motion_plan
from clipforge.source_router import route_sources, routing_decision
from clipforge.visual_fallback_policy import ai_fallback_decision, generation_restriction
from clipforge.visual_judge import (
    apply_vision_verdict,
    judge_and_rank,
    judge_candidate,
    prefilter,
    reframe_geometry,
)
from clipforge.visual_providers import AcquisitionBudget, ProviderRegistry, create_provider_registry
from clipforge.visual_quality_gate import evaluate_scene, needs_replacement
from clipforge.visual_rights import evaluate_rights
from clipforge.visual_search_planner import (
    build_facets,
    classify_domain,
    near_duplicate,
    query_for_source,
    scene_signals,
)
from clipforge.visual_transform import plan_transformation

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def wall_scene(**extra):
    return {
        "id": "s1",
        "block_id": "b1",
        "start": 0,
        "end": 4,
        "narration": "1961 riegelte die DDR über Nacht West-Berlin ab.",
        "visual_intent": {
            "visual_goal": "soldiers unrolling barbed wire at the Berlin Wall",
            "objects": ["soldiers with barbed wire"],
            "actions": ["unrolling barbed wire"],
            "context": ["divided city street"],
            "media_queries": ["soldiers barbed wire border"],
            "entities": ["Berlin Wall"],
            "location": "Berlin",
            "time_period": "1961",
            "alternate_terms": ["Berliner Mauer"],
            "factual_sensitivity": "historical_event",
        },
        **extra,
    }


def wall_state(**scene_extra):
    scene = wall_scene(**scene_extra)
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {},
            "intent": {"topic": "Why was the Berlin Wall built?"}}


def photo(provider_id, title, *, provider="wikimedia", width=1200, height=1600, query="q", **extra):
    return MediaCandidate(
        provider_id=provider_id, kind="photo", download_url=f"https://media.test/{provider_id}.jpg",
        source_url=f"https://{provider}.test/item/{provider_id}", creator=extra.pop("creator", "Archive"),
        creator_url=None, width=width, height=height, duration=None, query=query, rank=10,
        provider=provider, title=title, rights=TEST_REUSE_RIGHTS,
        preview_url=f"https://media.test/{provider_id}-preview.jpg", **extra,
    )


def verified(confidence="acceptable", scene_score=0.31, tier=2, **extra):
    return {"confidence": confidence, "selection_tier": tier,
            "visual": {"status": "verified", "score": scene_score, "scene_score": scene_score}, **extra}


# ---------------------------------------------------------------------------
# Search Planner
# ---------------------------------------------------------------------------


def test_planner_builds_entity_period_location_and_alternate_facets():
    state = wall_state()
    facets = {item["facet"]: item["query"] for item in build_facets(state["scenes"][0], state)}
    assert facets["entity_period"] == "berlin wall 1961"
    assert facets["subject_action"].startswith("soldiers")
    assert "berliner mauer" in facets["alternate"]
    assert "berlin" in facets["entity"]


def test_query_plan_keeps_authored_queries_first_and_adds_factual_facets():
    state = wall_state()
    plan = build_visual_query_plan(state["scenes"][0], state)
    assert plan["queries"][0] == "soldiers barbed wire border"
    assert len(plan["queries"]) <= 3
    assert any("1961" in query for query in plan["queries"][1:])
    search_plan = plan["search_plan"]
    assert search_plan["domain"] == "historical"
    assert search_plan["sensitivity"] == "historical_event"
    # Catalogue-style archives get the entity + date phrasing first.
    assert search_plan["source_queries"]["archive"][0] == "berlin wall 1961"


def test_planner_never_plans_a_protected_payoff_term_before_reveal():
    state = wall_state()
    state["payoff_plan"] = {"hook_must_not_reveal": "Berliner Mauer"}
    state["scenes"][0]["story_stage"] = "before_reveal"
    plan = build_visual_query_plan(state["scenes"][0], state)
    assert not any("mauer" in query or "berliner" in query for query in plan["queries"])
    assert plan["search_plan"]["protected_facets"]
    assert all("mauer" not in q for qs in plan["search_plan"]["source_queries"].values() for q in qs)


def test_narration_fallback_scene_borrows_same_block_planned_intent():
    planned = wall_scene(id="s1")
    fragment = {"id": "s2", "block_id": "b1", "start": 4, "end": 8, "narration": "Er ist vor allem deshalb so.",
                "visual_intent": {"visual_goal": "vor allem deshalb", "objects": ["allem", "deshalb"],
                                  "media_queries": ["vor allem deshalb"], "source": "narration_fallback"}}
    state = {"timeline": {"width": 1080, "height": 1920}, "scenes": [planned, fragment]}
    plan = build_visual_query_plan(fragment, state)
    assert plan["search_plan"]["signals"]["borrowed_block_intent"] is True
    assert plan["query_quality"] == "planned_facets"
    assert not any(word in " ".join(plan["queries"]) for word in ("allem", "deshalb"))


def test_meta_direction_words_never_become_queries():
    scene = {"id": "s", "narration": "x", "visual_intent": {
        "visual_goal": "show the real-world mechanism", "objects": ["lighthouse keeper"],
        "context": ["show the real-world mechanism"]}}
    queries = [item["query"] for item in build_facets(scene, {"scenes": [scene]})]
    assert queries and not any("show" in q.split() or "mechanism" in q.split() for q in queries)


def test_near_duplicate_queries_are_detected():
    assert near_duplicate("berlin wall 1961", "1961 berlin wall")
    assert not near_duplicate("berlin wall 1961", "soldiers barbed wire")


@pytest.mark.parametrize("text,domain", [
    ("dust storm on mars surface", "space"),
    ("warum ist der himmel auf dem mars rot", "space"),
    ("berlin wall construction 1961", "historical"),
    ("medieval manuscript illumination", "historical"),
    ("renaissance painting in a museum", "historical"),
    ("bronze sculpture in a museum hall", "art_culture"),
    ("cross-section diagram of an engine", "diagram"),
    ("red blood cells under a microscope", "anatomy"),
    ("bird species feeding chicks", "nature"),
    ("person opening a fridge at night", "general"),
])
def test_domain_classification(text, domain):
    assert classify_domain(text)[0] == domain


# ---------------------------------------------------------------------------
# Source Router
# ---------------------------------------------------------------------------


def test_router_sends_mars_scenes_to_nasa_first():
    routes = route_sources(registry_all(), {"visual_goal": "red dust over the mars surface"}, {}, "mars dust", "video")
    assert routes[0][0].adapter.provider == "nasa"


def test_router_records_unsuitable_providers_as_skipped():
    decision = routing_decision(registry_all(), {"visual_goal": "person opening a fridge"}, {}, "fridge")
    skipped = {item["provider"]: item["reason"] for item in decision["skipped"]}
    assert decision["domain"] == "general"
    assert skipped["nasa"] == "unsuitable_for_domain:general"
    assert "loc" in skipped and "europeana" in skipped
    assert "pexels" not in skipped


def test_router_art_scenes_prefer_europeana_and_commons():
    routes = route_sources(registry_all(), {"visual_goal": "bronze sculpture in a museum"}, {}, "sculpture", "photo")
    assert [s.adapter.provider for s in routes[0]] == ["europeana", "wikimedia"]


def test_router_gives_archives_the_entity_period_phrasing_per_stage():
    state = wall_state()
    scene = state["scenes"][0]
    scene["visual_query_plan"] = build_visual_query_plan(scene, state)
    routes = route_sources(registry_all(), scene, state, "soldiers barbed wire border", "photo", stage_index=0)
    by_provider = {s.adapter.provider: s.query for group in routes for s in group}
    assert by_provider["loc"] == "berlin wall 1961"
    assert by_provider["pexels"] is None  # stock keeps the visual logical query
    assert query_for_source(scene, "loc", "fallback", 9) == "fallback"


# ---------------------------------------------------------------------------
# Flickr: strict licence allow-list, normalization, malformed / unavailable
# ---------------------------------------------------------------------------


def flickr_row(**overrides):
    return {"id": "51", "owner": "12@N01", "title": "Berlin Wall 1961", "license": "4", "ownername": "Jane",
            "url_l": "https://live.staticflickr.test/51_l.jpg", "width_l": 1024, "height_l": 768,
            "url_c": "https://live.staticflickr.test/51_c.jpg", "description": {"_content": "East German border"},
            "tags": "berlin wall border", "datetaken": "1961-08-20 00:00:00", **overrides}


def flickr(payload):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request)))
    return FlickrProvider("secret-test-key", client=client)


def test_flickr_requests_only_reusable_licenses():
    assert set(FLICKR_LICENSE_URIS) == {"4", "9", "10", "11"}
    params = flickr({}).params("berlin wall")
    assert params["license"] == "4,9,10,11" and params["safe_search"] == "1"


def test_flickr_drops_rows_outside_the_allow_list_even_if_returned():
    rows = [flickr_row(), flickr_row(id="52", license="2"), flickr_row(id="53", license="7"), flickr_row(id="54", license="9")]
    result = flickr({"stat": "ok", "photos": {"photo": rows}}).search(
        "berlin wall", "photo", portrait=True, scene_duration=4, budget=AcquisitionBudget())
    assert [c.provider_id for c in result] == ["51", "54"]
    first = result[0]
    assert first.provider == "flickr" and first.source_url == "https://www.flickr.com/photos/12@N01/51/"
    assert first.width == 1024 and first.height == 768 and first.title == "Berlin Wall 1961"
    assert evaluate_rights(first.rights).status == "usable"
    assert first.rights.attribution_required is True and "Jane" in str(first.rights.attribution_text)
    assert result[1].rights.public_domain is True


def test_flickr_invalid_key_disables_provider_without_leaking_it():
    provider = flickr({"stat": "fail", "code": 100, "message": "Invalid API Key"})
    with pytest.raises(MediaProviderError) as error:
        provider.search("x", "photo", portrait=True, scene_duration=4, budget=AcquisitionBudget())
    assert error.value.category == "invalid_credentials" and provider.disabled
    assert "secret-test-key" not in str(error.value) and "secret" not in repr(provider)


def test_flickr_malformed_response_is_a_provider_error():
    with pytest.raises(MediaProviderError) as error:
        flickr({"stat": "ok", "photos": {"photo": "nope"}}).search(
            "x", "photo", portrait=True, scene_duration=4, budget=AcquisitionBudget())
    assert error.value.category == "malformed_response"


def test_flickr_is_only_registered_with_a_key(tmp_path):
    names = lambda settings: {p.provider for p in create_provider_registry(settings).enabled()}
    base = director_settings(tmp_path, pexels_api_key=None)
    assert "flickr" not in names(base)
    assert "flickr" in names(director_settings(tmp_path, pexels_api_key=None, flickr_api_key="k"))


# ---------------------------------------------------------------------------
# Visual Judge: pre-filter, scoring, hard requirements, ranking
# ---------------------------------------------------------------------------


def test_prefilter_rejects_license_resolution_crop_watermark_and_duplicates():
    good = photo("ok", "Berlin Wall 1961")
    assert prefilter(good) == []
    assert "license_unknown" in prefilter(replace(good, rights=replace(TEST_REUSE_RIGHTS, public_domain=None, commercial_use_allowed=None)))
    assert "tiny_resolution" in prefilter(replace(good, width=400, height=300))
    assert "reframe_infeasible_aspect" in prefilter(replace(good, width=6000, height=1500))
    assert "insufficient_resolution_after_reframe" in prefilter(replace(good, width=1000, height=520))
    assert "watermark_or_text_marker" in prefilter(replace(good, title="Wall photo with watermark"))
    assert "missing_media" in prefilter(replace(good, download_url=""))
    assert "duplicate_asset" in prefilter(good, seen_keys={good.identity})


def test_reframe_geometry_reports_retained_share_and_upscale():
    landscape = reframe_geometry(1920, 1080)
    assert landscape["orientation"] == "landscape" and 0.31 < landscape["retained_share"] < 0.32
    assert landscape["upscale"] == pytest.approx(1920 / 1080, rel=1e-3)
    assert reframe_geometry(1080, 1920)["retained_share"] == 1.0


def test_factually_wrong_entity_is_rejected_even_if_attractive():
    state = wall_state()
    scene = state["scenes"][0]
    church = photo("church", "Kaiser Wilhelm Memorial Church in Berlin, dramatic aerial close-up", width=3000, height=4000)
    verdict = judge_candidate(church, verified(scene_score=0.33, temporal_evidence={"required": True, "established": True}), scene, state)
    assert verdict["reject"] and "factual_below_floor" in verdict["reasons"]
    wall = photo("wall", "Berliner Mauer 1961, Stacheldraht an der Sektorengrenze")
    good = judge_candidate(wall, verified(scene_score=0.27, temporal_evidence={"required": True, "matched_years": [1961], "established": True}), scene, state)
    assert not good["reject"] and good["scores"]["factual_match"] == 1.0
    assert "entity_confirmed" in good["factual_notes"]


def test_period_and_setting_contradictions_zero_the_factual_score():
    state = wall_state()
    scene = state["scenes"][0]
    item = photo("modern", "Berlin Wall memorial 2019")
    assert judge_candidate(item, verified(temporal_evidence={"required": True, "mismatch": True}), scene, state)["scores"]["factual_match"] == 0
    assert judge_candidate(item, verified(setting_evidence={"mismatch": True}), scene, state)["reject"]


def test_replica_or_costume_is_not_the_real_subject():
    state = wall_state()
    replica = photo("replica", "Berlin Wall 1961 replica in a theme park")
    verdict = judge_candidate(replica, verified(temporal_evidence={"required": True, "matched_years": [1961]}), state["scenes"][0], state)
    assert any(note.startswith("staged_or_replica") for note in verdict["factual_notes"])
    assert verdict["reject"]


def test_loosely_related_filler_fails_the_semantic_floor():
    scene = {"id": "s", "narration": "A cup of coffee steams.", "visual_intent": {"objects": ["coffee cup"]}}
    state = {"scenes": [scene]}
    filler = photo("street", "City street", provider="pexels")
    verdict = judge_candidate(filler, verified(confidence="unknown", scene_score=0.245, tier=1), scene, state)
    assert verdict["reject"] and "semantic_below_floor" in verdict["reasons"]
    clear = judge_candidate(filler, verified(confidence="unknown", scene_score=0.31, tier=1), scene, state)
    assert not clear["reject"]


def test_semantic_authority_rejection_is_never_overridden():
    scene = {"id": "s", "narration": "x"}
    verdict = judge_candidate(photo("a", "anything"), verified(confidence="rejected", scene_score=0.4), scene, {"scenes": [scene]})
    assert verdict["reject"] and "semantic_authority_rejected" in verdict["reasons"]


def test_portrait_beats_landscape_when_semantics_tie():
    scene = {"id": "s", "narration": "A coffee cup", "visual_intent": {"objects": ["coffee cup"]}}
    state = {"scenes": [scene]}
    wide = photo("wide", "Coffee cup on a table", width=1920, height=1080)
    tall = photo("tall", "Coffee cup on a table", width=1080, height=1920)
    ranked = judge_and_rank([(wide, verified()), (tall, verified())], scene, state)
    assert ranked[0][0].provider_id == "tall"
    assert ranked[0][1]["judge"]["scores"]["vertical_fit"] > ranked[1][1]["judge"]["scores"]["vertical_fit"]


def test_ranking_puts_factually_correct_ahead_of_prettier_wrong_asset():
    state = wall_state()
    scene = state["scenes"][0]
    wrong = photo("wrong", "Dramatic aerial close-up of a modern Berlin skyline", width=4000, height=6000)
    right = photo("right", "Berlin Wall 1961 barbed wire", width=1000, height=1300)
    rows = [(wrong, verified(scene_score=0.34, temporal_evidence={"required": True, "established": True})),
            (right, verified(scene_score=0.26, temporal_evidence={"required": True, "matched_years": [1961]}))]
    ranked = judge_and_rank(rows, scene, state)
    assert ranked[0][0].provider_id == "right"
    assert ranked[-1][1]["judge"]["reject"]


def test_ranking_is_deterministic_for_ties():
    scene = {"id": "s", "narration": "coffee"}
    rows = [(photo(str(i), "Coffee cup"), verified()) for i in range(4)]
    first = [row[0].provider_id for row in judge_and_rank(copy.deepcopy(rows), scene, {"scenes": [scene]})]
    again = [row[0].provider_id for row in judge_and_rank(copy.deepcopy(rows), scene, {"scenes": [scene]})]
    assert first == again == ["0", "1", "2", "3"]


# ---------------------------------------------------------------------------
# Continuity / diversity
# ---------------------------------------------------------------------------


def neighbour_state(previous_media, *, same_block=False, objects=("coffee cup",)):
    first = {"id": "a", "block_id": "b1", "media": previous_media, "visual_intent": {"objects": list(objects)}}
    second = {"id": "b", "block_id": "b1" if same_block else "b2", "narration": "coffee",
              "visual_intent": {"objects": ["espresso machine"]}}
    return {"scenes": [first, second]}, second


def test_same_asset_in_adjacent_unrelated_scene_is_an_unsafe_repeat():
    item = photo("x", "Espresso machine pouring coffee")
    state, scene = neighbour_state({**media.candidate_evidence(item)})
    verdict = judge_candidate(item, verified(), scene, state)
    assert verdict["scores"]["novelty"] == 0 and "unsafe_repeat" in verdict["reasons"]


def test_intentional_same_block_continuity_is_allowed():
    item = photo("x", "Espresso machine pouring coffee")
    state, scene = neighbour_state({**media.candidate_evidence(item)}, same_block=True)
    verdict = judge_candidate(item, verified(), scene, state)
    assert not verdict["reject"] and verdict["scores"]["continuity"] == 1.0


def test_near_identical_shot_from_same_shoot_is_penalised():
    previous = photo("1", "Espresso machine pouring coffee into a cup", creator="Ana")
    candidate = photo("2", "Espresso machine pouring coffee into a cup close", creator="Ana")
    fresh = photo("3", "Barista grinding coffee beans", creator="Ben")
    state, scene = neighbour_state(media.candidate_evidence(previous))
    ranked = judge_and_rank([(candidate, verified()), (fresh, verified())], scene, state)
    assert ranked[0][0].provider_id == "3"
    assert any("near_identical_shot" in note for note in ranked[1][1]["judge"]["diversity_notes"])


# ---------------------------------------------------------------------------
# Optional VLM judge: bounded, can veto, never resurrects
# ---------------------------------------------------------------------------


class FakeVision:
    name = "fake"

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, []

    def judge(self, image_url, context):
        self.calls.append(image_url)
        return dict(self.verdict)


def test_vision_judge_is_bounded_and_can_veto():
    scene = {"id": "s", "narration": "coffee", "visual_intent": {"objects": ["coffee cup"]}}
    rows = [(photo(str(i), "Coffee cup"), verified()) for i in range(5)]
    vision = FakeVision({"semantic_match": 9, "factual_match": 2, "visual_impact": 8, "vertical_fit": 8,
                         "watermark_or_text": True, "wrong_subject": False, "reject": False, "rationale": "logo"})
    budget = {"remaining": 10, "per_scene": 2}
    ranked = judge_and_rank(rows, scene, {"scenes": [scene]}, vision_judge=vision, vision_budget=budget)
    assert len(vision.calls) == 2 and budget["remaining"] == 8
    vetoed = [row for row in ranked if row[1]["judge"].get("vlm_status") == "judged"]
    assert len(vetoed) == 2 and all("vlm_watermark_or_text" in row[1]["judge"]["reasons"] for row in vetoed)
    assert not ranked[0][1]["judge"]["reject"]  # an unvetoed candidate now leads


def test_vision_verdict_cannot_resurrect_a_rejection():
    scene = {"id": "s", "narration": "coffee"}
    base = judge_candidate(photo("a", "street"), verified(confidence="rejected"), scene, {"scenes": [scene]})
    blended = apply_vision_verdict(base, {"semantic_match": 10, "factual_match": 10, "visual_impact": 10,
                                          "vertical_fit": 10, "reject": False})
    assert blended["reject"]


def test_vision_judge_disabled_by_default(tmp_path):
    assert visual_judge.get_vision_judge(director_settings(tmp_path)) is None
    assert visual_judge.get_vision_judge(director_settings(tmp_path, visual_judge_vlm_provider="openai")) is None  # no key


# ---------------------------------------------------------------------------
# AI fallback rules
# ---------------------------------------------------------------------------


def test_real_people_are_never_generated():
    scene = {"id": "s", "narration": "x", "visual_intent": {"objects": ["Marie Curie"], "factual_sensitivity": "real_person"}}
    assert generation_restriction(scene, {"scenes": [scene]})["allowed"] is False
    decision = ai_fallback_decision(scene, {"scenes": [scene]}, None, generation_available=True)
    assert decision["prefer"] is False and decision["reason"] == "real_person_never_generated"


def test_historical_events_are_generated_only_as_illustration():
    state = wall_state()
    assert generation_restriction(state["scenes"][0], state)["style"] == "illustrative_reconstruction"
    from clipforge import visual_director

    strategy = visual_director.plan_scene_strategy(state["scenes"][0], state, build_visual_query_plan(state["scenes"][0], state))
    prompt = visual_director.build_generation_prompt(state["scenes"][0], state, strategy)
    assert prompt["illustrative"] and "not a photograph" in prompt["prompt"]
    assert all(text.startswith("an illustration of") for text in prompt["verification_texts"])


@pytest.mark.parametrize("judge,sensitivity,available,prefer,reason", [
    ({"weak": False, "confidence": "high"}, "none", True, False, "real_media_strong_enough"),
    ({"weak": True, "confidence": "medium"}, "none", True, True, "low_retrieval_confidence"),
    ({"weak": True, "confidence": "medium"}, "none", False, False, "generation_unavailable"),
    ({"weak": True, "confidence": "medium"}, "historical_event", True, False, "factual_scene_prefers_real_evidence"),
    (None, "none", True, True, "no_factually_safe_real_visual"),
])
def test_ai_fallback_decision_table(judge, sensitivity, available, prefer, reason):
    scene = {"id": "s", "narration": "x", "visual_intent": {"objects": ["cup"], "factual_sensitivity": sensitivity}}
    decision = ai_fallback_decision(scene, {"scenes": [scene]}, judge, generation_available=available)
    assert (decision["prefer"], decision["reason"]) == (prefer, reason)


def coffee_project():
    scene = {"id": "scene_01", "block_id": "b1", "start": 0, "end": 4, "preferred_media": "photo",
             "narration": "Steam rises from a fresh cup of coffee.",
             "visual_intent": {"visual_goal": "steaming coffee cup", "objects": ["coffee cup"],
                               "actions": ["steam rising"], "media_queries": ["steaming coffee cup"],
                               "factual_sensitivity": "none"}}
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {},
            "intent": {"topic": "Why does coffee steam?"}}


WEAK_COFFEE = MediaCandidate(
    provider_id="weak", kind="photo", download_url="https://media.test/weak", source_url="https://www.pexels.test/photo/weak/",
    creator="T", creator_url=None, width=1080, height=1920, duration=None, query="steaming coffee cup", rank=1,
    provider="pexels", title="Coffee cup on a table", rights=TEST_REUSE_RIGHTS)


def test_weak_real_winner_of_non_factual_scene_tries_generation_first(tmp_path):
    state = coffee_project()
    generator = FakeGenerator()
    prepare_project_media(state, "p", director_settings(tmp_path), client=Provider(photos=[WEAK_COFFEE]),
                          fallback_client=Provider(), visual_verifier=Verifier({"weak": WEAK_PASS}),
                          image_generator=generator, extra_clients=[])
    scene = state["scenes"][0]
    assert scene["media_search"]["ai_fallback"]["prefer"] is True
    assert scene["media"]["source"] == "generated_openai"
    assert scene["media_search"]["ai_fallback"]["outcome"] == "generated_replaced_weak_real_media"


def test_weak_real_winner_is_kept_when_generation_fails(tmp_path):
    state = coffee_project()
    prepare_project_media(state, "p", director_settings(tmp_path), client=Provider(photos=[WEAK_COFFEE]),
                          fallback_client=Provider(), visual_verifier=Verifier({"weak": WEAK_PASS}),
                          image_generator=FakeGenerator(error="provider_error"), extra_clients=[])
    scene = state["scenes"][0]
    assert scene["media"]["provider_id"] == "weak"
    assert scene["media_search"]["ai_fallback"]["outcome"] == "kept_weak_real_media"
    assert scene["visual_director"]["decision_reason"] == "real_media_low_confidence_kept"


def test_strong_real_media_never_triggers_generation(tmp_path):
    state = coffee_project()
    strong = replace(WEAK_COFFEE, provider_id="strong", title="Steaming coffee cup close-up")
    generator = FakeGenerator()
    prepare_project_media(state, "p", director_settings(tmp_path), client=Provider(photos=[strong]),
                          fallback_client=Provider(), visual_verifier=Verifier({"strong": STRONG}),
                          image_generator=generator, extra_clients=[])
    assert state["scenes"][0]["media"]["provider_id"] == "strong" and not generator.prompts
    assert state["scenes"][0]["visual_transform"]["reframe"] == "native_vertical"
    assert state["visual_judge"]["vlm_calls"] == 0


# ---------------------------------------------------------------------------
# Source fallback / unavailable providers inside routed acquisition
# ---------------------------------------------------------------------------


def test_unavailable_primary_source_widens_to_the_next_source():
    query = "lunar spacecraft"
    good = cand("good", query, "Lunar spacecraft orbit", provider="wikimedia", kind="photo")

    class Down(Commons):
        provider = "nasa"

        def search_photos(self, query, *, portrait):
            raise MediaProviderError("network_error", "down")

    from clipforge.visual_providers import ProviderAdapter, ProviderCapabilities

    nasa = ProviderAdapter("nasa", Down())
    nasa.capabilities = ProviderCapabilities(("photo",), suitability=("space",))
    registry = ProviderRegistry([nasa, ProviderAdapter("wikimedia", Commons(photos={query: [good]}))])
    state = project({"visual_goal": query, "search_queries": [query], "preferred_media": "photo",
                     "visual_intent": {"objects": ["lunar spacecraft"], "media_queries": [query]}})
    scene = state["scenes"][0]
    plan = build_visual_query_plan(scene, state)
    result = media.run_staged_scene_search(
        plan["queries"], scene, state, plan, pexels=None, wikimedia=registry.get("wikimedia"), registry=registry,
        preferred_kind="photo", portrait=True, scene_duration=4, used=set(),
        verifier=Verifier({"good": STRONG}), acquisition_budget=AcquisitionBudget())
    providers = [row for stage in result.provenance["stages"] for row in stage["providers"]]
    assert any(row["provider"] == "nasa" and row.get("failure") == "network_error" for row in providers)
    assert result.ranked[0][0].provider_id == "good"
    assert result.provenance["routing"]["domain"] == "space"
    assert result.provenance["judge"]["evaluated"] >= 1


# ---------------------------------------------------------------------------
# Transformation plans
# ---------------------------------------------------------------------------


def transform_state(media_record, **scene_extra):
    scene = {"id": "s", "media": media_record, "visual_director": {"visual_role": scene_extra.pop("role", "")}, **scene_extra}
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene]}, scene


def test_transform_plans_follow_the_narration_and_source():
    archival = media.candidate_evidence(photo("a", "Street 1905", provider="loc", width=2000, height=1500))
    state, scene = transform_state(archival)
    plan = plan_transformation(scene, state)
    assert plan["reframe"] == "smart_reframe" and plan["motion_pattern"] == "push_in" and plan["valid"]

    wide = media.candidate_evidence(photo("w", "Mountain range", provider="pexels", width=3000, height=1200))
    state, scene = transform_state(wide)
    plan = plan_transformation(scene, state)
    assert plan["motion_pattern"] == "pan"
    outpaint = next(step for step in plan["steps"] if step["technique"] == "outpainting")
    assert outpaint["applied"] is False and outpaint["reason"] == "never_outpaint_real_evidence"

    diagram = media.candidate_evidence(photo("d", "Engine", provider="wikimedia", width=1200, height=1600))
    state, scene = transform_state(diagram, visual_intent={"visual_strategy": "diagram_or_card"})
    assert plan_transformation(scene, state)["motion_pattern"] == "static"

    video = {**media.candidate_evidence(photo("v", "Clip", provider="pexels", width=1920, height=1080)), "kind": "video"}
    state, scene = transform_state(video)
    tracking = next(step for step in plan_transformation(scene, state)["steps"] if step["technique"] == "subject_tracking")
    assert tracking["applied"] is False and tracking["fallback"] == "static_subject_focal_crop"


def test_low_resolution_reframe_is_an_invalid_plan():
    small = media.candidate_evidence(photo("s", "Small", provider="pexels", width=1000, height=520))
    state, scene = transform_state(small)
    plan = plan_transformation(scene, state)
    assert not plan["valid"] and "insufficient_resolution_after_reframe" in plan["failures"]


def test_renderer_honours_the_planned_motion_pattern():
    scene = {"media": {"source": "pexels"}, "visual_transform": {"motion_pattern": "pull_out"}}
    assert still_motion_plan(scene, 0, "slow_push", 0.5, 90)["type"] == "pull_out"
    scene["visual_transform"]["motion_pattern"] = "static"
    assert still_motion_plan(scene, 0, "slow_push", 0.5, 90)["type"] == "static"
    # Without a plan the existing alternation is unchanged.
    assert still_motion_plan({"media": {"source": "pexels"}}, 2, "slow_push", 0.5, 90)["type"] == "pull_out"


# ---------------------------------------------------------------------------
# Final quality gate
# ---------------------------------------------------------------------------


def cached_media(tmp_path, item, scene, state, *, judge):
    path = tmp_path / "p" / "assets" / item.provider / f"photo-{item.provider_id}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    record = media.candidate_evidence(item) | {"cache_path": path.relative_to(tmp_path).as_posix(),
                                               "relevance": {"confidence": "high", "selection_tier": 3, "judge": judge}}
    record["acceptance_scene_key"] = media.scene_acceptance_key(scene, state)
    return record


def test_gate_flags_rejected_judgement_and_repeats(tmp_path):
    state = coffee_project()
    scene = state["scenes"][0]
    item = replace(WEAK_COFFEE, title="Steaming coffee cup")
    scene["media"] = cached_media(tmp_path, item, scene, state, judge={
        "reject": True, "reasons": ["factual_below_floor"], "scores": {"semantic_match": 0.7, "factual_match": 0.1}})
    scene["asset_status"] = "photo_ready"
    gate = evaluate_scene(scene, state, director_settings(tmp_path))
    assert not gate["passed"] and {"judge_rejected", "factual_below_floor"} <= set(gate["failures"])
    assert needs_replacement(gate, scene)
    scene["user_locked_visual"] = True
    assert not needs_replacement(gate, scene)  # reported, never replaced

    later = {**copy.deepcopy(scene), "id": "scene_02", "block_id": "b2", "user_locked_visual": False}
    later["media"]["relevance"]["judge"] = {"reject": False, "reasons": [], "scores": {"semantic_match": 0.8, "factual_match": 1}}
    state["scenes"].append(later)
    assert "unsafe_repeat" in evaluate_scene(later, state, director_settings(tmp_path))["failures"]


def test_render_admission_replaces_a_gate_failure_with_a_permitted_fallback(tmp_path):
    state = coffee_project()
    scene = state["scenes"][0]
    settings = director_settings(tmp_path)
    scene["media"] = cached_media(tmp_path, replace(WEAK_COFFEE, title="Steaming coffee cup"), scene, state, judge={
        "reject": True, "reasons": ["semantic_below_floor"], "scores": {"semantic_match": 0.2, "factual_match": 1}})
    scene["asset_status"] = "photo_ready"
    complete_project_visuals(state, "p", settings, image_generator=FakeGenerator(), visual_verifier=Verifier())
    assert scene["media"]["source"] == "generated_openai"
    assert scene["fallback_completion"]["reason"].startswith("visual_quality_gate:")
    assert state["visual_quality_gate"]["passed"] == 1


def test_render_admission_keeps_the_visual_when_no_fallback_exists(tmp_path):
    state = coffee_project()
    scene = state["scenes"][0]
    settings = director_settings(tmp_path, generated_image_fallback_enabled=False)
    scene["media"] = cached_media(tmp_path, replace(WEAK_COFFEE, title="Steaming coffee cup"), scene, state, judge={
        "reject": True, "reasons": ["semantic_below_floor"], "scores": {"semantic_match": 0.2, "factual_match": 1}})
    scene["asset_status"] = "photo_ready"
    complete_project_visuals(state, "p", settings, image_generator=None, visual_verifier=Verifier())
    assert scene["media"]["provider_id"] == "weak"
    assert scene["fallback_completion"]["status"] == "kept_failing_quality_gate"
    assert state["assets"]["missing_media_count"] == 0


def test_signals_expose_the_plan_inputs():
    state = wall_state()
    signals = scene_signals(state["scenes"][0], state)
    assert signals["entities"][0] == "Berlin Wall" and signals["period"] == "1961"
    assert signals["location"] == "Berlin" and signals["sensitivity"] == "historical_event"


def test_no_network_in_this_module(monkeypatch):
    # Guard: the suite-wide fixtures already forbid real open-media clients.
    from clipforge import open_media

    client = open_media.HTTP_CLIENT_FACTORY()
    response = client.get("https://api.openverse.org/v1/images/")
    assert response.status_code == 200 and response.json()["results"] == []
    assert isinstance(Path(__file__), Path)


def test_continuation_of_the_same_base_never_repeats_the_move():
    base = media.candidate_evidence(photo("b", "Wrinkled skin", provider="pexels", width=1080, height=1920))
    first = {"id": "a", "block_id": "b1", "media": base, "visual_director": {"visual_role": "final_payoff"}}
    second = {"id": "b", "block_id": "b1", "media": dict(base), "visual_director": {"visual_role": "final_payoff"},
              "asset_status": "block_visual_continued"}
    state = {"timeline": {"width": 1080, "height": 1920}, "scenes": [first, second]}
    first["visual_transform"] = plan_transformation(first, state)
    second_plan = plan_transformation(second, state)
    assert first["visual_transform"]["motion_pattern"] == "pull_out"
    assert second_plan["motion_pattern"] == "push_in"


def test_lone_capitalised_words_are_not_inferred_as_named_entities():
    scene = {"id": "s", "narration": "x", "visual_intent": {
        "visual_goal": "Close-up of Hand opening Fridge", "objects": ["Hand on Fridge door", "Apollo 11 capsule"]}}
    entities = scene_signals(scene, {"scenes": [scene]})["entities"]
    assert "Hand" not in entities and "Fridge" not in entities
    assert "Apollo 11" in entities
