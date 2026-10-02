"""Replay the observed acceptance mechanism without topic/asset-specific rules.

The real trace combined one incidental time word, planned-query provenance,
and a passing scene score despite a weak subject score. The second selection
had rejected metadata but a relaxed verifier path still admitted it.
"""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, project, settings_for

from clipforge import media
from clipforge.pipeline import _build_scenes
from clipforge.visual_providers import AcquisitionBudget
from clipforge.visual_verifier import VisualVerification


def material_project():
    return project(
        {
            "narration": "It can take years for recycled rubber to break down.",
            "visual_goal": "years for recycled rubber to break down",
            "visual_intent": {
                "visual_goal": "years for recycled rubber to break down",
                "objects": ["years", "recycled", "rubber"],
                "media_queries": ["years recycled rubber breakdown"],
            },
            "search_queries": ["years recycled rubber breakdown"],
            "preferred_media": "photo",
            "block_id": "material",
        },
        intent={"topic": "recycled rubber", "visual_subject": "recycled rubber"},
    )


def incidental_candidate():
    return cand("incidental", "years recycled rubber breakdown", "City street beside a Summer Years concert banner", kind="photo")


class IncidentalVerifier(Verifier):
    def verify_candidate(self, candidate, texts):
        self.calls.append(candidate.provider_id)
        return VisualVerification(0.2845, "verified", "provider_thumbnail", subject_score=0.163,
                                  scene_score=0.306, frame_count=1)


@pytest.mark.parametrize("planned", [True, False])
@pytest.mark.parametrize("topic_available", [True, False])
def test_one_incidental_metadata_word_is_not_corroborated_by_query(planned, topic_available):
    state = material_project()
    if not topic_available:
        state.pop("intent")
    candidate = incidental_candidate()
    if not planned:
        candidate = replace(candidate, query="city street")
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    assert relevance["scene_matches"] == ["year"]
    assert not relevance["subject_matches"]
    assert relevance["query_provenance"] is planned
    assert relevance["confidence"] == "rejected"
    relevance["visual"] = {"status": "verified", "score": 0.2845, "scene_score": 0.306, "subject_score": 0.163}
    assert media.real_media_quality_gate(candidate, relevance) == (False, "semantic_mismatch")


def test_provenance_without_metadata_or_vision_cannot_grant_acceptance():
    state = material_project()
    candidate = replace(incidental_candidate(), title="")
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    assert relevance["query_provenance"]
    assert not media.real_media_quality_gate(candidate, relevance)[0]


def test_rejected_metadata_cannot_enter_through_relaxed_verification():
    state = material_project()
    candidate = replace(incidental_candidate(), title="Person beside a city advertisement")
    scene = state["scenes"][0]
    verifier = IncidentalVerifier()
    budget = AcquisitionBudget()
    verdict, _score = media._relaxed_visual_verdict(candidate, None, verifier, scene, state, budget)
    assert verdict == "rejected"
    assert not verifier.calls


@pytest.mark.parametrize("availability", ["available", "unavailable"])
def test_no_better_candidate_uses_existing_director_fallback(tmp_path, monkeypatch, availability):
    from clipforge import visual_director

    state = material_project()
    candidate = incidental_candidate()
    verifier = IncidentalVerifier() if availability == "available" else SimpleNamespace(status="unavailable")
    provider = Provider(photos={candidate.query: [candidate]})
    calls = []
    original = visual_director.resolve_scene_fallback

    def fallback(*args, **kwargs):
        calls.append(kwargs.get("phase", "before_reuse"))
        return original(*args, **kwargs)

    monkeypatch.setattr(visual_director, "resolve_scene_fallback", fallback)
    media.prepare_project_media(state, "project", settings_for(tmp_path), client=provider,
                                fallback_client=Commons(), visual_verifier=verifier)
    scene = state["scenes"][0]
    assert scene.get("media", {}).get("provider_id") != candidate.provider_id
    assert scene["visual_director"]["decision"] != visual_director.ACCEPTED_REAL
    assert calls
    assert scene["media_search"]["logical_queries_executed"] <= 3


def test_loose_topic_metadata_cannot_justify_a_different_factual_beat():
    state = material_project()
    candidate = cand("rubber", "rubber sheets", "Recycled rubber sheets", kind="photo")
    asset = media.candidate_evidence(candidate)
    asset["cache_path"] = "rubber.jpg"
    asset["acceptance_scene_key"] = media.scene_acceptance_key(state["scenes"][0], state)
    asset["relevance"] = {"visual": {"status": "verified", "score": 0.31, "scene_score": 0.31}}
    destination = {"id": "new-beat", "narration": "A factory worker measures temperature and moisture.",
                   "visual_goal": "worker measures temperature moisture", "start": 4, "end": 8,
                   "visual_intent": {"objects": ["worker", "thermometer"], "actions": ["measuring"],
                                     "media_queries": ["worker measuring temperature moisture"]}}
    assert not media.destination_asset_allowed(asset, destination, state, reuse=True)


def test_fitting_same_block_continuity_remains_available():
    state = material_project()
    scene = state["scenes"][0]
    candidate = cand("fitting", "years recycled rubber breakdown", "Recycled rubber breakdown material", kind="photo")
    asset = {**media.candidate_evidence(candidate), "cache_path": "fitting.jpg"}
    destination = copy.deepcopy(scene)
    destination.update(id="continuation", narration="The material remains intact for a long time.")
    assert media.destination_asset_allowed(asset, destination, state, reuse=True)


def test_genuinely_relevant_generic_lifestyle_media_still_passes():
    scene = {"narration": "Walking outdoors helps you unwind.", "visual_goal": "person walking outdoors",
             "visual_intent": {"objects": ["person"], "actions": ["walking"], "context": ["outdoors"],
                               "media_queries": ["person walking outdoors"]},
             "search_queries": ["person walking outdoors"]}
    state = project(scene, intent={"topic": "stress and recovery"})
    candidate = cand("lifestyle", "person walking outdoors", "A person walking outdoors in a park")
    relevance = media.media_relevance(candidate, scene, state)
    assert media.real_media_quality_gate(candidate, relevance)[0]
    relevance["visual"] = {"status": "verified", "score": 0.12, "scene_score": 0.12}
    assert media.real_media_quality_gate(candidate, relevance) == (False, "visual_rejected")


def test_concise_local_subject_is_not_an_incidental_keyword():
    scene = {"narration": "There it stands.", "visual_goal": "lighthouse"}
    state = project(scene, intent={"topic": "navigation"})
    candidate = cand("local", "coast", "Lighthouse above the cliffs")
    relevance = media.media_relevance(candidate, scene, state)
    assert relevance["scene_matches"] == ["lighthouse"]
    assert media.real_media_quality_gate(candidate, relevance)[0]


def test_relaxed_search_persists_the_actual_verification_and_acceptance(tmp_path):
    state = project({"narration": "Recycled rubber decay", "visual_goal": "Recycled rubber decay",
                     "visual_intent": {"media_queries": ["recycled rubber decay"]},
                     "preferred_media": "photo"})
    candidate = cand("fitting", "rubber", "Recycled rubber breaking down", kind="photo")

    class BroadProvider(Provider):
        def search_photos(self, query, **kwargs):
            self.calls.append(("photo", query))
            return [replace(candidate, query=query)] if query == "recycled rubber" else []

    media.prepare_project_media(state, "project", settings_for(tmp_path), client=BroadProvider(),
                                fallback_client=Commons(), visual_verifier=Verifier())
    scene = state["scenes"][0]
    assert scene["media"]["provider_id"] == "fitting"
    relevance = scene["media"]["relevance"]
    assert relevance["fallback_stage"] == "real_media_only_relaxed_fit"
    assert relevance["visual"]["status"] == "verified"
    assert relevance["visual"]["scene_score"] == 0.31
    assert relevance["acceptance"]["accepted"] is True


def test_acquisition_reserves_assets_of_not_yet_visited_cached_scenes(tmp_path):
    state = material_project()
    first = state["scenes"][0]
    candidate = cand("reserved", first["search_queries"][0], "Recycled rubber breaking down", kind="photo")
    owner = copy.deepcopy(first)
    owner.update(id="owner", block_id="another-fact")
    owner["media"] = {**media.candidate_evidence(candidate), "cache_path": "owner.jpg"}
    owner["asset_status"] = "photo_ready"
    (tmp_path / "owner.jpg").write_bytes(b"existing")
    state["scenes"].append(owner)
    provider = Provider(photos={candidate.query: [candidate]})
    # no_reuse makes this a fresh-selection test, independently of later reuse.
    first["media_repair"] = {"no_reuse": True}
    media.prepare_project_media(state, "project", settings_for(tmp_path), client=provider,
                                fallback_client=Commons(), visual_verifier=Verifier())
    assert first.get("media", {}).get("identity") != candidate.identity
    assert owner["media"]["identity"] == candidate.identity


def test_retiming_preserves_search_and_candidate_evidence():
    scene = material_project()["scenes"][0]
    scene["media"] = {**media.candidate_evidence(incidental_candidate()), "cache_path": "trace.jpg"}
    scene["media_search"] = {"executed_queries": ["planned"], "quality_gate_rejections": {"semantic_mismatch": 1}}
    scene["visual_query_plan"] = {"primary_subjects": ["recycled rubber"]}
    rebuilt = _build_scenes([{"id": scene["block_id"], "text": scene["narration"], "role": "detail"}], 4, [scene])
    assert rebuilt[0]["media_search"] == scene["media_search"]
    assert rebuilt[0]["visual_query_plan"] == scene["visual_query_plan"]
    assert rebuilt[0]["media"]["rights"] == scene["media"]["rights"]


def test_rejected_cached_selection_cannot_gain_acceptance_from_file_existence(tmp_path):
    state = material_project()
    scene = state["scenes"][0]
    candidate = incidental_candidate()
    scene["media"] = {**media.candidate_evidence(candidate), "cache_path": "old.jpg",
                      "acceptance_scene_key": media.scene_acceptance_key(scene, state),
                      "relevance": {"visual": {"status": "verified", "score": 0.2845, "scene_score": 0.306}}}
    scene["asset_status"] = "photo_ready"
    (tmp_path / "old.jpg").write_bytes(b"cached")
    media.prepare_project_media(state, "project", settings_for(tmp_path), client=Provider(),
                                fallback_client=Commons(), visual_verifier=Verifier())
    assert scene.get("media", {}).get("identity") != candidate.identity


def test_renderer_cannot_admit_a_persisted_rejected_relaxed_selection(tmp_path):
    from clipforge.renderer import RenderUnavailable, _create_visual_segment

    state = material_project()
    state["timeline"]["fps"] = 30
    scene = state["scenes"][0]
    scene["media"] = {**media.candidate_evidence(incidental_candidate()), "cache_path": "rejected.jpg",
                      "relevance": {"confidence": "rejected", "fallback_stage": "real_media_only_relaxed_fit"}}
    scene["asset_status"] = "photo_ready"
    (tmp_path / "rejected.jpg").write_bytes(b"cached")
    with pytest.raises(RenderUnavailable, match="No real scene media"):
        _create_visual_segment("unused-ffmpeg", state, scene, 0, 4, tmp_path, settings_for(tmp_path))


@pytest.mark.parametrize("same_block", [True, False])
def test_critic_overlay_changes_do_not_excuse_unrelated_fact_repetition(tmp_path, same_block):
    from clipforge import final_critic

    first = {"id": "first", "block_id": "first-fact", "story_unit_ids": ["fact-a"]}
    second = {"id": "second", "block_id": "first-fact" if same_block else "another-fact",
              "story_unit_ids": ["fact-a"] if same_block else ["fact-b"]}
    review = final_critic._Review({"scenes": [first, second]}, project_id="project", revision=2,
                                settings=settings_for(tmp_path), verifier=None, pass_index=0, vision_critic=None)
    for number, sc in enumerate((first, second), 1):
        row = final_critic._Row({"scene_id": sc["id"], "block_id": sc["block_id"],
                                "media": {"identity": "pexels:photo:shared", "kind": "photo", "source": "pexels"},
                                "overlays": [{"spec": {"text": f"Distinct claim {number}"}}]}, sc, number)
        row.dimensions["semantic_match"] = {"rating": "good"}
        review.rows.append(row)
    review._adjacent()
    repeated = review.rows[1]
    assert repeated.dimensions["repetition"]["rating"] == ("good" if same_block else "poor")
    assert ("accidental_repeat" in {issue["code"] for issue in repeated.issues}) is not same_block
