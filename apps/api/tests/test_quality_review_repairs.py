"""Quality Review UX + safe auto-repair: measured repairs and an honest per-issue report.

Render scenarios use the same harness as the other Final Video Critic tests:
real MP4s from the bundled ffmpeg, ``PixelVerifier`` judging real pixels, a
local fake image API and no network.  Planner and report tests build the
critic's own objects directly (no render needed).  Nothing asserts
topic-specific wording; concept colours are test fixtures only.
"""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest
from critic_support import (
    Harness,
    ImageProvider,
    PaintingGenerator,
    PixelVerifier,
    critic_settings,
    media_pass,
    paint,
    photo,
    render,
    silent_voice,
    small_timeline,
)
from test_final_critic import finger_provider, fingers, issue_codes, scene
from test_story_visual_integration import fact, generate, visual

from clipforge import final_critic, visual_director
from clipforge.ai import AIStoryArc, AIStoryUnit
from clipforge.attention import callout_suppressed, replan_attention, visible_attention_events
from clipforge.final_critic import (
    DEFAULT_MAX_REPAIR_PASSES,
    FIXED,
    MANUAL,
    MAX_REPAIR_PASSES_LIMIT,
    REASON_TEXT,
    UNFIXABLE,
    build_issue_report,
    max_repair_passes,
    plan_repairs,
    report_counts,
    reveal_problems,
)
from clipforge.renderer import _write_ass_captions, ffmpeg_path
from clipforge.triple_hook import SOURCE as TRIPLE_HOOK_SOURCE

# ---------------------------------------------------------------------------
# Per-issue report (fixed / needs the user / not safely fixable)
# ---------------------------------------------------------------------------


def issue(scene_id: str, number: int, category: str, code: str, severity: str = "error", **evidence) -> dict:
    return {
        "id": f"{scene_id}:{category}:{code}", "scene_id": scene_id, "scene_number": number, "category": category,
        "code": code, "severity": severity, "message": f"{code} in scene {number}", "evidence": evidence,
    }


CROP = issue("s1", 1, "subject_visibility", "subject_lost_in_render")
MOTION = issue("s1", 1, "motion", "motion_loses_subject")
OVERLAY = issue("s2", 2, "overlay_quality", "overlay_too_dominant")
NO_VISUAL = issue("s3", 3, "semantic_match", "wrong_media")
TEXT = issue("s4", 4, "visual_quality", "text_heavy", text_layers=["overlay"])
HOOK = issue("s5", 5, "hook_alignment", "hook_verbal_reveals_answer")
WEAK = issue("s6", 6, "semantic_match", "weak_media")
BASE_TEXT = issue("s7", 7, "visual_quality", "text_heavy", text_layers=["base_visual"])

REPAIRS = [
    {"scene_id": "s1", "scene_number": 1, "action": "adjust_composition", "reframe": True, "status": "unresolved", "repair_attempted": True,
     "issue_ids": [CROP["id"], MOTION["id"]], "unresolved_reason": "framing_not_improved", "media_unresolved_reason": "project_budget_exhausted", "outcome": "unresolved"},
    {"scene_id": "s2", "scene_number": 2, "action": "adjust_composition", "status": "applied", "repair_attempted": True, "issue_ids": [OVERLAY["id"]],
     "adjustments": {"overlay": {"mode": "compact"}}, "accepted_step": "overlay_adjustment", "outcome": "resolved", "repair_effective": True,
     "result": {"resolved": [OVERLAY["id"]], "remaining": [], "new_reveal_issues": []}, "before_score": 0.2, "after_score": 0.31},
    {"scene_id": "s3", "scene_number": 3, "action": "replace_media", "status": "unresolved", "repair_attempted": True, "issue_ids": [NO_VISUAL["id"]],
     "unresolved_reason": "project_budget_exhausted", "outcome": "unresolved"},
    {"scene_id": "s4", "scene_number": 4, "action": "report_only", "status": "blocked", "issue_ids": [TEXT["id"]], "blocked_reason": "informative_text_kept"},
    # Code ran and something changed on screen, but the triggering issue is still there.
    {"scene_id": "s6", "scene_number": 6, "action": "replace_media", "status": "applied", "repair_attempted": True, "issue_ids": [WEAK["id"]],
     "accepted_step": "best_available", "unresolved_reason": "no_sufficiently_relevant_visual", "outcome": "unresolved", "repair_effective": False,
     "result": {"resolved": [], "remaining": [WEAK["id"]], "new_reveal_issues": []}},
]


def report_for(*, locked: set[str] = frozenset(), final_extra: list[dict] | None = None) -> list[dict]:
    initial = [CROP, MOTION, OVERLAY, NO_VISUAL, TEXT, HOOK, WEAK, BASE_TEXT]
    final = [item for item in initial if item is not OVERLAY] + (final_extra or [])
    state = {"scenes": [{"id": f"s{number}", "user_locked_visual": f"s{number}" in locked} for number in range(1, 9)]}
    return build_issue_report(initial=initial, final=final, unresolved=final, repairs=REPAIRS, state=state, pass_count=1, passes_allowed=1)


def entry(report: list[dict], scene_id: str, code: str) -> dict:
    return next(item for item in report if item["scene_id"] == scene_id and code in {item["code"], *(value.split(":")[-1] for value in item["issue_ids"])})


def test_report_marks_only_measured_repairs_as_fixed_and_explains_every_remaining_issue():
    report = report_for()

    fixed = [item for item in report if item["status"] == FIXED]
    assert [item["scene_id"] for item in fixed] == ["s2"]
    assert fixed[0]["detail"] == "simplified the overlay" and fixed[0]["after_score"] > fixed[0]["before_score"]
    # A repair that ran but did not remove the issue is never "fixed".
    weak = entry(report, "s6", "weak_media")
    assert weak["status"] == MANUAL and weak["reason"] == "no_sufficiently_relevant_visual" and weak["attempted"]
    # One problem, one row: two framing codes of one scene are merged.
    framing = entry(report, "s1", "subject_lost_in_render")
    assert framing["title"] == "Subject leaves the frame" and sorted(framing["issue_ids"]) == sorted([CROP["id"], MOTION["id"]])
    assert framing["reason"] == "framing_not_improved"
    assert framing["reason_text"] == f"{REASON_TEXT['framing_not_improved']} {REASON_TEXT['project_budget_exhausted']}"
    budget = entry(report, "s3", "wrong_media")
    assert budget["status"] == MANUAL and budget["reason"] == "project_budget_exhausted"
    assert budget["fix"] == {"kind": "change_media", "label": "Change media", "scene_id": "s3", "scene_number": 3, "description": "Choose another visual for scene 3"}
    # Overlay text: Change Media cannot help, so no misleading Fix.
    text = entry(report, "s4", "text_heavy")
    assert text["status"] == UNFIXABLE and text["fix"] is None and text["reason"] == "informative_text_kept"
    # Text inside the media itself: another visual does help.
    assert entry(report, "s7", "text_heavy")["fix"]["kind"] == "change_media"
    hook = entry(report, "s5", "hook_verbal_reveals_answer")
    assert hook["status"] == UNFIXABLE and hook["reason"] == "needs_regeneration"
    # Every remaining issue is reported exactly once, and every reason is human text.
    remaining_ids = [value for item in report if item["status"] != FIXED for value in item["issue_ids"]]
    assert sorted(remaining_ids) == sorted(item["id"] for item in [CROP, MOTION, NO_VISUAL, TEXT, HOOK, WEAK, BASE_TEXT])
    for item in report:
        if item["status"] != FIXED:
            assert item["reason_text"] and "_" not in item["reason_text"]
    assert report_counts(report) == {FIXED: 1, MANUAL: 4, UNFIXABLE: 2}


def test_report_reason_for_locked_visuals_and_issues_that_appeared_after_the_repair():
    new = issue("s8", 8, "repetition", "accidental_repeat")
    report = report_for(locked={"s3", "s1"}, final_extra=[new])
    assert entry(report, "s3", "wrong_media")["reason"] == "user_locked_visual"
    # The lock blocks replacement only; the attempted framing repair is still explained.
    framing = entry(report, "s1", "subject_lost_in_render")
    assert framing["reason"] == "framing_not_improved"
    assert entry(report, "s8", "accidental_repeat")["reason"] == "appeared_after_repair"


# ---------------------------------------------------------------------------
# Planner: text-heavy scenes reduce drawn text layers, never the narration
# ---------------------------------------------------------------------------


def planner_review(state: dict, tmp_path: Path, *entries: dict) -> final_critic._Review:
    review = final_critic._Review(
        state, project_id="project", revision=2, settings=critic_settings(tmp_path), verifier=None, pass_index=0, vision_critic=None,
    )
    for number, item in enumerate(entries, 1):
        review.rows.append(final_critic._Row(item, scene(state, item["scene_id"]), number))
    return review


def layout_entry(state: dict, scene_id: str, *, overlay: bool) -> dict:
    target = scene(state, scene_id)
    return {
        "scene_id": scene_id, "start": target["start"], "end": target["end"],
        "media": {"identity": f"pexels:photo:{scene_id}", "source": "pexels", "kind": "photo"},
        "overlays": [{"spec": {"kind": "process", "steps": ["Blutgefäße werden enger", "Haut legt sich in Falten"]}, "bbox_area": 0.05, "coverage": 0.03}] if overlay else [],
    }


def text_heavy_plan(state: dict, tmp_path: Path, scene_id: str, *, overlay: bool) -> tuple[list[str], dict]:
    review = planner_review(state, tmp_path, layout_entry(state, scene_id, overlay=overlay))
    row = review.rows[0]
    layers = review.text_layers(row)
    review._issue(row, "visual_quality", "text_heavy", "error", "text-heavy", text_layers=layers)
    return layers, plan_repairs(review)[0]


def callout(state: dict, scene_id: str, kind: str = "keyword_callout", text: str = "Falten") -> dict:
    index = next(position for position, item in enumerate(state["scenes"]) if item["id"] == scene_id)
    target = state["scenes"][index]
    return {"scene_index": index, "start": target["start"] + 0.1, "duration": 0.5, "type": kind, "text": text}


def test_text_heavy_scene_reduces_competing_callouts_and_the_overlay_first(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    narration = copy.deepcopy([item["narration"] for item in state["scenes"]])
    script = state["script"]["text"]
    state["attention_events"] = [callout(state, "scene_02_01"), callout(state, "scene_02_01", "statistic_callout", "3")]

    layers, action = text_heavy_plan(state, tmp_path, "scene_02_01", overlay=True)

    assert layers == ["overlay", "attention_callout"]  # the media itself is not blamed
    assert action["action"] == "adjust_composition" and action["status"] == "planned"
    # Competing layers: every text callout goes (captions stay) and the overlay is compacted.
    assert action["adjustments"] == {"overlay": {"mode": "compact"}, "attention": "no_text"}
    assert "replace" not in action["action"]
    # The planner never touches what is spoken.
    assert [item["narration"] for item in state["scenes"]] == narration and state["script"]["text"] == script


def test_callout_only_text_heavy_scene_keeps_its_statistic_and_its_visual(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    state["attention_events"] = [callout(state, "scene_03_01")]

    layers, action = text_heavy_plan(state, tmp_path, "scene_03_01", overlay=False)

    assert layers == ["attention_callout"]
    assert action["action"] == "adjust_composition" and action["adjustments"] == {"attention": "reduced"}


def test_informative_compact_overlay_is_kept_and_reported_not_removed(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    scene(state, "scene_02_01")["render_adjustments"] = {"overlay": {"mode": "compact"}, "source": "final_critic"}

    _layers, action = text_heavy_plan(state, tmp_path, "scene_02_01", overlay=True)

    # Already compact and informative: removing it would lose meaning.
    assert action["action"] == "report_only" and action["blocked_reason"] == "informative_text_kept"


def test_optional_hook_label_is_omitted_when_the_opening_stays_text_heavy(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    opening = scene(state, "scene_01_01")
    opening["visual_director"] = {**(opening.get("visual_director") or {}), "overlay_spec": {"kind": "label", "text": "Warum?", "source": TRIPLE_HOOK_SOURCE}}
    opening["render_adjustments"] = {"overlay": {"mode": "compact"}, "source": "final_critic"}

    layers, action = text_heavy_plan(state, tmp_path, "scene_01_01", overlay=True)

    assert layers == ["hook_label"]
    assert action["adjustments"]["overlay"]["mode"] == "remove"


def test_text_inside_the_media_itself_asks_for_another_visual(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    scene(state, "scene_03_01")["media"] = {"identity": "pexels:photo:page", "relevance": {"visual": {"presentation_risk": True}}}

    layers, action = text_heavy_plan(state, tmp_path, "scene_03_01", overlay=True)

    assert "base_visual" in layers
    assert action["action"] == "replace_media" and action["reason"] == "text_heavy"


def test_reduced_callouts_stay_reduced_through_replanning_and_never_reach_the_render(tmp_path):
    scenes = [{"id": "a", "start": 0.0, "end": 2.0}, {"id": "b", "start": 2.0, "end": 4.0}]
    items = [{"text": "42 Prozent mehr", "start": 0.2, "end": 0.9}, {"text": "Zuerst quillt die Haut", "start": 2.2, "end": 2.9}]
    state = {
        "intent": {"language": "de"}, "script": {"text": " ".join(item["text"] for item in items)}, "captions": {"enabled": True, "items": items},
        "scenes": scenes, "attention_preferences": {"attention_density": "high"}, "timeline": {"width": 1080, "height": 1920},
    }
    planned = replan_attention(state)
    assert {event["scene_index"] for event in planned} == {0, 1}

    scenes[0]["render_adjustments"] = {"attention": "no_text"}
    replanned = replan_attention(state)
    assert {event["scene_index"] for event in replanned} == {1}  # the repaired scene stays reduced on re-render
    # "reduced" keeps a statistic callout (it carries a number); "no_text" drops it.
    stat = {"scene_index": 0, "type": "statistic_callout", "text": "42", "start": 0.2, "duration": 0.5}
    keyword = {"scene_index": 0, "type": "keyword_callout", "text": "Haut", "start": 0.2, "duration": 0.5}
    symbol = {"scene_index": 0, "type": "icon_or_symbol", "text": "↑", "start": 0.2, "duration": 0.5}
    assert callout_suppressed(stat, scenes[0]) and callout_suppressed(keyword, scenes[0]) and not callout_suppressed(symbol, scenes[0])
    scenes[0]["render_adjustments"] = {"attention": "reduced"}
    assert not callout_suppressed(stat, scenes[0]) and callout_suppressed(keyword, scenes[0])
    # Whatever is withheld is not drawn into the burned-in text layer.
    state["attention_events"] = [stat, keyword, dict(keyword, scene_index=1, text="Falten", start=2.2)]
    assert visible_attention_events(state) == [stat, state["attention_events"][2]]
    ass = _write_ass_captions(state, 4, tmp_path).read_text()
    assert "Falten" in ass and "Dialogue: 1,0:00:00.20" in ass and ",Haut" not in ass.replace("}", ",")


def test_reveal_safety_still_sees_every_visible_label(monkeypatch, tmp_path):
    from test_final_critic import islands

    state = islands(monkeypatch, tmp_path)
    hook = next(item for item in state["scenes"] if item.get("story_stage") == "before_reveal")
    index = state["scenes"].index(hook)
    leak = {"scene_index": index, "start": hook["start"] + 0.1, "duration": 0.5, "type": "statistic_callout", "text": "Schweden 267.570"}
    state["attention_events"] = [leak]
    window = (hook["start"], hook["end"])
    assert {"component": "label", "code": "label_names_protected_answer"} in reveal_problems(hook, state, media={}, overlays=[], window=window)
    # A statistic callout survives "reduced", so it is still checked ...
    hook["render_adjustments"] = {"attention": "reduced"}
    assert reveal_problems(hook, state, media={}, overlays=[], window=window)
    # ... and only a callout that is really withheld stops counting.
    hook["render_adjustments"] = {"attention": "no_text"}
    assert reveal_problems(hook, state, media={}, overlays=[], window=window) == []


# ---------------------------------------------------------------------------
# Framing: crop before replacement, and only measured improvements count
# ---------------------------------------------------------------------------


def test_ineffective_crop_is_undone_and_never_reported_as_fixed(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    provider.sizes["answer"] = (1920, 1080)
    provider.regions["answer"] = (0.0, 0.4)  # the subject sits at the left edge of a landscape photo
    media_pass(state, tmp_path, provider)
    answer = scene(state, "scene_01_01")
    answer["media"]["manually_selected"] = True  # locked: framing may change, the visual may not
    answer["user_locked_visual"] = True
    answer["render_adjustments"] = {"motion": "static"}  # only the crop candidate exists
    render(state, tmp_path)
    identity = answer["media"]["identity"]
    # The recomputed crop points at an empty part of the photo: it cannot help.
    monkeypatch.setattr(final_critic, "_recalculated_crop", lambda _review, _row: {"center_x": 0.97, "center_y": 0.5, "source": "subject_window"})
    harness = Harness(tmp_path, provider, generator=PaintingGenerator("hand"))

    review = harness.review(state)

    # Nothing was re-rendered, so the reviewed issues are the final ones.
    assert "subject_lost_in_render" in issue_codes(review, "scene_01_01")
    record = next(item for item in review["repairs"] if item["scene_id"] == "scene_01_01" and item["action"] == "adjust_composition")
    step = record["steps"][0]
    assert step["step"] == "recompute_focal_crop" and not step["accepted"] and not step["improved"]
    assert step["reason"] == "did_not_improve_framing" and step["min_frame"] <= record["baseline"]["min_frame"] + final_critic.FRAMING_MIN_GAIN
    assert record["unresolved_reason"] == "framing_not_improved" and not record["repair_effective"] and record["outcome"] == "unresolved"
    # The change that did not help is undone; nothing changed, so nothing is re-rendered.
    assert "crop" not in scene(state, "scene_01_01")["render_adjustments"]
    assert harness.renders == 0 and harness.generator.prompts == []
    assert scene(state, "scene_01_01")["media"]["identity"] == identity
    assert "scene_01_01" not in review["repaired_scenes"]
    report = [item for item in review["report"] if item["scene_id"] == "scene_01_01"]
    assert all(item["status"] != FIXED for item in report)
    framing = next(item for item in report if item["title"] == "Subject leaves the frame")
    assert framing["status"] == MANUAL and framing["reason"] == "framing_not_improved"
    assert REASON_TEXT["user_locked_visual"] in framing["reason_text"]
    assert review["summary"]["fixed_count"] == 0


def test_crop_is_recalculated_for_video_clips_too(monkeypatch, tmp_path):
    state = fingers(monkeypatch, tmp_path)
    source = tmp_path / "project" / "media" / "clip-source.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    paint("hand", (1920, 1080), region=(0.0, 0.3)).save(source)
    clip = source.with_name("clip.mp4")
    subprocess.run([ffmpeg_path(), "-y", "-v", "error", "-loop", "1", "-i", str(source), "-t", "1", "-pix_fmt", "yuv420p", "-vf", "scale=960:540", str(clip)], check=True)
    answer = scene(state, "scene_01_01")
    answer["media"] = {"identity": "pexels:video:clip", "kind": "video", "source": "pexels", "cache_path": clip.relative_to(tmp_path).as_posix()}
    review = final_critic._Review(state, project_id="project", revision=2, settings=critic_settings(tmp_path), verifier=PixelVerifier(), pass_index=0, vision_critic=None)
    row = final_critic._Row({"scene_id": "scene_01_01", "media": answer["media"], "crop": {"center_x": 0.5, "center_y": 0.5}}, answer, 1)

    crop = final_critic._recalculated_crop(review, row)

    assert not row.still and crop is not None
    assert crop["source"] == "subject_window" and crop["center_x"] < 0.35


def test_repair_pass_limits_are_unchanged(tmp_path):
    assert (DEFAULT_MAX_REPAIR_PASSES, MAX_REPAIR_PASSES_LIMIT) == (1, 2)
    assert max_repair_passes(critic_settings(tmp_path)) == 1
    assert max_repair_passes(critic_settings(tmp_path, final_critic_max_repair_passes=9)) == 2


# ---------------------------------------------------------------------------
# Real-scenario regression: some repairs are safe, some are not
# ---------------------------------------------------------------------------

REGRESSION_Q = "Warum werden Finger im Wasser schrumpelig?"
REGRESSION = [
    (fact("Nach langem Baden werden die Fingerkuppen schrumpelig.", 0.95), "answer",
     visual("wrinkled wet fingertips after a bath", ["wrinkled wet fingertips"], ["shared"])),
    (fact("Nerven lassen die Blutgefäße in den Fingern enger werden; dadurch legt sich die Haut in Falten."), "explanation",
     visual("wrinkled fingertip skin close-up", ["wrinkled fingertip skin"], ["shared"])),
    (fact("Auch ein Krake bekommt im Wasser keine schrumpelige Haut an den Armen."), "support",
     visual("octopus tentacle underwater", ["octopus tentacle"], ["context"])),
    (fact("Bei einem Herzfehler kann die Durchblutung der Finger gestört sein."), "support",
     visual("heart blood circulation", ["heart blood circulation"], ["context"])),
    (fact("Gletscher formen über Jahrtausende tiefe Täler."), "support",
     visual("glacier meltwater stream", ["glacier meltwater"], ["context"])),
    (fact("Die Furchen helfen möglicherweise dabei, nasse Dinge besser festzuhalten.", 0.7), "payoff",
     visual("wrinkled fingers gripping a wet stone", ["wrinkled fingers gripping wet stone"], ["shared"])),
]
LONG_STEPS = ["Nerven lassen die Blutgefäße enger", "Das Gewebevolumen der Fingerkuppe", "Die Haut legt sich in tiefe Falten"]


def regression_provider() -> ImageProvider:
    titles = {
        "wrinkled wet fingertips": ("answer", "Wrinkled wet fingertips macro close-up"),
        "wrinkled fingertip skin": ("skin", "Wrinkled fingertip skin close-up"),
        "octopus tentacle": ("octojunk", "Octopus tentacle underwater close-up"),
        "heart blood circulation": ("heartjunk", "Heart blood circulation close-up"),
        "glacier meltwater": ("glacierpage", "Glacier meltwater stream"),
        "wrinkled fingers gripping wet stone": ("grip", "Wrinkled fingers gripping a wet stone"),
    }
    return ImageProvider(
        photos={query: [photo(provider_id, query, title)] for query, (provider_id, title) in titles.items()},
        # Metadata says "octopus", "heart", "glacier"; the pixels show a city, a bus stop and a printed page.
        concepts={"answer": "hand", "skin": "hand", "grip": "hand", "octojunk": "city", "heartjunk": "bus", "glacierpage": "book"},
        sizes={"answer": (1920, 1080)}, regions={"answer": (0.0, 0.4)},
    )


@pytest.fixture
def regression(monkeypatch, tmp_path):
    silent_voice(monkeypatch)
    # The planner vouches for every beat: this regression is about the
    # mislabelled visuals, not about retention pruning of weak facts.
    arc = AIStoryArc(units=[AIStoryUnit(fact_index=index, role="supporting_fact", serves_question=True) for index in (3, 4, 5)])
    state = small_timeline(generate(monkeypatch, tmp_path, REGRESSION_Q, REGRESSION, planner_target="", story_arc=arc))
    provider = regression_provider()
    media_pass(state, tmp_path, provider)  # metadata-only: the mislabelled assets pass
    # A text-heavy explanation card ...
    scene(state, "scene_02_01")["visual_director"]["overlay_spec"] = {"kind": "process", "steps": LONG_STEPS}
    # ... and a mid-fact switch to an unrelated clip.
    second = scene(state, "scene_02_02")
    city = Path(second["media"]["cache_path"]).with_name("photo-citycut.jpg")
    paint("city").save(tmp_path / city, format="JPEG")
    second["media"] = {**copy.deepcopy(second["media"]), "identity": "pexels:photo:citycut", "provider_id": "citycut", "cache_path": city.as_posix()}
    second["asset_status"] = "photo_ready"
    # The user picked this (weak) visual themselves.
    locked = scene(state, "scene_04_01")
    locked["media"]["manually_selected"] = True
    locked["user_locked_visual"] = True
    media_pass(state, tmp_path, provider)
    # The automatic AI image budget is already used up.
    state["visual_director"]["generations"] = [{"scene_id": f"other_{index}", "trigger": "auto", "billed": True, "status": "accepted"} for index in range(3)]
    render(state, tmp_path)
    return state, provider, tmp_path


def test_regression_safe_fixes_happen_unsafe_ones_stay_visible_and_explained(regression):
    state, provider, tmp_path = regression
    before = {item["id"]: item["media"]["identity"] for item in state["scenes"]}
    narration = [item["narration"] for item in state["scenes"]]
    script = state["script"]["text"]
    generator = PaintingGenerator("hand")
    harness = Harness(tmp_path, provider, generator=generator)

    review = harness.review(state)

    initial = issue_codes(review, key="initial_issues")
    assert {"subject_lost_in_render", "unnecessary_asset_switch", "text_heavy", "wrong_media"} <= initial
    by_scene = {}
    for item in review["report"]:
        by_scene.setdefault(item["scene_id"], []).append(item)

    # Safe fixes happen: crop, overlay text and same-fact continuity.
    crop = next(item for item in by_scene["scene_01_01"] if item["title"] == "Subject leaves the frame")
    assert crop["status"] == FIXED and crop["after_score"] > crop["before_score"]
    assert scene(state, "scene_01_01")["media"]["identity"] == before["scene_01_01"]  # reframed, not replaced
    assert any(item["status"] == FIXED and item["category"] in {"overlay_quality", "overlay_semantics"} for item in by_scene["scene_02_01"])
    switch = next(item for item in by_scene["scene_02_02"] if item["code"] == "unnecessary_asset_switch")
    assert switch["status"] == FIXED
    assert scene(state, "scene_02_02")["media"]["identity"] == scene(state, "scene_02_01")["media"]["identity"]
    # Unsafe ones stay unresolved, with the reason, and keep their (reported) visual.
    for scene_id in ("scene_03_01", "scene_05_01"):
        entries = [item for item in by_scene[scene_id] if item["code"] == "wrong_media"]
        assert entries and entries[0]["status"] == MANUAL and entries[0]["reason"] == "project_budget_exhausted"
        assert entries[0]["fix"]["kind"] == "change_media"
        assert scene(state, scene_id)["media"]["identity"] == before[scene_id]  # no unrelated filler
    page = next(item for item in by_scene["scene_05_01"] if item["code"] == "text_heavy")
    assert page["status"] == MANUAL and page["fix"] is not None  # text in the media: another visual helps
    locked = next(item for item in by_scene["scene_04_01"] if item["code"] == "wrong_media")
    assert locked["reason"] == "user_locked_visual" and scene(state, "scene_04_01")["media"]["identity"] == before["scene_04_01"]
    # Fallback order after the budget ran out: real alternative, (blocked) AI
    # image, at most two fitting project visuals, then an explicit stop.
    record = next(item for item in review["repairs"] if item["scene_id"] == "scene_03_01")
    names = [step["step"] for step in record["steps"]]
    assert names[:2] == ["real_alternative", "generated_image"]
    assert record["steps"][1]["reason"] == "project_budget_exhausted"
    assert set(names[2:]) <= {"fitting_base_visual", "planned_graphic"} and names.count("fitting_base_visual") <= final_critic.MAX_BASE_CANDIDATES
    # The AI image budget is never exceeded or bypassed.
    assert generator.prompts == []
    assert visual_director.generation_counts(state)["auto_generated_images"] == 3
    # Narration and captions are never rewritten to satisfy a visual issue.
    assert [item["narration"] for item in state["scenes"]] == narration and state["script"]["text"] == script
    # Bounded loop.
    assert harness.renders == review["repair_pass_count"] <= DEFAULT_MAX_REPAIR_PASSES

    # Counts match the report, and the report matches the critic's final state.
    counts = report_counts(review["report"])
    summary = review["summary"]
    assert (summary["fixed_count"], summary["manual_count"], summary["unfixable_count"]) == (counts[FIXED], counts[MANUAL], counts[UNFIXABLE])
    assert counts[FIXED] > 0 and counts[MANUAL] > 0
    final_ids = {item["id"] for item in review["issues"]}
    for item in review["report"]:
        if item["status"] == FIXED:
            assert not set(item["issue_ids"]) & final_ids
        else:
            assert set(item["issue_ids"]) <= set(review["unresolved"])
    reported = [value for item in review["report"] if item["status"] != FIXED for value in item["issue_ids"]]
    assert sorted(reported) == sorted(review["unresolved"])  # nothing hidden, nothing twice
    # The UI can tell the three outcomes apart from the data alone.
    for item in review["report"]:
        assert item["status"] in {FIXED, MANUAL, UNFIXABLE}
        assert (item["fix"] is not None) == (item["status"] == MANUAL)
        assert item["title"] and "_" not in item["title"]
