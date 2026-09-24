"""Lazy, optional local OpenCLIP verification for media candidates."""
from __future__ import annotations

import importlib.util
import io
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

MODEL_NAME = "ViT-B-32"
MODEL_PRETRAINED = "laion2b_s34b_b79k"
VISUAL_THRESHOLD = 0.18
SCENE_VISUAL_THRESHOLD = 0.24
PRESENTATION_RISK_MARGIN = 0.01
MAX_IMAGE_CACHE = 128
MAX_TEXT_CACHE = 64
MAX_VERIFY_VIDEO_BYTES = 40 * 1024 * 1024
THUMBNAIL_VISUAL_SHORTLIST = 5


@dataclass(frozen=True)
class VisualVerification:
    score: float | None
    status: str
    provenance: str | None = None
    frame_scores: tuple[float, ...] = ()
    frame_count: int = 0
    subject_score: float | None = None
    scene_score: float | None = None
    presentation_score: float | None = None
    photographic_score: float | None = None
    diagram_score: float | None = None
    presentation_risk: bool = False
    primary_visual_score: float | None = None
    secondary_visual_score: float | None = None
    context_visual_score: float | None = None
    visual_margin: float | None = None
    confidence: str = "low"


class VisualPromptSet(list[str]):
    def __init__(self, values: list[str], *, subject: list[str], scene: list[str]):
        super().__init__(values)
        self.subject = subject
        self.scene = scene


class UnavailableVisualVerifier:
    status = "unavailable_dependency"
    model_name = MODEL_NAME
    pretrained = MODEL_PRETRAINED
    device = None

    def verify_candidate(self, _candidate: Any, _texts: list[str]) -> VisualVerification:
        return VisualVerification(None, self.status)


def representative_timestamps(duration: float, count: int = 3) -> list[float]:
    if duration <= 0 or count <= 0:
        return []
    return [max(0.05, min(duration - 0.05, duration * fraction)) for fraction in (0.22, 0.50, 0.78)[:count]]


class OpenClipVisualVerifier:
    status = "model_not_prepared"

    def __init__(self, model_name: str = MODEL_NAME, pretrained: str = MODEL_PRETRAINED):
        self.model_name, self.pretrained = model_name, pretrained
        self._model = self._preprocess = self._tokenizer = self._torch = self._device = None
        self._lock = threading.Lock()
        self._image_cache: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._text_cache: OrderedDict[tuple[str, str], Any] = OrderedDict()

    @property
    def device(self) -> str | None:
        return str(self._device) if self._device is not None else None

    @property
    def model_identity(self) -> str:
        return f"{self.model_name}:{self.pretrained}"

    def available(self) -> bool:
        return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("open_clip") is not None

    def _load(self) -> None:
        if self._model is not None:
            return
        if not self.available():
            self.status = "unavailable_dependency"
            raise RuntimeError("OpenCLIP dependencies are not installed")
        try:
            import open_clip
            import torch
        except ImportError as exc:
            self.status = "unavailable_dependency"
            raise RuntimeError("OpenCLIP dependencies are not installed") from exc
        with self._lock:
            if self._model is not None:
                return
            self.status = "model_loading"
            try:
                model, _, preprocess = open_clip.create_model_and_transforms(self.model_name, pretrained=self.pretrained)
                try:
                    device = torch.device("mps") if torch.backends.mps.is_available() and torch.backends.mps.is_built() else torch.device("cpu")
                except (AttributeError, RuntimeError):
                    device = torch.device("cpu")
                try:
                    model = model.to(device)
                except (RuntimeError, AssertionError):
                    device = torch.device("cpu")
                    model = model.to(device)
                model.eval()
                self._model, self._preprocess, self._tokenizer, self._torch, self._device = model, preprocess, open_clip.get_tokenizer(self.model_name), torch, device
                self.status = "available"
            except Exception:
                self.status = "model_error"
                raise

    def prepare_model(self) -> dict[str, Any]:
        self._load()
        return {"status": "ready", "model": self.model_identity, "device": self.device}

    @staticmethod
    def _put(cache: OrderedDict, key: Any, value: Any, limit: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    def _image_embedding(self, image: Image.Image, identity: str) -> Any:
        self._load()
        key = (self.model_identity, identity)
        if key in self._image_cache:
            self._image_cache.move_to_end(key)
            return self._image_cache[key]
        tensor = self._preprocess(image.convert("RGB")).unsqueeze(0).to(self._device)
        with self._torch.inference_mode():
            vector = self._model.encode_image(tensor)
            vector = vector / vector.norm(dim=-1, keepdim=True)
        vector = vector.detach().cpu()
        self._put(self._image_cache, key, vector, MAX_IMAGE_CACHE)
        return vector

    def _text_embeddings(self, texts: Iterable[str]) -> list[Any]:
        self._load()
        values = list(dict.fromkeys(str(text).strip() for text in texts if str(text).strip()))
        result, missing = [], []
        for text in values:
            key = (self.model_identity, text.casefold())
            if key in self._text_cache:
                self._text_cache.move_to_end(key)
                result.append(self._text_cache[key])
            else:
                missing.append(text)
        if missing:
            with self._torch.inference_mode():
                encoded = self._model.encode_text(self._tokenizer(missing).to(self._device))
                encoded = encoded / encoded.norm(dim=-1, keepdim=True)
            for text, vector in zip(missing, encoded.detach().cpu(), strict=True):
                vector = vector.unsqueeze(0)
                self._put(self._text_cache, (self.model_identity, text.casefold()), vector, MAX_TEXT_CACHE)
                result.append(vector)
        return result

    def score_image(self, image: Image.Image, texts: list[str], *, asset_identity: str = "inline") -> float:
        image_vector = self._image_embedding(image, asset_identity)
        vectors = self._text_embeddings(texts)
        if not vectors:
            return 0.0
        scores = sorted(((image_vector @ vector.T).item() for vector in vectors), reverse=True)
        return float(sum(scores[:2]) / min(2, len(scores)))

    def _score_prompt_groups(self, image: Image.Image, texts: list[str], *, asset_identity: str) -> tuple[float, float, float]:
        subject_texts = list(getattr(texts, "subject", ()))
        scene_texts = list(getattr(texts, "scene", ())) or list(texts)
        subject_score = self.score_image(image, subject_texts or scene_texts, asset_identity=f"{asset_identity}:subject")
        scene_score = self.score_image(image, scene_texts, asset_identity=f"{asset_identity}:scene")
        combined = scene_score if not subject_texts else (scene_score * 0.85) + (subject_score * 0.15)
        return subject_score, scene_score, combined

    def _presentation_scores(
        self, image: Image.Image, *, asset_identity: str
    ) -> tuple[float, float, float, bool]:
        presentation_score = self.score_image(
            image,
            [
                "a flashcard with large printed text",
                "a screenshot of a document or presentation slide",
                "a text-heavy infographic or social media quote card",
            ],
            asset_identity=f"{asset_identity}:presentation",
        )
        photographic_score = self.score_image(
            image,
            [
                "a natural photograph or real-world video frame",
                "ordinary photographic footage without large text overlays",
            ],
            asset_identity=f"{asset_identity}:photographic",
        )
        diagram_score = self.score_image(
            image,
            [
                "a useful scientific diagram explaining a physical mechanism",
                "a clear data visualization that directly explains the subject",
            ],
            asset_identity=f"{asset_identity}:diagram",
        )
        risk = presentation_score >= max(photographic_score, diagram_score) + PRESENTATION_RISK_MARGIN
        return presentation_score, photographic_score, diagram_score, risk

    def score_video_frames(self, frames: list[Image.Image], texts: list[str], *, asset_identity: str = "video") -> VisualVerification:
        scores = []
        subject_scores = []
        scene_scores = []
        presentation_scores = []
        photographic_scores = []
        diagram_scores = []
        presentation_risks = []
        for i, frame in enumerate(frames):
            identity = f"{asset_identity}:frame:{i}"
            subject_score, scene_score, combined = self._score_prompt_groups(
                frame, texts, asset_identity=identity
            )
            presentation_score, photographic_score, diagram_score, presentation_risk = (
                self._presentation_scores(frame, asset_identity=identity)
            )
            subject_scores.append(subject_score)
            scene_scores.append(scene_score)
            presentation_scores.append(presentation_score)
            photographic_scores.append(photographic_score)
            diagram_scores.append(diagram_score)
            presentation_risks.append(presentation_risk)
            scores.append(combined)
        return (
            VisualVerification(
                float(statistics.median(scores)),
                "verified",
                "local_video_frames",
                tuple(scores),
                len(scores),
                float(statistics.median(subject_scores)),
                float(statistics.median(scene_scores)),
                float(statistics.median(presentation_scores)),
                float(statistics.median(photographic_scores)),
                float(statistics.median(diagram_scores)),
                sum(presentation_risks) > len(presentation_risks) / 2,
            )
            if scores
            else VisualVerification(None, "unavailable_frames")
        )

    @staticmethod
    def _probe_duration(path: Path) -> float:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return 3.0
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=10, check=False)
        try:
            match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
            if match:
                hours, minutes, seconds = match.groups()
                return max(1.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        except (ValueError, AttributeError):
            pass
        return 3.0

    def _extract_frames(self, path: Path) -> list[Image.Image]:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return []
        frames = []
        for timestamp in representative_timestamps(self._probe_duration(path)):
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(timestamp), "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"], capture_output=True, timeout=15, check=False)
            if result.returncode == 0 and result.stdout:
                try:
                    frames.append(Image.open(io.BytesIO(result.stdout)).convert("RGB"))
                except (OSError, ValueError):
                    pass
        return frames

    def score_video_file(self, path: Path, texts: list[str], *, asset_identity: str | None = None) -> VisualVerification:
        return self.score_video_frames(self._extract_frames(path), texts, asset_identity=asset_identity or str(path))

    def verify_thumbnail_image(
        self,
        path: Path,
        *,
        primary_texts: list[str],
        secondary_texts: list[str] | None = None,
        context_texts: list[str] | None = None,
        asset_identity: str | None = None,
    ) -> VisualVerification:
        """Compare one thumbnail image with subject and context concepts."""
        try:
            image = Image.open(path).convert("RGB")
            identity = asset_identity or str(path)
            primary = self.score_image(image, primary_texts, asset_identity=f"{identity}:primary") if primary_texts else None
            secondary = self.score_image(image, secondary_texts or [], asset_identity=f"{identity}:secondary") if secondary_texts else None
            context = self.score_image(image, context_texts or [], asset_identity=f"{identity}:context") if context_texts else None
            subject_scores = [score for score in (primary, secondary) if score is not None]
            subject = max(subject_scores, default=None)
            margin = round(subject - context, 6) if subject is not None and context is not None else None
            confidence = "high" if margin is not None and margin >= 0.08 else "medium" if margin is not None and margin >= 0.05 else "low"
            return VisualVerification(
                subject,
                "verified",
                "local_thumbnail",
                subject_score=subject,
                scene_score=subject,
                primary_visual_score=primary,
                secondary_visual_score=secondary,
                context_visual_score=context,
                visual_margin=margin,
                confidence=confidence,
            )
        except (OSError, ValueError, RuntimeError, TypeError):
            return VisualVerification(None, "unavailable_image", "local_thumbnail")

    def verify_candidate(self, candidate: Any, texts: list[str]) -> VisualVerification:
        if str(getattr(candidate, "kind", "photo")) == "video" and getattr(candidate, "verification_url", ""):
            try:
                with httpx.Client(timeout=12.0, follow_redirects=True) as client, tempfile.TemporaryDirectory(prefix="clipforge-verify-") as temp:
                    path = Path(temp) / "preview.mp4"
                    with client.stream("GET", str(candidate.verification_url)) as response:
                        response.raise_for_status()
                        total = 0
                        with path.open("wb") as handle:
                            for chunk in response.iter_bytes(262144):
                                total += len(chunk)
                                if total > MAX_VERIFY_VIDEO_BYTES:
                                    raise ValueError("verification video too large")
                                handle.write(chunk)
                    result = self.score_video_file(path, texts, asset_identity=str(getattr(candidate, "identity", "video")))
                    if result.frame_count:
                        return VisualVerification(
                            result.score,
                            "verified",
                            "provider_video_frames",
                            result.frame_scores,
                            result.frame_count,
                            result.subject_score,
                            result.scene_score,
                            result.presentation_score,
                            result.photographic_score,
                            result.diagram_score,
                            result.presentation_risk,
                        )
            except (OSError, ValueError, RuntimeError, httpx.HTTPError, subprocess.SubprocessError):
                pass
        preview = str(getattr(candidate, "preview_url", "") or "")
        if not preview:
            return VisualVerification(None, "unavailable_preview")
        try:
            response = httpx.get(preview, timeout=8.0, follow_redirects=True)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            subject_score, scene_score, combined = self._score_prompt_groups(
                image, texts, asset_identity=str(getattr(candidate, "identity", "image"))
            )
            presentation_score, photographic_score, diagram_score, presentation_risk = (
                self._presentation_scores(
                    image, asset_identity=str(getattr(candidate, "identity", "image"))
                )
            )
            return VisualVerification(
                combined,
                "verified",
                "provider_thumbnail",
                (),
                1,
                subject_score,
                scene_score,
                presentation_score,
                photographic_score,
                diagram_score,
                presentation_risk,
            )
        except (OSError, ValueError, RuntimeError, ImportError, httpx.HTTPError):
            return VisualVerification(None, "unavailable_preview")


_VERIFIER: OpenClipVisualVerifier | UnavailableVisualVerifier | None = None


def get_visual_verifier() -> OpenClipVisualVerifier | UnavailableVisualVerifier:
    global _VERIFIER
    if _VERIFIER is None:
        verifier = OpenClipVisualVerifier()
        _VERIFIER = verifier if verifier.available() else UnavailableVisualVerifier()
    return _VERIFIER


def visual_runtime_status() -> dict[str, Any]:
    verifier = get_visual_verifier()
    return {"status": verifier.status, "model": verifier.model_name, "pretrained": verifier.pretrained, "device": verifier.device}


def _subject_tokens(value: str) -> list[str]:
    """Generic fallback: the first content words of a topic string (function words removed)."""
    stop = {
        "why", "how", "what", "when", "where", "which", "does", "do", "did", "are", "is",
        "the", "a", "an", "der", "die", "das", "ein", "eine", "haben", "hat", "warum",
        "wieso", "wie", "sind", "werden", "wird", "kann", "können", "sich", "zu", "von", "im",
        "man", "sieht", "sehen", "seine", "seinen", "seinem", "seiner",
        "welche", "welcher", "welches", "mehr", "oder", "und", "als", "was", "wer",
        "for", "with", "about", "and", "or", "to", "in", "on", "more", "than",
        "write", "fictional", "story", "tell", "explain", "question",
    }
    tokens = [
        token
        for token in re.findall(r"[\wäöüß-]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 2 and token not in stop
    ]
    return tokens[:3]


def global_subject_text(state: dict[str, Any] | None = None) -> str:
    """Project subject for context and OpenCLIP subject prompts.

    Prefers the concepts the canonical visual plan repeats across its
    provider-facing queries (language-independent); the topic string is only
    a fallback for projects without a plan.
    """
    if state and state.get("scenes"):
        from .media import canonical_visual_subjects  # local import: media imports this module

        concepts = canonical_visual_subjects(state)["concepts"]
        if concepts:
            return " ".join(concepts[:3])
    topic = str((state or {}).get("intent", {}).get("topic") or "").strip()
    return " ".join(_subject_tokens(topic))


def visual_intent_text(scene: dict[str, Any], state: dict[str, Any] | None = None) -> list[str]:
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    goal = str(intent.get("visual_goal") or scene.get("visual_goal") or "").strip()
    objects = [str(v).strip() for v in intent.get("objects", []) if str(v).strip()]
    actions = [str(v).strip() for v in intent.get("actions", []) if str(v).strip()]
    context = [str(v).strip() for v in intent.get("context", []) if str(v).strip()]
    narration = str(scene.get("narration") or "").strip()
    provider_queries = [
        str(value).strip()
        for value in (scene.get("search_queries") or intent.get("media_queries") or [])
        if str(value).strip()
    ]
    # The plan's shared visual concept is canonical and provider-facing; the
    # topic-string heuristic is only a fallback for scenes without a plan.
    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    planned_subject = next((str(value) for value in plan.get("primary_subjects") or [] if str(value).strip()), "")
    subject_topic = planned_subject or global_subject_text(state)
    prompts = [narration] if narration else []
    prompts.extend(provider_queries[:1])
    if objects or actions or context:
        subject = " ".join([*actions, *objects]).strip() or "visual subject"
        prompts.append(
            f"a photo of {subject}"
            + (f" in {' '.join(context)}" if context else "")
        )
        prompts.append(" ".join([*actions, *objects, *context]).strip())
    if goal:
        prompts.append(goal)
    scene_prompts = list(dict.fromkeys(v for v in prompts if v))
    if state is None:
        return scene_prompts[:4] or ["a relevant visual scene"]
    scene_prompts = scene_prompts[:3] or ["a relevant visual scene"]
    subject_prompts = [f"a photo of {subject_topic}"] if subject_topic else []
    return VisualPromptSet(
        list(dict.fromkeys([*scene_prompts, *subject_prompts])),
        subject=subject_prompts,
        scene=scene_prompts,
    )
