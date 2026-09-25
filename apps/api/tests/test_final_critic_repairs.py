"""Final Video Critic repair effectiveness: a repair counts only when it works.

Every scenario renders real MP4s with the bundled ffmpeg; candidates of a
repair are trial-rendered and the final result is validated on new frames.
``PixelVerifier`` judges the actual pixels; the image API is a local fake.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from pathlib import Path

import pytest
from critic_support import (
    Harness,
    ImageProvider,
    PaintingGenerator,
    PixelVerifier,
    critic_settings,
    frame_at,
    media_pass,
    paint,
    photo,
    render,
)
from PIL import Image
from test_final_critic import (
    FINGER_TITLE,
    GRIP_Q,
    HAND_Q,
    SKIN_Q,
    finger_provider,
    fingers,
    frame_concepts,
    issue_codes,
    rendered,
    row,
    scene,
)

from clipforge import final_critic, renderer, visual_director
from clipforge.final_critic import MAX_REPAIR_PASSES_LIMIT, SEMANTIC_STRONG, max_repair_passes
from clipforge.media import prepare_project_media
from clipforge.renderer import safe_pan_range, still_motion_plan
from clipforge.visual_verifier import VisualVerification

JUNK = ("graffiti", "bus", "book", "city")


def record_for(review: dict, scene_id: str) -> dict:
    return next(item for item in review["repairs"] if item["scene_id"] == scene_id)


def steps(record: dict) -> list[tuple[str, bool | None]]:
    return [(item["step"], item.get("accepted")) for item in record["steps"]]


def inject(state: dict, tmp_path: Path, scene_id: str, concept: str, *, status: str, identity: str) -> None:
    """Give a scene a visual that passed metadata checks but shows ``concept``."""
    target = scene(state, scene_id)
    path = Path(target["media"]["cache_path"]).with_name(f"photo-{identity}.jpg")
    paint(concept).save(tmp_path / path, format="JPEG")
    target["media"] = {**copy.deepcopy(target["media"]), "identity": f"pexels:photo:{identity}", "provider_id": identity, "cache_path": path.as_posix()}
    target["asset_status"] = status


# ---------------------------------------------------------------------------
# 1, 2, 3 — effectiveness is measured on the new render
# ---------------------------------------------------------------------------

def test_bad_real_replacement_is_rejected_and_escalates_to_a_generated_image(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    media_pass(state, tmp_path, finger_provider("book"))
    render(state, tmp_path)
    # The targeted search finds another asset whose metadata fits but whose
    # pixels are urban graffiti; it passes the search-time check.
    provider = finger_provider("book", extra={HAND_Q: [photo("graffiti1", HAND_Q, "Close-up of wrinkled wet fingertips")]})
    provider.concepts["graffiti1"] = "graffiti"
    fooled = PixelVerifier(concepts={**provider.concepts, "graffiti1": "hand"})
    generator = PaintingGenerator("hand")
    harness = Harness(tmp_path, provider, verifier=fooled, generator=generator)

    review = harness.review(state)

    record = record_for(review, "scene_01_01")
    assert steps(record)[:2] == [("real_alternative", False), ("generated_image", True)]
    assert record["steps"][0]["identity"] == "pexels:photo:graffiti1"
    assert record["steps"][0]["reason"] == "rendered_match_below_threshold"
    assert record["repair_attempted"] and record["repair_effective"] and record["outcome"] == "resolved"
    assert record["after_score"] >= SEMANTIC_STRONG > record["before_score"]
    assert record["remaining_issue"] == []
    answer = scene(state, "scene_01_01")
    assert answer["media"]["source"] == "generated_openai" and len(generator.prompts) == 1
    assert "pexels:photo:graffiti1" in answer["rejected_media_identities"]
    assert frame_concepts(review, "scene_01_01", tmp_path)["graffiti"] < 0.05
    assert harness.renders == 1  # escalation happens inside the one repair pass


def test_successful_replacement_measurably_improves_the_rendered_score(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    media_pass(state, tmp_path, finger_provider("book"))
    render(state, tmp_path)
    provider = finger_provider("book", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})

    review = Harness(tmp_path, provider).review(state)

    record = record_for(review, "scene_01_01")
    assert steps(record) == [("real_alternative", True)]
    assert record["repair_effective"] and record["after_score"] - record["before_score"] >= 0.1
    assert record["result_message"] == "replaced a weak visual"
    assert review["summary"]["repaired_scene_count"] == 1 and review["repaired_scenes"] == ["scene_01_01"]


def all_junk_project(monkeypatch, tmp_path) -> tuple[dict, ImageProvider]:
    """Every search returns assets titled as the subject but showing junk."""
    state = fingers(monkeypatch, tmp_path)
    provider = ImageProvider(
        photos={
            HAND_Q: [photo("answer", HAND_Q, FINGER_TITLE)],
            SKIN_Q: [photo("skin", SKIN_Q, "Wrinkled fingertip skin close-up")],
            GRIP_Q: [photo("grip", GRIP_Q, "Wrinkled fingers gripping a wet stone")],
        },
        concepts={"answer": "book", "skin": "city", "grip": "bus"},
    )
    media_pass(state, tmp_path, provider)
    state["visual_director"]["generations"] = [
        {"scene_id": f"other_{index}", "trigger": "auto", "billed": True, "status": "accepted"} for index in range(3)
    ]
    render(state, tmp_path)
    for query in (HAND_Q, SKIN_Q, GRIP_Q):
        provider.photos[query].append(photo(f"graffiti-{len(query)}", query, "Close-up of wrinkled wet fingertips"))
        provider.concepts[f"graffiti-{len(query)}"] = "graffiti"
    return state, provider


def test_unsuccessful_replacements_are_not_counted_and_no_new_filler_is_selected(monkeypatch, tmp_path):
    state, provider = all_junk_project(monkeypatch, tmp_path)
    before = {item["id"]: item["media"]["identity"] for item in state["scenes"]}
    generator = PaintingGenerator("hand")
    fooled = PixelVerifier(concepts=defaultdict(lambda: "hand"))
    harness = Harness(tmp_path, provider, verifier=fooled, generator=generator)

    review = harness.review(state)

    # 14: the exhausted budget is never bypassed ...
    assert generator.prompts == []
    assert visual_director.generation_counts(state)["auto_generated_images"] == 3
    # ... failed replacements are never reported as successes.  Only the
    # explanation resolves, through its own planned explanatory graphic.
    assert review["repaired_scenes"] == ["scene_02_01", "scene_02_02"]
    assert all(record_for(review, scene_id)["accepted_step"] == "planned_graphic" for scene_id in review["repaired_scenes"])
    assert review["summary"]["repaired_scene_count"] == 2
    assert review["status"] == "issues_remain"
    answer = record_for(review, "scene_01_01")
    assert answer["repair_attempted"] and not answer["repair_effective"] and answer["outcome"] == "unresolved"
    assert ("real_alternative", False) in steps(answer)
    assert ("generated_image", False) in steps(answer)
    assert answer["result_message"] == "no relevant visual found and the AI image budget is used up"
    assert "scene_01_01:semantic_match:wrong_media" in review["unresolved"]
    evidence = record_for(review, "scene_03_01")
    assert not evidence["repair_effective"] and evidence["outcome"] == "unresolved"
    # Unrelated filler is never swapped in: unresolved scenes keep their (reported) visual.
    after = {item["id"]: item["media"]["identity"] for item in state["scenes"]}
    assert after["scene_01_01"] == before["scene_01_01"] and after["scene_03_01"] == before["scene_03_01"]
    for item in review["scenes"]:
        assert frame_concepts(review, item["scene_id"], tmp_path)["graffiti"] < 0.05


# ---------------------------------------------------------------------------
# 4, 12, 13 — reuse only when it clearly fits; story-critical thresholds
# ---------------------------------------------------------------------------

def test_story_critical_scene_never_falls_back_to_an_unrelated_project_visual(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = ImageProvider(photos={HAND_Q: [photo("answer", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]}, concepts={"answer": "hand"})
    prepare_project_media(state, "project", critic_settings(tmp_path), client=provider, fallback_client=ImageProvider(), visual_verifier=PixelVerifier(concepts=provider.concepts), image_generator=None, extra_clients=[])

    # In this story the explanation is also the final payoff (story-critical).
    for scene_id in ("scene_02_01", "scene_02_02"):
        payoff = scene(state, scene_id)
        assert payoff["visual_director"]["is_final_payoff"]
        # Before: the Visual Director reused "the last selected visual" merely
        # because it existed; now it uses the fact's own planned graphic.
        assert payoff["asset_status"] not in {"related_media_reused", "real_media_reused"}
        assert payoff["media"]["source"] == "simple_graphic"
    # A plain evidence scene may still fall back to a project visual.
    assert scene(state, "scene_03_01")["asset_status"] == "related_media_reused"


def test_final_payoff_rejects_a_generic_reused_visual(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    inject(state, tmp_path, "scene_02_01", "city", status="related_media_reused", identity="filler")
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    assert {"payoff_generic_reuse", "wrong_media"} <= issue_codes(review, "scene_02_01", "initial_issues")
    record = record_for(review, "scene_02_01")
    assert record["repair_effective"] and record["after_score"] >= SEMANTIC_STRONG
    # The fact's own fitting visual replaces the filler (not another random one).
    assert scene(state, "scene_02_01")["media"]["provider_id"] == "skin"
    assert frame_concepts(review, "scene_02_01", tmp_path)["hand"] > 0.5
    assert row(review, "scene_02_01")["dimensions"]["semantic_match"]["required"] == pytest.approx(SEMANTIC_STRONG)


def test_primary_answer_rejects_weak_reuse(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})
    media_pass(state, tmp_path, finger_provider("hand"))
    inject(state, tmp_path, "scene_01_01", "city", status="real_media_reused", identity="borrowed")
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    assert {"answer_without_own_visual", "wrong_media"} <= issue_codes(review, "scene_01_01", "initial_issues")
    record = record_for(review, "scene_01_01")
    assert record["repair_effective"]
    assert frame_concepts(review, "scene_01_01", tmp_path)["hand"] > 0.5


def test_loosely_matching_story_critical_visual_drives_a_repair(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    initial = finger_provider("hand")
    initial.regions["answer"] = (0.0, 0.2)  # only a sliver of the hand: a loose match
    media_pass(state, tmp_path, initial)
    render(state, tmp_path)
    provider = finger_provider("hand", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})
    provider.regions["answer"] = (0.0, 0.2)

    review = Harness(tmp_path, provider).review(state)

    weak = next(issue for issue in review["initial_issues"] if issue["scene_id"] == "scene_01_01" and issue["category"] == "semantic_match")
    assert weak["code"] == "weak_media" and weak["severity"] == "error"  # story-critical
    assert record_for(review, "scene_01_01")["repair_effective"]
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "fresh"


# ---------------------------------------------------------------------------
# 5, 6, 7 — framing: crop, then motion, then freeze; motion bounded by the focal point
# ---------------------------------------------------------------------------

def test_motion_that_loses_the_subject_is_reduced_before_any_replacement(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    provider.regions["answer"] = (0.0, 0.5)
    media_pass(state, tmp_path, provider)
    real_plan = renderer.still_motion_plan

    def legacy_wide_pan(scene_state, index, motion, center_x, frames, *, focal=None, limit=None):
        # An unbounded pan (as older renders could produce) sweeping off the subject.
        if scene_state.get("id") == "scene_01_01" and limit is None and motion:
            return {"type": "pan", "max_zoom": 2.0, "zoom": "2.0", "x": f"(0.0+1.0*on/{max(1, frames - 1)})"}
        return real_plan(scene_state, index, motion, center_x, frames, focal=focal, limit=limit)

    monkeypatch.setattr(renderer, "still_motion_plan", legacy_wide_pan)
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    first = issue_codes(review, "scene_01_01", "initial_issues")
    assert {"motion_loses_subject", "subject_lost_in_render"} & first
    record = record_for(review, "scene_01_01")
    assert record["action"] == "adjust_composition" and record["accepted_step"] == "reduce_motion"
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "answer"  # no replacement needed
    assert state["render"]["layout"][0]["motion"]["max_zoom"] == renderer.REDUCED_MAX_ZOOM
    assert record["repair_effective"] and record["result_message"] == "reduced the camera motion"
    # The subject is visible throughout the repaired scene.
    for item in row(review, "scene_01_01")["frames"]:
        with Image.open(tmp_path / item["path"]) as image:
            from critic_support import concept_fractions

            assert concept_fractions(image)["hand"] > 0.3


def test_motion_is_bounded_by_the_focal_point():
    photo_scene = {"media": {"source": "pexels"}}
    # Centred subject: a pan within a range that keeps it in view.
    plan = still_motion_plan(photo_scene, 1, "subtle_pan", 0.5, 90, focal=(0.5, 0.5))
    low, high = safe_pan_range(0.5, 1.06)
    assert plan["type"] == "pan" and plan["safe_range"] == [round(low, 4), round(high, 4)]
    for t in (low, high):
        assert renderer.FOCAL_MARGIN <= 1.06 * 0.5 - 0.06 * t <= 1 - renderer.FOCAL_MARGIN
    # Subject at the edge: no safe pan exists -> anchored push-in instead.
    edge = still_motion_plan(photo_scene, 1, "subtle_pan", 0.03, 90, focal=(0.03, 0.5))
    assert edge["type"] == "push_in" and edge["reason"] == "no_safe_pan" and edge["x"] == "0.0300"
    # A repair can reduce motion to a tiny anchored push.
    reduced = still_motion_plan(photo_scene, 1, "subtle_pan", 0.5, 90, focal=(0.4, 0.5), limit="reduced")
    assert reduced["max_zoom"] == renderer.REDUCED_MAX_ZOOM and reduced["x"] == "0.4000"


def test_real_render_keeps_an_edge_subject_in_view_during_motion(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    provider.regions["skin"] = (0.0, 0.2)
    provider.sizes["skin"] = (1920, 1080)
    media_pass(state, tmp_path, provider)
    monkeypatch.setattr(
        "clipforge.renderer.analyze_scene_media",
        lambda scene_state, *_a, **_k: {"status": "verified", "mode": "smart", "center_x": 0.1, "center_y": 0.5, "confidence": 0.9},
    )
    state["captions"]["enabled"] = False
    render(state, tmp_path)
    window = next(item for item in state["render"]["layout"] if item["scene_id"] == "scene_02_01")
    assert window["motion"]["focal"][0] < 0.5  # the crop is centred as far as the source allows

    from critic_support import concept_fractions

    video = rendered(state, tmp_path)
    fractions = [concept_fractions(frame_at(video, t))["hand"] for t in (window["start"] + 0.05, window["end"] - 0.08)]
    assert min(fractions) > 0.2 and abs(fractions[0] - fractions[1]) < 0.15


# ---------------------------------------------------------------------------
# 8, 9 — repetition
# ---------------------------------------------------------------------------

def test_accidental_repetition_is_replaced_and_counted_only_when_it_worked(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    payoff = scene(state, "scene_03_01")
    payoff["media"] = copy.deepcopy(scene(state, "scene_01_01")["media"])
    payoff["asset_status"] = "real_media_reused"
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    record = record_for(review, "scene_03_01")
    assert "accidental_repeat" in issue_codes(review, "scene_03_01", "initial_issues")
    assert record["repair_effective"] and scene(state, "scene_03_01")["media"]["provider_id"] == "grip"
    assert "scene_03_01" in review["repaired_scenes"]
    # 9: the explanation's two scenes still share one base (overlay evolves).
    assert row(review, "scene_02_02")["dimensions"]["repetition"]["reason"] == "intentional_continuity"


# ---------------------------------------------------------------------------
# 10, 11 — text-heavy and oversized overlays, measured in the rendered frame
# ---------------------------------------------------------------------------

LONG_STEPS = ["Nerven lassen die Blutgefäße enger", "Das Gewebevolumen der Fingerkuppe", "Die Haut legt sich in tiefe Falten"]


def test_card_like_overlay_is_simplified_and_the_frame_really_changes(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    for item in state["scenes"]:
        if item["block_id"] == "voice_block_02":
            item["visual_director"]["overlay_spec"] = {"kind": "process", "steps": LONG_STEPS}
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    before_layout = {item["scene_id"]: copy.deepcopy(item) for item in state["render"]["layout"]}

    review = Harness(tmp_path, provider).review(state)

    first = issue_codes(review, "scene_02_02", "initial_issues")
    assert {"overlay_too_dominant", "text_heavy", "overlay_too_long"} <= first
    record = record_for(review, "scene_02_02")
    assert record["action"] == "adjust_composition" and record["adjustments"]["overlay"]["mode"] == "compact"
    assert record["accepted_step"] == "rewrite_overlay_from_fact"  # sentences -> the fact's relation
    assert record["repair_effective"] and not {"overlay_too_dominant", "text_heavy", "overlay_too_long"} & issue_codes(review, "scene_02_02")
    after = {item["scene_id"]: item for item in state["render"]["layout"]}
    old, new = before_layout["scene_02_02"]["overlays"][0], after["scene_02_02"]["overlays"][0]
    assert new["bbox_area"] < old["bbox_area"] / 2 and new["coverage"] < old["coverage"] / 3
    assert new["bbox_area"] < final_critic.OVERLAY_MAX_BBOX and new["coverage"] < final_critic.OVERLAY_MAX_COVERAGE
    assert new["style"] == "minimal" and new["spec"]["steps"] != LONG_STEPS  # short relation, no panel
    assert all(len(step.split()) <= 6 for step in new["spec"]["steps"])
    # The repaired frame is materially different from the reviewed one.
    root = tmp_path
    old_frame = root / "project" / "critic" / "v2" / "pass0" / "scene_02_02-1.jpg"
    new_frame = root / row(review, "scene_02_02")["frames"][1]["path"]
    difference = final_critic._mean_abs_diff(Image.open(old_frame), Image.open(new_frame))
    assert difference > 3
    assert review["render"]["width"] * 16 == review["render"]["height"] * 9  # still a valid 9:16 render


# ---------------------------------------------------------------------------
# 15 — wet fingers: graffiti, bus stop, book page and city never survive
# ---------------------------------------------------------------------------

def test_wet_finger_render_never_keeps_urban_or_text_junk(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    initial = ImageProvider(
        photos={
            HAND_Q: [photo("graffiti", HAND_Q, "Wrinkled wet fingertips under a bridge"), photo("bus", HAND_Q, FINGER_TITLE),
                     photo("book", HAND_Q, FINGER_TITLE), photo("city", HAND_Q, FINGER_TITLE)],
            SKIN_Q: [photo("city2", SKIN_Q, "Wrinkled fingertip skin close-up")],
            GRIP_Q: [photo("bus2", GRIP_Q, "Wrinkled fingers gripping a wet stone")],
        },
        concepts={"graffiti": "graffiti", "bus": "bus", "book": "book", "city": "city", "city2": "city", "bus2": "bus", "fingers": "hand"},
    )
    media_pass(state, tmp_path, initial)  # metadata-only: the urban graffiti clip wins
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "graffiti"
    render(state, tmp_path)
    repair = ImageProvider(photos={**initial.photos, HAND_Q: [*initial.photos[HAND_Q], photo("fingers", HAND_Q, "Close-up of wrinkled wet fingertips")]},
                           concepts=initial.concepts)
    generator = PaintingGenerator("hand")
    harness = Harness(tmp_path, repair, generator=generator)

    review = harness.review(state)

    assert scene(state, "scene_01_01")["media"]["provider_id"] == "fingers"
    for item in review["scenes"]:
        concepts = frame_concepts(review, item["scene_id"], tmp_path)
        assert all(concepts[name] < 0.05 for name in JUNK), (item["scene_id"], concepts)
        assert concepts["hand"] > 0.4
    # Explanation: wrinkled hand base + a small process overlay, one fact one base.
    layout = {item["scene_id"]: item for item in state["render"]["layout"]}
    explanation = [item for item in state["scenes"] if item["block_id"] == "voice_block_02"]
    assert explanation[0]["media"]["identity"] == explanation[1]["media"]["identity"]
    for item in explanation:
        overlays = layout[item["id"]]["overlays"]
        assert overlays and overlays[0]["kind"] == "process" and overlays[0]["bbox_area"] < final_critic.OVERLAY_MAX_BBOX
    assert len(generator.prompts) <= 3 and visual_director.generation_counts(state)["auto_generated_images"] <= 3
    assert review["status"] == "repaired" and harness.renders == 1
    assert review["summary"]["repaired_scene_count"] == len(review["repaired_scenes"]) >= 3


# ---------------------------------------------------------------------------
# Loop bound (the improvement comes from scene-level escalation, not passes)
# ---------------------------------------------------------------------------

class FreshMislabelled(ImageProvider):
    """Every answer search returns a new asset titled as fingertips that shows a city."""

    def __init__(self):
        super().__init__(photos={SKIN_Q: [photo("skin", SKIN_Q, "Wrinkled fingertip skin close-up")], GRIP_Q: [photo("grip", GRIP_Q, "Wrinkled fingers gripping a wet stone")]},
                         concepts=defaultdict(lambda: "city", {"skin": "hand", "grip": "hand"}))
        self.counter = 0

    def search_photos(self, query, *, portrait):
        if query != HAND_Q:
            return super().search_photos(query, portrait=portrait)
        self.calls.append(("photo", query))
        self.counter += 1
        return [photo(f"junk{self.counter}", query, FINGER_TITLE)]


class TrialFooled(PixelVerifier):
    """Search-time checks and trial renders are fooled; the final review is not."""

    def score_video_frames(self, frames, texts, *, asset_identity="video"):
        if str(asset_identity).startswith("critic-trial"):
            return VisualVerification(0.36, "verified", "local_video_frames", (0.36, 0.36, 0.36), 3, 0.36, 0.36, 0.1, 0.3, 0.1, False)
        return super().score_video_frames(frames, texts, asset_identity=asset_identity)


@pytest.mark.parametrize(("configured", "explicit", "expected"), [(1, None, 1), (5, None, 2), (1, 0, 0)])
def test_repair_passes_stay_bounded(monkeypatch, tmp_path, configured, explicit, expected):
    state = fingers(monkeypatch, tmp_path)
    provider = FreshMislabelled()
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    settings = critic_settings(tmp_path, final_critic_max_repair_passes=configured)
    harness = Harness(tmp_path, provider, verifier=TrialFooled(concepts=defaultdict(lambda: "hand")), settings=settings)

    review = harness.review(state, **({} if explicit is None else {"max_passes": explicit}))

    assert harness.renders == expected == review["repair_pass_count"]
    assert len(review["history"]) == expected + 1
    assert review["status"] == "issues_remain" and review["summary"]["repaired_scene_count"] == 0
    assert max_repair_passes(settings) <= MAX_REPAIR_PASSES_LIMIT
    assert all(not item.get("repair_effective") for item in review["repairs"] if item["scene_id"] == "scene_01_01")
