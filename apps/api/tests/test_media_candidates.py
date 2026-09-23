import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge.config import Settings
from clipforge.media import MediaCandidate, derive_search_queries, media_relevance
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


def test_visual_rejections_do_not_prevent_wikimedia_fallback(local_settings, monkeypatch):
    provider = FakeProvider()
    provider.videos = [candidate(f"v{i}", "video", 100) for i in range(10)]
    provider.photos = []
    fallback = FakeProvider()
    fallback.photos = [replace(candidate("real", "photo", 80), provider="wikimedia"), replace(candidate("card", "photo", 90), title="Lighthouse flashcard")]
    def verify(items, *args):
        return [(item, {"confidence": "acceptable" if item.provider == "wikimedia" else "rejected"}) for item in items]
    monkeypatch.setattr("clipforge.media_candidates.verify_media_shortlist", verify)
    _, results = discover_scene_media_candidates(state(), "p", 1, 1, local_settings(), client=provider, fallback_client=fallback)
    assert [item["provider_id"] for item in results] == ["real"]


def test_apply_reopens_and_exports_only_target_scene_with_existing_audio(db, tmp_path, monkeypatch):
    from test_export import accept_mp4, export_settings, seed_project, state_for

    from clipforge.media_candidates import apply_scene_media_candidate
    from clipforge.services import export_project, get_project, serialize_project

    settings = export_settings(tmp_path)
    project_id = "11111111-1111-4111-8111-111111111111"
    initial = state_for(project_id, settings)
    initial.update(state())
    initial["timeline"]["duration"] = 16
    initial["scenes"].append({**copy.deepcopy(initial["scenes"][0]), "id": "scene-2", "start": 8, "end": 16})
    initial.update(script={"text": "A lighthouse"}, captions={"enabled": True, "items": []}, music={"enabled": False, "volume": 0.2, "track": {"id": "saved-song"}}, voice={"volume": 0.6})
    original = copy.deepcopy(initial)
    project = seed_project(db, project_id, initial)
    provider = FakeProvider()
    def download(item, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"selected media")
        return destination
    provider.download = download
    _, choices = discover_scene_media_candidates(initial, project_id, 1, 1, settings, client=provider, fallback_client=FakeWikimedia())
    # Opening/canceling alternatives has no revision or state mutation.
    assert project.current_revision == 1 and initial == original
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"v" * 20_000)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("clipforge.renderer.ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr("clipforge.renderer._create_visual_segment", lambda *args, **kwargs: tmp_path / "segment.mp4")
    monkeypatch.setattr("clipforge.renderer._write_ass_captions", lambda *args: tmp_path / "captions.ass")
    monkeypatch.setattr("clipforge.renderer._run_process", run)
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)
    apply_scene_media_candidate(db, project, 1, choices[0]["token"], settings, client=provider)
    db.expire_all()
    reopened = get_project(db, project_id)
    saved = serialize_project(reopened)["revision"]["state"]
    assert saved["scenes"][0]["media"]["provider_id"] == choices[0]["provider_id"]
    assert saved["scenes"][1] == original["scenes"][1]
    for key in ("script", "captions", "music"):
        assert saved[key] == original[key]
    assert saved["voice"]["volume"] == 0.6
    assert commands[-1][commands[-1].index("-c:a") + 1] == "copy"
    _, exported = export_project(db, reopened, settings, base_revision=reopened.current_revision)
    assert not exported.already_exported
    assert (settings.render_root / saved["scenes"][0]["media"]["cache_path"]).is_file()
    exported_state = serialize_project(reopened)["revision"]["state"]
    assert exported_state["scenes"][0]["media"]["cache_path"] == saved["scenes"][0]["media"]["cache_path"]


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


def test_missing_replacement_media_cannot_generate_a_card(local_settings, tmp_path, monkeypatch):
    from clipforge.renderer import RenderUnavailable, _create_visual_segment

    original = state()
    original["timeline"]["fps"] = 30
    def no_card(*args):
        pytest.fail("Scene replacement must never generate a fallback card")
    monkeypatch.setattr("clipforge.renderer._draw_scene", no_card)
    with pytest.raises(RenderUnavailable, match="replacement media is unavailable"):
        _create_visual_segment("ffmpeg", original, original["scenes"][0], 0, 8, tmp_path, local_settings(), require_real_media=True)


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
        title="Airplane window with a middle-pane breather hole",
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


def test_exact_scene_relevance_beats_generic_topic_relevance():
    scene = {
        "narration": "Water vapor condenses into fine droplets.",
        "visual_goal": "condensation forming tiny water droplets",
    }
    state_data = {"intent": {"topic": "visible breath in winter"}}
    exact = MediaCandidate(
        "droplets", "photo", "https://cdn.test/droplets", "https://source.test/droplets",
        "Creator", None, 1080, 1920, None, "condensation droplets", 20,
        title="Water vapor condensation forming fine droplets",
    )
    generic = MediaCandidate(
        "coat", "video", "https://cdn.test/coat", "https://source.test/coat",
        "Creator", None, 1080, 1920, 8, "winter breath", 200,
        title="Person wearing a winter coat",
    )

    exact_relevance = media_relevance(exact, scene, state_data)
    generic_relevance = media_relevance(generic, scene, state_data)

    assert exact_relevance["score"] > generic_relevance["score"]
    assert exact_relevance["selection_tier"] > generic_relevance["selection_tier"]


@pytest.mark.parametrize(
    "title",
    [
        "Flashcard with condensation definition",
        "Screenshot of a document page about water droplets",
    ],
)
def test_text_card_and_document_metadata_are_rejected(title):
    scene = {
        "narration": "Water vapor condenses into droplets.",
        "visual_goal": "water condensation droplets",
    }
    item = MediaCandidate(
        "card", "photo", "https://cdn.test/card", "https://source.test/card",
        "Creator", None, 1080, 1920, None, "condensation droplets", 100,
        title=title,
    )

    relevance = media_relevance(item, scene)

    assert relevance["confidence"] == "rejected"
    assert relevance["presentation_risk"]["rejected"] is True


def test_ordinary_photographic_footage_remains_eligible():
    scene = {
        "narration": "Water droplets form in cold air.",
        "visual_goal": "fine water droplets in cold air",
    }
    item = MediaCandidate(
        "photo", "photo", "https://cdn.test/photo", "https://source.test/photo",
        "Creator", None, 1080, 1920, None, "water droplets cold air", 50,
        title="Close-up photograph of water droplets in cold air",
    )

    assert media_relevance(item, scene)["confidence"] in {"high", "acceptable"}


def test_useful_real_diagram_is_not_globally_banned():
    scene = {
        "narration": "Water vapor condenses into droplets.",
        "visual_goal": "water vapor condensation droplets",
    }
    item = MediaCandidate(
        "diagram", "photo", "https://cdn.test/diagram", "https://source.test/diagram",
        "Creator", None, 1080, 1920, None, "condensation diagram", 50,
        title="Scientific diagram of water vapor condensation into droplets",
    )

    relevance = media_relevance(item, scene)

    assert relevance["confidence"] in {"high", "acceptable"}
    assert relevance["presentation_risk"]["rejected"] is False


def test_unrelated_candidate_is_rejected_when_only_topic_word_matches():
    scene = {
        "narration": "Water vapor condenses into droplets.",
        "visual_goal": "water condensation droplets",
    }
    state_data = {"intent": {"topic": "visible breath in winter"}}
    item = MediaCandidate(
        "fashion", "photo", "https://cdn.test/fashion", "https://source.test/fashion",
        "Creator", None, 1080, 1920, None, "winter", 100,
        title="Winter fashion portrait",
    )

    assert media_relevance(item, scene, state_data)["confidence"] == "rejected"


def test_global_topic_is_not_mandatory_for_exact_local_match():
    scene = {
        "narration": "Water vapor condenses into droplets.",
        "visual_goal": "water condensation droplets",
    }
    state_data = {"intent": {"topic": "airplane cabin window"}}
    item = MediaCandidate(
        "condensation", "photo", "https://cdn.test/condensation", "https://source.test/condensation",
        "Creator", None, 1080, 1920, None, "condensation droplets", 50,
        title="Water condensation and fine droplets",
    )

    relevance = media_relevance(item, scene, state_data)

    assert relevance["confidence"] in {"high", "acceptable"}
    assert relevance["subject_matches"] == []


def test_scene_query_precedes_global_topic_fallback():
    scene = {
        "narration": "Water vapor condenses into droplets.",
        "visual_goal": "water condensation droplets",
        "visual_intent": {"visual_goal": "water condensation droplets", "media_queries": ["condensation droplets"]},
    }
    queries = derive_search_queries(scene, {"intent": {"topic": "visible breath in winter"}})

    assert queries[0] == "condensation droplets"
    assert "winter" not in queries[0]
    assert any("winter" in query for query in queries[1:])


def test_stale_scene_queries_do_not_override_a_coherent_visual_intent():
    scene = {
        "narration": "Water vapor condenses into fine droplets.",
        "visual_goal": "materials being moved and assembled on Earth",
        "search_queries": ["construction materials", "earth materials"],
        "visual_intent": {
            "visual_goal": "water condensation droplets",
            "objects": ["water vapor", "droplets"],
            "actions": ["condensing"],
            "context": ["cold air"],
            "media_queries": ["condensation water droplets"],
        },
    }

    queries = derive_search_queries(scene, {"intent": {"topic": "visible breath in winter"}})

    assert "condensation" in queries[0]
    assert "construction" not in " ".join(queries)


def test_negated_smoke_does_not_become_a_provider_query_or_eligible_media():
    scene = {
        "narration": "Visible breath is not smoke; water vapor condenses into mist droplets.",
        "visual_goal": "mist droplets from water vapor condensation",
    }
    flame = MediaCandidate(
        "flame", "video", "https://cdn.test/flame", "https://source.test/flame",
        "Creator", None, 1080, 1920, 8, "visible breath", 200,
        title="Burning match emitting smoke",
    )

    assert "smoke" not in " ".join(derive_search_queries(scene, {"intent": {"topic": "visible breath"}}))
    assert media_relevance(flame, scene, {"intent": {"topic": "visible breath in winter"}})["confidence"] == "rejected"


def test_global_topic_overlap_cannot_rescue_unrelated_person_footage():
    scene = {
        "narration": "Water vapor condenses into fine droplets.",
        "visual_goal": "condensation forming water droplets in mist",
    }
    athlete = MediaCandidate(
        "athlete", "video", "https://cdn.test/athlete", "https://source.test/athlete",
        "Creator", None, 1080, 1920, 8, "winter breath", 250,
        title="Winter athlete catching breath outdoors",
    )
    mist = MediaCandidate(
        "mist", "photo", "https://cdn.test/mist", "https://source.test/mist",
        "Creator", None, 1080, 1920, None, "condensation mist droplets", 10,
        title="Fine water droplets condensing in cold mist",
    )
    state_data = {"intent": {"topic": "visible breath in winter"}}

    assert media_relevance(athlete, scene, state_data)["confidence"] == "unknown"
    assert media_relevance(mist, scene, state_data)["confidence"] in {"high", "acceptable"}
