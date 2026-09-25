"""Overlay semantic quality: an overlay must teach something on its own.

Regression for the real wet-finger render, where a well-sized overlay read
"Forschende vermuten" -> "schrumpelige Haut weniger": formatted, but chopped
narration.  The checks are about meaning, never about one exact phrase.
"""
from __future__ import annotations

import copy
import inspect

import pytest
from critic_support import Harness, media_pass, render, silent_voice, small_timeline
from test_final_critic import FINGERS_Q, finger_provider, issue_codes, row, scene
from test_story_visual_director import FINGERS
from test_story_visual_integration import fact, generate, visual

from clipforge import overlay_copy, visual_director
from clipforge.overlay_copy import assess_elements, assess_overlay, fact_statement, overlay_spec_for

GRIP_FACT = "Forschende vermuten schon seit einigen Jahren, dass schrumpelige Haut uns hilft, nasse Dinge besser zu greifen."
SLIPPERY_FACT = "Forschende vermuten, dass schrumpelige Haut weniger rutschig ist und so beim Greifen unter Wasser hilft."
BAD = ["Forschende vermuten", "schrumpelige Haut weniger"]


def codes(elements: list[str], source: str = SLIPPERY_FACT, narration: str = "") -> set[str]:
    return {issue["code"] for issue in assess_elements(elements, source=source, narration=narration)}


# ---------------------------------------------------------------------------
# The deterministic meaning check
# ---------------------------------------------------------------------------

def test_the_real_chopped_overlay_is_rejected_for_each_reason():
    assert codes(BAD) >= {"low_information_gain", "incomplete_overlay", "narration_fragment"}


@pytest.mark.parametrize("elements", [
    ["Runzlige Finger", "Besserer Halt bei Nässe"],
    ["Schrumpelige Haut", "Rutscht weniger beim Greifen"],
    ["Furchen in der Haut", "Mehr Grip unter Wasser?"],
    ["Wrinkled fingers", "Better grip when wet"],
])
def test_concise_relations_pass_without_a_hardcoded_phrase(elements):
    assert codes(elements) == set()


@pytest.mark.parametrize(("elements", "expected"), [
    (["Forschende vermuten", "Haut hilft beim Greifen"], "low_information_gain"),      # reporting frame as content
    (["Man geht davon aus", "Finger greifen besser"], "low_information_gain"),
    (["Schrumpelige Haut", "Hilft beim"], "incomplete_overlay"),                        # stops before its object
    (["Dass schrumpelige Haut", "Besser greift"], "incomplete_overlay"),               # starts mid-sentence
    (["Schrumpelige Haut weniger", "Beim Greifen"], "incomplete_overlay"),             # dangling comparative
    (["Schrumpelige Haut", "weniger rutschig"], "narration_fragment"),                  # the sentence cut in two
    (["Schrumpelige Haut ist weniger rutschig und so beim Greifen", "Hilft"], "overlay_too_long"),
    (["Runzlige Finger", "Runzlige Finger"], "low_information_gain"),
])
def test_each_weakness_is_named(elements, expected):
    assert expected in codes(elements)


def test_overlay_that_just_repeats_the_narration_is_duplicate():
    narration = "Schrumpelige Haut greift nasse Steine deutlich besser"
    spec = {"kind": "process", "steps": ["Schrumpelige Haut greift", "Nasse Steine deutlich besser"]}
    assert "duplicate_narration" in {issue["code"] for issue in assess_overlay(spec, source=narration, narration=narration)}


# ---------------------------------------------------------------------------
# Copy from the complete fact (never from a scene fragment)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [GRIP_FACT, SLIPPERY_FACT, "Die Furchen helfen möglicherweise dabei, nasse Dinge besser festzuhalten."])
def test_hypothesis_becomes_a_complete_hedged_relation(statement):
    spec = overlay_spec_for(statement)
    assert spec is not None and spec["kind"] == "process" and spec["relation"] and len(spec["steps"]) == 2
    assert assess_overlay(spec, source=statement) == []
    words = " ".join(spec["steps"]).casefold()
    assert "forschende" not in words and "vermuten" not in words  # no reporting frame on screen
    assert spec["hedged"] and spec["steps"][-1].endswith("?")        # uncertainty kept, not a proven fact
    assert all(1 <= len(step.rstrip("?").split()) <= overlay_copy.MAX_ELEMENT_WORDS for step in spec["steps"])


def test_statement_without_a_clear_relation_gets_no_overlay():
    assert overlay_spec_for("Forschende vermuten das schon lange.") is None


def split_fingers(monkeypatch, tmp_path, claim: str = GRIP_FACT) -> dict:
    silent_voice(monkeypatch)
    intent = visual("wrinkled fingers gripping a wet stone", ["wrinkled fingers gripping wet stone"], ["shared"])
    intent.visual_strategy = "process"  # the planner marks it as an explained relation
    blocks = [FINGERS[0], (fact(claim), "explanation", intent), FINGERS[1]]
    return small_timeline(generate(monkeypatch, tmp_path, FINGERS_Q, blocks, planner_target=""))


def test_fact_statement_recovers_the_full_fact_for_every_scene_fragment(monkeypatch, tmp_path):
    state = split_fingers(monkeypatch, tmp_path)
    fragments = [item for item in state["scenes"] if item["block_id"] == "voice_block_02"]
    assert len(fragments) >= 2 and fragments[0]["narration"] != fragments[1]["narration"]  # the arc split the fact
    assert {fact_statement(item, state) for item in fragments} == {GRIP_FACT}
    # Without a Story Arc the complete script block is used, not the fragment.
    legacy = copy.deepcopy(state)
    legacy.pop("story_arc", None)
    for item in legacy["scenes"]:
        item.pop("story_unit_ids", None)
        item.pop("visual_director", None)
    assert fact_statement(legacy["scenes"][1], legacy).startswith("Forschende vermuten")


def test_visual_director_plans_one_complete_overlay_per_fact(monkeypatch, tmp_path):
    state = split_fingers(monkeypatch, tmp_path)
    media_pass(state, tmp_path, finger_provider("hand"))
    fragments = [item for item in state["scenes"] if item["block_id"] == "voice_block_02"]
    specs = [item["visual_director"]["overlay_spec"] for item in fragments]
    assert specs[0] == specs[1] and specs[0]["source"] == "fact_relation"
    assert assess_overlay(specs[0], source=GRIP_FACT) == []
    # A two-part relation is one idea: every scene of the fact shows both sides.
    drawn = [item["overlays"][0]["spec"] for item in fragments]
    assert all(spec["steps"] == specs[0]["steps"] and spec["relation"] for spec in drawn)
    source = inspect.getsource(overlay_copy).casefold()
    assert not any(word in source for word in ("finger", "schrumpel", "runzl", "haut ", "grip", "nässe"))


# ---------------------------------------------------------------------------
# Final Video Critic: meaning, not only size; overlay-only repair
# ---------------------------------------------------------------------------

def test_well_sized_but_meaningless_overlay_fails_and_only_the_copy_is_repaired(monkeypatch, tmp_path):
    state = split_fingers(monkeypatch, tmp_path, claim=SLIPPERY_FACT)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    # The real failure: the old copy builder's chopped narration.
    for item in state["scenes"]:
        if item["block_id"] == "voice_block_02":
            item["visual_director"]["overlay_spec"] = {"kind": "process", "steps": list(BAD)}
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)
    target = next(item["id"] for item in state["scenes"] if item["block_id"] == "voice_block_02")
    base_before = scene(state, target)["media"]["identity"]
    harness = Harness(tmp_path, provider)

    review = harness.review(state)

    first = review["history"][0]
    before = {issue["code"] for issue in first["issues"] if issue["scene_id"] == target}
    assert {"low_information_gain", "incomplete_overlay", "narration_fragment"} <= before
    initial_row = next(item for item in review["initial_issues"] if item["scene_id"] == target and item["category"] == "overlay_semantics")
    assert initial_row["severity"] == "error"
    record = next(item for item in review["repairs"] if item["scene_id"] == target)
    assert record["action"] == "adjust_composition" and record["accepted_step"] == "rewrite_overlay_from_fact"
    assert record["repair_effective"] and record["result_message"] == "rewrote the overlay text from the full fact"
    assert record["before_overlay"]["steps"] == BAD
    # The base visual is untouched; only the copy changed.
    assert scene(state, target)["media"]["identity"] == base_before
    drawn = next(item for item in state["render"]["layout"] if item["scene_id"] == target)["overlays"][0]["spec"]
    assert drawn["steps"] != BAD and assess_overlay(drawn, source=SLIPPERY_FACT) == []
    dims = row(review, target)["dimensions"]
    assert dims["overlay_semantics"]["rating"] == "good" and dims["overlay_quality"]["rating"] == "good"
    assert not set(overlay_copy.OVERLAY_SEMANTIC_CODES) & issue_codes(review, target)
    assert harness.renders == 1  # re-rendered and validated once


def test_meaningless_overlay_without_a_better_relation_is_removed(monkeypatch, tmp_path):
    state = split_fingers(monkeypatch, tmp_path, claim="Forschende vermuten das schon lange.")
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    target = next(item for item in state["scenes"] if item["block_id"] == "voice_block_02")
    assert target["visual_director"]["overlay_spec"] is None  # nothing meaningful was planned
    for item in state["scenes"]:
        if item["block_id"] == "voice_block_02":
            item["visual_director"]["overlay_spec"] = {"kind": "process", "steps": ["Forschende vermuten", "Das schon lange"]}
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state)

    record = next(item for item in review["repairs"] if item["scene_id"] == target["id"])
    assert record["accepted_step"] == "remove_meaningless_overlay" and record["repair_effective"]
    assert next(item for item in state["render"]["layout"] if item["scene_id"] == target["id"])["overlays"] == []
    assert row(review, target["id"])["dimensions"]["overlay_semantics"]["rating"] == "not_applicable"


def test_good_fact_overlay_passes_the_critic_untouched(monkeypatch, tmp_path):
    state = split_fingers(monkeypatch, tmp_path)
    provider = finger_provider("hand")
    media_pass(state, tmp_path, provider)
    render(state, tmp_path)

    review = Harness(tmp_path, provider).review(state, max_passes=0)

    for item in review["scenes"]:
        if item["block_id"] == "voice_block_02":
            assert item["dimensions"]["overlay_semantics"]["rating"] == "good"
    assert not any(issue["category"] == "overlay_semantics" for issue in review["issues"])
    assert visual_director.SIMPLE_GRAPHIC  # module still importable with the new copy builder
