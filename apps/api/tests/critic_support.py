"""Deterministic support for Final Video Critic tests.

Nothing here is mocked at the ffmpeg level: providers "download" real images,
``render_video`` composes real MP4s with the bundled ffmpeg and the critic
extracts real frames.  Only paid/remote/heavy pieces are replaced:

* narration: a silent WAV of the planned length (no TTS),
* OpenCLIP: ``PixelVerifier`` reads the actual pixels.  Every test image is
  painted in a concept colour, and a frame "matches" a text when the colours
  of the concepts named by that text dominate it.  Overlays, crops and motion
  therefore change the score exactly as they change what a viewer sees.
* the image API: a fake generator painting a concept.

Concept keywords are test fixtures only; production code has no vocabulary.
"""
from __future__ import annotations

import io
import random
import subprocess
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw
from test_staged_media_search import Commons
from test_staged_media_search import cand as staged_cand
from test_visual_director import settings_for

from clipforge import renderer
from clipforge.image_generation import GeneratedImage, ImageGenerationError
from clipforge.media import prepare_project_media
from clipforge.renderer import ffmpeg_path, render_video
from clipforge.services import _apply_render_result
from clipforge.visual_verifier import UnavailableVisualVerifier, VisualVerification

WIDTH, HEIGHT = 360, 640
STRONG = (0.31, 0.31)
POOR = (0.12, 0.12)

# concept -> (colour, keywords that name it in visual-intent texts)
CONCEPTS: dict[str, tuple[tuple[int, int, int], tuple[str, ...]]] = {
    "hand": ((222, 170, 138), ("finger", "fingertip", "hand", "skin")),
    "book": ((236, 228, 200), ("book", "page")),
    "bus": ((212, 196, 40), ("bus stop",)),
    "city": ((52, 58, 110), ("city", "skyline")),
    "sweden": ((40, 110, 190), ("swed", "schwed")),
    "indonesia": ((40, 160, 80), ("indones",)),
    "glacier": ((170, 200, 215), ("glacier", "coastline")),
    "octopus": ((190, 60, 150), ("octopus", "tentacle", "krake")),
    "heart": ((200, 30, 40), ("heart", "blood")),
    "graffiti": ((250, 120, 20), ("graffiti", "bridge")),
    "wall": ((128, 128, 128), ()),
}


def _close(pixel: tuple[int, int, int], colour: tuple[int, int, int]) -> bool:
    return sum((a - b) ** 2 for a, b in zip(pixel, colour, strict=True)) < 45 ** 2


def concept_fractions(image: Image.Image) -> dict[str, float]:
    raw = image.convert("RGB").resize((18, 32)).tobytes()
    pixels = [tuple(raw[index:index + 3]) for index in range(0, len(raw), 3)]
    return {
        name: sum(1 for pixel in pixels if _close(pixel, colour)) / len(pixels)
        for name, (colour, _words) in CONCEPTS.items()
    }


def pixel_score(image: Image.Image, texts) -> float:
    joined = " ".join(str(text) for text in texts).casefold()
    fractions = concept_fractions(image)
    matched = sum(fraction for name, fraction in fractions.items() if any(word in joined for word in CONCEPTS[name][1]))
    return round(0.12 + 0.24 * min(1.0, matched * 2), 4)


TEXT_PANEL_FRACTION = 0.04


def text_heavy(image: Image.Image) -> bool:
    """Printed pages, and large dark translucent text panels (overlay pills), read as text."""
    raw = image.convert("RGB").resize((72, 128)).tobytes()
    pixels = [tuple(raw[index:index + 3]) for index in range(0, len(raw), 3)]
    panel = sum(1 for pixel in pixels if max(pixel) - min(pixel) < 40 and sum(pixel) / 3 < 110) / len(pixels)
    return concept_fractions(image)["book"] > 0.4 or panel >= TEXT_PANEL_FRACTION


class PixelVerifier:
    """Stand-in for OpenCLIP that judges real pixels (frames, crops, files)."""

    status = "available"
    model_identity = "pixel-test"

    def __init__(self, candidate_scores: dict[str, tuple[float, float]] | None = None, concepts: dict[str, str] | None = None):
        self.candidate_scores = candidate_scores or {}
        self.concepts = concepts if concepts is not None else {}
        self.frame_calls = 0

    def score_image(self, image, texts, *, asset_identity="inline"):
        return pixel_score(image, texts)

    def score_video_frames(self, frames, texts, *, asset_identity="video"):
        self.frame_calls += 1
        scores = [pixel_score(frame, texts) for frame in frames]
        if not scores:
            return VisualVerification(None, "unavailable_frames")
        middle = sorted(scores)[len(scores) // 2]
        risk = sum(text_heavy(frame) for frame in frames) > len(frames) / 2
        return VisualVerification(middle, "verified", "local_video_frames", tuple(scores), len(scores), middle, middle, 0.1, 0.3, 0.1, risk)

    def verify_local_image(self, path, texts, *, asset_identity=None):
        with Image.open(path) as image:
            score = pixel_score(image, texts)
        return VisualVerification(score, "verified", "local_image", (), 1, score, score, 0.1, 0.3, 0.1, False)

    def verify_candidate(self, candidate, texts):
        if candidate.provider_id in self.candidate_scores:
            score, scene = self.candidate_scores[candidate.provider_id]
            return VisualVerification(score, "verified", subject_score=score, scene_score=scene)
        concept = _concept(self.concepts, candidate.provider_id)
        joined = " ".join(str(text) for text in texts).casefold()
        score, scene = STRONG if any(word in joined for word in CONCEPTS[concept][1]) else POOR
        return VisualVerification(score, "verified", subject_score=score, scene_score=scene)


def _concept(concepts, provider_id: str) -> str:
    """Concept of a candidate; a defaultdict supplies one for unknown ids."""
    return concepts[provider_id] if provider_id in concepts or hasattr(concepts, "default_factory") else "wall"


def paint(concept: str, size: tuple[int, int] = (1080, 1920), *, region: tuple[float, float] | None = None, seed: int = 7) -> Image.Image:
    """A textured image of one concept (optionally only in a horizontal region, on a wall)."""
    rng = random.Random(f"{concept}:{seed}")
    colour = CONCEPTS[concept][0]
    base = CONCEPTS["wall"][0] if region else colour
    width, height = size
    tile = Image.new("RGB", (max(1, width // 10), max(1, height // 10)))
    pixels = []
    for y in range(tile.height):
        for x in range(tile.width):
            inside = region is None or region[0] <= x / tile.width < region[1]
            source = colour if inside else base
            jitter = rng.randint(-18, 18)
            pixels.append(tuple(max(0, min(255, channel + jitter)) for channel in source))
    tile.putdata(pixels)
    return tile.resize(size, Image.Resampling.NEAREST)


def image_bytes(image: Image.Image, fmt: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def photo(provider_id: str, query: str, title: str) -> object:
    return staged_cand(provider_id, query, title, kind="photo")


class ImageProvider:
    """Photo search keyed by query; downloads write real images of each candidate's concept."""

    def __init__(self, photos: dict[str, list] | None = None, concepts: dict[str, str] | None = None, regions: dict[str, tuple[float, float]] | None = None, sizes: dict[str, tuple[int, int]] | None = None):
        self.photos = photos or {}
        self.concepts = concepts if concepts is not None else {}
        self.regions = regions or {}
        self.sizes = sizes or {}
        self.calls: list[tuple[str, str]] = []

    def search_photos(self, query, *, portrait):
        self.calls.append(("photo", query))
        return [replace(item, query=query) for item in self.photos.get(query, [])]

    def search_videos(self, query, *, portrait, scene_duration):
        self.calls.append(("video", query))
        return []

    def download(self, candidate, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        concept = _concept(self.concepts, candidate.provider_id)
        paint(concept, self.sizes.get(candidate.provider_id, (1080, 1920)), region=self.regions.get(candidate.provider_id)).save(destination, format="JPEG")
        return destination

    def close(self):
        return None


class PaintingGenerator:
    """Fake image API: paints one concept; counts calls (no network, no credits)."""

    model = "gpt-image-2"

    def __init__(self, concept: str = "hand", *, error: str | None = None):
        self.concept = concept
        self.error = error
        self.prompts: list[str] = []

    def generate(self, prompt, *, quality, size):
        self.prompts.append(prompt)
        if self.error:
            raise ImageGenerationError(self.error, "mock failure")
        return GeneratedImage(image_bytes(paint(self.concept, (1024, 1536)), "PNG"), self.model, quality, size, "png", {"total_tokens": 100})


def critic_settings(tmp_path: Path, **overrides):
    return settings_for(tmp_path, **overrides)


def small_timeline(state: dict) -> dict:
    state["timeline"].update(width=WIDTH, height=HEIGHT)
    state["captions"]["font_size"] = 26
    return state


def silent_voice(monkeypatch) -> None:
    """Narration of the planned length without any TTS provider."""

    def fake_voice(state, project_id, temp, settings, *, progress=None):
        seconds = float(state["duration"].get("estimated_seconds") or 8)
        path = temp / "voice.wav"
        subprocess.run(
            [ffmpeg_path(), "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{seconds:.2f}", str(path)],
            check=True, capture_output=True,
        )
        state["voice"].update(provider="test", cached=False)
        return path, "test"

    monkeypatch.setattr(renderer, "_cached_voice", fake_voice)


def media_pass(state, tmp_path, provider, *, verifier=None, generator=None, settings=None):
    prepare_project_media(
        state, "project", settings or critic_settings(tmp_path), client=provider, fallback_client=Commons(),
        visual_verifier=verifier if verifier is not None else UnavailableVisualVerifier(),
        image_generator=generator, extra_clients=[],
    )
    return state


def render(state, tmp_path, *, revision: int = 2, settings=None):
    result = render_video(state, "project", revision, settings or critic_settings(tmp_path))
    _apply_render_result(state, result, revision)
    return result


class Harness:
    """Media pass + real render + critic, with the repair callables wired like services."""

    def __init__(self, tmp_path, provider, *, verifier=None, generator=None, settings=None, revision: int = 2):
        self.tmp_path = tmp_path
        self.provider = provider
        self.verifier = verifier or PixelVerifier(concepts=provider.concepts)
        self.generator = generator
        self.settings = settings or critic_settings(tmp_path)
        self.revision = revision
        self.renders = 0
        self.media_passes = 0

    def prepare(self, state):
        self.media_passes += 1
        return media_pass(state, self.tmp_path, self.provider, verifier=self.verifier, generator=self.generator, settings=self.settings)

    def rerender(self, state):
        self.renders += 1
        return render(state, self.tmp_path, revision=self.revision, settings=self.settings)

    def review(self, state, **kwargs):
        from clipforge.final_critic import run_final_quality_review

        return run_final_quality_review(
            state, project_id="project", revision=self.revision, settings=self.settings,
            rerender=self.rerender, prepare_media=self.prepare, verifier=self.verifier, generator=self.generator, **kwargs,
        )


def frame_at(video: Path, timestamp: float) -> Image.Image:
    completed = subprocess.run(
        [ffmpeg_path(), "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True, check=True,
    )
    return Image.open(io.BytesIO(completed.stdout)).convert("RGB")


def draw_circle_image(size=(1600, 1200)) -> Image.Image:
    image = paint("wall", size)
    draw = ImageDraw.Draw(image)
    cx, cy, radius = size[0] // 2, size[1] // 2, 300
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(10, 10, 10))
    return image
