"""Real-world Visual Director repairs: manual generation, prompt quality, base + overlay.

The OpenAI client, the visual translator, providers and OpenCLIP are mocked
at their module factories, so the real request paths run without network or
spend.
"""
import base64
import copy
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_export import seed_project
from test_staged_media_search import Commons, Provider, Verifier, cand
from test_staged_media_search import settings_for as staged_settings
from test_story_visual_director import FINGERS, FINGERS_Q, junk_provider
from test_story_visual_integration import fact, generate, visual
from test_visual_director import (
    POOR,
    WEAK_PASS,
    FakeGenerator,
    finger_project,
    png_bytes,
    settings_for,
)

import clipforge.services  # noqa: F401 - registers ORM models for the db fixture
from clipforge import image_generation, visual_director, visual_translation
from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.main import app
from clipforge.media import prepare_project_media
from clipforge.pipeline import _fallback_visual_intent
from clipforge.renderer import _create_visual_segment, _scene_media_path
from clipforge.services import get_project, serialize_project
from clipforge.visual_verifier import VisualVerification

PROMPT = "Photorealistic macro photo of a wet human hand with wrinkled fingertips holding a smooth river stone, water droplets, natural light, no text"
PROJECT_ID = "66666666-6666-4666-8666-666666666666"


class FakeImages:
    """Stands in for ``OpenAI().images`` so the real generator code runs."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        color = (80 + len(self.calls) * 20, 120, 150)
        buffer = io.BytesIO()
        Image.new("RGB", (1024, 1536), color).save(buffer, format="PNG")
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(buffer.getvalue()).decode())],
            usage=None, quality=kwargs.get("quality"), size=kwargs.get("size"), output_format="png",
        )


def status_error(cls, status: int, body: dict):
    request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
    return cls("error from provider (key sk-live-secret)", response=httpx.Response(status, request=request), body=body)


@pytest.fixture
def api(db, tmp_path, monkeypatch):
    """Seeded project + API client; returns (client, images, settings, project)."""
    settings = settings_for(tmp_path, openai_api_key="sk-live-secret")
    state = finger_project()
    scene = state["scenes"][2]
    current = tmp_path / PROJECT_ID / "graphics" / "old.png"
    current.parent.mkdir(parents=True)
    Image.new("RGB", (1080, 1920), (30, 30, 30)).save(current)
    scene["media"] = {
        "identity": "simple_graphic:photo:old", "provider": "simple_graphic", "source": "simple_graphic",
        "provider_id": "old", "kind": "photo", "cache_path": f"{PROJECT_ID}/graphics/old.png",
        "graphic": {"kind": "process", "steps": ["a", "b"]}, "source_url": "", "creator": "ClipForge graphic", "query": "",
    }
    scene["asset_status"] = "graphic_ready"
    state.update(render={"status": "complete", "url": None}, captions={"enabled": True, "items": []})
    state["timeline"].update(fps=30, duration=12)
    project = seed_project(db, PROJECT_ID, state)
    images = FakeImages()
    monkeypatch.setattr(image_generation, "OPENAI_CLIENT_FACTORY", lambda *_a: SimpleNamespace(images=images))
    monkeypatch.setattr("clipforge.main.discover_scene_media_candidates", lambda *_a, **_k: ("token", []))

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client, images, settings, project
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Manual Generate AI Image, end to end
# ---------------------------------------------------------------------------

def test_manual_generate_creates_visible_new_asset_and_next_render_uses_it(api, db, monkeypatch, tmp_path):
    client, images, settings, _project = api

    generated = client.post(f"/api/projects/{PROJECT_ID}/scenes/3/generate-image", json={"base_revision": 1, "prompt": PROMPT})

    assert generated.status_code == 200
    body = generated.json()
    assert body["status"] == "generated", body
    # The exact edited prompt is what the image model received.
    assert images.calls[0]["prompt"] == PROMPT
    assert images.calls[0]["model"] == "gpt-image-2" and images.calls[0]["quality"] == "low"
    assert body["prompt_used"] == PROMPT and body["prompt_source"] == "user_edited"
    candidate = body["candidate"]
    assert candidate["generated"] and candidate["new"] and candidate["token"].startswith("gen:")
    assert candidate["provider_id"] != "old" and candidate["preview_url"].startswith("/media/")
    asset_path = tmp_path / candidate["preview_url"].removeprefix("/media/")
    assert asset_path.is_file()  # persisted before any Apply
    assert body["project"]["current_revision"] == 2  # persisted as a system revision

    # Reopening Change Media shows the generated alternative.
    reopened = client.get(f"/api/projects/{PROJECT_ID}/scenes/3/media-candidates").json()
    assert reopened["candidates"][0]["token"] == candidate["token"]
    assert reopened["candidates"][0]["preview_url"] == candidate["preview_url"]

    # A second generation with the same prompt is a NEW asset, never the old one.
    again = client.post(f"/api/projects/{PROJECT_ID}/scenes/3/generate-image", json={"base_revision": 2, "prompt": PROMPT}).json()
    assert again["status"] == "generated" and again["candidate"]["provider_id"] != candidate["provider_id"]

    applied = client.post(
        f"/api/projects/{PROJECT_ID}/scenes/3/media-candidates/apply", json={"token": candidate["token"], "base_revision": 3}
    )
    assert applied.status_code == 200, applied.text
    scene = applied.json()["revision"]["state"]["scenes"][2]
    assert scene["media"]["provider_id"] == candidate["provider_id"]
    assert scene["media"]["source"] == "generated_openai" and scene["asset_status"] == "generated_image_ready"
    # No finished render yet: the choice is still saved and the render marked stale.
    assert applied.json()["revision"]["state"]["render"]["status"] == "regeneration_required"

    # The next render uses the applied asset.
    db.expire_all()
    saved = serialize_project(get_project(db, PROJECT_ID))["revision"]["state"]
    source, kind = _scene_media_path(saved["scenes"][2], settings)
    assert source == asset_path.resolve() and kind == "photo"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    (tmp_path / "render-temp").mkdir()
    _create_visual_segment("ffmpeg", saved, saved["scenes"][2], 2, 2.0, tmp_path / "render-temp", settings)
    assert str(asset_path.resolve()) in commands[0]


@pytest.mark.parametrize(
    ("error", "code", "phrase"),
    [
        (None, "missing_api_key", "API key missing"),
        (status_error(openai.AuthenticationError, 401, {"code": "invalid_api_key"}), "invalid_credentials", "rejected the API key"),
        (status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), "billing", "billing"),
        (status_error(openai.PermissionDeniedError, 403, {"code": None}), "permission_denied", "no access to gpt-image-2"),
        (openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")), "timeout", "timed out"),
        (status_error(openai.NotFoundError, 404, {"code": "model_not_found"}), "model_unavailable", "gpt-image-2 is not available"),
        (status_error(openai.BadRequestError, 400, {"code": "unsupported_parameter", "param": "quality"}), "unsupported_parameter", "(quality)"),
        (status_error(openai.BadRequestError, 400, {"code": "invalid_request_error"}), "request_rejected", "rejected the request"),
    ],
)
def test_generation_failures_return_ui_safe_messages(api, monkeypatch, error, code, phrase):
    client, images, settings, _project = api
    if code == "missing_api_key":
        app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"openai_api_key": None})
    images.error = error

    response = client.post(f"/api/projects/{PROJECT_ID}/scenes/3/generate-image", json={"base_revision": 1, "prompt": PROMPT})

    assert response.status_code == 200  # never an endless or silent state
    body = response.json()
    assert body["status"] == "failed" and body["error_code"] == code
    assert phrase in body["message"]
    assert "sk-live-secret" not in response.text and "Traceback" not in response.text
    assert body["candidate"] is None
    assert body["project"]["revision"]["state"]["scenes"][2]["media"]["provider_id"] == "old"


def test_verification_rejection_is_reported_and_audited(api, monkeypatch):
    client, _images, _settings, _project = api

    class Rejecting:
        status = "available"

        def verify_local_image(self, *_args, **_kwargs):
            return VisualVerification(0.1, "verified", subject_score=0.1, scene_score=0.1)

    monkeypatch.setattr("clipforge.visual_verifier.get_visual_verifier", lambda: Rejecting())

    body = client.post(f"/api/projects/{PROJECT_ID}/scenes/3/generate-image", json={"base_revision": 1, "prompt": PROMPT}).json()

    assert body["status"] == "rejected" and "did not match this scene" in body["message"]
    assert body["candidate"] is None
    # The paid image stays auditable (billed record) without replacing media.
    state = body["project"]["revision"]["state"]
    assert state["visual_director"]["generations"][-1]["billed"] is True
    assert state["scenes"][2]["media"]["provider_id"] == "old"


def test_generation_returning_the_current_asset_is_unchanged_not_success(api, monkeypatch):
    client, _images, _settings, _project = api

    def same_asset(scene, *_args, **_kwargs):
        return dict(scene["media"]), {"status": "accepted", "prompt": PROMPT, "prompt_source": "user_edited"}

    monkeypatch.setattr(visual_director, "generate_scene_image", same_asset)

    body = client.post(f"/api/projects/{PROJECT_ID}/scenes/3/generate-image", json={"base_revision": 1, "prompt": PROMPT}).json()

    assert body["status"] == "unchanged" and body["candidate"] is None


# ---------------------------------------------------------------------------
# Prompt quality: full fact, visible subject, no abstract words
# ---------------------------------------------------------------------------

FULL = "Forschende vermuten, dass runzlige Haut beim Greifen nasser Gegenstände hilft."
FRAGMENT = "Forschende vermuten dass runzlige Haut weniger"


def fragment_state():
    state = finger_project(finger_scenes_with(FULL))
    scene = state["scenes"][0]
    scene["narration"] = FRAGMENT
    scene["visual_intent"] = _fallback_visual_intent(FRAGMENT, "en")
    return state, scene


def finger_scenes_with(text):
    return [("support", text, {"visual_goal": "x y", "media_queries": ["x"]})]


@pytest.mark.parametrize("legacy", [False, True])
def test_prompt_uses_full_fact_translated_into_a_visible_subject(tmp_path, monkeypatch, legacy):
    state, scene = fragment_state()
    if legacy:
        scene["visual_intent"].pop("source")  # old project: detected by shape
    seen = {}

    class Responses:
        def parse(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(output_parsed=visual_translation.VisualTranslation(
                main_subject="a human hand with visibly wrinkled wet fingertips",
                visible_state_or_action="securely gripping a smooth wet stone",
                setting="water droplets, natural light",
                details=["realistic skin texture"],
            ))

    monkeypatch.setattr(visual_translation, "TRANSLATOR_CLIENT_FACTORY", lambda *_a: SimpleNamespace(responses=Responses()))
    strategy = visual_director.plan_scene_strategy(scene, state, {"protected_entities": []})

    built = visual_director.build_generation_prompt(scene, state, strategy, settings=settings_for(tmp_path, openai_api_key="sk-test"))

    assert FULL in seen["input"] and FRAGMENT not in seen["input"]  # full Story fact, not the fragment
    prompt = built["prompt"]
    assert built["visual_source"] == "fact_translation"
    assert "wrinkled wet fingertips" in prompt and "gripping a smooth wet stone" in prompt
    for word in ("Forschende", "vermuten", "dass", "weniger"):
        assert word.casefold() not in prompt.casefold()
    for rule in ("no text", "no labels", "no diagram", "no infographic", "no watermark", "9:16", "overlays"):
        assert rule in prompt


def test_prompt_without_translator_keeps_only_concrete_fact_words(tmp_path):
    state, scene = fragment_state()
    strategy = visual_director.plan_scene_strategy(scene, state, {"protected_entities": []})

    built = visual_director.build_generation_prompt(scene, state, strategy)

    prompt = built["prompt"].casefold()
    assert built["visual_source"] == "fact_words"
    assert "runzlige haut" in prompt and "greifen" in prompt  # from the FULL fact
    for word in ("forschende", "vermuten", " dass", "weniger"):
        assert word not in prompt


def test_planned_intent_of_the_same_fact_is_shared_by_its_fragments(tmp_path):
    state = finger_project(finger_scenes_with(FULL))
    first = state["scenes"][0]
    first["visual_intent"] = {"visual_goal": "wet wrinkled fingertips gripping a stone", "media_queries": ["wet wrinkled fingertips"]}
    second = {**copy.deepcopy(first), "id": "scene_01b", "narration": FRAGMENT, "visual_intent": _fallback_visual_intent(FRAGMENT, "en")}
    state["scenes"].append(second)
    strategy = visual_director.plan_scene_strategy(second, state, {"protected_entities": []})

    built = visual_director.build_generation_prompt(second, state, strategy)

    assert built["visual_source"] == "visual_intent"
    assert "wet wrinkled fingertips gripping a stone" in built["prompt"]
    assert "vermuten" not in built["prompt"].casefold()


# ---------------------------------------------------------------------------
# Base visual + overlay
# ---------------------------------------------------------------------------

FINGERS_SPLIT = [
    FINGERS[0],
    (fact("Nervensignale sorgen dafür, dass sich Blutgefäße verengen; dadurch legt sich die Haut in Falten."), "explanation",
     visual("wrinkled fingertip skin close-up", ["wrinkled fingertip skin", "wet fingertips close-up"], ["shared", "shared"])),
    FINGERS[2],
]


@pytest.mark.parametrize("with_hand", [True, False])
def test_wet_finger_explanations_use_one_base_visual_with_evolving_overlay(monkeypatch, tmp_path, with_hand):
    state = generate(monkeypatch, tmp_path, FINGERS_Q, FINGERS_SPLIT, planner_target="")
    provider = junk_provider(with_hand=with_hand)
    if with_hand:
        hand = cand("hand2", "wrinkled fingertip skin", "Wrinkled wet fingertip skin close-up", kind="photo")
        original = provider.search_photos
        provider.search_photos = lambda query, **k: [*original(query, **k), *([hand] if query == "wrinkled fingertip skin" else [])]
    generator = FakeGenerator()

    prepare_project_media(
        state, "project", staged_settings(tmp_path), client=provider, fallback_client=Commons(),
        visual_verifier=Verifier({"book": POOR, "bus": WEAK_PASS, "city": POOR}), image_generator=generator, extra_clients=[],
    )

    for scene in state["scenes"]:
        assert (scene.get("media") or {}).get("provider_id") not in {"book", "bus", "city"}
    explanation = [scene for scene in state["scenes"] if scene.get("story_role") == "explanation"]
    assert len(explanation) >= 2  # the arc split one fact into several scenes
    bases = {scene["media"]["identity"] for scene in explanation}
    assert len(bases) == 1  # visual continuity across the fact
    base = explanation[0]["media"]
    assert base["source"] != "simple_graphic"  # never a full-screen card here
    assert base["provider_id"] == "hand2" if with_hand else base["source"] == "generated_openai"
    overlays = [scene["overlays"][0]["spec"] for scene in explanation]
    assert all(spec["kind"] == "process" for spec in overlays)
    assert [spec["active"] for spec in overlays] == sorted(spec["active"] for spec in overlays)
    assert overlays[0]["active"] < overlays[-1]["active"]  # the overlay evolves
    assert "Nervensignale" in overlays[0]["steps"][0]
    assert all(scene["visual_director"]["composition"] == "base_with_overlay" for scene in explanation)
    assert explanation[1]["media_search"]["logical_queries_executed"] == 0  # no extra search/generation
    assert len(generator.prompts) <= 3


def test_fullscreen_graphic_only_when_no_base_visual_exists(tmp_path):
    scenes = [("support", "Schweden hat rund 267.570 Inseln.", {"visual_goal": "swedish archipelago", "objects": ["archipelago"], "media_queries": ["swedish archipelago"]})]
    state = finger_project(scenes)

    prepare_project_media(state, "project", settings_for(tmp_path), client=Provider(), fallback_client=Commons(), visual_verifier=Verifier(), image_generator=FakeGenerator(), extra_clients=[])

    scene = state["scenes"][0]
    assert scene["media"]["source"] == "simple_graphic"
    assert scene["visual_director"]["composition"] == "fullscreen_graphic"
    assert scene["visual_director"]["composition_reason"] == "no_acceptable_base_visual"
    assert "overlays" not in scene


def test_overlay_is_never_drawn_over_an_unsafe_background():
    strategy = {"reveal_allowed": False, "overlay_spec": {"kind": "process", "steps": ["a", "b"]}}
    unsafe = {"media": {"source": "pexels", "reveal_safe": False}}
    safe = {"media": {"source": "pexels", "reveal_safe": True}}
    graphic = {"media": {"source": "simple_graphic"}}

    assert visual_director.attach_overlays(unsafe, strategy) == []
    assert visual_director.attach_overlays(graphic, strategy) == []
    overlays = visual_director.attach_overlays(safe, strategy, position=0, count=2)
    assert overlays[0]["spec"] == {"kind": "process", "steps": ["a", "b"], "active": 0}
    assert visual_director.attach_overlays(safe, strategy, position=1, count=2)[0]["spec"]["active"] == 1


def test_overlay_text_obeys_story_arc_before_reveal(monkeypatch, tmp_path):
    from test_story_visual_director import islands

    state = islands(monkeypatch, tmp_path)
    prepare_project_media(state, "project", staged_settings(tmp_path), client=Provider(), fallback_client=Commons(), visual_verifier=Verifier(), image_generator=FakeGenerator(), extra_clients=[])

    for scene in state["scenes"]:
        if scene["story_stage"] == "before_reveal":
            text = str(scene.get("overlays")) + str((scene.get("visual_director") or {}).get("overlay_spec"))
            assert "schwed" not in text.casefold() and "swed" not in text.casefold()


def test_renderer_composites_overlay_over_base_visual(monkeypatch, tmp_path):
    settings = settings_for(tmp_path)
    state = finger_project()
    state["timeline"]["fps"] = 30
    source = tmp_path / "project" / "assets" / "pexels" / "photo-9.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (1600, 1200), (40, 90, 60)).save(source)
    scene = state["scenes"][1]
    scene["media"] = {"identity": "pexels:photo:9", "provider": "pexels", "kind": "photo", "cache_path": "project/assets/pexels/photo-9.jpg"}
    scene["overlays"] = [{"kind": "process", "spec": {"kind": "process", "steps": ["Nervensignal", "Blutgefäße verengen sich"], "active": 1}}]
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"segment")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", run)
    monkeypatch.setattr("clipforge.renderer.analyze_scene_media", lambda *_a, **_k: {"center_x": 0.5, "center_y": 0.6, "confidence": 0.9})
    (tmp_path / "temp").mkdir()
    _create_visual_segment("ffmpeg", state, scene, 1, 2.0, tmp_path / "temp", settings)

    command = commands[0]
    graph = command[command.index("-filter_complex") + 1]
    assert "zoompan" in graph and "overlay=0:0" in graph and command[command.index("-map") + 1] == "[vout]"
    overlay_png = Path(command[command.index("-i", command.index("-i") + 1) + 1])
    assert overlay_png.is_file() and Image.open(overlay_png).mode == "RGBA"
    assert scene["overlay_render"]["placement"] == "upper"  # focal subject below, captions at the bottom
    assert png_bytes  # fixture helper import kept for parity
