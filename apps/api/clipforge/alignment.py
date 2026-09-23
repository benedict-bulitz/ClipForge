from __future__ import annotations

import importlib.util
import os
import re
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


_SENTENCE_ENDING = re.compile(r"[.!?…]+(?:[\"'”’\)\]]*)$")
_PUNCTUATION_ONLY = re.compile(r"^[.!?…]+(?:[\"'”’\)\]]*)$")


def _spoken_key(text: str) -> str:
    """Comparison key for matching aligned tokens to the canonical narration."""
    return "".join(character for character in text.casefold() if character.isalnum())


def _source_words(script: str) -> list[dict[str, str]]:
    """Return canonical spoken words and the sentence-ending suffix on each word."""
    source: list[dict[str, str]] = []
    for token in re.findall(r"\S+", script):
        if _PUNCTUATION_ONLY.fullmatch(token):
            if source:
                source[-1]["ending"] += token
            continue
        key = _spoken_key(token)
        if not key:
            continue
        ending = _SENTENCE_ENDING.search(token)
        source.append({"key": key, "ending": ending.group(0) if ending else ""})
    return source


def _normalise_aligned_words(
    words: list[dict[str, Any]], script: str | None = None
) -> list[dict[str, Any]]:
    """Keep punctuation on spoken words and restore sentence ends lost by alignment.

    Some aligners emit punctuation as an independent token and others omit it.
    Punctuation never has its own spoken timing, so a standalone token is folded
    into the preceding aligned word without changing that word's timestamps.
    """
    normalised: list[dict[str, Any]] = []
    for word in words:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        if _PUNCTUATION_ONLY.fullmatch(text):
            if normalised:
                normalised[-1]["text"] = f"{normalised[-1]['text']}{text}"
            continue
        copy = dict(word)
        copy["text"] = text
        normalised.append(copy)

    if not script:
        return normalised
    source = _source_words(script)
    source_index = 0
    for word in normalised:
        key = _spoken_key(str(word["text"]))
        if not key:
            continue
        while source_index < len(source) and source[source_index]["key"] != key:
            source_index += 1
        if source_index == len(source):
            break
        ending = source[source_index]["ending"]
        if ending and not _SENTENCE_ENDING.search(str(word["text"])):
            word["text"] = f"{word['text']}{ending}"
        source_index += 1
    return normalised


def _sentence_aware_groups(words: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    group: list[dict[str, Any]] = []
    for word in words:
        group.append(word)
        if len(group) >= size or _SENTENCE_ENDING.search(str(word["text"])):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    return groups


def group_aligned_words(
    words: list[dict[str, Any]], words_per_group: int, script: str | None = None
) -> list[dict[str, Any]]:
    size = max(2, min(8, words_per_group))
    items = []
    for group in _sentence_aware_groups(_normalise_aligned_words(words, script), size):
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
    words = _normalise_aligned_words(
        [{"text": word} for word in re.findall(r"\S+", script)], script
    )
    size = max(2, min(8, words_per_group))
    groups = _sentence_aware_groups(words, size)
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
                "text": " ".join(str(word["text"]) for word in group),
                "start": round(start, 2),
                "end": round(end, 2),
                "timing": "phrase_estimate",
            }
        )
    return items
