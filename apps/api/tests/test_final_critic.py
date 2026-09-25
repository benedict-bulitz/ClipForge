"""Final Video Critic V1: review the actual rendered MP4, repair narrowly, stop.

Every test renders real MP4s with the bundled ffmpeg (small 9:16 timeline)
and the critic extracts real frames from them.  OpenCLIP is replaced by
``PixelVerifier`` (it judges the real pixels), providers download real
images, narration is silent and the image API is a local fake — no network,
no paid calls (see ``forbid_real_image_generation`` in conftest).
"""
from __future__ import annotations

import copy
import inspect
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

import pytest
from critic_support import (
    CONCEPTS,
    HEIGHT,
    WIDTH,
    Harness,
    ImageProvider,
    PaintingGenerator,
    PixelVerifier,
    concept_fractions,
    critic_settings,
    draw_circle_image,
    frame_at,
    media_pass,
    paint,
    photo,
    render,
    silent_voice,
    small_timeline,
)
from PIL import Image
from test_story_visual_director import FINGERS, FINGERS_Q
from test_story_visual_integration import ISLANDS, ISLANDS_Q, _strip_story, fact, generate, visual

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge import final_critic, visual_director
from clipforge.final_critic import (
    MAX_REPAIR_PASSES_LIMIT,
    max_repair_passes,
    record_manual_change,
    run_final_quality_review,
    scene_sample_times,
    user_locked_visual,
)
from clipforge.pipeline import _build_scenes
from clipforge.renderer import RenderResult

HAND_Q = "wrinkled wet fingertips"
SKIN_Q = "wrinkled fingertip skin"
GRIP_Q = "wrinkled fingers gripping wet stone"
FINGER_TITLE = "Wrinkled wet fingertips macro close-up"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def fingers(monkeypatch, tmp_path) -> dict:
    silent_voice(monkeypatch)
    return small_timeline(generate(monkeypatch, tmp_path, FINGERS_Q, FINGERS, planner_target=""))


def finger_provider(answer: str = "hand", *, skin=None, extra: dict | None = None) -> ImageProvider:
    """Answer candidate is titled as fingertips; ``answer`` decides what its pixels show."""
    photos = {
        HAND_Q: [photo("answer", HAND_Q, FINGER_TITLE)],
        SKIN_Q: skin if skin is not None else [photo("skin", SKIN_Q, "Wrinkled fingertip skin close-up")],
        GRIP_Q: [photo("grip", GRIP_Q, "Wrinkled fingers gripping a wet stone")],
    }
    for query, rows in (extra or {}).items():
        photos[query] = [*photos.get(query, []), *rows]
    return ImageProvider(photos=photos, concepts={"answer": answer, "skin": "hand", "grip": "hand", "fresh": "hand", "book": "book"})


def scene(state: dict, scene_id: str) -> dict:
    return next(item for item in state["scenes"] if item["id"] == scene_id)


def row(review: dict, scene_id: str) -> dict:
    return next(item for item in review["scenes"] if item["scene_id"] == scene_id)


def issue_codes(review: dict, scene_id: str | None = None, key: str = "issues") -> set[str]:
    return {issue["code"] for issue in review.get(key) or [] if scene_id is None or issue["scene_id"] == scene_id}


def rendered(state: dict, tmp_path: Path) -> Path:
    return tmp_path / state["render"]["url"].removeprefix("/media/")


def frame_concepts(review: dict, scene_id: str, tmp_path: Path) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    frames = row(review, scene_id)["frames"]
    for item in frames:
        with Image.open(tmp_path / item["path"]) as image:
            for name, value in concept_fractions(image).items():
                totals[name] += value / len(frames)
    return dict(totals)


def detect_only(state, tmp_path, provider, **kwargs) -> dict:
    render(state, tmp_path)
    return Harness(tmp_path, provider, **kwargs).review(state, max_passes=0)


# ---------------------------------------------------------------------------
# Rendered-frame review
# ---------------------------------------------------------------------------

def test_critic_reads_real_frames_at_each_scene_window_of_a_valid_9x16_render(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand", skin=[photo("skinbook", SKIN_Q, "Wrinkled fingertip skin close-up")], extra={GRIP_Q: []})
    provider.photos[GRIP_Q] = [photo("gripcity", GRIP_Q, "Wrinkled fingers gripping a wet stone")]
    provider.concepts.update(skinbook="book", gripcity="city")
    media_pass(state, tmp_path, provider)
    review = detect_only(state, tmp_path, provider)

    assert review["status"] != "not_reviewed"
    assert review["render"]["width"] == WIDTH and review["render"]["height"] == HEIGHT and review["render"]["aspect_ok"]
    layout = state["render"]["layout"]
    assert [item["scene_id"] for item in layout] == [item["scene_id"] for item in review["scenes"]]
    assert layout[0]["start"] == 0 and all(a["end"] == pytest.approx(b["start"]) for a, b in pairwise(layout))
    assert layout[-1]["end"] == pytest.approx(review["render"]["duration"], abs=0.1)
    expected = {"scene_01_01": "hand", "scene_02_01": "book", "scene_02_02": "book", "scene_03_01": "city"}
    for entry in review["scenes"]:
        window = entry["window"]
        # Near start, middle and near end of the scene's own window ...
        times = [item["time"] for item in entry["frames"]]
        assert len(times) == 3 and all(window["start"] < value < window["end"] for value in times)
        assert times == scene_sample_times(window["start"], window["end"])
        # ... and those frames really show that scene's media.
        concepts = frame_concepts(review, entry["scene_id"], tmp_path)
        assert max(concepts, key=concepts.get) == expected[entry["scene_id"]], entry["scene_id"]
    # Real overlays are drawn and measured by the renderer.
    overlays = [item for entry in layout if entry["scene_id"].startswith("scene_02") for item in entry["overlays"]]
    assert overlays and all(item["bbox"] and 0 < item["coverage"] < 0.2 for item in overlays)


def test_crop_stays_undistorted_with_real_ffmpeg(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    target = scene(state, "scene_01_01")
    path = tmp_path / target["media"]["cache_path"]
    draw_circle_image((1600, 1200)).save(path, format="JPEG")
    target["render_adjustments"] = {"motion": "static"}
    state["captions"]["enabled"] = False  # only the circle is dark in this frame
    state["attention_events"] = []
    render(state, tmp_path)
    window = state["render"]["layout"][0]

    frame = frame_at(rendered(state, tmp_path), (window["start"] + window["end"]) / 2)
    assert frame.size == (WIDTH, HEIGHT)
    dark = frame.convert("L").point(lambda value: 255 if value < 40 else 0)
    left, top, right, bottom = dark.getbbox()
    # A circle in the source is still a circle after scale + 9:16 crop.
    assert (right - left) / (bottom - top) == pytest.approx(1.0, abs=0.06)


# ---------------------------------------------------------------------------
# 1, 2, 16 — wrong media vs good media; wet-finger scenarios A–E
# ---------------------------------------------------------------------------

def test_wet_finger_rendered_scenarios_prefer_real_wrinkled_fingers(monkeypatch, tmp_path):
    results = {}
    for name, concept in {"A_book": "book", "B_bus": "bus", "C_city": "city", "D_fingers": "hand"}.items():
        folder = tmp_path / name
        folder.mkdir()
        state = fingers(monkeypatch, folder)
        provider = finger_provider(concept)
        media_pass(state, folder, provider)  # metadata-only: the mislabelled asset passes
        review = detect_only(state, folder, provider)
        results[name] = (row(review, "scene_01_01"), issue_codes(review, "scene_01_01"))
        if name == "D_fingers":
            # E: wet wrinkled hand + the explanation's process overlay.
            results["E_fingers_overlay"] = (row(review, "scene_02_01"), issue_codes(review, "scene_02_01"))
    for name in ("A_book", "B_bus", "C_city"):
        record, codes = results[name]
        assert "wrong_media" in codes, name
        assert record["dimensions"]["semantic_match"]["rating"] == "poor"
        assert record["dimensions"]["semantic_match"]["source"] == "openclip_rendered_frames"
    for name in ("D_fingers", "E_fingers_overlay"):
        record, codes = results[name]
        assert not {"wrong_media", "weak_media"} & codes, name
        assert record["dimensions"]["semantic_match"]["rating"] == "good"
    assert results["E_fingers_overlay"][0]["dimensions"]["overlay_quality"]["rating"] == "good"
    worst_good = min(results[name][0]["score"] for name in ("D_fingers", "E_fingers_overlay"))
    best_bad = max(results[name][0]["score"] for name in ("A_book", "B_bus", "C_city"))
    assert worst_good - best_bad >= 0.08
    semantic = {name: results[name][0]["dimensions"]["semantic_match"]["score"] for name in results}
    assert min(semantic["D_fingers"], semantic["E_fingers_overlay"]) - max(semantic[name] for name in ("A_book", "B_bus", "C_city")) >= 0.15


def test_good_media_is_retained_and_nothing_is_repaired(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    before = {item["id"]: item["media"]["identity"] for item in state["scenes"]}
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    assert review["status"] == "passed" and review["summary"]["label"] == "Passed"
    assert review["repairs"] == [] and review["repair_pass_count"] == 0 and harness.renders == 0
    assert {item["id"]: item["media"]["identity"] for item in state["scenes"]} == before
    assert state["final_quality_review"] is review
    # Individual dimensions are persisted per scene, not only one score.
    dimensions = row(review, "scene_02_01")["dimensions"]
    assert set(final_critic.DIMENSIONS) <= set(dimensions)
    assert review["overall_score"] == 100


def test_wrong_real_media_is_replaced_through_the_normal_media_search(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    media_pass(state, tmp_path, finger_provider("book"))
    render(state, tmp_path)
    # The targeted re-search finds a real match; the wrong asset is never re-picked.
    provider = finger_provider("book", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    answer = scene(state, "scene_01_01")
    assert answer["media"]["provider_id"] == "fresh"
    assert answer["rejected_media_identities"] == ["pexels:photo:answer"]
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01")
    assert repair["action"] == "replace_media" and repair["outcome"] == "resolved" and repair["improved"]
    assert repair["before"]["media_identity"] == "pexels:photo:answer" and repair["after"]["media_identity"] == "pexels:photo:fresh"
    assert review["status"] == "repaired" and review["summary"]["label"] == "Repaired 1 scene"
    assert review["changed_scenes"] == ["scene_01_01"] and harness.renders == 1
    assert "wrong_media" in issue_codes(review, "scene_01_01", "initial_issues")
    assert "wrong_media" not in issue_codes(review, "scene_01_01")
    assert frame_concepts(review, "scene_01_01", tmp_path)["hand"] > 0.6
    # Only the wrong scene was touched.
    assert scene(state, "scene_03_01")["media"]["provider_id"] == "grip"


# ---------------------------------------------------------------------------
# 3, 4, 12 — generated fallback and its budget
# ---------------------------------------------------------------------------

def test_generated_image_is_the_bounded_fallback_when_no_real_alternative_exists(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("book")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    generator = PaintingGenerator("hand")
    harness = Harness(tmp_path, provider, generator=generator)

    review = harness.review(state)

    answer = scene(state, "scene_01_01")
    assert answer["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1
    records = [item for item in state["visual_director"]["generations"] if item.get("billed")]
    assert len(records) == 1 and records[0]["trigger"] == "auto" and records[0]["scene_id"] == "scene_01_01"
    assert records[0]["model"] == "gpt-image-2" and records[0]["quality"] == "low"
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01")
    assert repair["outcome"] == "resolved"
    assert repair["generation_budget"] == {"auto_generated_before": 0, "auto_generated_after": 1, "project_limit": 3}
    assert review["status"] == "repaired"


def test_exhausted_generation_budget_is_never_bypassed_and_the_issue_is_reported(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("book")
    media_pass(state, tmp_path, provider)
    state["visual_director"]["generations"] = [
        {"scene_id": f"other_{index}", "trigger": "auto", "billed": True, "status": "accepted"} for index in range(3)
    ]
    render(state, tmp_path)
    generator = PaintingGenerator("hand")
    harness = Harness(tmp_path, provider, generator=generator)

    review = harness.review(state)

    assert generator.prompts == []  # no paid call
    assert visual_director.generation_counts(state)["auto_generated_images"] == 3
    answer = scene(state, "scene_01_01")
    assert answer["media"]["identity"] == "pexels:photo:answer"
    assert answer["visual_director"]["generation"]["status"] == "project_budget_exhausted"
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01")
    assert repair["status"] == "no_alternative" and repair["outcome"] == "unresolved"
    assert repair["generation_budget"]["auto_generated_after"] == repair["generation_budget"]["auto_generated_before"] == 3
    # 12: unresolved issue is reported, pointing at the scene for a manual fix.
    assert review["status"] == "issues_remain"
    assert "scene_01_01:semantic_match:wrong_media" in review["unresolved"]
    issue = next(item for item in review["issues"] if item["code"] == "wrong_media")
    assert issue["scene_number"] == 1 and issue["severity"] == "error"
    assert review["summary"]["remaining_issue_count"] >= 1
    assert "remain" in review["summary"]["label"]


# ---------------------------------------------------------------------------
# 5, 6 — overlay dominance and an overlay-only repair
# ---------------------------------------------------------------------------

def test_dominant_overlay_is_detected_and_only_the_overlay_is_repaired(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    for item in state["scenes"]:
        if item["block_id"] == "voice_block_02":
            item["visual_director"]["overlay_spec"] = {
                "kind": "process",
                "steps": ["Nerven lassen die Blutgefäße enger", "Das Gewebevolumen der Fingerkuppe", "Die Haut legt sich in tiefe Falten"],
            }
    media_pass(state, tmp_path, provider)  # cached media; overlays re-attached from the strategy
    render(state, tmp_path)
    media_before = {item["id"]: item["media"]["identity"] for item in state["scenes"]}
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    first = review["history"][0]
    dominant = {issue["scene_id"] for issue in first["issues"] if issue["code"] == "overlay_too_dominant"}
    assert "scene_02_02" in dominant
    repairs = [item for item in review["repairs"] if item["scene_id"] in dominant]
    for repair in repairs:
        # semantic_match good, overlay poor -> overlay only, never the whole scene.
        assert repair["action"] == "adjust_composition"
        assert set(repair["adjustments"]) == {"overlay"} and repair["adjustments"]["overlay"]["mode"] == "compact"
        assert repair["invalidated"] == ["overlay"]
        assert repair["outcome"] == "resolved"
    assert {item["id"]: item["media"]["identity"] for item in state["scenes"]} == media_before
    after = {item["scene_id"]: item for item in state["render"]["layout"]}
    for scene_id in dominant:
        overlay = after[scene_id]["overlays"][0]
        assert overlay["style"] == "minimal" and overlay["bbox_area"] < final_critic.OVERLAY_WARN_BBOX
        assert row(review, scene_id)["dimensions"]["overlay_quality"]["rating"] == "good"
    # 21: targeted invalidation — unchanged scenes reuse their encoded segments.
    assert after["scene_01_01"]["segment_cache"] == "hit" and after["scene_03_01"]["segment_cache"] == "hit"
    assert all(after[scene_id]["segment_cache"] == "miss" for scene_id in dominant)
    assert review["status"] == "repaired" and harness.renders == 1


def test_overlay_colliding_with_captions_is_moved(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    state["captions"]["position"] = "upper"  # captions now share the band the overlay uses
    target = scene(state, "scene_02_01")
    target["render_adjustments"] = {"overlay": {"placement": "upper"}}
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    assert "overlay_hits_captions" in issue_codes(review, "scene_02_01", "initial_issues")
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_02_01")
    assert repair["adjustments"]["overlay"]["placement"] == "lower"
    assert row(review, "scene_02_01")["dimensions"]["caption_overlay_collision"]["rating"] == "good"


# ---------------------------------------------------------------------------
# 7, 8 — repetition vs intentional continuity
# ---------------------------------------------------------------------------

def test_accidental_repetition_replaces_one_scene(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    payoff = scene(state, "scene_03_01")
    payoff["media"] = copy.deepcopy(scene(state, "scene_01_01")["media"])
    payoff["asset_status"] = "real_media_reused"
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    initial = issue_codes(review, "scene_03_01", "initial_issues")
    assert "accidental_repeat" in initial
    assert "accidental_repeat" not in issue_codes(review, "scene_01_01", "initial_issues")  # only one scene is blamed
    assert scene(state, "scene_03_01")["media"]["provider_id"] == "grip"
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "answer"
    assert review["status"] == "repaired"
    assert "accidental_repeat" not in issue_codes(review)


def test_intentional_block_continuity_is_not_punished(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    assert scene(state, "scene_02_02")["asset_status"] == "block_visual_continued"
    assert scene(state, "scene_02_02")["media"]["identity"] == scene(state, "scene_02_01")["media"]["identity"]

    review = detect_only(state, tmp_path, provider)

    repetition = row(review, "scene_02_02")["dimensions"]["repetition"]
    assert repetition == {"rating": "good", "reason": "intentional_continuity", "with": "scene_02_01"}
    assert not {"accidental_repeat", "unnecessary_asset_switch"} & issue_codes(review)


def test_unnecessary_asset_switch_inside_one_fact_continues_the_base_visual(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    # The second scene of the explanation switched to an unrelated clip.
    second = scene(state, "scene_02_02")
    city = Path(second["media"]["cache_path"]).with_name("photo-citycut.jpg")
    paint("city").save(tmp_path / city, format="JPEG")
    second["media"] = {**copy.deepcopy(second["media"]), "identity": "pexels:photo:citycut", "provider_id": "citycut", "cache_path": city.as_posix()}
    second["asset_status"] = "photo_ready"
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    assert "unnecessary_asset_switch" in issue_codes(review, "scene_02_02", "initial_issues")
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_02_02")
    assert repair["action"] == "continue_base_visual" and repair["base_scene_id"] == "scene_02_01"
    assert scene(state, "scene_02_02")["media"]["identity"] == scene(state, "scene_02_01")["media"]["identity"]
    assert row(review, "scene_02_02")["dimensions"]["repetition"]["reason"] == "intentional_continuity"
    assert review["status"] == "repaired"


# ---------------------------------------------------------------------------
# 9 — bad crop; still motion
# ---------------------------------------------------------------------------

def test_bad_crop_is_recalculated_without_replacing_the_asset(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    provider.sizes["answer"] = (1920, 1080)
    provider.regions["answer"] = (0.0, 0.4)  # the hand is at the left edge of a landscape photo
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)  # centre crop (no smart-crop model at render time)

    review = Harness(tmp_path, provider).review(state)

    assert "subject_lost_in_render" in issue_codes(review, "scene_01_01", "initial_issues")
    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01")
    assert repair["action"] == "adjust_composition"
    assert repair["adjustments"]["crop"]["center_x"] < 0.4
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "answer"
    assert state["render"]["layout"][0]["crop"]["mode"] == "critic_adjusted"
    assert repair["outcome"] == "resolved"
    assert row(review, "scene_01_01")["dimensions"]["subject_visibility"]["rating"] == "good"
    assert frame_concepts(review, "scene_01_01", tmp_path)["hand"] > 0.5
    # Crop and motion survive retiming, so later renders keep the repair.
    assert scene(state, "scene_01_01")["render_adjustments"]["source"] == "final_critic"


class SpreadVerifier(PixelVerifier):
    """Frames of one scene degrade across the still motion (subject drifts out)."""

    def score_video_frames(self, frames, texts, *, asset_identity="video"):
        result = super().score_video_frames(frames, texts, asset_identity=asset_identity)
        if "fingertip skin" in " ".join(texts).casefold() and len(frames) == 3 and ":scene_02_02" in asset_identity and "->" not in asset_identity:
            from clipforge.visual_verifier import VisualVerification

            return VisualVerification(0.3, "verified", "local_video_frames", (0.36, 0.3, 0.15), 3, 0.3, 0.3, 0.1, 0.3, 0.1, False)
        return result


def test_awkward_still_motion_is_switched_off(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    assert state["render"]["layout"][2]["motion"]["type"] != "static"

    review = Harness(tmp_path, provider, verifier=SpreadVerifier(concepts=provider.concepts)).review(state)

    repair = next(item for item in review["repairs"] if item["scene_id"] == "scene_02_02")
    assert repair["adjustments"]["motion"] == "static"
    assert state["render"]["layout"][2]["motion"]["type"] == "static"
    assert scene(state, "scene_02_02")["media"]["identity"] == scene(state, "scene_02_01")["media"]["identity"]


# ---------------------------------------------------------------------------
# 10 — user-locked media
# ---------------------------------------------------------------------------

def test_manually_selected_asset_is_reported_but_never_replaced(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("book", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})
    media_pass(state, tmp_path, finger_provider("book"))
    answer = scene(state, "scene_01_01")
    answer["media"]["manually_selected"] = True  # what Change Media persists
    answer["user_locked_visual"] = True
    render(state, tmp_path)
    assert scene(state, "scene_01_01")["user_locked_visual"] is True  # survives retiming
    harness = Harness(tmp_path, provider, generator=PaintingGenerator("hand"))

    review = harness.review(state)

    assert scene(state, "scene_01_01")["media"]["identity"] == "pexels:photo:answer"
    assert harness.renders == 0 and harness.generator.prompts == []
    blocked = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01")
    assert blocked["status"] == "blocked" and blocked["blocked_reason"] == "user_locked_visual"
    assert blocked["message"] == "Manual asset appears weak; it was not replaced automatically."
    assert {"wrong_media", "manual_asset_weak"} <= issue_codes(review, "scene_01_01")
    assert row(review, "scene_01_01")["user_locked"] is True
    assert review["status"] == "issues_remain"


def test_manual_changes_are_recorded_and_lock_the_scene():
    scene_state = {"id": "scene_01_01", "media": {"identity": "pexels:photo:new"}}
    state = {"scenes": [scene_state], "final_quality_review": {"issues": [{"id": "scene_01_01:semantic_match:wrong_media", "scene_id": "scene_01_01"}], "repairs": [{"scene_id": "scene_01_01", "status": "applied"}]}}
    record_manual_change(state, scene_state, "real_media_user_selected")
    override = state["final_quality_review"]["user_overrides"][0]
    assert override["addressed_issue_ids"] == ["scene_01_01:semantic_match:wrong_media"] and override["after_auto_repair"] is True
    from clipforge.media_candidates import _mark_manual

    _mark_manual(scene_state, "real_media_user_selected", "stock_photo")
    assert user_locked_visual(scene_state)
    record_manual_change({"scenes": []}, scene_state, "x")  # old projects: no review, no error


# ---------------------------------------------------------------------------
# 11, 17 — Story Arc reveal safety (Sweden / Indonesia)
# ---------------------------------------------------------------------------

def islands(monkeypatch, tmp_path) -> dict:
    silent_voice(monkeypatch)
    return small_timeline(generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target="subject_b"))


def island_provider(hook_concept: str = "indonesia", *, hook_extra=()) -> ImageProvider:
    return ImageProvider(
        photos={
            "indonesian islands aerial": [photo("id", "q", "Indonesian islands aerial drone view"), *hook_extra],
            "swedish archipelago": [photo("se", "q", "Swedish archipelago islands from above")],
            "glacier carved coastline": [photo("gl", "q", "Glacier carved rocky coastline")],
            "indonesian island nation": [photo("nation", "q", "Indonesian island nation aerial islands")],
        },
        concepts={"id": hook_concept, "se": "sweden", "gl": "glacier", "nation": "indonesia", "id2": "indonesia", "se2": "sweden"},
    )


def before_reveal(state: dict) -> list[dict]:
    return [item for item in state["scenes"] if item.get("story_stage") == "before_reveal"]


def shows_sweden(media: dict) -> bool:
    return any(term in final_critic._media_text(media).casefold() for term in ("swed", "schwed"))


def test_sweden_indonesia_final_render_keeps_the_story_arc(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    provider = island_provider()
    media_pass(state, tmp_path, provider)
    review = detect_only(state, tmp_path, provider)

    assert review["status"] == "passed"
    hook = row(review, "scene_01_01")
    assert hook["story_stage"] == "before_reveal" and not hook["reveal_allowed"]
    assert hook["dimensions"]["reveal_safety"]["rating"] == "good"
    # Indonesia evidence is allowed before the reveal; no Sweden pixels there.
    assert frame_concepts(review, "scene_01_01", tmp_path)["indonesia"] > 0.5
    assert frame_concepts(review, "scene_01_01", tmp_path)["sweden"] < 0.05
    answer = [item for item in review["scenes"] if item["visual_role"] == "primary_answer"]
    assert answer and all(item["story_stage"] == "reveal" for item in answer)
    assert frame_concepts(review, answer[0]["scene_id"], tmp_path)["sweden"] > 0.5
    final = next(item for item in review["scenes"] if item["visual_role"] == "final_payoff")
    # The final payoff stays visually distinct from the primary answer.
    assert final["media"]["identity"] not in {item["media"]["identity"] for item in answer}
    assert frame_concepts(review, final["scene_id"], tmp_path)["indonesia"] > 0.5


def test_repair_before_the_reveal_cannot_introduce_the_protected_answer(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    media_pass(state, tmp_path, island_provider("city"))  # hook passed metadata checks, shows a city
    render(state, tmp_path)
    # The targeted re-search is offered Swedish footage first.
    provider = island_provider(
        "city", hook_extra=(photo("se2", "q", "Swedish archipelago islands from above"), photo("id2", "q", "Indonesian islands from a boat")),
    )
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    hook = scene(state, "scene_01_01")
    assert "wrong_media" in issue_codes(review, "scene_01_01", "initial_issues")
    assert hook["media"]["provider_id"] == "id2"
    for item in before_reveal(state):
        assert not shows_sweden(item["media"])
        assert frame_concepts(review, item["id"], tmp_path)["sweden"] < 0.05
        assert row(review, item["id"])["dimensions"]["reveal_safety"]["rating"] == "good"
    assert not any(issue["category"] == "reveal_safety" for issue in review["issues"])


def test_answer_visual_shown_before_the_reveal_is_flagged_and_replaced(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    provider = island_provider()
    media_pass(state, tmp_path, provider)
    hook = scene(state, "scene_01_01")
    answer = next(item for item in state["scenes"] if item.get("is_primary_answer"))
    hook["media"] = copy.deepcopy(answer["media"])
    hook["asset_status"] = "real_media_reused"
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    reveal = [issue for issue in review["initial_issues"] if issue["scene_id"] == "scene_01_01" and issue["category"] == "reveal_safety"]
    assert reveal and all(issue["severity"] == "error" for issue in reveal)
    assert not shows_sweden(scene(state, "scene_01_01")["media"])
    assert frame_concepts(review, "scene_01_01", tmp_path)["sweden"] < 0.05
    assert review["status"] == "repaired"


def test_final_payoff_that_collapses_into_the_answer_visual_is_repaired(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    provider = island_provider()
    media_pass(state, tmp_path, provider)
    final = next(item for item in state["scenes"] if item.get("is_final_payoff"))
    answer = next(item for item in state["scenes"] if item.get("is_primary_answer"))
    final["media"] = copy.deepcopy(answer["media"])
    final["asset_status"] = "related_media_reused"
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    assert "repeats_primary_answer_visual" in issue_codes(review, final["id"], "initial_issues")
    repaired = scene(state, final["id"])
    assert repaired["media"]["provider_id"] == "nation"
    assert repaired["media"]["identity"] != scene(state, answer["id"])["media"]["identity"]
    assert review["status"] == "repaired"


# ---------------------------------------------------------------------------
# Full-screen graphic -> base visual + lightweight overlay (wet fingers)
# ---------------------------------------------------------------------------

def test_fullscreen_process_card_becomes_hand_background_with_light_overlay(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    junk = {HAND_Q: [photo("book", HAND_Q, "Old book page with printed text")], SKIN_Q: [photo("book", SKIN_Q, "Old book page with printed text")]}
    initial = ImageProvider(photos={**junk, GRIP_Q: [photo("grip", GRIP_Q, "Wrinkled fingers gripping a wet stone")]}, concepts={"book": "book", "grip": "hand"})
    state["visual_director"] = {"policy": {"max_auto_generated_images_per_project": 0}, "generations": []}
    media_pass(state, tmp_path, initial, generator=PaintingGenerator("hand"))
    # No base visual existed when the explanation was planned: a full-screen card.
    explanation = [item["id"] for item in state["scenes"] if item["block_id"] == "voice_block_02"]
    assert explanation and all(scene(state, scene_id)["media"]["source"] == "simple_graphic" for scene_id in explanation)
    render(state, tmp_path)
    provider = ImageProvider(photos={**initial.photos, HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]},
                             concepts={**initial.concepts, "fresh": "hand"})
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    first = review["history"][0]
    assert {issue["code"] for issue in first["issues"] if issue["scene_id"] in explanation} >= {"fullscreen_graphic_with_base"}
    layout = {item["scene_id"]: item for item in state["render"]["layout"]}
    hand_identities = {scene(state, "scene_01_01")["media"]["identity"], scene(state, "scene_03_01")["media"]["identity"]}
    for scene_id in explanation:
        item = scene(state, scene_id)
        assert item["media"]["identity"] in hand_identities and item["media"]["source"] == "pexels"
        assert item["visual_director"]["composition"] == "base_with_overlay"
        overlays = layout[scene_id]["overlays"]
        assert overlays and overlays[0]["kind"] == "process"
        assert overlays[0]["bbox_area"] < final_critic.OVERLAY_POOR_BBOX and overlays[0]["coverage"] < final_critic.OVERLAY_POOR_COVERAGE
        assert frame_concepts(review, scene_id, tmp_path)["hand"] > 0.5
        assert row(review, scene_id)["dimensions"]["repetition"]["rating"] == "good"  # deliberate continuation
        repair = next(record for record in review["repairs"] if record["scene_id"] == scene_id)
        assert repair["action"] == "convert_graphic_to_overlay" and repair["outcome"] == "resolved"
    assert review["status"] == "repaired" and harness.renders == 1
    assert not {"fullscreen_graphic_with_base", "accidental_repeat"} & issue_codes(review)


# ---------------------------------------------------------------------------
# 13 — bounded loop
# ---------------------------------------------------------------------------

class FreshMislabelled(ImageProvider):
    """Every search for the answer returns a new asset titled as fingertips that shows a city."""

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


@pytest.mark.parametrize(("configured", "explicit", "expected"), [(1, None, 1), (5, None, 2), (2, None, 2), (1, 0, 0)])
def test_repair_passes_are_bounded(monkeypatch, tmp_path, configured, explicit, expected):
    state = fingers(monkeypatch, tmp_path)
    provider = FreshMislabelled()
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    settings = critic_settings(tmp_path, final_critic_max_repair_passes=configured)
    verifier = PixelVerifier(concepts=defaultdict(lambda: "hand"))  # search-time check is fooled every time
    harness = Harness(tmp_path, provider, verifier=verifier, settings=settings)

    review = harness.review(state, **({} if explicit is None else {"max_passes": explicit}))

    assert harness.renders == expected == review["repair_pass_count"]
    assert review["max_repair_passes"] == min(expected if explicit is not None else configured, MAX_REPAIR_PASSES_LIMIT)
    assert len(review["history"]) == expected + 1  # one validation review per repair render
    assert review["status"] == "issues_remain"
    assert max_repair_passes(settings) <= MAX_REPAIR_PASSES_LIMIT


def test_failed_repair_render_restores_the_original_render(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("book", extra={HAND_Q: [photo("fresh", HAND_Q, "Close-up of wrinkled wet fingertips after a bath")]})
    media_pass(state, tmp_path, finger_provider("book"))
    render(state, tmp_path)
    video = rendered(state, tmp_path)
    original = video.read_bytes()
    harness = Harness(tmp_path, provider)

    def broken(_state):
        video.write_bytes(b"partial")
        raise RuntimeError("ffmpeg crashed")

    harness.rerender = broken
    review = harness.review(state)

    assert video.read_bytes() == original
    assert scene(state, "scene_01_01")["media"]["identity"] == "pexels:photo:answer"
    assert review["repair_error"] == "ffmpeg crashed" and review["status"] == "issues_remain"
    assert all(item["status"] == "failed" for item in review["repairs"])


# ---------------------------------------------------------------------------
# 14 — old projects
# ---------------------------------------------------------------------------

def test_old_renders_without_layout_are_reported_as_not_reviewed(tmp_path):
    state = {"render": {"status": "complete", "url": "/media/project/renders/v1/clipforge.mp4"}, "scenes": [{"id": "scene_01"}]}
    review = run_final_quality_review(state, project_id="project", revision=1, settings=critic_settings(tmp_path), verifier=PixelVerifier())
    assert review["status"] == "not_reviewed" and review["reason"] == "render_layout_unavailable"
    assert review["summary"]["label"] == "Not reviewed"


def test_legacy_project_without_story_arc_or_director_state_is_reviewed(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    _strip_story(state)
    provider = finger_provider("book")
    media_pass(state, tmp_path, provider)
    for item in state["scenes"]:
        item.pop("visual_director", None)
    review = detect_only(state, tmp_path, provider)
    assert review["status"] == "issues_remain"
    assert "wrong_media" in issue_codes(review, "scene_01_01")


def test_rendered_window_whose_scene_was_retimed_away_is_not_judged(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("book")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    state["render"]["layout"][0]["scene_id"] = "scene_01_09"  # no longer in the retimed state

    review = Harness(tmp_path, provider).review(state)

    assert row(review, "scene_01_09")["dimensions"]["semantic_match"] == {"rating": "unavailable", "reason": "scene_retimed"}
    assert not issue_codes(review, "scene_01_09") and review["repairs"] == []


def test_build_scenes_keeps_locks_and_repairs_through_retiming():
    blocks = [{"id": "voice_block_01", "role": "answer", "text": "One two three four five six."}]
    old = [{"id": "scene_01_01", "block_id": "voice_block_01", "user_locked_visual": True, "render_adjustments": {"motion": "static"},
            "rejected_media_identities": ["pexels:photo:x"], "visual_continuity": {"source_scene_id": "a"}, "media": {"identity": "i"}}]
    rebuilt = _build_scenes(blocks, 5.0, old)[0]
    assert rebuilt["user_locked_visual"] is True and rebuilt["render_adjustments"] == {"motion": "static"}
    assert rebuilt["rejected_media_identities"] == ["pexels:photo:x"] and rebuilt["visual_continuity"] == {"source_scene_id": "a"}
    assert "render_adjustments" not in _build_scenes(blocks, 5.0, [{"id": "scene_01_01", "block_id": "voice_block_01"}])[0]


# ---------------------------------------------------------------------------
# 15 — an unseen topic, decided by structure only
# ---------------------------------------------------------------------------

OCTOPUS_Q = "Warum hat ein Oktopus drei Herzen?"
OCTOPUS = [
    (fact("Ein Oktopus hat drei Herzen: zwei Kiemenherzen und ein Körperherz.", 0.95), "answer",
     visual("octopus swimming close-up", ["octopus swimming close-up"], ["shared"])),
    (fact("Die Kiemenherzen pumpen Blut durch die Kiemen; das Körperherz versorgt den übrigen Körper."), "explanation",
     visual("octopus tentacles on the seabed", ["octopus tentacles seabed"], ["shared"])),
    (fact("Beim Schwimmen setzt das Körperherz sogar kurz aus.", 0.7), "payoff",
     visual("octopus jet swimming", ["octopus jet swimming"], ["shared"])),
]


def test_unseen_topic_is_reviewed_and_repaired_from_structure_alone(monkeypatch, tmp_path):
    silent_voice(monkeypatch)
    state = small_timeline(generate(monkeypatch, tmp_path, OCTOPUS_Q, OCTOPUS, planner_target=""))
    rows = {
        "octopus swimming close-up": [photo("o-city", "q", "Octopus swimming close-up in the ocean")],
        "octopus tentacles seabed": [photo("o-tent", "q", "Octopus tentacles on the seabed")],
        "octopus jet swimming": [photo("o-jet", "q", "Octopus jet swimming underwater")],
    }
    concepts = {"o-city": "city", "o-tent": "octopus", "o-jet": "octopus", "o-real": "octopus"}
    media_pass(state, tmp_path, ImageProvider(photos=rows, concepts=concepts))
    render(state, tmp_path)
    repair_rows = {**rows, "octopus swimming close-up": [*rows["octopus swimming close-up"], photo("o-real", "q", "Octopus swimming over a reef")]}
    harness = Harness(tmp_path, ImageProvider(photos=repair_rows, concepts=concepts))

    review = harness.review(state)

    assert "wrong_media" in issue_codes(review, "scene_01_01", "initial_issues")
    assert scene(state, "scene_01_01")["media"]["provider_id"] == "o-real"
    assert review["status"] == "repaired"
    source = inspect.getsource(final_critic).casefold()
    for word in ("finger", "wrinkl", "swed", "schwed", "indones", "octopus", "oktopus", "island", "insel"):
        assert word not in source


# ---------------------------------------------------------------------------
# Wiring, persistence and the optional vision critic
# ---------------------------------------------------------------------------

def test_render_state_persists_the_review_and_a_critic_failure_never_fails_the_render(monkeypatch, tmp_path):
    from clipforge import services

    state = fingers(monkeypatch, tmp_path)
    settings = critic_settings(tmp_path)
    monkeypatch.setattr(services, "prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr(services, "run_ai_review", lambda *_a, **_k: None)
    monkeypatch.setattr(services, "render_video", lambda *_a, **_k: RenderResult("/media/project/renders/v2/clipforge.mp4", 12.0, "test", 100, ()))

    rendered_state = services._render_state(state, "project", 2, settings)
    assert rendered_state["render"]["status"] == "complete" and rendered_state["render"]["layout"] == []
    assert rendered_state["final_quality_review"]["status"] == "not_reviewed"
    assert all(item.get("story_stage") for item in rendered_state["scenes"])  # arc annotations survive retiming

    def explode(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "run_final_quality_review", explode)
    rendered_state = services._render_state(state, "project", 3, settings)
    assert rendered_state["render"]["status"] == "complete"
    assert rendered_state["final_quality_review"]["reason"] == "critic_failed"
    disabled = services._render_state(state, "project", 4, critic_settings(tmp_path, final_critic_enabled=False))
    assert disabled["final_quality_review"]["status"] == "disabled" and disabled["final_quality_review"]["revision"] == 4


def test_generation_progress_plan_includes_the_final_review(db):
    from clipforge.generation import build_stage_plan
    from clipforge.schemas import ProjectCreate

    plan = build_stage_plan(db, ProjectCreate(prompt="Warum ist der Himmel blau?"))
    assert plan[-1]["id"] == "quality_review" and plan[-2]["id"] == "finalizing"


def test_optional_vision_critic_is_disabled_by_default_and_pluggable(monkeypatch, tmp_path):
    settings = critic_settings(tmp_path)
    assert settings.final_critic_vision_provider == "none"
    assert final_critic.get_vision_critic(settings) is None

    class LocalVision:
        name = "test_vision"

        def review_scene(self, frames, context):
            assert frames and all(Path(frame).is_file() for frame in frames)
            return [{"category": "story_alignment", "code": "odd", "message": "Looks off."}] if context["scene_id"] == "scene_03_01" else []

    monkeypatch.setitem(final_critic.VISION_CRITICS, "test_vision", lambda _settings: LocalVision())
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    review = Harness(tmp_path, provider, settings=critic_settings(tmp_path, final_critic_vision_provider="test_vision")).review(state, max_passes=0)
    assert review["reviewer"]["vision"] == "test_vision"
    assert "vision_odd" in issue_codes(review, "scene_03_01")
    assert review["status"] == "passed_with_warnings"


def test_summary_labels_are_compact():
    summary = final_critic._summary
    assert summary("passed", [], [], [])["label"] == "Passed"
    assert summary("repaired", ["a", "b"], [], [])["label"] == "Repaired 2 scenes"
    assert summary("issues_remain", [], [{"id": 1}], [])["label"] == "1 issue remains"
    assert summary("issues_remain", ["a"], [{"id": 1}, {"id": 2}], [])["label"] == "Repaired 1 scene · 2 issues remain"
    assert summary("passed_with_warnings", [], [], [{"id": 1}])["label"] == "Passed · 1 note"


def test_concept_palette_is_distinct():
    colours = [colour for colour, _words in CONCEPTS.values()]
    assert len(set(colours)) == len(colours)
