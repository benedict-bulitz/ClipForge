"""Final admission finishes the existing chain; stock rejection never becomes permission.

The observed run had five scenes, one stock file, three accepted generation
sidecars, and failed after encoding scene one. Its transient scene state was
not retained. These fixtures reproduce the admission gap generically rather
than inventing an unrecorded provider/verifier verdict for that run.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from test_visual_director import (
    BOOK,
    BUS,
    FINGER_SCENES,
    HAND,
    FakeGenerator,
    Verifier,
    finger_project,
    run,
    settings_for,
)

from clipforge import image_generation, media, renderer, visual_director
from clipforge.pipeline import _build_scenes
from clipforge.visual_providers import AcquisitionBudget


def admit(state, tmp_path, generator=None, verifier=None):
    media.complete_project_visuals(state, "project", settings_for(tmp_path),
                                  image_generator=generator, visual_verifier=verifier or Verifier())


def test_all_stock_rejected_still_generates_verified_image_and_renders(tmp_path, monkeypatch):
    state = finger_project(FINGER_SCENES[:1])
    state.update(render={"status": "planned"}, script={**state["script"], "text": "A physical material stretches."})
    # A missing admission source with its intended chain, as in a failed render.
    run(state, tmp_path, photos=[BOOK, BUS], generator=None)
    scene = state["scenes"][0]
    assert scene["asset_status"] == "real_media_unavailable"
    assert scene["media_search"]["relaxed_fallback"]
    generator, verifier = FakeGenerator(), Verifier()
    monkeypatch.setattr(image_generation, "get_image_generator", lambda _settings: generator)
    monkeypatch.setattr(media, "get_visual_verifier", lambda: verifier)
    monkeypatch.setattr(renderer, "ffmpeg_path", lambda: "ffmpeg")

    def encode(state, *args, **kwargs):
        assert renderer._scene_media_path(scene, settings_for(tmp_path))[0]
        assert scene["media"]["source"] == "generated_openai"
        return "encoded"

    monkeypatch.setattr(renderer, "_render_video", encode)
    assert renderer.render_video(state, "project", 2, settings_for(tmp_path)) == "encoded"
    assert len(generator.prompts) == 1
    assert verifier.local_calls
    assert scene["fallback_completion"]["resolved_type"] == visual_director.GENERATED_IMAGE
    assert "generated_card_count" not in state["assets"]
    evidence = json.loads((tmp_path / "project/diagnostics/visual-acquisition.json").read_text())
    assert evidence["scenes"][0]["media"]["generation"]["verification"]["accepted"]
    assert evidence["scenes"][0]["media_search"]["logical_queries_executed"] <= 3


def test_stock_rejection_is_unchanged_even_with_a_high_visual_scalar(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    candidate = copy.copy(BUS)
    relevance = media.media_relevance(candidate, state["scenes"][0], state)
    relevance["visual"] = {"status": "verified", "scene_score": .9}
    assert media.real_media_quality_gate(candidate, relevance) == (False, "semantic_mismatch")
    run(state, tmp_path, photos=[BOOK, BUS], generator=FakeGenerator())
    assert state["scenes"][0]["media"]["source"] == "generated_openai"


def test_generation_failure_continues_to_permitted_graphic_without_text_card(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    scene = state["scenes"][0]
    scene["visual_director"] = {
        "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"],
        "graphic": {"kind": "process", "steps": ["Water cools", "Droplets form"]}, "reveal_allowed": True,
    }
    generator = FakeGenerator(error="timeout")
    admit(state, tmp_path, generator)
    assert len(generator.prompts) == 1
    assert scene["asset_status"] == "graphic_ready"
    assert scene["media"]["source"] == "simple_graphic"
    assert scene["media"]["graphic"]["steps"] == ["Water cools", "Droplets form"]
    assert media.cached_scene_asset_path(scene["media"], settings_for(tmp_path))


def test_generation_rejection_uses_next_fallback_and_does_not_retry(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    scene = state["scenes"][0]
    scene["visual_director"] = {
        "fallback_chain": ["real_media", "generated_image", "reuse_previous_visual", "simple_graphic"],
        "graphic": {"kind": "process", "steps": ["Water cools", "Droplets form"]}, "reveal_allowed": True,
    }
    generator = FakeGenerator()
    admit(state, tmp_path, generator, Verifier(generated=(.12, .12)))
    admit(state, tmp_path, generator)
    assert len(generator.prompts) == 1
    assert scene["asset_status"] == "graphic_ready"
    assert state["visual_director"]["generations"][0]["status"] == "rejected"


def test_renderer_fails_only_after_permitted_fallbacks_exhausted_and_saves_evidence(tmp_path, monkeypatch):
    state = finger_project(FINGER_SCENES[:1])
    state["timeline"]["fps"] = 30
    state.update(render={"status": "planned"}, script={**state["script"], "text": "A physical material stretches."})
    generator = FakeGenerator(error="timeout")
    monkeypatch.setattr(image_generation, "get_image_generator", lambda _settings: generator)
    monkeypatch.setattr(media, "get_visual_verifier", lambda: Verifier())
    monkeypatch.setattr(renderer, "ffmpeg_path", lambda: "ffmpeg")

    def encode(state, *args, **kwargs):
        renderer._create_visual_segment("ffmpeg", state, state["scenes"][0], 0, 3, tmp_path, settings_for(tmp_path))

    monkeypatch.setattr(renderer, "_render_video", encode)
    with pytest.raises(renderer.RenderUnavailable, match="No real scene media"):
        renderer.render_video(state, "project", 2, settings_for(tmp_path))
    scene = state["scenes"][0]
    assert len(generator.prompts) == 1
    assert scene["fallback_completion"]["status"] == "exhausted"
    assert not scene.get("media")
    with pytest.raises(renderer.RenderUnavailable, match="cards are disabled"):
        renderer._draw_scene(state, scene, 0, tmp_path)
    evidence = json.loads((tmp_path / "project/diagnostics/visual-acquisition.json").read_text())
    assert evidence["status"] == "render_failed"
    assert evidence["visual_director"]["generations"][0]["error"] == "timeout"


def test_paid_budget_and_scene_attempt_limits_survive_render_boundary(tmp_path):
    state = finger_project()
    generator = FakeGenerator()
    run(state, tmp_path, photos=[BOOK], generator=generator)
    for scene in state["scenes"]:
        scene.pop("media", None)
        scene["asset_status"] = "real_media_unavailable"
    admit(state, tmp_path, generator)
    assert len(generator.prompts) == 3
    assert visual_director.generation_counts(state)["auto_generated_images"] == 3
    assert all(scene["visual_director"]["generation"]["status"] == "project_budget_exhausted" for scene in state["scenes"])


def test_acquisition_outage_circuit_breaker_survives_render_boundary(tmp_path):
    state, generator = finger_project(), FakeGenerator(error="timeout")
    run(state, tmp_path, generator=generator)
    admit(state, tmp_path, generator)
    assert len(generator.prompts) == 1


def test_lost_stock_cache_finishes_fallback_and_clears_inconsistent_status(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    state["scenes"][0].update(asset_status="video_ready", media={
        **media.candidate_evidence(BUS), "cache_path": "missing.mp4",
        "relevance": {"confidence": "rejected"},
    })
    admit(state, tmp_path, FakeGenerator())
    scene = state["scenes"][0]
    assert scene["asset_status"] == "generated_image_ready"
    assert media.destination_asset_allowed(scene["media"], scene, state)
    assert state["assets"]["selected_count"] == 1
    assert state["assets"]["missing_media_count"] == 0


def generated_base(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    run(state, tmp_path, generator=FakeGenerator())
    state["visual_director"]["policy"]["max_auto_generated_images_per_project"] = 1
    return state


def destination(state, **changes):
    scene = copy.deepcopy(state["scenes"][0])
    scene.pop("media")
    scene.pop("visual_director")
    scene.update(id="destination", block_id="different-beat", asset_status="real_media_unavailable",
                 narration="The same physical material bends without breaking.",
                 visual_goal="physical material bends",
                 visual_intent={"visual_goal": "physical material bends", "objects": ["physical material"],
                                "actions": ["bending"], "media_queries": ["material bending"]})
    scene.update(changes)
    state["scenes"].append(scene)
    return scene


@pytest.mark.parametrize("score,allowed", [(.12, False), (.31, True)])
def test_foreign_query_reuse_requires_independent_destination_verification(tmp_path, score, allowed):
    state = generated_base(tmp_path)
    scene = destination(state)
    verifier = Verifier(generated=(score, score))
    admit(state, tmp_path, FakeGenerator(), verifier)
    assert bool(scene.get("media")) is allowed
    assert verifier.local_calls
    if allowed:
        assert scene["asset_status"] == "generated_media_reused"
        assert media.destination_asset_allowed(scene["media"], scene, state, reuse=True)
        evidence = scene["media"]["destination_verifications"][media.scene_acceptance_key(scene, state)]
        assert evidence["scene_score"] == score
    else:
        assert scene["fallback_completion"]["status"] == "exhausted"


def test_unavailable_verifier_cannot_establish_foreign_destination_fit(tmp_path):
    state = generated_base(tmp_path)
    scene = destination(state)
    admit(state, tmp_path, FakeGenerator(), SimpleNamespace(status="unavailable"))
    assert not scene.get("media")


@pytest.mark.parametrize("constraint", ["no_reuse", "rejected", "protected", "locked"])
def test_destination_constraints_still_block_verified_generated_reuse(tmp_path, constraint):
    state = generated_base(tmp_path)
    scene = destination(state)
    if constraint == "no_reuse":
        scene["media_repair"] = {"no_reuse": True}
    elif constraint == "rejected":
        scene["rejected_media_identities"] = [state["scenes"][0]["media"]["identity"]]
    elif constraint == "locked":
        scene["user_locked_visual"] = True
    else:
        scene["visual_director"] = {"reveal_allowed": False, "fallback_chain": ["real_media", "reuse_previous_visual"]}
        state["scenes"][0]["media"].update(reveal_safe=False)
        state["scenes"][0]["media"]["generation"]["reveal_safe"] = False
    verifier = Verifier()
    admit(state, tmp_path, FakeGenerator(), verifier)
    assert not scene.get("media")
    assert not verifier.local_calls


def test_fitting_intentional_continuity_does_not_pay_again(tmp_path):
    state = generated_base(tmp_path)
    scene = destination(state, block_id=state["scenes"][0]["block_id"],
                        visual_intent=copy.deepcopy(state["scenes"][0]["visual_intent"]))
    admit(state, tmp_path, FakeGenerator())
    assert scene["asset_status"] == "generated_media_reused"
    assert scene["media"]["identity"] == state["scenes"][0]["media"]["identity"]
    assert visual_director.generation_counts(state)["auto_generated_images"] == 1


def test_reuse_verification_uses_remaining_shared_acquisition_budget(tmp_path):
    state = generated_base(tmp_path)
    scene = destination(state)
    budget = AcquisitionBudget(verifications=72)
    scene["media_search"] = {"acquisition_budget": budget.snapshot()}
    verifier = Verifier()
    admit(state, tmp_path, FakeGenerator(), verifier)
    assert not verifier.local_calls
    assert not scene.get("media")
    assert scene["media_search"]["acquisition_budget"]["used"]["verifications"] == 72


def test_retiming_missing_scene_keeps_strategy_and_repair_constraints(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    scene = state["scenes"][0]
    scene.update(visual_director={"fallback_chain": ["real_media", "simple_graphic"]},
                 media_repair={"no_reuse": True}, fallback_completion={"status": "exhausted"})
    rebuilt = _build_scenes(state["script"]["blocks"], 4, state["scenes"], cut_pace="slow")[0]
    assert rebuilt["visual_director"] == scene["visual_director"]
    assert rebuilt["media_repair"] == scene["media_repair"]
    assert rebuilt["fallback_completion"] == scene["fallback_completion"]


def test_five_scene_missing_second_beat_can_use_verified_foreign_language_generation(tmp_path):
    # The surviving real-run evidence: five beats, an encoded first beat,
    # three accepted AI images, and a missing second beat. A lexical query
    # mismatch must not exhaust independently fitting, already-paid imagery.
    scenes = [(role, narration, intent) for role, narration, intent in FINGER_SCENES]
    scenes.extend(FINGER_SCENES[1:])
    state = finger_project(scenes)
    generator = FakeGenerator()
    run(state, tmp_path, generator=generator)
    assert len(generator.prompts) == 3
    missing = state["scenes"][1]
    # Preserve the already-paid image as a later beat, then reproduce the
    # failed scene's admission state with a query in another language.
    state["scenes"][3]["media"] = missing.pop("media")
    state["scenes"][3]["asset_status"] = "generated_image_ready"
    missing.update(asset_status="real_media_unavailable", narration="Das Material bleibt flexibel.",
                   visual_goal="flexibles Material", visual_intent={
                       "visual_goal": "flexibles Material", "objects": ["Material"],
                       "media_queries": ["flexibles Material"]})
    assert media._related_media(media.derive_search_queries(missing, state),
                                [scene["media"] for scene in state["scenes"] if scene.get("media")]) is None
    admit(state, tmp_path, generator, Verifier())
    assert len(generator.prompts) == 3
    assert missing["asset_status"] == "generated_media_reused"
    assert media.destination_asset_allowed(missing["media"], missing, state, reuse=True)
    for scene in state["scenes"]:
        assert media.cached_scene_asset_path(scene["media"], settings_for(tmp_path))
        assert scene["asset_status"] != "real_media_unavailable"
    assert state["assets"]["missing_media_count"] == 0


def test_completed_ai_fallback_encodes_a_real_segment(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    state["timeline"].update(width=108, height=192, fps=12)
    admit(state, tmp_path, FakeGenerator())
    ffmpeg = renderer.ffmpeg_path()
    if not ffmpeg:
        pytest.skip("FFmpeg unavailable")
    output = renderer._create_visual_segment(ffmpeg, state, state["scenes"][0], 0, .5, tmp_path, settings_for(tmp_path))
    assert output.is_file() and output.stat().st_size > 0


def test_verified_destination_evidence_survives_reopen_and_cannot_be_overridden_by_query(tmp_path):
    state = generated_base(tmp_path)
    scene = destination(state)
    admit(state, tmp_path, FakeGenerator(), Verifier())
    state = json.loads(json.dumps(state))
    scene = state["scenes"][1]
    assert media.destination_asset_allowed(scene["media"], scene, state, reuse=True)
    key = media.scene_acceptance_key(scene, state)
    scene["media"]["destination_verifications"][key].update(accepted=False, scene_score=.12)
    scene["media"]["query"] = media.derive_search_queries(scene, state)[0]
    assert not media.destination_asset_allowed(scene["media"], scene, state, reuse=True)


def test_locked_invalid_source_is_not_overwritten_or_rendered(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    state["timeline"]["fps"] = 12
    scene = state["scenes"][0]
    path = tmp_path / "unrelated.jpg"
    path.write_bytes(b"cached unrelated stock")
    scene.update(user_locked_visual=True, asset_status="photo_ready", media={
        **media.candidate_evidence(BUS), "cache_path": path.name,
        "relevance": {"confidence": "acceptable"},
    })
    generator = FakeGenerator()
    admit(state, tmp_path, generator)
    assert scene["asset_status"] == "real_media_unavailable"
    assert scene["media"]["identity"] == BUS.identity
    assert not generator.prompts
    with pytest.raises(renderer.RenderUnavailable, match="No real scene media"):
        renderer._create_visual_segment("ffmpeg", state, scene, 0, .5, tmp_path, settings_for(tmp_path))


def test_late_semantic_rejection_of_existing_stock_activates_permitted_ai(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    run(state, tmp_path, photos=[HAND])
    scene = state["scenes"][0]
    old = scene["media"]
    old["relevance"]["confidence"] = "rejected"
    old["relevance"]["acceptance"] = {"accepted": False, "reason": "semantic_mismatch"}
    assert media.cached_scene_asset_path(old, settings_for(tmp_path))
    assert not media.destination_asset_allowed(old, scene, state)
    generator = FakeGenerator()
    admit(state, tmp_path, generator)
    assert len(generator.prompts) == 1
    assert scene["media"]["source"] == "generated_openai"
    assert scene["media"]["identity"] != old["identity"]
