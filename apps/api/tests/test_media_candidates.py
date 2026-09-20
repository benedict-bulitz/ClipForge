from pathlib import Path

import pytest

from clipforge.config import Settings
from clipforge.media import MediaCandidate, media_relevance
from clipforge.media_candidates import (
    CandidateError,
    clear_candidate_sets,
    discover_scene_media_candidates,
)


def candidate(provider_id: str, kind: str, rank: float) -> MediaCandidate:
    return MediaCandidate(provider_id, kind, f"https://cdn.test/{provider_id}", f"https://source.test/{provider_id}", "Creator", None, 1080, 1920, 8.0 if kind == "video" else None, "lighthouse", rank, title="lighthouse")


class FakeProvider:
    def __init__(self):
        self.videos = [candidate("v1", "video", 100), candidate("v1", "video", 99), candidate("v2", "video", 90)]
        self.photos = [candidate("p1", "photo", 80), candidate("p2", "photo", 70)]

    def search_videos(self, query, *, portrait, scene_duration):
        return self.videos

    def search_photos(self, query, *, portrait):
        return self.photos


class FakeWikimedia:
    def search_photos(self, query, *, portrait):
        return []


def state():
    return {
        "timeline": {"width": 1080, "height": 1920},
        "intent": {"topic": "lighthouse"},
        "scenes": [{"id": "scene-1", "start": 0, "end": 8, "visual_goal": "lighthouse", "narration": "A lighthouse", "preferred_media": "video"}],
    }


@pytest.fixture
def local_settings(tmp_path: Path):
    return lambda: Settings(clipforge_ai_mode="local", openai_api_key=None, brave_search_api_key=None, pexels_api_key="test", render_root=tmp_path)


@pytest.fixture(autouse=True)
def reset_candidates():
    clear_candidate_sets()
    yield
    clear_candidate_sets()


def test_discovery_is_bounded_preferred_and_deduplicated(local_settings):
    token, results = discover_scene_media_candidates(state(), "project", 1, 1, local_settings(), client=FakeProvider(), fallback_client=FakeWikimedia())
    assert token
    assert len(results) == 4
    assert [item["kind"] for item in results[:2]] == ["video", "video"]
    assert len({item["provider_id"] for item in results}) == len(results)


def test_current_media_is_not_returned_and_discovery_does_not_mutate_state(local_settings):
    current = candidate("v1", "video", 100)
    original = state()
    original["scenes"][0]["media"] = {"identity": current.identity, "kind": "video"}
    token, results = discover_scene_media_candidates(original, "project", 1, 1, local_settings(), client=FakeProvider(), fallback_client=FakeWikimedia())
    assert token and all(item["provider_id"] != "v1" for item in results)
    assert original["scenes"][0]["media"]["identity"] == current.identity


def test_invalid_candidate_token_fails_without_accepting_urls():
    with pytest.raises(CandidateError):
        from clipforge.media_candidates import apply_scene_media_candidate

        apply_scene_media_candidate(None, type("Project", (), {"id": "p", "current_revision": 1})(), 1, "https://evil.test/file", type("Settings", (), {})())


def test_semantic_relevance_beats_format_and_technical_rank():
    scene = {"narration": "Workers pouring concrete foundation", "visual_goal": "residential foundation construction"}
    state_data = {"intent": {"topic": "building a house"}}
    relevant_photo = MediaCandidate("photo", "photo", "https://cdn.test/photo", "https://source.test/photo", "Creator", None, 1080, 1920, None, "residential foundation construction", 10, title="Workers pouring concrete foundation")
    unrelated_video = MediaCandidate("dog", "video", "https://cdn.test/dog", "https://source.test/dog", "Creator", None, 1080, 1920, 20, "dog playing in park", 200, title="Dog playing in park")
    assert media_relevance(relevant_photo, scene, state_data)["score"] > media_relevance(unrelated_video, scene, state_data)["score"]


def test_action_specific_candidate_beats_generic_topic_match():
    scene = {"narration": "Workers pouring concrete foundation", "visual_goal": "house construction"}
    specific = MediaCandidate("pour", "video", "https://cdn.test/pour", "https://source.test/pour", "Creator", None, 1080, 1920, 8, "house construction", 80, title="Workers pouring concrete slab")
    generic = MediaCandidate("exterior", "video", "https://cdn.test/exterior", "https://source.test/exterior", "Creator", None, 1080, 1920, 8, "house construction", 100, title="Luxury house exterior")
    assert media_relevance(specific, scene)["score"] > media_relevance(generic, scene)["score"]


def test_global_subject_context_rejects_generic_hole_media():
    scene = {
        "narration": "The hole sits in the middle pane.",
        "visual_goal": "hole in the middle pane",
        "visual_intent": {
            "visual_goal": "hole sits middle pane",
            "objects": ["hole", "middle pane"],
            "actions": [],
            "context": [],
        },
    }
    state_data = {"intent": {"topic": "airplane window breather hole"}}
    road = MediaCandidate(
        "road", "video", "https://cdn.test/road", "https://source.test/road",
        "Creator", None, 1080, 1920, 8, "hole middle pane", 200,
        title="Normal road with cars and trees",
    )
    fabric = MediaCandidate(
        "fabric", "photo", "https://cdn.test/fabric", "https://source.test/fabric",
        "Creator", None, 1080, 1920, None, "hole middle pane", 200,
        title="Macro hole in fabric",
    )
    airplane = MediaCandidate(
        "airplane", "photo", "https://cdn.test/airplane", "https://source.test/airplane",
        "Creator", None, 1080, 1920, None, "airplane window breather hole", 20,
        title="Close-up of an airplane window and breather hole",
    )
    assert media_relevance(road, scene, state_data)["confidence"] == "rejected"
    assert media_relevance(fabric, scene, state_data)["confidence"] == "rejected"
    assert media_relevance(airplane, scene, state_data)["confidence"] in {"high", "acceptable"}


def test_relevant_photo_beats_unrelated_generic_hole_video():
    scene = {
        "narration": "The hole sits in the middle pane.",
        "visual_goal": "hole in the middle pane",
    }
    state_data = {"intent": {"topic": "airplane window"}}
    photo = MediaCandidate(
        "window-photo", "photo", "https://cdn.test/window", "https://source.test/window",
        "Creator", None, 1080, 1920, None, "airplane window hole", 10,
        title="Airplane window close-up",
    )
    video = MediaCandidate(
        "hole-video", "video", "https://cdn.test/hole", "https://source.test/hole",
        "Creator", None, 1080, 1920, 8, "hole middle pane", 200,
        title="Generic hole in material",
    )
    assert media_relevance(photo, scene, state_data)["score"] > media_relevance(video, scene, state_data)["score"]
    assert media_relevance(photo, scene, state_data)["confidence"] in {"high", "acceptable"}
    assert media_relevance(video, scene, state_data)["confidence"] == "rejected"


def test_subject_gate_rejects_house_window_for_smartphone_camera():
    scene = {
        "narration": "The outer protective lens element covers the camera.",
        "visual_goal": "outer protective camera lens element",
    }
    state_data = {"intent": {"topic": "smartphone camera"}}
    house_window = MediaCandidate(
        "house-window", "photo", "https://cdn.test/window", "https://source.test/window",
        "Creator", None, 1080, 1920, None, "protective glass", 200,
        title="House window glass",
    )
    assert media_relevance(house_window, scene, state_data)["confidence"] == "rejected"


def test_subject_gate_accepts_volcano_ash_positive_control():
    scene = {
        "narration": "An ash cloud rises from the volcano.",
        "visual_goal": "volcano ash cloud",
    }
    state_data = {"intent": {"topic": "volcano"}}
    eruption = MediaCandidate(
        "eruption", "video", "https://cdn.test/eruption", "https://source.test/eruption",
        "Creator", None, 1080, 1920, 8, "volcano ash cloud", 20,
        title="Real volcanic eruption with ash cloud",
    )
    assert media_relevance(eruption, scene, state_data)["confidence"] in {"high", "acceptable"}


def test_global_subject_context_is_preserved_for_compound_topics():
    from clipforge.media import derive_search_queries

    state_data = {"intent": {"topic": "smartphone camera multiple lens elements"}}
    scene = {
        "narration": "The outer element protects the inner optics.",
        "visual_goal": "outer camera lens protecting inner optics",
        "visual_intent": {
            "visual_goal": "outer element protects inner optics",
            "objects": ["outer element", "inner optics"],
            "actions": ["protects"],
            "context": [],
            "media_queries": ["protective glass"],
        },
    }
    queries = derive_search_queries(scene, state_data)
    assert any("smartphone" in query and "camera" in query for query in queries)
    assert not all(query == "protective glass" for query in queries)


def test_german_scene_queries_are_provider_friendly():
    from clipforge.media import derive_search_queries

    scene = {"narration": "Beim Hausbau wird zuerst das Fundament gegossen.", "visual_goal": "Hausbau Fundament"}
    queries = derive_search_queries(scene, {"intent": {"topic": "Hausbau"}})
    assert any("foundation" in query and "construction" in query for query in queries)
