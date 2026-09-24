"""Visual Director V2: strategy, strict real-media gate and bounded generated fallback.

Providers, OpenCLIP and the OpenAI image API are all mocked; the suite never
spends real API credits (see ``forbid_real_image_generation`` in conftest).
"""
import copy
import inspect
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from clipforge import visual_director
from clipforge.config import Settings
from clipforge.image_generation import (
    GeneratedImage,
    ImageGenerationError,
    OpenAIImageGenerator,
    get_image_generator,
)
from clipforge.media import (
    MAX_SCENE_QUERY_BUDGET,
    MediaCandidate,
    is_real_media_allowed,
    is_scene_asset_allowed,
    media_relevance,
    prepare_project_media,
    real_media_quality_gate,
)
from clipforge.pixabay import parse_pixabay_photos, parse_pixabay_videos
from clipforge.renderer import RenderUnavailable, _draw_scene, _scene_media_path, still_motion_plan
from clipforge.simple_graphics import normalise_graphic_spec, render_simple_graphic
from clipforge.visual_verifier import VisualVerification

STRONG = (0.31, 0.31)
WEAK_PASS = (0.245, 0.245)
POOR = (0.12, 0.12)


def cand(provider_id: str, title: str, *, query: str = "wrinkled fingers water", kind: str = "photo", provider: str = "pexels") -> MediaCandidate:
    return MediaCandidate(
        provider_id=provider_id,
        kind=kind,
        download_url=f"https://media.test/{provider_id}",
        source_url=f"https://www.{provider}.test/{kind}/{provider_id}/",
        creator="Unit Tester",
        creator_url=None,
        width=1080,
        height=1920,
        duration=12 if kind == "video" else None,
        query=query,
        rank=100,
        provider=provider,
        title=title,
    )


class Provider:
    """Returns the same pool for every query; records requests."""

    def __init__(self, photos=(), videos=()):
        self.photos, self.videos = list(photos), list(videos)
        self.calls: list[tuple[str, str]] = []

    def search_videos(self, query, *, portrait, scene_duration):
        self.calls.append(("video", query))
        return [replace_query(item, query) for item in self.videos]

    def search_photos(self, query, *, portrait):
        self.calls.append(("photo", query))
        return [replace_query(item, query) for item in self.photos]

    @property
    def queries(self):
        return list(dict.fromkeys(query for _kind, query in self.calls))

    def download(self, _candidate, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-media")
        return destination

    def close(self):
        return None


def replace_query(item: MediaCandidate, query: str) -> MediaCandidate:
    from dataclasses import replace

    return replace(item, query=query)


class Verifier:
    """Mock OpenCLIP keyed by provider id; generated images use ``generated``."""

    status = "available"

    def __init__(self, scores=None, *, default=STRONG, generated=STRONG):
        self.scores = scores or {}
        self.default = default
        self.generated = generated
        self.calls: list[str] = []
        self.local_calls: list[Path] = []

    def verify_candidate(self, candidate, _texts):
        self.calls.append(candidate.provider_id)
        score, scene_score = self.scores.get(candidate.provider_id, self.default)
        return VisualVerification(score, "verified", subject_score=score, scene_score=scene_score)

    def verify_local_image(self, path, _texts, *, asset_identity=None):
        self.local_calls.append(Path(path))
        score, scene_score = self.generated
        return VisualVerification(score, "verified", subject_score=score, scene_score=scene_score)


def png_bytes(color=(90, 120, 150)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1536), color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeGenerator:
    """Stands in for the OpenAI Images API (no network, no credits)."""

    model = "gpt-image-2"

    def __init__(self, *, error: str | None = None):
        self.error = error
        self.prompts: list[str] = []

    def generate(self, prompt, *, quality, size):
        self.prompts.append(prompt)
        if self.error:
            raise ImageGenerationError(self.error, "mock failure")
        return GeneratedImage(png_bytes(), self.model, quality, size, "png", {"total_tokens": 321})


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "clipforge_ai_mode": "local",
        "openai_api_key": None,
        "brave_search_api_key": None,
        "pexels_api_key": "unit-test-token",
        "pixabay_api_key": None,
        "render_root": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


FINGER_SCENES = [
    ("hook", "Warum werden unsere Finger im Wasser schrumpelig?", {
        "visual_goal": "wrinkled fingertips after a long bath",
        "objects": ["wrinkled fingertips"],
        "context": ["bath water"],
        "media_queries": ["wrinkled fingers water", "pruney fingertips bath"],
    }),
    ("answer", "Nerven lassen die Blutgefäße in den Fingern enger werden.", {
        "visual_goal": "close-up of wet wrinkled fingertips",
        "objects": ["wet wrinkled fingertips"],
        "media_queries": ["wet wrinkled fingertips", "wrinkled fingers water"],
    }),
    ("support", "Furchen helfen möglicherweise dabei, nasse Dinge besser festzuhalten.", {
        "visual_goal": "wrinkled fingers gripping a wet object",
        "objects": ["wrinkled fingers", "wet stone"],
        "actions": ["gripping"],
        "media_queries": ["wrinkled fingers gripping wet object", "wet hand holding stone"],
    }),
]


def finger_project(scenes=FINGER_SCENES, *, payoff=None) -> dict:
    blocks, scene_rows = [], []
    for index, (role, narration, intent) in enumerate(scenes, 1):
        block_id = f"voice_block_{index:02d}"
        blocks.append({"id": block_id, "role": role, "text": narration, "fact_ids": [f"fact_{index}"]})
        scene_rows.append({
            "id": f"scene_{index:02d}",
            "block_id": block_id,
            "start": (index - 1) * 4,
            "end": index * 4,
            "narration": narration,
            "visual_goal": intent["visual_goal"],
            "visual_intent": copy.deepcopy(intent),
            "preferred_media": "photo",
            "motion": "subtle_pan",
        })
    return {
        "intent": {"topic": "Warum werden unsere Finger im Wasser schrumpelig?", "language": "de", "content_type": "explainer"},
        "payoff_plan": payoff or {"reveal_policy": "immediate_context_allowed", "hook_must_not_reveal": ""},
        "format_plan": {"selected_format": "explanation"},
        "script": {"blocks": blocks},
        "timeline": {"width": 1080, "height": 1920},
        "scenes": scene_rows,
        "assets": {},
    }


HAND = cand("hand", "Close-up of wrinkled fingers after a bath in water")
BOOK = cand("book", "Old book page with printed text", provider="wikimedia")
BUS = cand("bus", "Bus stop on a city street")


def run(state, tmp_path, *, photos=(), verifier=None, generator=None, settings=None, commons=None):
    pexels = Provider(photos=photos)
    prepare_project_media(
        state, "project", settings or settings_for(tmp_path), client=pexels,
        fallback_client=commons or Provider(), visual_verifier=verifier if verifier is not None else Verifier(),
        image_generator=generator, extra_clients=[],
    )
    return pexels


# ---------------------------------------------------------------------------
# A. Strict real-media quality gate
# ---------------------------------------------------------------------------

def test_relevant_hand_wins_over_book_page_and_bus_stop(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    verifier = Verifier({"hand": STRONG, "book": POOR, "bus": WEAK_PASS})

    run(state, tmp_path, photos=[BOOK, BUS, HAND], verifier=verifier)

    scene = state["scenes"][0]
    assert scene["media"]["provider_id"] == "hand"
    assert scene["visual_director"]["decision"] == visual_director.ACCEPTED_REAL
    assert scene["media"]["source"] == "pexels"


@pytest.mark.parametrize("verifier", [Verifier({"book": POOR, "bus": WEAK_PASS}), SimpleNamespace(status="unavailable")])
def test_book_and_bus_cannot_win_just_because_they_exist(tmp_path, verifier):
    state = finger_project(FINGER_SCENES[:1])

    run(state, tmp_path, photos=[BOOK, BUS], verifier=verifier, commons=Provider(photos=[BOOK]))

    scene = state["scenes"][0]
    assert scene.get("media", {}).get("provider_id") not in {"book", "bus"}
    assert scene["asset_status"] == "real_media_unavailable"
    assert scene["visual_director"]["decision"] == visual_director.MISSING
    assert scene["media_search"]["logical_queries_executed"] <= MAX_SCENE_QUERY_BUDGET


def test_quality_gate_reuses_existing_verifier_thresholds():
    scene = finger_project()["scenes"][0]
    state = finger_project()
    book_relevance = media_relevance(BOOK, scene, state)
    assert book_relevance["confidence"] == "rejected"
    # Borderline OpenCLIP pass cannot rescue strongly mismatched metadata ...
    assert real_media_quality_gate(BOOK, {**book_relevance, "visual": {"status": "verified", "score": 0.245, "scene_score": 0.245}}) == (False, "semantic_mismatch")
    # ... an OpenCLIP rejection always loses ...
    assert real_media_quality_gate(HAND, {**media_relevance(HAND, scene, state), "visual": {"status": "verified", "score": 0.12, "scene_score": 0.12}})[0] is False
    # ... and without OpenCLIP, matching metadata is required.
    assert real_media_quality_gate(HAND, media_relevance(HAND, scene, state))[0] is True
    assert real_media_quality_gate(BUS, media_relevance(BUS, scene, state))[0] is False


# ---------------------------------------------------------------------------
# B. Generated fallback
# ---------------------------------------------------------------------------

def test_generated_image_fallback_is_selected_when_no_real_candidate_survives(tmp_path):
    state = finger_project()
    generator = FakeGenerator()
    verifier = Verifier({"book": POOR, "bus": POOR})

    run(state, tmp_path, photos=[BOOK, BUS], verifier=verifier, generator=generator)

    scene = state["scenes"][2]
    media = scene["media"]
    assert media["source"] == media["provider"] == "generated_openai"
    assert is_scene_asset_allowed(media) and not is_real_media_allowed(media)
    assert scene["asset_status"] == "generated_image_ready"
    assert scene["visual_director"]["decision"] == visual_director.GENERATE_FALLBACK
    assert scene["visual_director"]["resolved_type"] == visual_director.GENERATED_IMAGE
    assert (tmp_path / media["cache_path"]).is_file()
    generation = media["generation"]
    assert generation["model"] == "gpt-image-2" and generation["quality"] == "low"
    assert generation["scene_id"] == "scene_03" and generation["fact_ids"] == ["fact_3"]
    assert generation["reason"].startswith("no_accepted_real_media")
    assert generation["usage"] == {"total_tokens": 321}
    assert generation["cost_usd"] is None  # never fabricated
    assert verifier.local_calls, "generated images go through visual verification"
    # Prompt comes from structured semantics, not raw narration.
    prompt = generator.prompts[2]
    assert "wrinkled fingers gripping a wet object" in prompt
    assert "Furchen" not in prompt and "festzuhalten" not in prompt
    assert "no text" in prompt.lower() or "do not include any text" in prompt.lower()
    assert "9:16" in prompt and "captions" in prompt
    summary = state["visual_director"]["summary"]
    assert summary["auto_generated_images"] == 3
    assert state["visual_director"]["policy"]["generated_image_model"] == "gpt-image-2"


def test_real_media_is_searched_before_any_generation(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    generator = FakeGenerator()

    pexels = run(state, tmp_path, photos=[HAND], generator=generator)

    assert pexels.calls, "free providers are searched first"
    assert generator.prompts == []
    assert state["scenes"][0]["media"]["provider_id"] == "hand"


def test_generator_is_only_created_with_key_and_enabled_setting(tmp_path):
    assert get_image_generator(settings_for(tmp_path)) is None
    assert get_image_generator(settings_for(tmp_path, openai_api_key="sk-test", generated_image_fallback_enabled=False)) is None
    assert get_image_generator(settings_for(tmp_path, openai_api_key="sk-test", generated_image_fallback_enabled=False), automatic=False) is not None
    generator = get_image_generator(settings_for(tmp_path, openai_api_key="sk-test"))
    assert isinstance(generator, OpenAIImageGenerator)
    assert "sk-test" not in repr(generator)
    assert generator.model == "gpt-image-2"


def test_openai_generator_requests_low_quality_portrait_png(tmp_path):
    captured = {}

    class Images:
        def generate(self, **kwargs):
            captured.update(kwargs)
            import base64

            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png_bytes()).decode())], usage=None, quality="low", size="1024x1536", output_format="png")

    generator = OpenAIImageGenerator(settings_for(tmp_path, openai_api_key="sk-test"), client=SimpleNamespace(images=Images()))
    image = generator.generate("A photo of wet fingertips", quality="low", size="1024x1536")

    assert captured["model"] == "gpt-image-2"
    assert captured["quality"] == "low" and captured["size"] == "1024x1536" and captured["n"] == 1
    assert image.data.startswith(b"\x89PNG") and image.usage == {}


# ---------------------------------------------------------------------------
# C. Cost limit
# ---------------------------------------------------------------------------

def test_project_budget_stops_automatic_paid_generation(tmp_path):
    scenes = [(role, f"{narration} ({index})", intent) for index, (role, narration, intent) in enumerate(FINGER_SCENES * 2)]
    state = finger_project(scenes)
    generator = FakeGenerator()

    run(state, tmp_path, photos=[BOOK], generator=generator, verifier=Verifier({"book": POOR}))

    assert len(generator.prompts) == 3  # max_auto_generated_images_per_project
    statuses = [scene["visual_director"]["generation"]["status"] for scene in state["scenes"]]
    assert statuses.count("accepted") == 3
    assert statuses.count("project_budget_exhausted") == 3
    for scene in state["scenes"][3:]:
        # Clearly marked, safe non-generated fallback: reuse of accepted media;
        # the closing idea keeps its process steps as an overlay over it
        # instead of becoming a full-screen card.
        assert scene["visual_director"]["decision"] == visual_director.DEGRADED
        assert scene["visual_director"]["resolved_type"] == visual_director.REUSE_PREVIOUS_VISUAL
        if scene["visual_director"]["visual_role"] == "final_payoff":
            assert scene["overlays"][0]["kind"] == "process"
            assert scene["visual_director"]["composition"] == "base_with_overlay"


def test_exhausted_budget_from_earlier_revisions_is_respected(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    state["visual_director"] = {
        "policy": {"max_auto_generated_images_per_project": 2},
        "generations": [{"trigger": "auto", "billed": True, "scene_id": "old", "status": "accepted"}] * 2,
    }
    generator = FakeGenerator()

    run(state, tmp_path, generator=generator)

    assert generator.prompts == []
    assert state["scenes"][0]["visual_director"]["generation"]["status"] == "project_budget_exhausted"


# ---------------------------------------------------------------------------
# D + E. Failures and rejected generations
# ---------------------------------------------------------------------------

def test_generation_failure_is_nonfatal_and_not_retried_per_scene(tmp_path):
    state = finger_project()
    generator = FakeGenerator(error="timeout")

    run(state, tmp_path, generator=generator)

    assert len(generator.prompts) == 1  # provider outage: circuit breaker for this run
    first, *rest = state["scenes"]
    assert first["visual_director"]["generation"]["status"] == "failed"
    assert all(scene["visual_director"]["generation"]["status"] == "provider_timeout" for scene in rest)
    *unillustrated, closing = state["scenes"]
    assert all(scene["asset_status"] == "real_media_unavailable" for scene in unillustrated)
    # The closing idea still gets a free, deterministic graphic (nonfatal).
    assert closing["asset_status"] == "graphic_ready"
    assert state["visual_director"]["generations"][0]["billed"] is False


def test_rejected_generated_image_is_discarded_and_never_retried(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    generator = FakeGenerator()
    verifier = Verifier(generated=POOR)

    run(state, tmp_path, generator=generator, verifier=verifier)
    run(state, tmp_path, generator=generator, verifier=verifier)

    assert len(generator.prompts) == 1  # one attempt per scene, even across runs
    scene = state["scenes"][0]
    assert "media" not in scene
    assert scene["visual_director"]["generation"]["status"] == "scene_attempts_exhausted"
    record = state["visual_director"]["generations"][0]
    assert record["status"] == "rejected" and record["billed"] is True
    assert not list((tmp_path / "project").rglob("*.png"))


def test_accepted_generation_is_reused_without_paying_again(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    generator = FakeGenerator()
    run(state, tmp_path, generator=generator)
    media = state["scenes"][0]["media"]

    # A new revision without the asset (for example after undo) reuses the file.
    again = finger_project(FINGER_SCENES[:1])
    run(again, tmp_path, generator=generator)

    assert len(generator.prompts) == 1
    assert again["scenes"][0]["media"]["cache_path"] == media["cache_path"]
    assert again["visual_director"]["generations"][0]["status"] == "reused_cached"


# ---------------------------------------------------------------------------
# F. Protected reveal
# ---------------------------------------------------------------------------

def protected_project() -> dict:
    scenes = [
        ("hook", "Welches Land hat mehr Inseln?", {
            "visual_goal": "islands of Sweden and Indonesia",
            "objects": ["indonesian islands", "swedish islands"],
            "media_queries": ["swedish islands", "indonesian islands"],
            "media_query_targets": ["subject_a", "subject_b"],
        }),
        ("support", "Schweden hat sehr viele kleine Inseln.", {
            "visual_goal": "swedish archipelago islands",
            "objects": ["swedish archipelago"],
            "media_queries": ["swedish islands"],
            "media_query_targets": ["subject_a"],
        }),
        ("answer", "Indonesien gewinnt knapp.", {
            "visual_goal": "indonesian islands from above",
            "objects": ["indonesian islands"],
            "media_queries": ["indonesian islands"],
            "media_query_targets": ["subject_b"],
        }),
    ]
    state = finger_project(scenes, payoff={
        "reveal_policy": "after_supporting_information",
        "hook_must_not_reveal": "Indonesien",
        "protected_visual_target": "subject_b",
    })
    state["intent"] = {"topic": "Schweden oder Indonesien: wer hat mehr Inseln?", "language": "de", "content_type": "explainer"}
    state["format_plan"] = {"selected_format": "comparison"}
    return state


def test_story_arc_roles_and_reveal_permission():
    arc = visual_director.story_arc(protected_project())
    assert [item["story_role"] for item in arc.values()] == ["hook", "evidence", "primary_answer"]
    assert [item["reveal_allowed"] for item in arc.values()] == [False, False, True]
    assert [item["story_stage"] for item in arc.values()] == ["setup", "setup", "reveal"]


def test_generated_visual_never_reveals_protected_answer_before_reveal(tmp_path):
    state = protected_project()
    generator = FakeGenerator()

    run(state, tmp_path, generator=generator)

    hook, support, answer = state["scenes"]
    assert hook["visual_director"]["reveal_allowed"] is False
    for prompt in generator.prompts[:2]:
        assert "indones" not in prompt.casefold()
    for scene in (hook, support):
        media = scene.get("media") or {}
        assert "indones" not in str(media.get("query", "")).casefold()
        if media.get("source") == "generated_openai":
            assert media["generation"]["reveal_safe"] is True
    assert answer["visual_director"]["reveal_allowed"] is True


def test_reveal_only_subject_before_reveal_is_never_generated(tmp_path):
    state = protected_project()
    state["scenes"][0]["visual_intent"] = {
        "visual_goal": "indonesian islands",
        "objects": ["indonesian islands"],
        "media_queries": ["indonesian islands"],
        "media_query_targets": ["subject_b"],
    }
    state["scenes"][0]["visual_goal"] = "indonesian islands"
    generator = FakeGenerator()

    run(state, tmp_path, generator=generator)

    hook, support, answer = state["scenes"]
    records = {item["scene_id"]: item for item in state["visual_director"]["generations"]}
    # The planner already dropped the protected query; with nothing reveal-safe
    # left, the hook is not generated at all (no paid call).
    assert records[hook["id"]]["status"] == "skipped_no_safe_prompt"
    hook_media = hook.get("media") or {}
    if hook_media.get("source") == "generated_openai":
        # Only a reveal-safe visual from another scene may be reused here.
        assert hook_media["generation"]["reveal_safe"] is True
        assert hook["asset_status"] == "generated_media_reused"
    assert "indones" not in str(hook_media.get("query", "")).casefold()
    assert "indones" not in records[support["id"]]["prompt"].casefold()
    # Once the Story Arc reveals the answer, its visual may be shown.
    assert answer["visual_director"]["reveal_allowed"] is True
    assert len(generator.prompts) == 2


def test_protected_statistic_graphic_is_not_shown_before_reveal():
    state = protected_project()
    state["payoff_plan"]["hook_must_not_reveal"] = "17.000 Inseln"
    scene = state["scenes"][1]
    scene["narration"] = "Eine Seite hat 17.000 Inseln."
    plan = {"protected_entities": [], "primary_subjects": [], "secondary_subjects": []}

    strategy = visual_director.plan_scene_strategy(scene, state, plan)

    assert strategy["reveal_allowed"] is False
    assert strategy["planned_type"] != visual_director.TEXT_NUMBER_VISUAL


# ---------------------------------------------------------------------------
# G. Unseen arbitrary topic
# ---------------------------------------------------------------------------

def test_unseen_topic_needs_no_topic_vocabulary(tmp_path):
    scenes = [("hook", "Warum knistert Kaminholz beim Brennen?", {
        "visual_goal": "burning firewood crackling in a fireplace",
        "objects": ["burning firewood"],
        "context": ["fireplace"],
        "media_queries": ["burning firewood fireplace"],
    })]
    state = finger_project(scenes)
    fire = cand("fire", "Burning firewood crackling in a fireplace", query="burning firewood fireplace")
    office = cand("office", "Modern office building facade", query="burning firewood fireplace")

    run(state, tmp_path, photos=[office, fire], verifier=Verifier({"office": WEAK_PASS}))

    assert state["scenes"][0]["media"]["provider_id"] == "fire"
    source = inspect.getsource(visual_director)
    for topic_word in ("finger", "island", "sweden", "firewood", "wrinkl"):
        assert topic_word not in source.casefold()


# ---------------------------------------------------------------------------
# Strategy types, graphics and motion
# ---------------------------------------------------------------------------

def test_strategy_types_cover_number_comparison_and_process():
    state = finger_project()
    plan = {"protected_entities": [], "primary_subjects": [], "secondary_subjects": []}
    number_scene = {**state["scenes"][1], "narration": "Schweden hat rund 267.570 Inseln."}
    number = visual_director.plan_scene_strategy(number_scene, state, plan)
    assert number["planned_type"] == visual_director.TEXT_NUMBER_VISUAL
    assert number["graphic"] == {"kind": "number", "value": "267.570", "label": "Inseln"}
    assert number["overlay"] == "statistic_callout"

    process_scene = {**state["scenes"][1], "narration": "Nervensignal, Blutgefäße verengen sich, die Haut legt sich in Falten."}
    process_scene["visual_intent"] = {**process_scene["visual_intent"], "visual_strategy": "process"}
    process = visual_director.plan_scene_strategy(process_scene, state, plan)
    assert process["planned_type"] == visual_director.SIMPLE_GRAPHIC
    assert len(process["graphic"]["steps"]) == 3
    # The process is an overlay over a base visual (which may be generated);
    # the full-screen process graphic is the very last resort.
    assert process["overlay_spec"]["kind"] == "process" and process["composition"] == "base_with_overlay"
    assert process["fallback_chain"][-1] == visual_director.SIMPLE_GRAPHIC

    comparison_state = {**state, "format_plan": {"selected_format": "comparison"}}
    comparison_scene = {**state["scenes"][0], "narration": "Sweden or Indonesia?", "visual_intent": {"visual_goal": "islands", "media_queries": ["swedish islands", "indonesian islands"], "media_query_targets": ["subject_a", "subject_b"]}}
    from clipforge.media import build_visual_query_plan

    comparison = visual_director.plan_scene_strategy(comparison_scene, comparison_state, build_visual_query_plan(comparison_scene, comparison_state))
    assert comparison["planned_type"] == visual_director.COMPARISON_VISUAL
    assert {comparison["graphic"]["left"], comparison["graphic"]["right"]} == {"Swedish", "Indonesian"}

    stock = visual_director.plan_scene_strategy(state["scenes"][2], state, plan)
    assert stock["planned_type"] == visual_director.STOCK_PHOTO
    assert stock["story_role"] == "final_payoff"
    assert set(visual_director.VISUAL_TYPES) >= {"stock_video", "stock_photo", "generated_image", "simple_graphic", "text_number_visual", "comparison_visual", "reuse_previous_visual"}


def test_number_scene_without_real_media_gets_deterministic_graphic(tmp_path):
    scenes = [("support", "Schweden hat rund 267.570 Inseln.", {
        "visual_goal": "swedish archipelago", "objects": ["archipelago"], "media_queries": ["swedish archipelago"],
    })]
    state = finger_project(scenes)
    generator = FakeGenerator()

    run(state, tmp_path, generator=generator)

    scene = state["scenes"][0]
    assert scene["media"]["source"] == "simple_graphic"
    assert scene["asset_status"] == "graphic_ready"
    assert scene["visual_director"]["decision"] == visual_director.DEGRADED
    assert generator.prompts == []  # graphics are free; no paid call
    with Image.open(tmp_path / scene["media"]["cache_path"]) as image:
        assert image.size == (1080, 1920)
    assert _scene_media_path(scene, settings_for(tmp_path))[0] is not None


@pytest.mark.parametrize("spec", [
    {"kind": "number", "value": "267.570", "label": "Inseln"},
    {"kind": "comparison", "left": "Schweden", "right": "Indonesien"},
    {"kind": "process", "steps": ["Nervensignal", "Blutgefäße verengen", "Haut faltet sich"]},
])
def test_simple_graphics_render_deterministically(tmp_path, spec):
    first = render_simple_graphic(spec, tmp_path / "a.png", width=1080, height=1920)
    second = render_simple_graphic(spec, tmp_path / "b.png", width=1080, height=1920)
    assert first.read_bytes() == second.read_bytes()
    assert normalise_graphic_spec({"kind": "process", "steps": ["only one"]}) is None
    assert normalise_graphic_spec({"kind": "comparison", "left": "A", "right": "a"}) is None


def test_still_images_get_bounded_deterministic_motion():
    photo = {"media": {"source": "pexels"}}
    graphic = {"media": {"source": "simple_graphic"}}
    types = [still_motion_plan(photo, index, "subtle_pan", 0.6, 90)["type"] for index in range(3)]
    assert types == ["push_in", "pan", "pull_out"]
    assert all(still_motion_plan(photo, index, "subtle_pan", 0.6, 90)["max_zoom"] <= 1.08 for index in range(3))
    assert still_motion_plan(graphic, 1, "subtle_pan", 0.6, 90)["max_zoom"] == 1.03
    assert still_motion_plan(photo, 1, "", 0.6, 90)["type"] == "static"
    # The pan drifts toward the focal side of the frame.
    assert still_motion_plan(photo, 1, "subtle_pan", 0.6, 90)["x"].startswith("(0.2000+0.6000")
    assert still_motion_plan(photo, 1, "subtle_pan", 0.3, 90)["x"].startswith("(0.8000+-0.6000")


# ---------------------------------------------------------------------------
# H. Change Media
# ---------------------------------------------------------------------------

def test_change_media_offers_generation_instead_of_dead_end(tmp_path):
    from clipforge.media_candidates import (
        clear_candidate_sets,
        discover_scene_media_candidates,
        scene_generation_option,
    )

    clear_candidate_sets()
    state = finger_project()
    with_key = settings_for(tmp_path, openai_api_key="sk-test")
    _, candidates = discover_scene_media_candidates(
        state, "project", 3, 1, with_key, client=Provider(photos=[BOOK, BUS]), fallback_client=Provider(),
        visual_verifier=Verifier({"book": POOR, "bus": POOR}), extra_clients=[],
    )
    option = scene_generation_option(state, 3, with_key)

    assert candidates == []
    assert option["available"] is True
    assert option["model_label"] == "GPT Image 2" and option["quality_label"] == "Low"
    assert option["uses_paid_credits"] is True
    assert "wrinkled fingers gripping" in option["prompt"]
    assert "sk-test" not in str(option)
    missing_key = scene_generation_option(state, 3, settings_for(tmp_path))
    assert missing_key["available"] is False and missing_key["unavailable_reason"] == "no_api_key"


def test_manual_generation_uses_shared_path_and_persists_provenance(db, tmp_path):
    from test_export import seed_project

    from clipforge.media_candidates import generate_scene_media
    from clipforge.services import get_project, serialize_project

    settings = settings_for(tmp_path, openai_api_key="sk-test")
    project_id = "22222222-2222-4222-8222-222222222222"
    state = finger_project()
    state.update(render={"status": "complete", "url": None})
    project = seed_project(db, project_id, state)
    generator = FakeGenerator()

    result = generate_scene_media(db, project, 3, settings, prompt=None, generator=generator, visual_verifier=Verifier())

    assert result["status"] == "generated" and result["prompt_source"] == "automatic"
    db.expire_all()
    saved = serialize_project(get_project(db, project_id))["revision"]["state"]
    scene = saved["scenes"][2]
    # Generation adds a new alternative; the current media changes only on Apply.
    alternative = scene["media_alternatives"][0]
    assert alternative["source"] == "generated_openai"
    assert alternative["generation"]["trigger"] == "manual"
    assert saved["visual_director"]["generations"][0]["trigger"] == "manual"
    assert len(generator.prompts) == 1


def test_manual_prompt_edit_cannot_reveal_protected_answer(db, tmp_path):
    from test_export import seed_project

    from clipforge.media_candidates import CandidateError, generate_scene_media

    settings = settings_for(tmp_path, openai_api_key="sk-test")
    state = protected_project()
    state.update(render={"status": "complete", "url": None})
    project = seed_project(db, "33333333-3333-4333-8333-333333333333", state)
    generator = FakeGenerator()

    result = generate_scene_media(db, project, 1, settings, prompt="Aerial photo of Indonesien islands", generator=generator)
    assert result["status"] == "failed" and result["error_code"] == "protected_reveal"
    assert "reveal" in result["message"]
    assert generator.prompts == []
    assert CandidateError  # still exported for route errors


# ---------------------------------------------------------------------------
# I. Old project compatibility
# ---------------------------------------------------------------------------

def test_old_projects_without_director_state_still_load_and_render(tmp_path):
    state = finger_project(FINGER_SCENES[:1])
    for block in state["script"]["blocks"]:
        block.pop("fact_ids")
    cached = tmp_path / "project" / "assets" / "pexels" / "photo-old.jpg"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old")
    legacy_media = {
        "identity": "pexels:photo:old", "provider": "pexels", "provider_id": "old", "kind": "photo",
        "cache_path": "project/assets/pexels/photo-old.jpg", "query": "fingers",
    }
    state["scenes"][0]["media"] = legacy_media
    state["scenes"][0]["asset_status"] = "photo_ready"
    generator = FakeGenerator()

    pexels = run(state, tmp_path, generator=generator)

    assert pexels.calls == [] and generator.prompts == []
    assert state["scenes"][0]["media"] == legacy_media
    assert is_scene_asset_allowed(legacy_media)
    assert state["visual_director"]["policy"]["max_auto_generated_images_per_project"] == 3
    assert state["visual_director"]["policy"]["max_generation_attempts_per_scene"] == 1
    # Missing director state defaults safely.
    assert visual_director.generation_counts({})["generated_images"] == 0
    assert visual_director.scene_story_context({"block_id": "unknown"}, {})["reveal_allowed"] is True


# ---------------------------------------------------------------------------
# J. Existing visual/search invariants
# ---------------------------------------------------------------------------

def test_invariants_hold_with_generation_enabled(tmp_path):
    state = finger_project()
    generator = FakeGenerator()

    run(state, tmp_path, photos=[BOOK], generator=generator, verifier=Verifier({"book": POOR}))

    for scene in state["scenes"]:
        assert scene["media_search"]["logical_queries_executed"] <= MAX_SCENE_QUERY_BUDGET
        assert scene["media_search"]["planned_query_count"] <= MAX_SCENE_QUERY_BUDGET
    # Synthetic cards stay disabled and forged "generated" media is not trusted.
    with pytest.raises(RenderUnavailable, match="cards are disabled"):
        _draw_scene(state, state["scenes"][0], 0, tmp_path)
    forged = {"provider": "generated_openai", "kind": "photo", "cache_path": "x.png", "identity": "generated_openai:photo:x"}
    assert not is_scene_asset_allowed(forged)
    assert not is_scene_asset_allowed({**forged, "source": "generated_openai"})
    assert not is_scene_asset_allowed({"source": "generated_card", "provider": "generated_card", "kind": "photo", "cache_path": "x", "identity": "generated_card:photo:x"})
    assert not is_real_media_allowed(state["scenes"][0]["media"])


def test_thumbnails_consider_generated_images_but_not_graphics(tmp_path):
    from clipforge.thumbnails import _scene_payoff_safe, _source_images

    state = finger_project()
    run(state, tmp_path, generator=FakeGenerator())
    state["scenes"][0]["media"] = visual_director.render_scene_graphic(
        state["scenes"][0], state, {"graphic": {"kind": "number", "value": "42 %", "label": "Wasser"}},
        project_id="project", settings=settings_for(tmp_path),
    )

    sources = _source_images(state, tmp_path / "project", {"primary_subject": "wrinkled fingers"})

    scene_ids = {source["scene_id"] for source in sources}
    assert "scene_02" in scene_ids and "scene_01" not in scene_ids
    protected = {"media": {"generation": {"reveal_safe": False}}}
    assert _scene_payoff_safe(protected, {"protected_information": "Indonesien"}) is False


# ---------------------------------------------------------------------------
# Pixabay (optional provider)
# ---------------------------------------------------------------------------

def test_pixabay_parsing_preserves_license_metadata_and_is_skipped_without_key(tmp_path):
    photos = parse_pixabay_photos({"hits": [{
        "id": 7, "pageURL": "https://pixabay.com/photos/hand-7/", "tags": "hand, water, wrinkled",
        "largeImageURL": "https://cdn.pixabay.test/7.jpg", "webformatURL": "https://cdn.pixabay.test/7w.jpg",
        "imageWidth": 1080, "imageHeight": 1920, "user": "Maker", "user_id": 9,
    }]}, query="wrinkled hand", portrait=True)
    videos = parse_pixabay_videos({"hits": [{
        "id": 8, "pageURL": "https://pixabay.com/videos/water-8/", "tags": "water", "duration": 10,
        "videos": {"medium": {"url": "https://cdn.pixabay.test/8.mp4", "width": 1080, "height": 1920, "thumbnail": "https://cdn.pixabay.test/8.jpg"}},
        "user": "Maker", "user_id": 9,
    }]}, query="water", portrait=True, scene_duration=4)

    assert photos[0].provider == "pixabay" and photos[0].tags == ("hand", "water", "wrinkled")
    assert photos[0].source_url.startswith("https://pixabay.com/")
    assert videos[0].kind == "video" and videos[0].verification_url
    assert is_real_media_allowed(photos[0])
    from clipforge.media import _optional_real_clients

    assert _optional_real_clients(settings_for(tmp_path)) == []
    clients = _optional_real_clients(settings_for(tmp_path, pixabay_api_key="px-test"))
    assert [client.provider for client in clients] == ["pixabay"] and "px-test" not in repr(clients[0])
    clients[0].close()


def test_provider_errors_are_categorised_without_leaking_secrets(tmp_path):
    from openai import OpenAIError

    class NotFoundError(OpenAIError):
        pass

    class Images:
        def generate(self, **_kwargs):
            raise NotFoundError("model gpt-image-2 not found for key sk-secret-123")

    generator = OpenAIImageGenerator(settings_for(tmp_path, openai_api_key="sk-secret-123"), client=SimpleNamespace(images=Images()))
    with pytest.raises(ImageGenerationError) as caught:
        generator.generate("A photo of wet fingertips", quality="low", size="1024x1536")
    assert caught.value.category == "model_unavailable"
    assert "sk-secret" not in str(caught.value) and caught.value.__cause__ is None

    state = finger_project()
    failing = FakeGenerator(error="model_unavailable")
    run(state, tmp_path, generator=failing)
    assert len(failing.prompts) == 1  # unavailable model trips the per-run breaker


# ---------------------------------------------------------------------------
# Story Intelligence V2 integration
#
# States below carry the scene annotations written by
# ``story_arc.annotate_story_roles`` (story_role, story_stage, story_unit_ids,
# is_primary_answer, is_final_payoff) and a ``story_arc`` with its curiosity
# gap.  The Visual Director must read them directly.
# ---------------------------------------------------------------------------

def annotate(state, rows, *, withhold, primary, final):
    """Apply Story Arc annotations: rows = (story_role, story_stage) per scene."""
    for scene, (role, stage) in zip(state["scenes"], rows, strict=True):
        units = [f"fact_{scene['id'][-1]}"]
        scene.update(
            story_role=role, story_stage=stage, story_unit_ids=units,
            is_primary_answer=units[0] == primary, is_final_payoff=units[0] == final,
        )
        scene["visual_intent"].update(story_role=role, story_stage=stage)
    state["story_arc"] = {
        "version": 1,
        "primary_answer_id": primary,
        "final_payoff_id": final,
        "curiosity_gap": {"withhold_answer": withhold},
    }
    return state


def arc_finger_project():
    scenes = [*FINGER_SCENES, ("explanation", "Die Blutgefäße werden enger, die Haut legt sich in Falten.", {
        "visual_goal": "close-up of wrinkled wet fingertip skin",
        "objects": ["wrinkled fingertip skin"],
        "context": ["water droplets"],
        "media_queries": ["wrinkled fingertip skin", "wet fingertips close-up"],
    })]
    state = finger_project(scenes)
    # Explanation topic: answer first, nothing withheld -> stage "open".
    return annotate(
        state,
        [(None, "open"), ("primary_answer", "reveal"), ("secondary_insight", "open"), ("explanation", "open")],
        withhold=False, primary="fact_2", final="fact_2",
    )


def arc_island_project():
    state = protected_project()
    scenes = state["scenes"]
    scenes.append({
        **copy.deepcopy(scenes[1]), "id": "scene_04", "block_id": "voice_block_04", "start": 12, "end": 16,
        "narration": "Dafür sind viele Inseln Schwedens winzig.",
    })
    state["script"]["blocks"].append({"id": "voice_block_04", "role": "support", "text": scenes[3]["narration"], "fact_ids": ["fact_4"]})
    return annotate(
        state,
        [(None, "before_reveal"), ("evidence", "before_reveal"), ("primary_answer", "reveal"), ("secondary_insight", "after_reveal")],
        withhold=True, primary="fact_3", final="fact_4",
    )


def test_story_arc_fields_are_authoritative_over_legacy_inference():
    state = arc_island_project()
    # Legacy inference would call block 2 "evidence" and block 4 "final_payoff";
    # swap the block roles to prove the arc, not block roles, decides.
    state["script"]["blocks"][2]["role"] = "support"
    plan = {"protected_entities": [], "primary_subjects": [], "secondary_subjects": []}
    contexts = [visual_director.scene_story_context(scene, state) for scene in state["scenes"]]

    assert [context["source"] for context in contexts] == ["story_arc"] * 4
    assert [context["story_role"] for context in contexts] == [None, "evidence", "primary_answer", "secondary_insight"]
    assert [context["visual_role"] for context in contexts] == ["hook", "evidence", "primary_answer", "final_payoff"]
    assert [context["reveal_allowed"] for context in contexts] == [False, False, True, True]
    assert contexts[2]["fact_ids"] == ["fact_3"]
    strategy = visual_director.plan_scene_strategy(state["scenes"][3], state, plan)
    # Final payoff and primary answer stay distinct identities.
    assert strategy["story_role"] == "secondary_insight" and strategy["visual_role"] == "final_payoff"
    assert strategy["is_final_payoff"] and not strategy["is_primary_answer"]


def test_wet_fingers_story_arc_reaches_director_and_generation(tmp_path):
    state = arc_finger_project()
    generator = FakeGenerator()

    run(state, tmp_path, photos=[BOOK, BUS, HAND], verifier=Verifier({"book": POOR, "bus": WEAK_PASS}), generator=generator)

    assert state["scenes"][0]["media"]["provider_id"] == "hand"  # free real media first
    assert len(generator.prompts) <= 3
    roles = [scene["visual_director"]["story_role"] for scene in state["scenes"]]
    assert roles == [None, "primary_answer", "secondary_insight", "explanation"]  # the arc's own roles
    assert state["scenes"][0]["visual_director"]["visual_role"] == "hook"
    assert {scene["visual_director"]["story_source"] for scene in state["scenes"]} == {"story_arc"}
    for scene in state["scenes"]:
        assert scene.get("media", {}).get("provider_id") not in {"book", "bus"}
    explanation = state["scenes"][3]
    assert explanation["visual_director"]["decision"] == visual_director.GENERATE_FALLBACK
    assert visual_director.SIMPLE_GRAPHIC in explanation["visual_director"]["fallback_chain"]  # before filler reuse
    prompt = explanation["media"]["generation"]["prompt"].casefold()
    assert "wrinkled" in prompt and ("fingertip" in prompt or "finger" in prompt) and "wet" in prompt
    assert "haut" not in prompt and "falten" not in prompt  # no raw narration
    generation = explanation["media"]["generation"]
    assert generation["ai_generated"] is True and explanation["media"]["ai_generated"] is True
    assert generation["story_role"] == "explanation" and generation["story_stage"] == "open"
    assert generation["fact_ids"] == ["fact_4"] and generation["scene_id"] == "scene_04"
    # Nothing is withheld in an explanation: everything is reveal-safe.
    assert all(scene["media"].get("reveal_safe", True) for scene in state["scenes"] if scene.get("media"))


def test_sweden_indonesia_arc_hides_answer_until_reveal(tmp_path):
    state = arc_island_project()
    generator = FakeGenerator()

    run(state, tmp_path, generator=generator)

    hook, evidence, answer, payoff = state["scenes"]
    records = {item["scene_id"]: item for item in state["visual_director"]["generations"]}
    for scene in (hook, evidence):
        assert scene["visual_director"]["reveal_allowed"] is False
        record = records.get(scene["id"], {})
        assert "indones" not in str(record.get("prompt", "")).casefold()
        media = scene.get("media") or {}
        # Nothing that may depict the answer appears before the reveal.
        assert media.get("reveal_safe", True) is True
        assert media.get("story_role") != "primary_answer"
    # The reveal scene may show the answer; its visual is recorded as unsafe
    # for any earlier scene (reuse, thumbnails).
    assert answer["visual_director"]["reveal_allowed"] is True
    assert "indones" in records[answer["id"]]["prompt"].casefold()
    assert answer["media"]["reveal_safe"] is False
    assert answer["media"]["generation"]["reveal_safe"] is False
    # Primary answer and final payoff stay distinct.
    assert answer["visual_director"]["story_role"] == "primary_answer"
    assert payoff["visual_director"]["story_role"] == "secondary_insight"
    assert payoff["visual_director"]["visual_role"] == "final_payoff" and payoff["visual_director"]["is_final_payoff"]


def test_reveal_visuals_cannot_be_reused_or_used_as_thumbnails_before_reveal():
    from clipforge.media import _reuse_safe
    from clipforge.thumbnails import _scene_payoff_safe

    state = arc_island_project()
    answer_strategy = visual_director.plan_scene_strategy(state["scenes"][2], state, {"protection_scope": "revealed"})
    hook_strategy = visual_director.plan_scene_strategy(state["scenes"][0], state, {})
    # Real media picked at the reveal without active protection is unsafe earlier.
    assert visual_director.visual_reveal_safe(state, answer_strategy, {"protection_scope": "revealed"}) is False
    assert visual_director.visual_reveal_safe(state, hook_strategy, {"protection_scope": "before_reveal"}) is True
    answer_media = {"source": "pexels", "provider": "pexels", "reveal_safe": False, "story_role": "primary_answer"}
    assert not _reuse_safe(answer_media, hook_strategy)
    assert _reuse_safe(answer_media, {**hook_strategy, "reveal_allowed": True, "story_role": "evidence"})
    # A secondary insight stays visually distinct from the primary answer.
    assert not _reuse_safe({**answer_media, "reveal_safe": True}, {"reveal_allowed": True, "story_role": "secondary_insight"})
    assert _scene_payoff_safe({"media": answer_media}, {"protected_information": "x"}) is False


def test_arc_governs_on_screen_text_without_payoff_string_matching():
    state = arc_island_project()
    state["payoff_plan"]["hook_must_not_reveal"] = "17.000 Inseln"
    scene = {**state["scenes"][1], "narration": "Eine Seite hat 17.000 Inseln."}
    plan = {"protected_entities": [], "primary_subjects": [], "secondary_subjects": []}

    strategy = visual_director.plan_scene_strategy(scene, state, plan)

    # The arc keeps the answer out of pre-reveal narration structurally; the
    # graphic only repeats what the scene says, so no text matching applies.
    assert strategy["reveal_allowed"] is False
    assert strategy["planned_type"] == visual_director.TEXT_NUMBER_VISUAL
    # Structural identity still applies: a protected-side term is blocked.
    blocked = visual_director.plan_scene_strategy(scene, state, {**plan, "protected_entities": ["inseln"]})
    assert blocked["planned_type"] != visual_director.TEXT_NUMBER_VISUAL


def test_unseen_topic_with_story_arc_needs_no_vocabulary(tmp_path):
    scenes = [
        ("hook", "Warum knistert Kaminholz beim Brennen?", {
            "visual_goal": "burning firewood crackling in a fireplace", "objects": ["burning firewood"],
            "media_queries": ["burning firewood fireplace"],
        }),
        ("answer", "Wasser im Holz verdampft und sprengt kleine Poren.", {
            "visual_goal": "steam escaping from a burning log", "objects": ["steaming log"],
            "media_queries": ["steam burning log"],
        }),
    ]
    state = annotate(finger_project(scenes), [(None, "open"), ("primary_answer", "reveal")], withhold=False, primary="fact_2", final="fact_2")
    generator = FakeGenerator()
    fire = cand("fire", "Burning firewood crackling in a fireplace", query="burning firewood fireplace")

    run(state, tmp_path, photos=[fire, BUS], verifier=Verifier({"bus": POOR}), generator=generator)

    hook, answer = state["scenes"]
    assert hook["media"]["provider_id"] == "fire"
    assert answer["visual_director"]["story_role"] == "primary_answer"
    assert "steam" in generator.prompts[0].casefold()


@pytest.mark.parametrize("combination", ["no_arc_old_media", "arc_no_director", "director_no_arc", "combined"])
def test_story_and_director_state_combinations_load(tmp_path, combination):
    state = arc_island_project() if combination in {"arc_no_director", "combined"} else protected_project()
    cached = tmp_path / "project" / "assets" / "pexels" / "photo-old.jpg"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"old")
    if combination == "no_arc_old_media":
        state["scenes"][0]["media"] = {
            "identity": "pexels:photo:old", "provider": "pexels", "provider_id": "old", "kind": "photo",
            "cache_path": "project/assets/pexels/photo-old.jpg", "query": "islands",
        }
    if combination in {"director_no_arc", "combined"}:
        # Director state from an earlier build, including a stale model id.
        state["visual_director"] = {"version": 2, "policy": {"generated_image_model": "an-obsolete-model"}, "generations": []}
        for scene in state["scenes"]:
            scene["visual_director"] = {"version": 2, "story_role": "evidence", "decision": "MISSING"}

    run(state, tmp_path, generator=FakeGenerator())

    source = "story_arc" if combination in {"arc_no_director", "combined"} else "legacy_inference"
    for scene in state["scenes"]:
        if "visual_director" not in scene:
            assert scene["media"]["identity"] == "pexels:photo:old"  # cached old media kept as is
            continue
        assert scene["visual_director"]["story_source"] == source
    assert state["visual_director"]["policy"]["generated_image_model"] == "gpt-image-2"
    assert all(item["model"] == "gpt-image-2" for item in state["visual_director"]["generations"] if item.get("billed"))


def test_manual_generation_uses_scene_story_arc(db, tmp_path):
    from test_export import seed_project

    from clipforge.media_candidates import generate_scene_media, scene_generation_option

    settings = settings_for(tmp_path, openai_api_key="sk-test")
    state = arc_island_project()
    state.update(render={"status": "complete", "url": None})
    project = seed_project(db, "44444444-4444-4444-8444-444444444444", state)
    generator = FakeGenerator()

    option = scene_generation_option(state, 2, settings)
    generate_scene_media(db, project, 2, settings, generator=generator, visual_verifier=Verifier())

    assert option["model"] == "gpt-image-2" and option["model_label"] == "GPT Image 2"
    assert "indones" not in generator.prompts[0].casefold()
    assert "swedish" in generator.prompts[0].casefold()
    db.expire_all()
    from clipforge.services import get_project, serialize_project

    scene = serialize_project(get_project(db, project.id))["revision"]["state"]["scenes"][1]
    generation = scene["media_alternatives"][0]["generation"]
    assert generation["trigger"] == "manual" and generation["story_source"] == "story_arc"
    assert generation["story_stage"] == "before_reveal" and generation["reveal_safe"] is True
    assert generation["model"] == "gpt-image-2" and scene["media_alternatives"][0]["ai_generated"] is True


def test_image_size_resolves_to_a_supported_portrait_size():
    from clipforge.image_generation import resolve_image_size

    assert resolve_image_size("gpt-image-2", "1024x1536") == "1024x1536"
    assert resolve_image_size("gpt-image-2", "1088x1920") == "1088x1920"  # 9:16-ish, edges divisible by 16
    assert resolve_image_size("gpt-image-2", "1080x1920") == "1024x1536"  # 1080 not divisible by 16
    assert resolve_image_size("gpt-image-2", "512x4096") == "1024x1536"  # beyond 1:3 / max edge
    assert resolve_image_size("gpt-image-1", "1088x1920") == "1024x1536"  # standard sizes only
    assert resolve_image_size("gpt-image-2", "nonsense") == "1024x1536"
