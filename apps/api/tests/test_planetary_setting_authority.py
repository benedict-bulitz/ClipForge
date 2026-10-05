"""Setting contradictions survive selection, saved-state replay and repair.

The real assets had strong positive scores; one lacked setting metadata while
another explicitly described open water in its source slug. No live APIs/models.
"""
import copy
import json
from dataclasses import replace

import pytest
from PIL import Image
from test_planetary_setting_regression import environment_project
from test_staged_media_search import cand
from test_visual_director import FakeGenerator, settings_for
from test_visual_director import Provider as PoolProvider
from test_visual_director import Verifier as FallbackVerifier

from clipforge import final_critic, media, renderer, visual_director
from clipforge.visual_context import environment_requirement, planetary_setting_evidence
from clipforge.visual_providers import AcquisitionBudget
from clipforge.visual_verifier import OpenClipVisualVerifier, VisualVerification, visual_intent_text


def asset(caption="Venus atmosphere dust over a red rocky landscape"):
    return cand("landscape", "Venus atmosphere dust", caption, kind="photo")


def conflict(scene, state):
    return {"requirement": environment_requirement(scene, state), "mismatch": True,
            "source": "openclip_setting_contrast", "markers": ["built_environment"],
            "witnesses": [{"view": "right", "score": .34, "required_score": .18}]}


class ContradictionVerifier(FallbackVerifier):
    def __init__(self, evidence):
        super().__init__(default=(.99, .99))
        self.evidence = evidence
        self.local_calls = 0

    def verify_candidate(self, candidate, texts):
        assert texts.required_environment
        return VisualVerification(.99, "verified", scene_score=.99, setting_evidence=self.evidence)

    def verify_local_image(self, path, texts, *, asset_identity=None):
        self.local_calls += 1
        return VisualVerification(.99, "verified", scene_score=.99, setting_evidence=self.evidence)

    def score_video_frames(self, images, texts, *, asset_identity=None):
        return VisualVerification(.99, "verified", scene_score=.99, setting_evidence=self.evidence)


@pytest.mark.parametrize("caption,marker", [
    ("Red dusty landscape with a city skyline and buildings on the horizon", "built_environment"),
    ("Orange sunset over the ocean and waves", "open_water"),
    ("Red rocky landscape in Namibia", "earth_geography"),
    ("Red dust around scrub and trees", "vegetation"),
    ("Planetary red sky above a paved highway", "road_transport"),
])
def test_explicit_conflict_beats_positive_topic_colour_and_query(caption, marker):
    state = environment_project(); scene = state["scenes"][0]
    item = replace(asset(caption), rank=999999)
    evidence = media.media_relevance(item, scene, state)
    assert marker in evidence["setting_evidence"]["markers"]
    evidence["visual"] = {"status": "verified", "scene_score": .99}
    assert not media.real_media_quality_gate(item, evidence, "pass", .99)[0]


def test_ocean_source_slug_is_not_lost_when_provider_caption_is_empty():
    state = environment_project(); scene = state["scenes"][0]
    item = replace(asset(""), source_url="https://www.pexels.com/video/serene-ocean-sunset-with-vibrant-sky-98765/")
    assert media.media_relevance(item, scene, state)["setting_evidence"]["markers"] == ["open_water"]


@pytest.mark.parametrize("caption", [
    "Red desert and clouds at sunset", "Rocky surface under blue sky",
    "Venus surface imaged by a planetary mission", "Rocky landscape without buildings",
])
def test_ambiguous_or_valid_environment_is_not_globally_banned(caption):
    state = environment_project()
    assert not planetary_setting_evidence(state["scenes"][0], state, caption)["mismatch"]


def test_constraint_survives_serialization_and_generated_prompt_wrapping():
    state = environment_project(); scene = state["scenes"][0]
    required = visual_intent_text(scene, state).required_environment
    reopened = json.loads(json.dumps(state))
    assert reopened["scenes"][0]["required_environment"] == required
    assert visual_director._verification_texts(reopened["scenes"][0], reopened, ["red rocks"]).required_environment == required
    scene["visual_goal"] = "Earth landscape comparison"
    scene["visual_intent"] = {"objects": ["Earth landscape"]}
    assert environment_requirement(scene, state) is None
    assert "required_environment" not in scene


def test_vision_conflict_cannot_be_rescued_by_relaxed_or_loose_scalar():
    state = environment_project(); scene = state["scenes"][0]
    verifier = ContradictionVerifier(conflict(scene, state))
    rows = media.verify_media_shortlist([asset()], scene, state, verifier)
    assert rows[0][1]["visual"]["setting_evidence"]["mismatch"]
    assert not media.real_media_quality_gate(*rows[0], "pass", .99)[0]
    assert media._relaxed_visual_verdict(asset(), None, verifier, scene, state)[0] == "rejected"


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("generated", [False, True])
def test_persisted_conflict_blocks_replay_reuse_and_generated_provenance(reuse, generated):
    state = environment_project(); scene = state["scenes"][0]
    stored = media.candidate_evidence(asset())
    if generated:
        stored.update(provider="generated_openai", source="generated_openai", ai_generated=True,
                      identity="generated_openai:photo:landscape", cache_path="generated.png",
                      generation={"model": "gpt-image-1", "verification": {"accepted": True}})
    stored.update(acceptance_scene_key=media.scene_acceptance_key(scene, state),
                  relevance={"confidence": "high", "visual": {"status": "verified", "scene_score": .99,
                                                             "setting_evidence": conflict(scene, state)}})
    assert media.is_scene_asset_allowed(stored)
    clean = copy.deepcopy(stored)
    clean["relevance"]["visual"].pop("setting_evidence")
    assert media.destination_asset_allowed(clean, scene, state, reuse=reuse)
    assert not media.destination_asset_allowed(json.loads(json.dumps(stored)), scene, state, reuse=reuse)


def test_legacy_cache_is_locally_checked_within_shared_budget(tmp_path):
    state = environment_project(); scene = state["scenes"][0]; settings = settings_for(tmp_path)
    settings.render_root.mkdir(parents=True, exist_ok=True)
    path = settings.render_root / "legacy.png"; Image.new("RGB", (100, 200)).save(path)
    stored = {**media.candidate_evidence(asset()), "cache_path": "legacy.png"}
    verifier = ContradictionVerifier(conflict(scene, state)); budget = AcquisitionBudget(max_verifications=1)
    assert not media.destination_asset_allowed(stored, scene, state, settings=settings, verifier=verifier, acquisition_budget=budget)
    assert budget.verifications == 1 and verifier.local_calls == 1
    assert not media.destination_asset_allowed(stored, scene, state, settings=settings, verifier=verifier, acquisition_budget=budget)
    assert verifier.local_calls == 1


def test_generated_verification_cannot_accept_high_score_with_conflict(tmp_path):
    state = environment_project(); scene = state["scenes"][0]
    result = visual_director._verify_generated(tmp_path / "generated.png", scene, state, ContradictionVerifier(conflict(scene, state)))
    assert result["verified"] and not result["accepted"] and result["setting_evidence"]["mismatch"]


def test_setting_rejection_uses_existing_ai_fallback(tmp_path):
    state = environment_project(); generator = FakeGenerator()
    media.prepare_project_media(state, "project", settings_for(tmp_path),
                                client=PoolProvider(photos=[asset("Red sky above an ocean")]),
                                fallback_client=PoolProvider(photos=[]), extra_clients=[],
                                visual_verifier=FallbackVerifier(default=(.99, .99)), image_generator=generator)
    assert state["scenes"][0]["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1
    assert "generated_card_count" not in state["assets"]


@pytest.mark.parametrize("metadata", [False, True])
def test_final_critic_cannot_approve_known_contradiction_or_cross_reuse(tmp_path, metadata):
    state = environment_project(); scene = state["scenes"][0]
    scene["media"] = media.candidate_evidence(asset("Red ocean sunset" if metadata else "Venus red dusty landscape"))
    verifier = ContradictionVerifier(None if metadata else conflict(scene, state))
    review = final_critic._Review(state, project_id="project", revision=1, settings=settings_for(tmp_path),
                                 verifier=verifier, pass_index=0, vision_critic=None)
    row = final_critic._Row({"scene_id": scene["id"], "media": scene["media"]}, scene, 1)
    row.images = [Image.new("RGB", (100, 200))]
    review._semantic(row)
    assert row.dimensions["semantic_match"]["rating"] == final_critic.POOR
    assert row.dimensions["semantic_match"]["source"] == "setting_authority"
    assert review.cross_score(row, row) == 0.0
    assert not media.destination_asset_allowed(scene["media"], scene, state)


def test_fresh_renderer_path_cannot_skip_known_setting_conflict(tmp_path, monkeypatch):
    state = environment_project(); scene = state["scenes"][0]
    state["timeline"].update(width=360, height=640, fps=30)
    scene["media"] = media.candidate_evidence(asset("Red ocean sunset"))
    monkeypatch.setattr(renderer, "_scene_media_path", lambda *_: (tmp_path / "source.mp4", "video"))
    with pytest.raises(renderer.RenderUnavailable, match="No real scene media"):
        renderer._create_visual_segment("unused", state, scene, 0, 4, tmp_path, settings_for(tmp_path))


def test_bounded_spatial_contrast_detects_background_without_second_model(monkeypatch):
    state = environment_project(); scene = state["scenes"][0]
    verifier = OpenClipVisualVerifier(); calls = []
    def score(image, texts, *, asset_identity):
        calls.append(asset_identity)
        if asset_identity.endswith(":setting:right") and "city" in texts[0]:
            return .31
        return .20 if "planet" in texts[0] else .16
    monkeypatch.setattr(verifier, "score_image", score)
    evidence = verifier._setting_evidence(Image.new("RGB", (360, 640)), visual_intent_text(scene, state), asset_identity="test")
    assert evidence["mismatch"] and evidence["markers"] == ["built_environment"]
    assert len(set(calls)) == 4
    assert len(calls) == 20
    assert verifier._setting_evidence(Image.new("RGB", (360, 640)), ["red sky"], asset_identity="none") is None


def test_video_requires_persistent_contradiction_and_ambiguity_is_not_rejection():
    state = environment_project(); scene = state["scenes"][0]
    bad = conflict(scene, state); good = {**bad, "mismatch": False, "markers": [], "witnesses": []}
    assert not OpenClipVisualVerifier._setting_consensus([bad, good, good])["mismatch"]
    assert OpenClipVisualVerifier._setting_consensus([bad, bad, good])["mismatch"]


def test_distinct_setting_conflict_never_enters_novelty_pool():
    state = environment_project(); scene = state["scenes"][0]
    good = asset(); bad = replace(asset("Red dusty city skyline"), provider_id="new")
    state["scenes"].insert(0, {"id": "prior", "media": media.candidate_evidence(good)})
    verifier = FallbackVerifier(default=(.31, .31))
    rows = media.verify_media_shortlist([good, bad], scene, state, verifier)
    assert [candidate.provider_id for candidate, _ in rows] == [good.provider_id]
    assert "new" not in verifier.calls


def test_intent_edit_does_not_inherit_unrelated_setting_verdict():
    state = environment_project(); scene = state["scenes"][0]
    stored = {**media.candidate_evidence(asset()), "setting_evidence": conflict(scene, state)}
    other = copy.deepcopy(scene)
    other.update(narration="Earth desert sunset", visual_goal="Earth desert sunset", visual_intent={"objects": ["Earth desert sunset"]})
    assert media.asset_setting_conflict(stored, other, state) is None


def test_atmospheric_waves_are_not_open_water_evidence():
    state = environment_project()
    assert not planetary_setting_evidence(state["scenes"][0], state, "Atmospheric waves in a rocky planet's dusty sky")["mismatch"]


def test_render_admission_finishes_fallback_after_invalidating_saved_ocean(tmp_path):
    state = environment_project(); scene = state["scenes"][0]; settings = settings_for(tmp_path)
    settings.render_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 640)).save(settings.render_root / "old.png")
    scene["media"] = {**media.candidate_evidence(asset("Red sky above an ocean")), "cache_path": "old.png"}
    scene["asset_status"] = "photo_ready"
    generator = FakeGenerator()
    media.complete_project_visuals(state, "project", settings, image_generator=generator,
                                   visual_verifier=FallbackVerifier(default=(.31, .31)))
    assert scene["media"]["source"] == "generated_openai"
    assert scene["fallback_completion"]["status"] == "selected"
    assert len(generator.prompts) == 1
    assert state["assets"]["missing_media_count"] == 0
