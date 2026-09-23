from test_export import accept_mp4, export_settings, seed_project, state_for

from clipforge.music import MusicTrack
from clipforge.schemas import AudioSettingsUpdate, MusicSelectionUpdate
from clipforge.services import (
    export_project,
    get_project,
    serialize_project,
    update_project_audio,
    update_project_music_selection,
)


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
