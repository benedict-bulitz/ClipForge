import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from clipforge.config import Settings
from clipforge.main import voice_preview_route
from clipforge.renderer import VoiceGenerationError, _create_voice
from clipforge.schemas import AdvancedOptions, ProjectCreate, VoicePreviewCreate
from clipforge.services import create_project, current_revision
from clipforge.voice import initial_voice
from clipforge.voice_preview import generate_voice_preview


def preview_settings(tmp_path: Path, *, key: str | None = "unit-openai-preview-token") -> Settings:
    return Settings(
        _env_file=None,
        clipforge_ai_mode="local",
        openai_api_key=key,
        brave_search_api_key=None,
        pexels_api_key=None,
        render_root=tmp_path,
    )


def install_fake_tts(monkeypatch, captured: list[dict]):
    def fake_create(state, temp, settings):
        captured.append({"state": state, "settings": settings})
        output = temp / "voice.wav"
        output.write_bytes(b"preview-audio" * 500)
        return output, "openai"

    monkeypatch.setattr("clipforge.voice_preview._create_voice", fake_create)


def voice_state() -> dict:
    return {
        "script": {"text": "A safe voice test.", "word_count": 4},
        "intent": {"language": "en"},
        "duration": {"estimated_seconds": 3},
        "voice": initial_voice(voice_id="marin"),
    }


def test_initial_voice_options_reach_project_state(db, tmp_path):
    settings = preview_settings(tmp_path, key=None)
    project = create_project(
        db,
        ProjectCreate(
            prompt="Tell a short story about a careful explorer",
            options=AdvancedOptions(
                voice_id="cedar",
                voice_presentation="masculine",
                voice_tone="documentary",
                voice_speed=0.85,
            ),
        ),
        settings,
    )
    voice = current_revision(project).state["voice"]

    assert voice["voice_id"] == "cedar"
    assert voice["gender_presentation"] == "masculine"
    assert voice["tone"] == "documentary"
    assert voice["speed"] == 0.85


def test_preview_schema_rejects_unsupported_voice_and_excessive_text():
    with pytest.raises(ValidationError):
        VoicePreviewCreate(voice_id="made-up-voice")
    with pytest.raises(ValidationError):
        VoicePreviewCreate(text="x" * 281)


def test_preview_uses_resolved_settings_without_exposing_credentials(
    monkeypatch, tmp_path
):
    captured = []
    install_fake_tts(monkeypatch, captured)
    settings = preview_settings(tmp_path)

    result = generate_voice_preview(
        VoicePreviewCreate(voice_id="marin", tone="warm"), settings
    )

    assert captured[0]["settings"].openai_api_key == "unit-openai-preview-token"
    assert "unit-openai-preview-token" not in json.dumps(result)
    assert set(result) == {"url", "provider", "cached", "cache_key"}


def test_identical_preview_request_reuses_cache(monkeypatch, tmp_path):
    captured = []
    install_fake_tts(monkeypatch, captured)
    settings = preview_settings(tmp_path)
    payload = VoicePreviewCreate(text="A short neutral preview.", voice_id="marin")

    first = generate_voice_preview(payload, settings)
    second = generate_voice_preview(payload, settings)

    assert first["cache_key"] == second["cache_key"]
    assert second["cached"] is True
    assert second["provider"] == "openai"
    assert len(captured) == 1


def test_voice_and_speed_each_change_preview_cache_key(monkeypatch, tmp_path):
    captured = []
    install_fake_tts(monkeypatch, captured)
    settings = preview_settings(tmp_path)

    base = generate_voice_preview(VoicePreviewCreate(voice_id="marin", speed=1), settings)
    voice = generate_voice_preview(VoicePreviewCreate(voice_id="cedar", speed=1), settings)
    speed = generate_voice_preview(VoicePreviewCreate(voice_id="marin", speed=0.85), settings)

    assert len({base["cache_key"], voice["cache_key"], speed["cache_key"]}) == 3
    assert len(captured) == 3


def test_preview_and_final_project_share_voice_resolution(monkeypatch, db, tmp_path):
    captured = []
    install_fake_tts(monkeypatch, captured)
    settings = preview_settings(tmp_path)
    options = AdvancedOptions(
        voice_id="onyx",
        voice_presentation="masculine",
        voice_tone="deep",
        voice_speed=0.9,
    )
    project = create_project(
        db, ProjectCreate(prompt="Explain a fictional eclipse", options=options), settings
    )

    generate_voice_preview(
        VoicePreviewCreate(
            voice_id="onyx",
            presentation="masculine",
            tone="deep",
            speed=0.9,
        ),
        settings,
    )

    expected = initial_voice(
        voice_id="onyx", presentation="masculine", tone="deep", speed=0.9
    )
    final_voice = current_revision(project).state["voice"]
    preview_voice = captured[0]["state"]["voice"]
    for key, value in expected.items():
        assert final_voice[key] == value
        assert preview_voice[key] == value


def test_default_create_project_voice_still_works(db, tmp_path):
    settings = preview_settings(tmp_path, key=None)

    project = create_project(
        db, ProjectCreate(prompt="Explain a fictional mountain"), settings
    )

    voice = current_revision(project).state["voice"]
    assert voice["voice_id"] == "marin"
    assert voice["speed"] == 1


def test_configured_openai_auth_failure_never_falls_back_to_system_voice(
    monkeypatch, tmp_path
):
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
    response = httpx.Response(401, request=request)
    auth_error = __import__("openai").AuthenticationError(
        "Your API key has been invalidated.",
        response=response,
        body={"error": {"code": "token_invalidated"}},
    )
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(create=lambda **_kwargs: (_ for _ in ()).throw(auth_error))
        )
    )
    monkeypatch.setattr("clipforge.renderer.OpenAI", lambda **_kwargs: fake_client)
    monkeypatch.setattr("clipforge.renderer.shutil.which", lambda name: f"/usr/bin/{name}")
    system_calls = []
    monkeypatch.setattr(
        "clipforge.renderer._run_process", lambda *_args, **_kwargs: system_calls.append(True)
    )

    with pytest.raises(VoiceGenerationError) as raised:
        _create_voice(voice_state(), tmp_path, preview_settings(tmp_path))

    assert raised.value.category == "openai_authentication_error"
    assert raised.value.status_code == 401
    assert "Update the OpenAI key" in str(raised.value)
    assert "unit-openai-preview-token" not in str(raised.value)
    assert system_calls == []


def test_no_openai_key_intentionally_uses_system_voice(monkeypatch, tmp_path):
    monkeypatch.setattr("clipforge.renderer.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("clipforge.renderer._system_voice", lambda *_args: None)

    def fake_run(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(b"system-audio" * 500)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("clipforge.renderer._run_process", fake_run)

    output, provider = _create_voice(
        voice_state(), tmp_path, preview_settings(tmp_path, key=None)
    )

    assert output.is_file()
    assert provider == "macos_say"


def test_preview_cache_uses_actual_provider_identity(monkeypatch, tmp_path):
    def unexpected_explicit_fallback(_state, temp, _settings):
        output = temp / "voice.aiff"
        output.write_bytes(b"system-audio" * 500)
        return output, "macos_say"

    monkeypatch.setattr("clipforge.voice_preview._create_voice", unexpected_explicit_fallback)
    result = generate_voice_preview(
        VoicePreviewCreate(text="Cache identity test."), preview_settings(tmp_path)
    )

    assert result["provider"] == "macos_say"
    assert result["cache_key"] in Path(result["url"]).name
    assert not list(tmp_path.glob("voice-previews/*.wav"))
    assert list(tmp_path.glob("voice-previews/*.aiff"))


def test_voice_preview_route_returns_actionable_provider_error(monkeypatch, tmp_path):
    failure = VoiceGenerationError(
        "OpenAI voice authentication failed. Update the OpenAI key in Settings.",
        category="openai_authentication_error",
        status_code=401,
    )
    monkeypatch.setattr(
        "clipforge.main.generate_voice_preview",
        lambda *_args: (_ for _ in ()).throw(failure),
    )
    request = Request({"type": "http", "client": ("127.0.0.1", 1234)})

    with pytest.raises(HTTPException) as raised:
        voice_preview_route(
            VoicePreviewCreate(), request, preview_settings(tmp_path)
        )

    assert raised.value.status_code == 401
    assert raised.value.detail == {
        "status": "openai_authentication_error",
        "message": "OpenAI voice authentication failed. Update the OpenAI key in Settings.",
    }
