"""Media relevance is driven by the canonical visual plan, not topic vocabulary.

Every topic here is deliberately absent from production code: the scenes carry
the provider-facing visual intent the AI planner already emits (English
``media_queries``), while narration stays in the user's language.
"""
import copy
import re
from pathlib import Path

import pytest
from test_staged_media_search import POOR, Commons, Provider, Verifier, cand, project, settings_for

import clipforge.media as media_module
from clipforge.media import (
    build_visual_query_plan,
    media_relevance,
    prepare_project_media,
    run_staged_scene_search,
    scene_coverage_targets,
    verify_media_shortlist,
)
from clipforge.visual_verifier import visual_intent_text

FIREFLY_STATE = {
    "intent": {"topic": "Warum leuchten Glühwürmchen nachts?", "question": "Warum leuchten Glühwürmchen nachts?"},
    "format_plan": {"selected_format": "explanation"},
}
FIREFLY_INTENT = {
    "visual_goal": "firefly glowing at night",
    "objects": ["firefly"],
    "actions": ["glowing"],
    "media_queries": ["glowing firefly at night", "firefly light forest"],
}


def firefly_project() -> dict:
    scene = {"narration": "Glühwürmchen leuchten nachts, um Partner anzulocken.", "visual_intent": copy.deepcopy(FIREFLY_INTENT)}
    return project(scene, **copy.deepcopy(FIREFLY_STATE))


def test_unseen_topic_with_german_narration_reaches_visual_verification(tmp_path):
    state = firefly_project()
    firefly = cand("ff", "glowing firefly night", "Firefly glowing in dark forest")
    headlights = cand("car", "glowing firefly night", "Car headlights at night")
    pexels = Provider(videos={"glowing firefly night": [headlights, firefly]})
    verifier = Verifier({"car": POOR})

    prepare_project_media(state, "project", settings_for(tmp_path), client=pexels, fallback_client=Commons(), visual_verifier=verifier)

    scene = state["scenes"][0]
    assert scene["search_queries"][0] == "glowing firefly night"
    assert "ff" in verifier.calls
    assert scene["media"]["provider_id"] == "ff"
    assert scene["media"]["relevance"]["confidence"] == "high"
    assert scene["media_search"]["early_stop"] is True
    assert scene["media_search"]["coverage_targets"] == {"firefly": "primary"}


def test_production_media_code_carries_no_topic_vocabulary():
    source = Path(media_module.__file__).read_text(encoding="utf-8").casefold()
    for word in (
        "schweden", "sweden", "swedish", "indonesien", "indonesia", "inseln", "island", "archipel",
        "ägypten", "egypt", "sudan", "pyramid", "gepard", "cheetah", "flugzeug", "airplane",
        "glühwürmchen", "firefly",
    ):
        assert not re.search(rf"\b{re.escape(word)}", source), word


def test_query_provenance_preserves_candidate_with_zero_narration_overlap():
    # No visual intent and no shared word with the German narration: only the
    # fact that this scene planned (and ran) the query ties the asset to it.
    scene = {"narration": "Warum leuchten Glühwürmchen nachts?", "search_queries": ["glowing firefly night"]}
    planned = cand("ff", "glowing firefly night", "Firefly glowing in dark forest")
    unplanned = cand("ff2", "summer evening", "Firefly glowing in dark forest")

    relevance = media_relevance(planned, scene, FIREFLY_STATE)

    assert relevance["query_provenance"] is True
    assert relevance["confidence"] in {"high", "acceptable"}
    assert media_relevance(unplanned, scene, FIREFLY_STATE)["confidence"] == "rejected"


def test_openclip_rejects_metadata_and_provenance_false_positive():
    state = firefly_project()
    scene = state["scenes"][0]
    scene["search_queries"] = build_visual_query_plan(scene, state)["queries"]
    lookalike = cand("fake", "glowing firefly night", "Firefly glowing in dark forest")

    (candidate, relevance), = verify_media_shortlist([lookalike], scene, state, Verifier({"fake": POOR}))

    assert media_relevance(candidate, scene, state)["confidence"] == "high"
    assert relevance["confidence"] == "rejected"


def test_provider_metadata_disagreeing_with_its_query_is_downgraded():
    state = {"intent": {"topic": "Korallenriffe"}, "format_plan": {"selected_format": "explanation"}}
    scene = {
        "narration": "Korallenriffe in Australien sind riesig.",
        "visual_intent": {"visual_goal": "coral reef in Australia", "objects": ["coral reef", "Australia"], "media_queries": ["coral reef underwater"]},
        "search_queries": ["coral reef underwater"],
    }
    road = cand("road", "coral reef underwater", "Outback road in Australia")
    reef = cand("reef", "coral reef underwater", "Colorful coral reef underwater in Australia")

    road_relevance = media_relevance(road, scene, state)
    reef_relevance = media_relevance(reef, scene, state)

    assert road_relevance["metadata_query_disagreement"] is True
    assert road_relevance["confidence"] != "high"
    assert road_relevance["selection_tier"] <= 1
    assert reef_relevance["metadata_query_disagreement"] is False
    assert reef_relevance["selection_tier"] > road_relevance["selection_tier"]
    assert reef_relevance["score"] > road_relevance["score"]


PETS_STATE = {
    "intent": {"topic": "Hören Katzen oder Hunde besser?"},
    "format_plan": {"selected_format": "comparison"},
}
PET_QUERIES = ["cat ears closeup", "dog ears closeup", "pets listening"]


@pytest.mark.parametrize(
    ("narration", "goal", "first", "last"),
    [
        ("Hunde hören hohe Töne sehr gut.", "dog ears listening", "dog ears closeup", "cat ears closeup"),
        ("Katzen hören noch höhere Töne.", "cat ears listening", "cat ears closeup", "dog ears closeup"),
    ],
)
def test_arbitrary_comparison_orders_the_scene_side_first(narration, goal, first, last):
    scene = {"narration": narration, "visual_intent": {"visual_goal": goal, "media_queries": PET_QUERIES}}
    plan = build_visual_query_plan(scene, PETS_STATE)
    targets = scene_coverage_targets(scene, PETS_STATE, plan)["targets"]
    pexels = Provider()

    result = run_staged_scene_search(
        plan["queries"], scene, PETS_STATE, plan, pexels=pexels, wikimedia=Commons(), preferred_kind="video",
        portrait=True, scene_duration=4, used=set(), verifier=Verifier(),
    )

    assert plan["primary_subjects"] == ["ear"]
    assert first.split()[0] in targets and last.split()[0] not in targets
    assert result.provenance["planned_queries"][0] == first
    assert result.provenance["planned_queries"][-1] == last
    assert pexels.calls[0] == ("video", first)
    assert result.provenance["logical_queries_executed"] <= 3


def test_weak_topic_subject_cannot_dominate_relevance_or_openclip_prompt():
    state = copy.deepcopy(FIREFLY_STATE) | {"intent": {"topic": "Welches Land hat mehr Glühwürmchen?"}}
    scene = firefly_project()["scenes"][0]
    plan = build_visual_query_plan(scene, state)
    scene["visual_query_plan"] = {key: value for key, value in plan.items() if key != "queries"}
    scene["search_queries"] = plan["queries"]
    junk = cand("land", "glowing firefly night", "Open land with more fields")
    firefly = cand("ff", "glowing firefly night", "Firefly glowing in dark forest")

    junk_relevance = media_relevance(junk, scene, state)
    firefly_relevance = media_relevance(firefly, scene, state)
    prompts = visual_intent_text(scene, state)

    assert "land" in firefly_relevance["subject_terms"]  # still derived, but inert
    assert junk_relevance["confidence"] not in {"high", "acceptable"}
    assert firefly_relevance["confidence"] == "high"
    assert prompts.subject == ["a photo of firefly"]


def test_openclip_subject_prompt_falls_back_to_topic_without_a_plan():
    scene = {"narration": "Fireflies glow.", "visual_intent": {"visual_goal": "firefly glowing"}}

    assert visual_intent_text(scene, {"intent": {"topic": "fireflies at night"}}).subject == ["a photo of fireflies night"]


def test_generic_matching_uses_no_translation_or_provider_call(monkeypatch):
    calls = []
    monkeypatch.setattr(media_module.httpx, "Client", lambda *args, **kwargs: calls.append(args) or pytest.fail("network"))
    scene = firefly_project()["scenes"][0]

    plan = build_visual_query_plan(scene, FIREFLY_STATE)
    media_relevance(cand("ff", plan["queries"][0], "Firefly glowing in dark forest"), scene, FIREFLY_STATE)

    assert calls == []
