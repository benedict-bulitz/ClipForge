from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .config import Settings


@dataclass(frozen=True)
class AlignmentResult:
    words: list[dict[str, Any]]
    status: str
    provider: str
    diagnostic: str | None = None


class WordAligner(Protocol):
    name: str

    def align(self, audio: Path, language: str) -> list[dict[str, Any]]: ...


class FasterWhisperAligner:
    """Free local aligner. The lightweight model is loaded once and cached on disk."""

    name = "faster_whisper"

    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def align(self, audio: Path, language: str) -> list[dict[str, Any]]:
        segments, _info = self.model.transcribe(
            str(audio), language=language, word_timestamps=True, vad_filter=True
        )
        words = []
        for segment in segments:
            for word in segment.words or []:
                if word.start is not None and word.end is not None and word.word.strip():
                    words.append(
                        {
                            "text": word.word.strip(),
                            "start": round(float(word.start), 3),
                            "end": round(float(word.end), 3),
                        }
                    )
        return words


@dataclass(frozen=True)
class AlignmentReadiness:
    ready: bool
    status: str
    dependency_installed: bool
    model_cached: bool


def alignment_readiness(settings: Settings) -> AlignmentReadiness:
    if settings.caption_alignment_provider not in {"auto", "faster_whisper"}:
        return AlignmentReadiness(False, "Word alignment disabled", False, False)
    installed = importlib.util.find_spec("faster_whisper") is not None
    if not installed:
        return AlignmentReadiness(False, "Alignment dependency missing", False, False)
    model = settings.caption_alignment_model
    model_path = Path(model).expanduser()
    looks_like_path = model_path.is_absolute() or model.startswith((".", "~"))
    if looks_like_path and not model_path.is_dir():
        return AlignmentReadiness(False, "Alignment model unavailable", True, False)
    if model_path.is_dir() and (model_path / "model.bin").exists():
        cached = True
    else:
        cache_root = Path(
            os.environ.get("HF_HUB_CACHE")
            or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        )
        snapshots = cache_root / f"models--Systran--faster-whisper-{model}" / "snapshots"
        cached = snapshots.exists() and any(snapshots.glob("*/model.bin"))
    return AlignmentReadiness(
        cached,
        "Word alignment ready" if cached else "Preparing local alignment",
        True,
        cached,
    )


@lru_cache(maxsize=2)
def _local_aligner(model_name: str) -> FasterWhisperAligner:
    return FasterWhisperAligner(model_name)


def align_narration(
    audio: Path,
    script: str,
    duration: float,
    language: str,
    settings: Settings,
    *,
    aligner: WordAligner | None = None,
) -> AlignmentResult:
    selected = aligner
    load_diagnostic: str | None = None
    if selected is None and settings.caption_alignment_provider in {"auto", "faster_whisper"}:
        try:
            selected = _local_aligner(settings.caption_alignment_model)
        except ImportError:
            load_diagnostic = "Alignment dependency missing; phrase timing fallback is in use."
        # Third-party model loading can fail with provider-specific cache and HTTP exception types.
        except Exception:  # noqa: BLE001
            selected = None
            load_diagnostic = "Alignment model unavailable; phrase timing fallback is in use."
    if selected is not None:
        try:
            words = selected.align(audio, language)
            if len(words) >= max(2, len(script.split()) // 2):
                return AlignmentResult(words, "word_aligned", selected.name)
            diagnostic = "Alignment failed — phrase timing fallback used."
        except (OSError, RuntimeError, ValueError):
            diagnostic = "Alignment failed — phrase timing fallback used."
    else:
        diagnostic = load_diagnostic or "Local word alignment is disabled; phrase timing fallback is in use."
    return AlignmentResult([], "phrase_fallback", "estimated_segments", diagnostic)


def group_aligned_words(words: list[dict[str, Any]], words_per_group: int) -> list[dict[str, Any]]:
    size = max(2, min(8, words_per_group))
    items = []
    for index in range(0, len(words), size):
        group = words[index : index + size]
        if not group:
            continue
        items.append(
            {
                "text": " ".join(str(word["text"]) for word in group),
                "start": group[0]["start"],
                "end": group[-1]["end"],
                "words": group,
                "timing": "word_aligned",
            }
        )
    return items


def phrase_fallback_items(script: str, duration: float, words_per_group: int) -> list[dict[str, Any]]:
    words = script.split()
    size = max(3, min(8, words_per_group))
    groups = [words[index : index + size] for index in range(0, len(words), size)]
    if not groups:
        return []
    total_words = max(1, len(words))
    cursor = 0
    items = []
    for group in groups:
        start = duration * cursor / total_words
        cursor += len(group)
        end = duration * cursor / total_words
        items.append(
            {
                "text": " ".join(group),
                "start": round(start, 2),
                "end": round(end, 2),
                "timing": "phrase_estimate",
            }
        )
    return items
