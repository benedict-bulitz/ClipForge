from test_export import accept_mp4, export_settings, seed_project, state_for

from clipforge.config import Settings
from clipforge.music import MusicTrack
from clipforge.schemas import AudioSettingsUpdate, MusicSelectionUpdate
from clipforge.services import (
    _ensure_music_recommendations,
    export_project,
    get_project,
    project_music_recommendations,
    serialize_project,
    update_project_audio,
    update_project_music_selection,
)


def test_ai_match_activates_default_music_layer_without_touching_render(tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    track = MusicTrack("matched", "Matched Track", "tracks/matched.mp3", "documentary", "low", ("ambient",), "catalog", "CC BY")
    state = state_for("55555555-5555-4555-8555-555555555555", settings)
    state["music"] = {"enabled": True, "selection": {"mode": "automatic"}, "volume": 0.22}
    from clipforge.ai import AIMusicRecommendationResult
    monkeypatch.setattr("clipforge.services.rank_music_with_openai", lambda *_args: AIMusicRecommendationResult(["matched"], "connected"))

    tracks, changed = _ensure_music_recommendations(state, (track,), settings)

    assert changed is True
    assert [item.id for item in tracks] == ["matched"]
    assert state["music"]["track"]["id"] == "matched"
    assert state["music"]["selection"]["mode"] == "ai_matched"
    assert state["music"]["volume"] == 0.22


def test_audio_controls_persist_reopen_and_export_latest_mix(db, tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    project_id = "11111111-1111-4111-8111-111111111111"
    state = state_for(project_id, settings)
    audio = settings.render_root / project_id / "audio" / "narration-test.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"immutable narration")
    track = tmp_path / "song.ogg"
    track.write_bytes(b"immutable selected song")
    state.update(voice={}, music={"enabled": True, "volume": 0.14, "track": {"id": "song", "title": "Selected Song"}})
    project = seed_project(db, project_id, state)
    monkeypatch.setattr("clipforge.exporter.verify_mp4", accept_mp4)
    update_project_audio(db, project, AudioSettingsUpdate(base_revision=1, voice_volume=0.6, music_volume=0.2, music_enabled=False), settings)
    db.expire_all()
    reopened = get_project(db, project_id)
    saved = serialize_project(reopened)["revision"]["state"]
    assert saved["voice"]["volume"] == 0.6
    assert saved["music"]["volume"] == 0.2
    assert saved["music"]["enabled"] is False
    assert saved["music"]["requested_enabled"] is False
    assert saved["music"]["track"]["id"] == "song"
    assert not (settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4").is_symlink()
    export_project(db, reopened, settings, base_revision=reopened.current_revision)
    assert track.read_bytes() == b"immutable selected song"


def test_post_render_track_selection_persists_without_changing_base_render(db, tmp_path, monkeypatch):
    settings = export_settings(tmp_path)
    project_id = "22222222-2222-4222-8222-222222222222"
    state = state_for(project_id, settings)
    base = settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4"
    before = base.read_bytes()
    track = MusicTrack("licensed", "Licensed Track", "tracks/licensed.mp3", "documentary", "low", ("instrumental",), "catalog", "CC BY 4.0")
    monkeypatch.setattr("clipforge.services.available_music_tracks", lambda: (track,))
    project = seed_project(db, project_id, state)

    update_project_music_selection(db, project, MusicSelectionUpdate(base_revision=1, track_id="licensed", mode="ai_matched"))
    db.expire_all()
    reopened = get_project(db, project_id)
    saved = serialize_project(reopened)["revision"]["state"]
    assert saved["music"]["track"]["id"] == "licensed"
    assert saved["music"]["selection"]["mode"] == "ai_matched"
    assert base.read_bytes() == before
    update_project_audio(db, reopened, AudioSettingsUpdate(base_revision=reopened.current_revision, voice_volume=1, music_volume=0.3, music_enabled=True), settings)
    assert base.read_bytes() == before


def test_ai_music_recommendations_only_send_real_candidates_validate_output_and_persist(db, tmp_path, monkeypatch):
    project_id = "33333333-3333-4333-8333-333333333333"
    state = state_for(project_id, export_settings(tmp_path))
    state.update(intent={"topic": "Ocean science", "tone": "calm", "content_type": "factual_explainer"}, script={"text": "A calm explanation of ocean currents."}, music={"mood": "documentary"})
    project = seed_project(db, project_id, state)
    catalog = (
        MusicTrack("calm", "Calm Current", "tracks/calm.mp3", "documentary", "low", ("ambient",), "catalog", "CC BY"),
        MusicTrack("bright", "Bright Current", "tracks/bright.mp3", "tech", "medium", ("electronic",), "catalog", "CC BY"),
    )
    calls = []
    def fake_ai(sent_state, candidates, _settings):
        calls.append(candidates)
        from clipforge.ai import AIMusicRecommendationResult
        return AIMusicRecommendationResult(["bright", "invented", "bright"], "connected")
    monkeypatch.setattr("clipforge.services.rank_music_with_openai", fake_ai)
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test")

    first = project_music_recommendations(db, project, catalog, settings)
    assert next(track.id for track in first) == "bright"
    assert {candidate["id"] for candidate in calls[0]} <= {track.id for track in catalog}
    assert "invented" not in [track.id for track in first]
    revision_after_first_match = project.current_revision

    reopened = get_project(db, project_id)
    second = project_music_recommendations(db, reopened, catalog, settings)
    assert [track.id for track in second] == [track.id for track in first]
    assert len(calls) == 1
    assert reopened.current_revision == revision_after_first_match
    saved = serialize_project(reopened)["revision"]["state"]["music"]["recommendations"]
    assert saved["track_ids"][0] == "bright"


def test_ai_music_matching_failure_uses_deterministic_order_without_changing_export(db, tmp_path, monkeypatch):
    project_id = "44444444-4444-4444-8444-444444444444"
    settings = export_settings(tmp_path)
    project = seed_project(db, project_id, state_for(project_id, settings))
    catalog = (MusicTrack("licensed", "Licensed Track", "tracks/licensed.mp3", "documentary", "low", ("instrumental",), "catalog", "CC BY"),)
    from clipforge.ai import AIMusicRecommendationResult
    monkeypatch.setattr("clipforge.services.rank_music_with_openai", lambda *_args: AIMusicRecommendationResult([], "provider_error"))
    before = (settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4").read_bytes()

    recommendations = project_music_recommendations(db, project, catalog, settings)
    assert [track.id for track in recommendations] == ["licensed"]
    assert (settings.render_root / project_id / "renders" / "v2" / "clipforge.mp4").read_bytes() == before
