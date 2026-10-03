"""Novelty only breaks semantic ties; mocked providers and local real renders."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from critic_support import frame_at
from PIL import Image, ImageChops, ImageDraw, ImageStat
from test_staged_media_search import Commons, Provider, Verifier, cand, settings_for
from test_visual_director import FakeGenerator
from test_visual_director import Provider as ImageProvider
from test_visual_director import Verifier as ImageVerifier

from clipforge import media, renderer, visual_director, visual_translation
from clipforge.config import Settings
from clipforge.routed_search import run_routed_scene_search
from clipforge.visual_diversity import (
    MAX_NOVELTY_PENALTY,
    concept,
    novelty_evidence,
    prefer_useful_novelty,
)
from clipforge.visual_providers import AcquisitionBudget, ProviderAdapter, ProviderRegistry
from clipforge.visual_rights import MediaRights

REPEAT = "Copper wire coiled on a wooden work table"
DISTINCT = "Copper wire stretched between two fingers"


def setup_scene(same_block=False):
    prior = {
        "id": "first",
        "block_id": "b1",
        "narration": "The copper wire is initially coiled.",
        "media": {"identity": "pexels:photo:old", "title": REPEAT, "query": "copper wire"},
    }
    scene = {
        "id": "next",
        "block_id": "b1" if same_block else "b2",
        "narration": "Copper wire remains flexible.",
        "visual_goal": "copper wire",
        "visual_intent": {"visual_goal": "copper wire", "objects": ["copper", "wire"]},
        "start": 4,
        "end": 8,
        "preferred_media": "photo",
    }
    return scene, {
        "scenes": [prior, scene],
        "timeline": {"width": 1080, "height": 1920},
        "assets": {},
    }


def row(pid, title, clip=0.31, score=60, tier=3):
    return cand(pid, "copper wire", title, kind="photo"), {
        "selection_tier": tier,
        "score": score,
        "visual": {"scene_score": clip},
        "confidence": "high",
    }


def test_distinct_concept_wins_between_equally_valid_candidates():
    scene, state = setup_scene()
    repeated, distinct = row("repeat", REPEAT), row("distinct", DISTINCT)
    ranked = prefer_useful_novelty([repeated, distinct], scene, state)
    assert ranked[0][0].provider_id == "distinct"
    assert ranked[0][1]["diversity"]["selection_reason"] == "useful_novelty_among_semantic_peers"
    assert repeated[1]["diversity"]["penalty"] > 0


@pytest.mark.parametrize("advantage", ["clip", "metadata", "tier", "coverage"])
def test_semantic_superiority_overrides_novelty(advantage):
    scene, state = setup_scene()
    repeated = row(
        "repeat",
        REPEAT,
        clip=0.34 if advantage == "clip" else 0.31,
        score=72 if advantage == "metadata" else 60,
    )
    other = row("distinct", DISTINCT, tier=2 if advantage == "tier" else 3)
    coverage = (lambda r: (3, 0) if r is repeated else (1, 2)) if advantage == "coverage" else None
    assert (
        prefer_useful_novelty([repeated, other], scene, state, coverage=coverage)[0][0].provider_id
        == "repeat"
    )


@pytest.mark.parametrize(
    "title,identity", [(REPEAT, "new"), (REPEAT + " at sunset", "new"), ("", "pexels:photo:old")]
)
def test_identical_and_near_identical_concepts_have_bounded_penalty(title, identity):
    scene, state = setup_scene()
    evidence = novelty_evidence({"identity": identity, "title": title}, scene, state)
    assert 0 < evidence["penalty"] <= MAX_NOVELTY_PENALTY


def test_same_statement_continuity_keeps_baseline_order_and_exempts_penalty():
    scene, state = setup_scene(same_block=True)
    ranked = prefer_useful_novelty([row("repeat", REPEAT), row("distinct", DISTINCT)], scene, state)
    assert ranked[0][0].provider_id == "repeat"
    assert ranked[0][1]["diversity"]["continuity_exemption"]
    assert ranked[0][1]["diversity"]["penalty"] == 0


def test_missing_caption_query_and_provider_rank_do_not_invent_concepts():
    empty = cand("x", "a completely distinct action", "", kind="photo")
    assert concept(replace(empty, rank=10**9, provider="nasa"))["terms"] == []


def routed(scene, state, candidates, verifier=None):
    registry = ProviderRegistry(
        [ProviderAdapter("pexels", Provider(photos={"copper wire": candidates}))]
    )
    budget = AcquisitionBudget()
    result = run_routed_scene_search(
        ["copper wire"] * 4,
        scene,
        state,
        media.build_visual_query_plan(scene, state),
        registry=registry,
        preferred_kind="photo",
        portrait=True,
        scene_duration=4,
        used=set(),
        verifier=verifier or Verifier(),
        acquisition_budget=budget,
    )
    return result, budget


def test_routed_pool_prefers_novelty_without_extra_acquisition_work():
    scene, state = setup_scene()
    candidates = [
        cand("repeat", "copper wire", REPEAT, kind="photo"),
        cand("distinct", "copper wire", DISTINCT, kind="photo"),
    ]
    result, budget = routed(scene, state, candidates)
    assert result.ranked[0][0].provider_id == "distinct"
    assert result.provenance["logical_queries_executed"] == 1
    assert budget.search_requests <= budget.max_search_requests
    assert budget.verifications <= budget.max_verifications
    evidence = result.provenance["stages"][0]["candidate_evidence"]
    assert (
        next(e for e in evidence if e["identity"].endswith(":distinct"))["diversity"]["penalty"]
        == 0
    )
    assert (
        json.loads(json.dumps(result.ranked[0][1]))["diversity"]["candidate_concept"]["summary"]
        == DISTINCT
    )


@pytest.mark.parametrize("reason", ["metadata_mismatch", "clip_reject", "rights_unknown"])
def test_different_candidate_cannot_gain_admission_through_novelty(reason):
    scene, state = setup_scene()
    good = cand("repeat", "copper wire", REPEAT, kind="photo")
    bad = cand(
        "bad",
        "copper wire",
        "Busy traffic crossing a city bridge" if reason == "metadata_mismatch" else DISTINCT,
        kind="photo",
    )
    if reason == "rights_unknown":
        bad = replace(bad, rights=MediaRights())
    verifier = Verifier(scores={"bad": (0.8, 0.1)} if reason == "clip_reject" else {})
    result, _ = routed(scene, state, [good, bad], verifier)
    assert [c.provider_id for c, _ in result.ranked] == ["repeat"]


def test_legacy_staged_path_shares_the_same_tie_preference():
    scene, state = setup_scene()
    result = media.run_staged_scene_search(
        ["copper wire"],
        scene,
        state,
        media.build_visual_query_plan(scene, state),
        pexels=Provider(
            photos={
                "copper wire": [
                    cand("repeat", "copper wire", REPEAT, kind="photo"),
                    cand("distinct", "copper wire", DISTINCT, kind="photo"),
                ]
            }
        ),
        wikimedia=Commons(),
        preferred_kind="photo",
        portrait=True,
        scene_duration=4,
        used=set(),
        verifier=Verifier(),
    )
    assert result.ranked[0][0].provider_id == "distinct"


def test_related_reuse_breaks_only_equal_relation_ties():
    scene, state = setup_scene()
    repeated = state["scenes"][0]["media"]
    distinct = {"identity": "pexels:photo:new", "title": DISTINCT, "query": "copper wire"}
    chosen = media._related_media(["copper wire"], [repeated, distinct], scene=scene, state=state)
    assert chosen["identity"] == distinct["identity"]
    assert chosen["reuse_diversity"]["selection_reason"] == "destination_fit_then_novelty"
    # A weaker focused relation does not gain legitimacy through variety.
    distinct["query"] = "copper alloy"
    assert (
        media._related_media(
            ["copper wire flexibility"], [repeated, distinct], scene=scene, state=state
        )["identity"]
        == repeated["identity"]
    )
    scene["block_id"] = "b1"
    distinct["query"] = "copper wire"
    assert (
        media._related_media(["copper wire"], [repeated, distinct], scene=scene, state=state)[
            "identity"
        ]
        == repeated["identity"]
    )


def test_stronger_destination_verification_overrides_reuse_diversity():
    scene, state = setup_scene()
    key = media.scene_acceptance_key(scene, state)
    repeated = {
        "identity": "generated_openai:photo:old",
        "source": "generated_openai",
        "query": "copper wire",
        "title": REPEAT,
        "destination_verifications": {key: {"scene_score": 0.36}},
    }
    state["scenes"][0]["media"] = repeated
    distinct = {
        "identity": "generated_openai:photo:new",
        "source": "generated_openai",
        "query": "copper wire",
        "title": DISTINCT,
        "destination_verifications": {key: {"scene_score": 0.29}},
    }
    assert (
        media._related_media(["copper wire"], [repeated, distinct], scene=scene, state=state)[
            "identity"
        ]
        == repeated["identity"]
    )


def test_generated_facets_survive_metadata_persistence():
    prompt = {
        "summary": DISTINCT,
        "visual_concept": {
            "subject": "Copper wire",
            "action": "stretched between fingers",
            "environment": "workbench",
        },
    }
    metadata = visual_director._generated_metadata(
        digest="test",
        relative="asset.png",
        width=1024,
        height=1536,
        prompt=prompt,
        record={"model": "gpt-image-2", "reveal_safe": True, "story_role": "explanation"},
    )
    reopened = json.loads(json.dumps(metadata))
    profile = concept(reopened)
    assert (
        profile["action"] == "stretched between fingers"
        and profile["explanatory_role"] == "explanation"
    )


def test_translation_receives_progression_context_and_caches_same_statement(monkeypatch, tmp_path):
    scene, state = setup_scene()
    scene["visual_intent"] = {"source": "narration_fallback"}
    state["script"] = {"blocks": [{"id": "b2", "text": "The metal can be bent without breaking."}]}
    requests = []

    def parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            output_parsed=visual_translation.VisualTranslation(
                main_subject="Copper wire",
                visible_state_or_action="bending around a rounded tool",
                setting="Workbench",
            )
        )

    monkeypatch.setattr(
        visual_translation,
        "TRANSLATOR_CLIENT_FACTORY",
        lambda _: SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    settings = Settings(_env_file=None, openai_api_key="mock", render_root=tmp_path)
    strategy = {"reveal_allowed": True, "story_role": "explanation"}
    first = visual_director.build_generation_prompt(scene, state, strategy, settings=settings)
    repeated = visual_director.build_generation_prompt(
        copy.deepcopy(scene), state, strategy, settings=settings
    )
    assert first == repeated and len(requests) == 1
    request = json.loads(requests[0]["input"])
    assert request["previous_visual"] == REPEAT
    assert request["statement"] == "The metal can be bent without breaking."
    assert "bending" in first["visual_concept"]["action"]
    assert "Relevant repetition" in requests[0]["instructions"]


@pytest.mark.parametrize("frames", [30, 90, 240])
def test_still_zoom_finishes_over_scene_duration_deterministically(frames):
    scene = {"media": {"source": "pexels"}}
    plan = renderer.still_motion_plan(scene, 0, "subtle_pan", 0.5, frames, focal=(0.5, 0.5))
    assert plan == renderer.still_motion_plan(scene, 0, "subtle_pan", 0.5, frames, focal=(0.5, 0.5))
    increment = float(plan["zoom"].split("+")[1].split(",")[0])
    assert abs(increment * (frames - 1) - min(0.08, 0.0006 * (frames - 1))) < 0.0002
    assert plan["max_zoom"] <= 1.08


@pytest.mark.parametrize(
    "size,focal", [((1920, 1080), (0.1, 0.5)), ((1080, 1920), (0.4, 0.3)), ((800, 800), (0.9, 0.7))]
)
def test_focal_crop_and_motion_keep_relevant_subject_visible(size, focal):
    geometry = renderer.focal_crop(focal, size, (1080, 1920))
    assert geometry is not None and all(0 <= n <= 1 for n in geometry)
    for index in range(3):
        plan = renderer.still_motion_plan({}, index, "subtle_pan", focal[0], 90, focal=geometry[2:])
        anchors = plan.get("safe_range") or [float(plan["x"])]
        for anchor in anchors:
            position = plan["max_zoom"] * geometry[2] - (plan["max_zoom"] - 1) * anchor
            assert 0 <= position <= 1


@pytest.mark.parametrize("size", [(800, 400), (400, 800)])
def test_document_render_preserves_full_frame_and_is_static(monkeypatch, tmp_path, size):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle((0, 0, size[0] - 1, size[1] - 1), outline="red", width=20)
    image.save(tmp_path / "document.png")
    candidate = cand("doc", "archival manuscript", "Archival manuscript", kind="photo")
    scene = {
        "id": "doc",
        "start": 0,
        "end": 1,
        "motion": "subtle_pan",
        "media": media.candidate_evidence(candidate) | {"cache_path": "document.png"},
    }
    state = {
        "timeline": {"width": 180, "height": 320, "fps": 15},
        "scenes": [scene],
        "captions": {"enabled": False},
    }
    monkeypatch.setattr(
        renderer, "analyze_scene_media", lambda *_: {"center_x": 0.9, "center_y": 0.1}
    )
    segment = renderer._create_visual_segment(
        renderer.ffmpeg_path(), state, scene, 0, 1, tmp_path, settings_for(tmp_path)
    )
    start, end = frame_at(segment, 0.05), frame_at(segment, 0.8)
    assert start.size == (180, 320) and end.size == start.size
    assert (
        scene["still_motion"]["type"] == "static" and scene["still_motion"]["framing"] == "contain"
    )
    red = [
        (x, y)
        for y in range(start.height)
        for x in range(start.width)
        if start.getpixel((x, y))[0] > 150 and start.getpixel((x, y))[1] < 100
    ]
    assert len(red) > 200
    scale = min(start.width / size[0], start.height / size[1])
    left, top = (start.width - size[0] * scale) / 2, (start.height - size[1] * scale) / 2
    assert abs(min(x for x, y in red) - left) < 3
    assert abs(max(x for x, y in red) - (start.width - left - 1)) < 3
    assert abs(min(y for x, y in red) - top) < 3
    assert abs(max(y for x, y in red) - (start.height - top - 1)) < 3
    # Encoded I/P frames differ slightly; geometry and mean pixel difference
    # distinguish compression noise from actual image motion.
    assert max(ImageStat.Stat(ImageChops.difference(start, end)).mean) < 0.5


def test_no_good_stock_still_completes_permitted_ai_fallback_with_novelty_context(tmp_path):
    scene, state = setup_scene()
    previous = state["scenes"][0]
    previous["media"] = media.candidate_evidence(
        cand("old", "copper wire", REPEAT, kind="photo")
    ) | {"cache_path": "old.png"}
    Image.new("RGB", (1080, 1920), "gray").save(tmp_path / "old.png")
    bad = cand("bad", "copper wire", "Traffic on a city bridge", kind="photo")
    generator = FakeGenerator()
    media.prepare_project_media(
        state,
        "project",
        settings_for(tmp_path),
        client=ImageProvider(photos=[bad]),
        fallback_client=ImageProvider(),
        visual_verifier=ImageVerifier(),
        image_generator=generator,
        extra_clients=[],
    )
    assert scene["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1
    assert scene["media_search"]["logical_queries_executed"] <= 3
    assert state["visual_director"]["summary"]["auto_generated_images"] <= 3
    assert "generated_card_count" not in state["assets"]
