from __future__ import annotations

"""Caption timing for the canonical narration script.

The narration script is the only source of visible caption text. Audio
analysis (native TTS word boundaries, a known-transcript forced aligner or,
today, Faster-Whisper word timestamps) only answers *when* each canonical
token was spoken. Whatever text a timing provider reports is used to line its
timestamps up with the script and is then discarded: it never reaches a
caption, the persisted state or the alignment cache.

Authority order:
1. native TTS word timestamps (when a voice provider supplies them),
2. a timing provider (known-transcript aligner, or recognised-word timing),
3. bounded interpolation of a few unaligned canonical tokens,
4. phrase timing estimated from the canonical script.
"""


import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .caption_text import (
    NORMALIZATION_VERSION,
    CanonicalToken,
    canonical_tokens,
    spoken_key,
    spoken_unit_sequence,
)
from .config import Settings
from .language import infer_language

# Bump whenever mapping, repair or the gate changes so cached alignments rebuild.
ALIGNMENT_VERSION = "canonical-alignment-v1"

TIMING_NATIVE = "native_tts"
TIMING_FORCED = "forced_alignment"
TIMING_RECOGNISED = "recognised_word_timing"
TIMING_PHRASE = "phrase_fallback"

# Share of canonical tokens that must be timed by audio evidence (not interpolated).
MIN_COVERAGE = 0.8
# Share of canonical spoken characters the evidence must agree with exactly.
# Below this the provider heard something else and its timing is not trusted.
MIN_LEXICAL_AGREEMENT = 0.6
# Longest run of consecutive unaligned tokens that may be interpolated.
MAX_INTERPOLATED_RUN = 3
MIN_WORD_SECONDS = 0.06
TIME_TOLERANCE = 0.05
OVERLAP_TOLERANCE = 0.01
_SECONDS_PER_CHARACTER = (0.03, 0.15)


@dataclass(frozen=True)
class AlignmentResult:
    """Canonical tokens with timings. ``words[i]["text"]`` is always script text."""

    words: list[dict[str, Any]]
    status: str
    provider: str
    diagnostic: str | None = None
    report: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptionTrack:
    items: list[dict[str, Any]]
    timing: str
    provider: str
    diagnostic: str | None
    report: dict[str, Any]


class WordAligner(Protocol):
    """Timing evidence for narration audio.

    ``align`` returns ``{"text", "start", "end"}`` entries. The text only
    locates each timestamp in the canonical script and is never displayed.
    A known-transcript aligner may additionally implement
    ``align_transcript(audio, units, language)`` (``units`` holds the spoken
    form of each canonical token) and set ``timing_source`` to
    ``"forced_alignment"``; it is then preferred over ``align``.
    """

    name: str

    def align(self, audio: Path, language: str) -> list[dict[str, Any]]: ...


class FasterWhisperAligner:
    """Free local word timing. The lightweight model is loaded once and cached on disk.

    It transcribes, so its words can be wrong ("gelb" heard as "Gap"); only its
    timestamps are used.
    """

    name = "faster_whisper"
    timing_source = TIMING_RECOGNISED

    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self.model_name = model_name
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


# ---------------------------------------------------------------------------
# Mapping timing evidence onto canonical tokens


@dataclass
class _TokenTiming:
    characters: int
    start: float | None = None
    end: float | None = None
    matched: int = 0
    status: str = "unaligned"


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _evidence_words(
    evidence: Sequence[dict[str, Any]], language: str
) -> list[tuple[str, float, float, int]]:
    """Spoken-form key, finite timestamps and spoken unit count of each evidence word."""
    usable: list[tuple[str, float, float]] = []
    for entry in evidence:
        start, end = _finite_number(entry.get("start")), _finite_number(entry.get("end"))
        if start is None or end is None or end < start:
            continue
        usable.append((str(entry.get("text") or ""), start, end))
    units = spoken_unit_sequence([text for text, _start, _end in usable], language)
    words = []
    for (_text, start, end), spoken in zip(usable, units, strict=True):
        key = spoken_key(" ".join(spoken))
        if key:  # punctuation has no spoken time of its own
            words.append((key, start, end, len(spoken)))
    return words


def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_character in enumerate(left, 1):
        current = [row]
        for column, right_character in enumerate(right, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


# A canonical token may be heard as several words ("2,5" / "zwei Komma fünf",
# "Sonnensystem" / "Sonnen system") and up to three tokens as one word.
_MAX_TOKENS_PER_WORD = 3
_MAX_WORDS_PER_TOKEN = 8
_SKIP_COST = 1.0
# Charged per spoken unit the two sides of a pair disagree on, so a merge
# only wins when the spelling agrees ("heute Abend" / "heuteabend") and a
# missing word is skipped instead of being glued onto its neighbour.
_MERGE_COST = 0.5
_BAND = 12


def _map_evidence(
    tokens: list[CanonicalToken], evidence: Sequence[dict[str, Any]], language: str
) -> tuple[list[_TokenTiming], float]:
    """Assign evidence timestamps to canonical tokens by monotonic word alignment.

    A banded dynamic programme pairs canonical tokens with evidence words in
    order. Pairs may be one-to-many or many-to-one (splits and merges) and
    cost their normalised spelling distance, so a misheard word ("Gap" for
    "gelb") keeps its acoustic slot while a missing word stays untimed for
    repair. Evidence text only scores pairs; it is never copied.
    """
    timings = [_TokenTiming(characters=len(token.key)) for token in tokens]
    words = _evidence_words(evidence, language)
    count, heard = len(tokens), len(words)
    if not count or not heard:
        return timings, 0.0
    keys = [token.key for token in tokens]
    unit_counts = [len(token.units) for token in tokens]
    distances: dict[tuple[int, int, int, int], tuple[float, int]] = {}

    def pair_cost(first: int, size: int, word: int, words_used: int) -> tuple[float, int] | None:
        cache_key = (first, size, word, words_used)
        if cache_key not in distances:
            canonical = "".join(keys[first:first + size])
            spoken = "".join(entry[0] for entry in words[word:word + words_used])
            longest = max(len(canonical), len(spoken))
            if (size > 1 or words_used > 1) and abs(len(canonical) - len(spoken)) * 2 > longest:
                distances[cache_key] = (math.inf, 0)
            else:
                edits = _edit_distance(canonical, spoken)
                unit_gap = abs(
                    sum(unit_counts[first:first + size])
                    - sum(entry[3] for entry in words[word:word + words_used])
                )
                cost = edits / longest * (size + words_used) / 2 + _MERGE_COST * unit_gap
                distances[cache_key] = (cost, max(0, len(canonical) - edits))
        cost, matched = distances[cache_key]
        return None if math.isinf(cost) else (cost, matched)

    # Pair shapes ending at each row: one token with up to its spoken unit
    # count + 1 words, or several tokens heard as one word.
    shapes = [[]] + [
        [(1, used) for used in range(1, min(_MAX_WORDS_PER_TOKEN, max(3, units + 1)) + 1)]
        + [(size, 1) for size in range(2, _MAX_TOKENS_PER_WORD + 1)]
        for units in unit_counts
    ]
    band = _BAND + abs(count - heard)
    best = [[math.inf] * (heard + 1) for _ in range(count + 1)]
    moves: list[list[tuple[int, int] | None]] = [[None] * (heard + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for row in range(count + 1):
        centre = round(row * heard / count)
        for column in range(max(0, centre - band), min(heard, centre + band) + 1):
            if row == column == 0:
                continue
            value, move = math.inf, None
            for size, used in shapes[row]:
                if row >= size and column >= used and not math.isinf(best[row - size][column - used]):
                    pair = pair_cost(row - size, size, column - used, used)
                    if pair is not None and best[row - size][column - used] + pair[0] < value:
                        value, move = best[row - size][column - used] + pair[0], (size, used)
            if row and best[row - 1][column] + _SKIP_COST < value:
                value, move = best[row - 1][column] + _SKIP_COST, (1, 0)
            if column and best[row][column - 1] + _SKIP_COST < value:
                value, move = best[row][column - 1] + _SKIP_COST, (0, 1)
            best[row][column], moves[row][column] = value, move

    matched_characters = 0
    row, column = count, heard
    while row or column:
        move = moves[row][column]
        if move is None:  # outside the band: nothing further can be paired
            break
        size, used = move
        row, column = row - size, column - used
        if not size or not used:
            continue
        pair = pair_cost(row, size, column, used)
        if pair is None:
            continue
        matched_characters += pair[1]
        start, end = words[column][1], words[column + used - 1][2]
        members = timings[row:row + size]
        total = sum(member.characters for member in members)
        cursor = start
        for member in members:
            member.start = cursor
            cursor += (end - start) * member.characters / total
            member.end = cursor
            member.matched = round(member.characters * pair[1] / total)
            member.status = "aligned"
    return timings, matched_characters / sum(len(key) for key in keys)


def _sanitise(timings: list[_TokenTiming], bound: float) -> None:
    """Drop out-of-range, non-monotonic or empty evidence; trim small overlaps."""
    previous: _TokenTiming | None = None
    for timing in timings:
        if timing.status != "aligned" or timing.start is None or timing.end is None:
            timing.status = "unaligned"
            continue
        if timing.start < -TIME_TOLERANCE or timing.start >= bound or timing.end > bound + TIME_TOLERANCE:
            timing.status = "unaligned"
            continue
        timing.start, timing.end = max(0.0, timing.start), min(bound, timing.end)
        if previous is not None and previous.start is not None and previous.end is not None:
            if timing.start < previous.start - OVERLAP_TOLERANCE:
                timing.status = "unaligned"  # evidence jumped backwards
                continue
            if timing.start < previous.end:
                if timing.start > previous.start:
                    previous.end = timing.start
                elif timing.end > previous.end:
                    timing.start = previous.end
                else:
                    timing.status = "unaligned"
                    continue
        if timing.end - timing.start < 0.005:
            timing.status = "unaligned"
            continue
        previous = timing


def _spread(members: list[_TokenTiming], weights: list[int], start: float, end: float) -> None:
    total = sum(weights)
    cursor = start
    for member, weight in zip(members, weights, strict=True):
        member.start = cursor
        cursor = cursor + (end - start) * weight / total
        member.end = cursor


def _repair(tokens: list[CanonicalToken], timings: list[_TokenTiming], bound: float) -> list[str]:
    """Interpolate short runs of unaligned tokens between reliable neighbours."""
    aligned = [timing for timing in timings if timing.status == "aligned"]
    if not aligned:
        return ["no canonical token was timed by audio evidence"]
    rates = [
        (timing.end - timing.start) / max(1, timing.characters)
        for timing in aligned
        if timing.start is not None and timing.end is not None
    ]
    low, high = _SECONDS_PER_CHARACTER
    per_character = min(high, max(low, statistics.median(rates)))
    issues: list[str] = []
    index = 0
    while index < len(timings):
        if timings[index].status == "aligned":
            index += 1
            continue
        run_end = index
        while run_end < len(timings) and timings[run_end].status != "aligned":
            run_end += 1
        run = list(range(index, run_end))
        index = run_end
        if len(run) > MAX_INTERPOLATED_RUN:
            issues.append(f"{len(run)} consecutive canonical tokens could not be timed")
            continue
        left = timings[run[0] - 1] if run[0] > 0 else None
        right = timings[run_end] if run_end < len(timings) else None
        weights = [max(1, len(tokens[position].key)) for position in run]
        needed = per_character * sum(weights)
        if left is not None and right is not None:
            start, end = float(left.end or 0), float(right.start or 0)
            if end - start > 2 * needed:
                # A pause sits in this gap: keep the words next to the side they belong to.
                if tokens[run[0] - 1].sentence_end:
                    start = end - needed
                else:
                    end = start + needed
        elif right is not None:
            end = float(right.start or 0)
            start = max(0.0, end - needed)
        else:
            start = float(left.end or 0) if left is not None else 0.0
            end = min(bound, start + needed)
        members = [timings[position] for position in run]
        if end - start >= MIN_WORD_SECONDS * len(run):
            _spread(members, weights, start, end)
        else:
            # No room between the neighbours: share their window proportionally.
            shared = members
            shared_weights = weights
            if left is not None:
                shared, shared_weights = [left, *shared], [max(1, left.characters), *shared_weights]
                start = float(left.start or 0)
            if right is not None:
                shared, shared_weights = [*shared, right], [*shared_weights, max(1, right.characters)]
                end = float(right.end or 0)
            if end - start < MIN_WORD_SECONDS * len(shared):
                issues.append("no room to place unaligned canonical tokens")
                continue
            _spread(shared, shared_weights, start, end)
        for member in members:
            member.status = "interpolated"
    return issues


def map_canonical_timings(
    tokens: list[CanonicalToken],
    evidence: Sequence[dict[str, Any]],
    language: str,
    bound: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Canonical timed words, quality metrics and the reasons they must not be used."""
    timings, agreement = _map_evidence(tokens, evidence, language)
    _sanitise(timings, bound)
    aligned = sum(timing.status == "aligned" for timing in timings)
    issues = _repair(tokens, timings, bound) if tokens else ["narration has no caption words"]
    coverage = aligned / len(tokens) if tokens else 0.0
    metrics = {
        "token_count": len(tokens),
        "aligned_tokens": aligned,
        "interpolated_tokens": sum(timing.status == "interpolated" for timing in timings),
        "coverage": round(coverage, 3),
        "lexical_agreement": round(agreement, 3),
    }
    if coverage < MIN_COVERAGE:
        issues.append(f"alignment coverage {coverage:.0%} is below {MIN_COVERAGE:.0%}")
    if agreement < MIN_LEXICAL_AGREEMENT:
        issues.append(
            f"timing evidence matches only {agreement:.0%} of the narration (minimum {MIN_LEXICAL_AGREEMENT:.0%})"
        )
    words = [
        {
            "text": token.display,
            "start": round(float(timing.start), 3) if timing.start is not None else None,
            "end": round(float(timing.end), 3) if timing.end is not None else None,
            "status": timing.status,
            "confidence": round(timing.matched / max(1, timing.characters), 2)
            if timing.status == "aligned"
            else 0.0,
            "sentence_end": token.sentence_end,
        }
        for token, timing in zip(tokens, timings, strict=True)
    ]
    return words, metrics, issues


# ---------------------------------------------------------------------------
# Alignment entry point, cache and diagnostics


def _provider_identity(aligner: WordAligner | None, settings: Settings) -> tuple[str, str] | None:
    if aligner is not None:
        return str(aligner.name), str(getattr(aligner, "model_name", "") or "")
    if settings.caption_alignment_provider in {"auto", "faster_whisper"}:
        return FasterWhisperAligner.name, settings.caption_alignment_model
    return None


def _cache_file(
    cache_dir: Path | None,
    audio: Path,
    script: str,
    language: str,
    identity: tuple[str, str] | None,
) -> Path | None:
    if cache_dir is None or identity is None:
        return None
    digest = hashlib.sha256()
    try:
        with audio.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    inputs = {
        "audio_sha256": digest.hexdigest(),
        "script": script,
        "language": language,
        "provider": identity[0],
        "model": identity[1],
        "normalization": NORMALIZATION_VERSION,
        "alignment": ALIGNMENT_VERSION,
    }
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return cache_dir / f"alignment-{key.hexdigest()[:24]}.json"


def _load_cached(path: Path | None, tokens: list[CanonicalToken]) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    words = payload.get("words") if isinstance(payload, dict) else None
    if (
        not isinstance(words, list)
        or payload.get("alignment") != ALIGNMENT_VERSION
        or payload.get("normalization") != NORMALIZATION_VERSION
        or [word.get("text") if isinstance(word, dict) else None for word in words]
        != [token.display for token in tokens]
    ):
        return None
    return payload


def _store_cached(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".partial")
        staging.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        staging.replace(path)
    except OSError:
        pass  # a cache write never blocks captions


def _report(**values: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "text_source": "canonical_script",
        "timing_source": TIMING_PHRASE,
        "provider": None,
        "model": None,
        "normalization_version": NORMALIZATION_VERSION,
        "alignment_version": ALIGNMENT_VERSION,
        "token_count": 0,
        "aligned_tokens": 0,
        "interpolated_tokens": 0,
        "coverage": 0.0,
        "lexical_agreement": 0.0,
        "fallback_reason": None,
        "cache": "disabled",
    }
    report.update(values)
    return report


def _fallback(diagnostic: str, report: dict[str, Any], started: float) -> AlignmentResult:
    report.update(timing_source=TIMING_PHRASE, elapsed_seconds=round(time.monotonic() - started, 3))
    if not report.get("fallback_reason"):
        report["fallback_reason"] = diagnostic
    return AlignmentResult([], "phrase_fallback", "estimated_segments", diagnostic, report)


def align_narration(
    audio: Path,
    script: str,
    duration: float,
    language: str,
    settings: Settings,
    *,
    aligner: WordAligner | None = None,
    cache_dir: Path | None = None,
    native_timings: Sequence[dict[str, Any]] | None = None,
    audio_seconds: float | None = None,
) -> AlignmentResult:
    """Time every canonical script token, or report why phrase timing must be used."""
    started = time.monotonic()
    tokens = canonical_tokens(script, language)
    bound = float(audio_seconds if audio_seconds is not None else duration)
    report = _report(token_count=len(tokens))
    if not tokens:
        report["fallback_reason"] = "narration has no caption words"
        return _fallback("Narration has no caption words; phrase timing fallback is in use.", report, started)

    rejected: list[str] = []
    if native_timings:
        words, metrics, issues = map_canonical_timings(tokens, native_timings, language, bound)
        if not issues:
            report.update(metrics, timing_source=TIMING_NATIVE, provider=TIMING_NATIVE)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return AlignmentResult(words, "word_aligned", TIMING_NATIVE, None, report)
        rejected.append("native word timing rejected: " + "; ".join(issues))

    identity = _provider_identity(aligner, settings)
    cache_path = _cache_file(cache_dir, audio, script, language, identity)
    cached = _load_cached(cache_path, tokens)
    if cached is not None:
        report.update(cached.get("metrics") or {})
        report.update(
            timing_source=cached.get("timing_source"),
            provider=cached.get("provider"),
            model=cached.get("model"),
            cache="hit",
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return AlignmentResult(list(cached["words"]), "word_aligned", str(cached.get("provider")), None, report)
    if cache_path is not None:
        report["cache"] = "miss"

    selected = aligner
    load_diagnostic: str | None = None
    if selected is None and identity is not None:
        try:
            selected = _local_aligner(settings.caption_alignment_model)
        except ImportError:
            load_diagnostic = "Alignment dependency missing; phrase timing fallback is in use."
        # Third-party model loading can fail with provider-specific cache and HTTP exception types.
        except Exception:  # noqa: BLE001
            selected = None
            load_diagnostic = "Alignment model unavailable; phrase timing fallback is in use."
    if selected is None:
        diagnostic = load_diagnostic or "Local word alignment is disabled; phrase timing fallback is in use."
        report["fallback_reason"] = "; ".join([*rejected, diagnostic])
        return _fallback(diagnostic, report, started)

    model = str(getattr(selected, "model_name", "") or (identity[1] if identity else ""))
    report.update(provider=selected.name, model=model or None)
    known_transcript = getattr(selected, "align_transcript", None)
    try:
        if callable(known_transcript):
            evidence = known_transcript(audio, [list(token.units) for token in tokens], language)
            timing_source = str(getattr(selected, "timing_source", TIMING_FORCED))
        else:
            evidence = selected.align(audio, language)
            timing_source = str(getattr(selected, "timing_source", TIMING_RECOGNISED))
    except (OSError, RuntimeError, ValueError) as exc:
        report["fallback_reason"] = "; ".join([*rejected, f"timing provider failed: {type(exc).__name__}"])
        return _fallback("Alignment failed — phrase timing fallback used.", report, started)

    words, metrics, issues = map_canonical_timings(tokens, evidence or [], language, bound)
    report.update(metrics)
    if issues:
        report["fallback_reason"] = "; ".join([*rejected, *issues])
        return _fallback("Alignment failed — phrase timing fallback used.", report, started)

    report.update(timing_source=timing_source)
    if rejected:
        report["native_rejected"] = rejected[0]
    _store_cached(
        cache_path,
        {
            "alignment": ALIGNMENT_VERSION,
            "normalization": NORMALIZATION_VERSION,
            "provider": selected.name,
            "model": model or None,
            "timing_source": timing_source,
            "metrics": metrics,
            "words": words,
        },
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return AlignmentResult(words, "word_aligned", selected.name, None, report)


# ---------------------------------------------------------------------------
# Caption integrity gate


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def caption_integrity_issues(
    items: list[dict[str, Any]],
    script: str,
    language: str,
    bound: float,
    report: dict[str, Any] | None = None,
) -> list[str]:
    """Every reason these word-timed captions must not be rendered (empty = safe)."""
    issues: list[str] = []
    tokens = canonical_tokens(script, language)
    words = [word for item in items for word in (item.get("words") or [])]
    if [word.get("text") for word in words] != [token.display for token in tokens]:
        issues.append("caption words differ from the canonical narration tokens or their order")
    if _compact(" ".join(str(item.get("text") or "") for item in items)) != _compact(script):
        issues.append("captions do not reconstruct the canonical narration")
    previous_word: dict[str, Any] | None = None
    previous_item: dict[str, Any] | None = None
    for item in items:
        group = item.get("words") or []
        if not group:
            issues.append("a word-timed caption group has no words")
            continue
        if item.get("text") != " ".join(str(word.get("text") or "") for word in group):
            issues.append("caption group text differs from its canonical words")
        for word in group:
            start, end = _finite_number(word.get("start")), _finite_number(word.get("end"))
            if start is None or end is None:
                issues.append("a caption word has a missing or non-finite timestamp")
                continue
            if start < 0:
                issues.append("a caption word starts before the narration")
            if end <= start:
                issues.append("a caption word ends before it starts")
            if end > bound + TIME_TOLERANCE:
                issues.append("a caption word ends after the narration")
            if previous_word is not None:
                previous_start = _finite_number(previous_word.get("start")) or 0.0
                previous_end = _finite_number(previous_word.get("end")) or 0.0
                if start < previous_start:
                    issues.append("caption word timestamps are not monotonic")
                elif start < previous_end - OVERLAP_TOLERANCE:
                    issues.append("caption words overlap")
            previous_word = word
        item_start, item_end = _finite_number(item.get("start")), _finite_number(item.get("end"))
        if item_start != _finite_number(group[0].get("start")) or item_end != _finite_number(group[-1].get("end")):
            issues.append("caption group timing differs from its first and last word")
        previous_end = _finite_number(previous_item.get("end")) if previous_item is not None else None
        if item_start is not None and previous_end is not None and item_start < previous_end - OVERLAP_TOLERANCE:
            issues.append("caption groups overlap or are out of order")
        previous_item = item
    if report is not None:
        if float(report.get("coverage") or 0) < MIN_COVERAGE:
            issues.append("alignment coverage is below the minimum")
        if float(report.get("lexical_agreement") or 0) < MIN_LEXICAL_AGREEMENT:
            issues.append("timing evidence agrees too little with the narration")
    return list(dict.fromkeys(issues))


def build_caption_track(
    audio: Path,
    script: str,
    duration: float,
    language: str,
    settings: Settings,
    *,
    words_per_group: int,
    audio_seconds: float | None = None,
    cache_dir: Path | None = None,
    aligner: WordAligner | None = None,
    native_timings: Sequence[dict[str, Any]] | None = None,
) -> CaptionTrack:
    """Word-timed canonical captions that passed the integrity gate, else phrase timing."""
    result = align_narration(
        audio,
        script,
        duration,
        language,
        settings,
        aligner=aligner,
        cache_dir=cache_dir,
        native_timings=native_timings,
        audio_seconds=audio_seconds,
    )
    report = dict(result.report)
    if result.status == "word_aligned":
        items = group_canonical_words(result.words, words_per_group)
        bound = float(audio_seconds if audio_seconds is not None else duration)
        issues = caption_integrity_issues(items, script, language, bound, report)
        report["integrity"] = {"passed": not issues, "issues": issues}
        if not issues:
            return CaptionTrack(items, "word_aligned", result.provider, None, report)
        report.update(timing_source=TIMING_PHRASE, fallback_reason="caption integrity: " + "; ".join(issues))
        diagnostic = "Caption integrity check failed — phrase timing fallback used."
    else:
        diagnostic = result.diagnostic or "Alignment failed — phrase timing fallback used."
    return CaptionTrack(
        phrase_fallback_items(script, duration, words_per_group),
        "phrase_fallback",
        "estimated_segments",
        diagnostic,
        report,
    )


# ---------------------------------------------------------------------------
# Grouping


_SENTENCE_ENDING = re.compile(r"[.!?…]+(?:[\"'”’\)\]]*)$")
_PUNCTUATION_ONLY = re.compile(r"^[.!?…]+(?:[\"'”’\)\]]*)$")


def _normalise_aligned_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold standalone punctuation into the preceding word without changing its timing."""
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
    return normalised


def _ends_group(word: dict[str, Any]) -> bool:
    if "sentence_end" in word:
        return bool(word["sentence_end"])
    return bool(_SENTENCE_ENDING.search(str(word["text"])))


def _sentence_aware_groups(words: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    group: list[dict[str, Any]] = []
    for word in words:
        group.append(word)
        if len(group) >= size or _ends_group(word):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    return groups


_WORD_KEYS = ("text", "start", "end", "status", "confidence")


def group_canonical_words(words: list[dict[str, Any]], words_per_group: int) -> list[dict[str, Any]]:
    """Caption groups from canonical timed tokens; a group spans its own words exactly."""
    size = max(2, min(8, words_per_group))
    items = []
    for group in _sentence_aware_groups(words, size):
        items.append(
            {
                "text": " ".join(str(word["text"]) for word in group),
                "start": group[0]["start"],
                "end": group[-1]["end"],
                "words": [{key: word[key] for key in _WORD_KEYS if key in word} for word in group],
                "timing": "word_aligned",
            }
        )
    return items


def group_aligned_words(
    words: list[dict[str, Any]],
    words_per_group: int,
    script: str | None = None,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Group timed words. With ``script`` the words are timing evidence only and
    every visible word comes from the script."""
    if script:
        lang = language or infer_language(script)
        tokens = canonical_tokens(script, lang)
        timed, _metrics, _issues = map_canonical_timings(tokens, words, lang, math.inf)
        resolved = [word for word in timed if word["start"] is not None and word["status"] != "unaligned"]
        return group_canonical_words(resolved, words_per_group)
    return group_canonical_words(_normalise_aligned_words(words), words_per_group)


def phrase_fallback_items(script: str, duration: float, words_per_group: int) -> list[dict[str, Any]]:
    words = _normalise_aligned_words([{"text": word} for word in re.findall(r"\S+", script)])
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
