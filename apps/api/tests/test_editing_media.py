import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge.config import Settings
from clipforge.media import (
    MediaCandidate,
    derive_search_queries,
    parse_photo_results,
    parse_video_results,
    prepare_project_media,
)
from clipforge.pipeline import UnsupportedEdit, apply_edit, build_initial_state
from clipforge.renderer import (
    RenderUnavailable,
    _cached_voice,
    _create_visual_segment,
    _create_voice,
)
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.services import create_project, edit_project, render_project


def local_settings(tmp_path: Path | None = None, *, pexels: str | None = None) -> Settings:
    return Settings(
        clipforge_ai_mode="local",
        openai_api_key=None,
        brave_search_api_key=None,
        pexels_api_key=pexels,
        render_root=tmp_path or Path("projects"),
    )


def sample_state(settings: Settings) -> dict:
    return build_initial_state(
        "Write a fictional story about a lighthouse keeper",
        AdvancedOptions(max_duration=30),
        settings,
    )


@pytest.mark.parametrize(
    ("instruction", "gender", "tone"),
    [
        ("Make the voice male", "masculine", "warm"),
        ("Use a male narrator", "masculine", "warm"),
        ("Make the narrator more masculine", "masculine", "warm"),
        ("Use a deeper male voice", "masculine", "deep"),
    ],
)
def test_masculine_voice_requests_change_voice_state(instruction, gender, tone):
    settings = local_settings()
    edited, changed = apply_edit(sample_state(settings), instruction, settings)

    assert edited["voice"]["gender_presentation"] == gender
    assert edited["voice"]["tone"] == tone
    assert edited["voice"]["voice_id"] in {"cedar", "onyx"}
    assert edited["voice"]["status"] == "regeneration_required"
    assert "voice" in changed
    assert "render" in changed


def test_voice_character_and_speed_are_passed_to_openai_tts(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    settings.openai_api_key = "unit-test-token"
    state, _ = apply_edit(sample_state(settings), "Make the narration deeper and slower", settings)
    captured = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=b"a" * 5000)

    monkeypatch.setattr(
        "clipforge.renderer.OpenAI",
        lambda **_kwargs: SimpleNamespace(audio=SimpleNamespace(speech=Speech())),
    )

    output, provider = _create_voice(state, tmp_path, settings)

    assert output.read_bytes() == b"a" * 5000
    assert provider == "openai"
    assert captured["voice"] == "onyx"
    assert captured["speed"] < 1
    assert "lower register" in captured["instructions"]


def test_unchanged_voice_and_script_reuse_cached_narration(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    state = sample_state(settings)
    calls = []

    def create(_state, temp, _settings):
        calls.append(True)
        output = temp / "voice.wav"
        output.write_bytes(b"a" * 5000)
        return output, "test_voice"

    monkeypatch.setattr("clipforge.renderer._create_voice", create)
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first, _ = _cached_voice(state, "project", tmp_path / "first", settings)
    second, _ = _cached_voice(state, "project", tmp_path / "second", settings)

    assert first == second
    assert len(calls) == 1


@pytest.mark.parametrize(
    "instruction",
    [
        "Use more videos",
        "Use real footage",
        "Use less text",
        "Make the visuals more dynamic",
        "Show more relevant footage",
    ],
)
def test_natural_visual_requests_require_new_real_media(instruction):
    settings = local_settings()
    edited, changed = apply_edit(sample_state(settings), instruction, settings)

    assert "assets" in changed
    assert edited["assets"]["status"] == "search_required"
    assert all(scene["preferred_media"] == "video" for scene in edited["scenes"])
    assert all(scene["asset_status"] == "replacement_required" for scene in edited["scenes"])


def test_ambiguous_edit_asks_a_useful_clarifying_question():
    settings = local_settings()

    with pytest.raises(UnsupportedEdit, match="What would you like to change"):
        apply_edit(sample_state(settings), "Make it better", settings)


def test_hook_stronger_and_natural_format_requests_are_concrete_edits():
    settings = local_settings()
    state = sample_state(settings)

    hooked, _ = apply_edit(state, "Make the hook stronger", settings)
    portrait, _ = apply_edit(state, "Make the video landscape", settings)

    assert hooked["script"]["blocks"][0]["text"] != state["script"]["blocks"][0]["text"]
    assert portrait["timeline"]["aspect_ratio"] == "16:9"


def test_edit_preserves_last_successful_preview_until_replacement():
    settings = local_settings()
    state = sample_state(settings)
    state["render"] = {
        "status": "complete",
        "url": "/media/project/renders/v2/clipforge.mp4",
        "revision": 2,
    }

    edited, _ = apply_edit(state, "Use a warmer narrator voice", settings)

    assert edited["render"]["url"] == state["render"]["url"]
    assert edited["render"]["status"] == "regeneration_required"
    assert edited["render"]["stale"] is True


def rendered_state(state: dict, url: str, revision: int) -> dict:
    rendered = copy.deepcopy(state)
    rendered["render"] = {
        "status": "complete",
        "url": url,
        "revision": revision,
        "stale": False,
    }
    return rendered


def test_successful_edit_render_replaces_preview_only_after_success(db, monkeypatch):
    settings = local_settings()
    project = create_project(
        db, ProjectCreate(prompt="Write a fictional story about a lighthouse keeper"), settings
    )
    monkeypatch.setattr(
        "clipforge.services._render_state",
        lambda state, _project, revision, _settings: rendered_state(
            state, f"/media/old-v{revision}.mp4", revision
        ),
    )
    render_project(db, project, settings)
    old_url = project.revisions[-1].state["render"]["url"]

    revision = edit_project(
        db, project, "Make the voice male", settings, auto_render=True
    )

    assert revision.state["render"]["url"] != old_url
    assert revision.state["render"]["status"] == "complete"
    assert revision.state["voice"]["gender_presentation"] == "masculine"


def test_failed_edit_render_keeps_last_valid_preview(db, monkeypatch):
    settings = local_settings()
    project = create_project(
        db, ProjectCreate(prompt="Write a fictional story about a lighthouse keeper"), settings
    )
    monkeypatch.setattr(
        "clipforge.services._render_state",
        lambda state, _project, revision, _settings: rendered_state(
            state, f"/media/old-v{revision}.mp4", revision
        ),
    )
    render_project(db, project, settings)
    old_url = project.revisions[-1].state["render"]["url"]

    def fail(*_args, **_kwargs):
        raise RenderUnavailable("Narration provider unavailable.")

    monkeypatch.setattr("clipforge.services._render_state", fail)
    revision = edit_project(
        db, project, "Make the voice deeper", settings, auto_render=True
    )

    assert revision.state["render"]["url"] == old_url
    assert revision.state["render"]["status"] == "regeneration_failed"
    assert revision.state["render"]["stale"] is True


def video_payload() -> dict:
    return {
        "videos": [
            {
                "id": 42,
                "width": 1080,
                "height": 1920,
                "duration": 9,
                "url": "https://www.pexels.com/video/42/",
                "user": {"name": "Unit Tester", "url": "https://www.pexels.com/@tester"},
                "video_files": [
                    {
                        "id": 1,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": 1080,
                        "height": 1920,
                        "link": "https://videos.pexels.com/video-42.mp4",
                    }
                ],
            }
        ]
    }


def test_pexels_video_search_parsing_prefers_usable_portrait_video():
    results = parse_video_results(
        video_payload(), query="lighthouse storm", portrait=True, scene_duration=6
    )

    assert len(results) == 1
    assert results[0].kind == "video"
    assert results[0].provider_id == "42"
    assert results[0].height > results[0].width
    assert results[0].duration == 9


def test_pexels_photo_search_parsing_supports_fallback():
    results = parse_photo_results(
        {
            "photos": [
                {
                    "id": 7,
                    "width": 1200,
                    "height": 1800,
                    "url": "https://www.pexels.com/photo/7/",
                    "photographer": "Unit Tester",
                    "src": {"portrait": "https://images.pexels.com/photo-7.jpg"},
                }
            ]
        },
        query="lighthouse",
        portrait=True,
    )

    assert results[0].kind == "photo"
    assert results[0].download_url.endswith("photo-7.jpg")


def candidate(
    provider_id: str,
    kind: str = "video",
    *,
    provider: str = "pexels",
    query: str = "lighthouse",
) -> MediaCandidate:
    return MediaCandidate(
        provider_id=provider_id,
        kind=kind,
        download_url=f"https://media.pexels.com/{provider_id}",
        source_url=f"https://www.pexels.com/{kind}/{provider_id}/",
        creator="Unit Tester",
        creator_url=None,
        width=1080,
        height=1920,
        duration=12 if kind == "video" else None,
        query=query,
        rank=100 - int(provider_id),
        provider=provider,
        title=query,
    )


class FakePexels:
    def __init__(self, candidates: list[MediaCandidate], photos: list[MediaCandidate] | None = None):
        self.candidates = candidates
        self.photos = photos or []

    def search_videos(self, _query, *, portrait, scene_duration):
        assert portrait is True
        assert scene_duration > 0
        return self.candidates

    def search_photos(self, _query, *, portrait):
        assert portrait is True
        return self.photos

    def download(self, selected, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-media")
        return destination

    def close(self):
        return None


class FakeWikimedia:
    def __init__(self, candidates: list[MediaCandidate] | None = None):
        self.candidates = candidates or []
        self.queries: list[str] = []

    def search_photos(self, query, *, portrait):
        assert portrait is True
        self.queries.append(query)
        return self.candidates

    def download(self, selected, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-wikimedia")
        return destination

    def close(self):
        return None


def test_media_selection_avoids_duplicate_clips(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:2]
    for scene in state["scenes"]:
        scene["visual_goal"] = "lighthouse"
        scene["visual_intent"] = {"visual_goal": "lighthouse", "media_queries": ["lighthouse"]}

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([candidate("1"), candidate("2")]),
        fallback_client=FakeWikimedia(),
    )

    identities = [scene["media"]["identity"] for scene in state["scenes"]]
    assert len(set(identities)) == 2
    assert state["assets"]["selected_count"] == 2


def test_media_selection_uses_photo_before_scene_card(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([], [candidate("3", "photo")]),
        fallback_client=FakeWikimedia(),
    )

    assert state["scenes"][0]["media"]["kind"] == "photo"
    assert state["scenes"][0]["asset_status"] == "photo_ready"


def test_media_replacement_prefers_existing_photo_kind(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    state["scenes"][0]["preferred_media"] = "photo"

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels(
            [candidate("7", "video")],
            [candidate("8", "photo")],
        ),
        fallback_client=FakeWikimedia(),
    )

    assert state["scenes"][0]["media"]["kind"] == "photo"
    assert state["scenes"][0]["asset_status"] == "photo_ready"


def test_failed_media_replacement_keeps_previous_cached_asset(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    previous = candidate("9", "photo")
    cached = settings.render_root / "project" / "assets" / "pexels" / "photo-9.jpg"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"previous-media")
    state["scenes"][0]["media"] = {
        "identity": previous.identity,
        "provider": previous.provider,
        "provider_id": previous.provider_id,
        "kind": previous.kind,
        "cache_path": "project/assets/pexels/photo-9.jpg",
        "source_url": previous.source_url,
        "creator": previous.creator,
        "creator_url": previous.creator_url,
        "query": previous.query,
    }
    state["scenes"][0]["preferred_media"] = "photo"
    state["scenes"][0]["asset_status"] = "replacement_required"

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([]),
        fallback_client=FakeWikimedia(),
    )

    scene = state["scenes"][0]
    assert scene["media"]["identity"] == previous.identity
    assert scene["asset_status"] == "replacement_failed"
    assert scene["fallback_reason"]
    assert "previous media was kept" in state["assets"]["diagnostic"]


def test_scene_card_is_last_resort_when_real_media_is_unavailable(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([]),
        fallback_client=FakeWikimedia(),
    )

    assert state["scenes"][0]["asset_status"] == "generated_card_fallback"
    assert "media" not in state["scenes"][0]


def test_media_client_is_created_from_resolved_settings(monkeypatch, tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    captured = {}

    def build_client(api_key):
        captured["configured"] = api_key == settings.pexels_api_key
        return FakePexels([])

    monkeypatch.setattr("clipforge.media.PexelsMediaClient", build_client)
    monkeypatch.setattr("clipforge.media.WikimediaMediaClient", FakeWikimedia)
    prepare_project_media(state, "project", settings)

    assert captured == {"configured": True}


def test_media_queries_strip_internal_story_labels_and_remain_concise():
    state = {
        "intent": {"topic": "Why Earth does not gain mass when people build houses"}
    }
    scene = {
        "visual_goal": "Illustrate answer: Construction workers moving bricks into a house",
        "narration": "DETAIL Earth materials are rearranged during construction.",
        "search_queries": ["Illustrate support: Earth building materials"],
    }

    queries = derive_search_queries(scene, state)
    joined = " ".join(queries)

    assert queries
    assert all(len(query.split()) <= 6 for query in queries)
    assert not {"illustrate", "answer", "detail", "support"}.intersection(joined.split())


def test_media_search_retries_with_broader_semantic_query(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    scene = state["scenes"][0]
    scene["visual_goal"] = "storm waves striking a remote lighthouse window"
    scene["search_queries"] = []
    calls = []

    class BroadRetry(FakePexels):
        def search_videos(self, query, *, portrait, scene_duration):
            calls.append(query)
            return [] if len(calls) == 1 else [candidate("4", query=query)]

    prepare_project_media(
        state,
        "project",
        settings,
        client=BroadRetry([]),
        fallback_client=FakeWikimedia(),
    )

    assert len(calls) >= 2
    assert calls[0] != calls[1]
    assert scene["asset_status"] == "video_ready"


def test_wikimedia_is_attempted_after_pexels_before_card_fallback(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    commons = FakeWikimedia(
        [candidate("5", "photo", provider="wikimedia", query="lighthouse storm")]
    )

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([]),
        fallback_client=commons,
    )

    scene = state["scenes"][0]
    assert commons.queries
    assert scene["media"]["provider"] == "wikimedia"
    assert scene["asset_status"] == "photo_ready"


def test_relevant_media_can_be_reused_only_after_retrieval_attempts(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:2]
    for scene in state["scenes"]:
        scene["visual_goal"] = "lighthouse storm ocean waves"
        scene["search_queries"] = []
    pexels = FakePexels([candidate("6", query="lighthouse storm ocean")])
    commons = FakeWikimedia()

    prepare_project_media(
        state,
        "project",
        settings,
        client=pexels,
        fallback_client=commons,
    )

    assert state["scenes"][0]["asset_status"] == "video_ready"
    assert state["scenes"][1]["asset_status"] == "related_media_reused"
    assert state["scenes"][0]["media"]["identity"] == state["scenes"][1]["media"]["identity"]


def test_generated_card_records_degraded_coverage_after_all_searches(tmp_path):
    settings = local_settings(tmp_path, pexels="unit-test-token")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    commons = FakeWikimedia()

    prepare_project_media(
        state,
        "project",
        settings,
        client=FakePexels([]),
        fallback_client=commons,
    )

    assert commons.queries
    assert state["scenes"][0]["asset_status"] == "generated_card_fallback"
    assert state["scenes"][0]["fallback_reason"]
    assert state["assets"]["generated_card_count"] == 1
    assert state["assets"]["status"] == "fallback_only"


def test_renderer_uses_selected_video_as_moving_media(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    state = sample_state(settings)
    source = tmp_path / "project" / "assets" / "pexels" / "video-42.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mock-video")
    scene = state["scenes"][0]
    scene["media"] = {
        "kind": "video",
        "cache_path": source.relative_to(tmp_path).as_posix(),
    }
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    (tmp_path / "temp").mkdir()
    output = _create_visual_segment(
        "ffmpeg", state, scene, 0, 4.0, tmp_path / "temp", settings
    )

    assert output.exists()
    assert str(source) in commands[0]
    assert "-loop" not in commands[0]
    video_filter = commands[0][commands[0].index("-vf") + 1]
    assert "tpad=stop_mode=clone" in video_filter
    assert "trunc((in_w-" in video_filter
    assert "zoompan" not in video_filter


def test_still_zoompan_keeps_headroom_and_integer_window_origins(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    state = sample_state(settings)
    source = tmp_path / "project" / "assets" / "pexels" / "photo-7.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mock-photo")
    scene = state["scenes"][0]
    scene["media"] = {
        "kind": "photo",
        "cache_path": source.relative_to(tmp_path).as_posix(),
    }
    scene["smart_crop"] = {"center_x": 0.62, "center_y": 0.41, "confidence": 0.9}
    scene["motion"] = "subtle_pan"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    monkeypatch.setattr(
        "clipforge.renderer.analyze_scene_media",
        lambda *_args, **_kwargs: scene["smart_crop"],
    )
    (tmp_path / "temp").mkdir()
    width = int(state["timeline"]["width"])
    height = int(state["timeline"]["height"])
    _create_visual_segment("ffmpeg", state, scene, 0, 2.0, tmp_path / "temp", settings)

    assert "-loop" in commands[0]
    still_filter = commands[0][commands[0].index("-vf") + 1]
    assert f"scale={int(width * 1.08 * 2)}:{int(height * 1.08 * 2)}:force_original_aspect_ratio=increase" in still_filter
    assert f"crop={width}:{height}:" not in still_filter
    assert "zoompan=z=" in still_filter
    assert "x='trunc((iw-" in still_filter
    assert "y='trunc((ih-" in still_filter
    assert "0.6200" in still_filter
    assert "0.4100" in still_filter
    assert f"s={width * 2}x{height * 2}" in still_filter
    assert f"scale={width}:{height}:flags=lanczos" in still_filter


def test_still_without_motion_is_static_and_does_not_implicitly_zoom(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    state = sample_state(settings)
    source = tmp_path / "project" / "assets" / "pexels" / "photo-static.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mock-photo")
    scene = state["scenes"][0]
    scene["media"] = {"kind": "photo", "cache_path": source.relative_to(tmp_path).as_posix()}
    scene.pop("motion", None)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    monkeypatch.setattr(
        "clipforge.renderer.analyze_scene_media",
        lambda *_args, **_kwargs: {"center_x": 0.5, "center_y": 0.5, "confidence": 0.0},
    )
    (tmp_path / "temp").mkdir()
    _create_visual_segment("ffmpeg", state, scene, 0, 2.0, tmp_path / "temp", settings)

    still_filter = commands[0][commands[0].index("-vf") + 1]
    assert "zoompan=z='min(zoom+0.0000,1.0)'" in still_filter
    assert "s=2160x3840" in still_filter


def test_smart_crop_center_is_reused_without_frame_drift(monkeypatch, tmp_path):
    settings = local_settings(tmp_path)
    state = sample_state(settings)
    source = tmp_path / "project" / "assets" / "pexels" / "photo-stable.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mock-photo")
    scene = state["scenes"][0]
    scene["media"] = {"kind": "photo", "cache_path": source.relative_to(tmp_path).as_posix()}
    scene["motion"] = "subtle_pan"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    monkeypatch.setattr(
        "clipforge.renderer.analyze_scene_media",
        lambda *_args, **_kwargs: {"center_x": 0.62, "center_y": 0.41, "confidence": 0.9},
    )
    (tmp_path / "temp").mkdir()
    _create_visual_segment("ffmpeg", state, scene, 0, 2.0, tmp_path / "temp", settings)
    _create_visual_segment("ffmpeg", state, scene, 0, 2.0, tmp_path / "temp", settings)

    first = commands[0][commands[0].index("-vf") + 1]
    second = commands[1][commands[1].index("-vf") + 1]
    assert first == second
    assert "0.6200" in first
    assert "0.4100" in first
