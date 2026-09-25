"""Final Video Critic V1: review what the viewer actually sees, repair narrowly, stop.

Runs after ``render_video``.  Every scene of the finished MP4 is sampled with
ffmpeg (near its start, middle and end) and judged on separate quality
dimensions — semantic match, visual quality, subject visibility, overlay
footprint, caption collision, continuity, repetition, story alignment, reveal
safety, still motion and full-screen graphic usage — using only what already
exists: the Story Arc's scene annotations, the Visual Director's persisted
strategy, the renderer's layout metadata and the local OpenCLIP verifier
(metadata-only when OpenCLIP is not installed).  Nothing here knows a topic.

Repairs are driven by individual problems, never by the score: an overlay
problem changes only the overlay, a lost subject only the crop/motion, a
wrong visual only the media — and media changes go through the normal Visual
Director chain (``prepare_project_media``), so the free-first search order,
the Story Arc reveal rules and the paid-generation budget all still apply.
User-locked visuals are never replaced.  At most ``max_repair_passes`` (default
1, hard limit 2) repair renders run, each followed by one validation review;
whatever remains is reported, never retried.
"""
from __future__ import annotations

import copy
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .media import (
    GENERATED_ASSET_SOURCE,
    GRAPHIC_ASSET_SOURCE,
    _coverage_tokens,
    _mentions,
    _reuse_safe,
    build_visual_query_plan,
    is_scene_asset_allowed,
    media_source,
    protected_candidate_terms,
)
from .renderer import caption_band, ffmpeg_path
from .simple_graphics import normalise_graphic_spec, normalise_overlay_spec
from .smart_crop import crop_windows
from .thumbnails import _asset_quality, extract_video_frame
from .visual_director import (
    DEGRADED,
    MISSING,
    REUSE_PREVIOUS_VISUAL,
    generation_counts,
    generation_policy,
    scene_story_context,
)
from .visual_verifier import (
    SCENE_VISUAL_THRESHOLD,
    VISUAL_THRESHOLD,
    UnavailableVisualVerifier,
    get_visual_verifier,
    visual_intent_text,
)

VERSION = 1
DEFAULT_MAX_REPAIR_PASSES = 1
MAX_REPAIR_PASSES_LIMIT = 2

# Ratings per quality dimension (a dimension is never folded into another).
GOOD = "good"
WARNING = "warning"
POOR = "poor"
NOT_APPLICABLE = "not_applicable"
UNAVAILABLE = "unavailable"
_RATING_VALUE = {GOOD: 1.0, WARNING: 0.6, POOR: 0.15}
_RATING_ORDER = {POOR: 0, WARNING: 1, GOOD: 2}

DIMENSIONS = (
    "semantic_match",
    "visual_quality",
    "subject_visibility",
    "overlay_quality",
    "caption_overlay_collision",
    "visual_continuity",
    "repetition",
    "story_alignment",
    "reveal_safety",
    "motion",
    "graphic_usage",
)

# Thresholds reuse the media gate's OpenCLIP scale; rendered frames include
# captions/overlays, so "poor" is the gate's hard floor, not its pass mark.
SEMANTIC_GOOD = SCENE_VISUAL_THRESHOLD
SEMANTIC_POOR = VISUAL_THRESHOLD
SUBJECT_DROP = 0.05
MOTION_SPREAD = 0.06
# Overlay footprint (share of the 9:16 frame) from the renderer's layout.
OVERLAY_WARN_BBOX, OVERLAY_POOR_BBOX = 0.10, 0.14
OVERLAY_WARN_COVERAGE, OVERLAY_POOR_COVERAGE = 0.06, 0.085
NEAR_IDENTICAL_DIFF = 18.0  # mean grey-level difference of two 32x56 thumbnails
CROP_SHIFT = 0.05
FRAME_WIDTH = 360
_REUSE_STATUSES = {"related_media_reused", "real_media_reused", "generated_media_reused"}
_CONTINUED_STATUSES = {"block_visual_continued"}
_MEDIA_ACTIONS = {"replace_media", "convert_graphic_to_overlay", "continue_base_visual"}


# ---------------------------------------------------------------------------
# Optional stronger vision critic (disabled in V1; no paid call is required)
# ---------------------------------------------------------------------------

class VisionCritic(Protocol):
    """A pluggable reviewer of rendered frames (for example a cloud VLM).

    Returns extra issues ``[{"category", "code", "severity", "message"}]`` for
    one scene, or ``None`` when it cannot judge.  Findings are advisory input
    to the same planner; they never bypass locks, budgets or the Story Arc.
    """

    name: str

    def review_scene(self, frames: list[Path], context: dict[str, Any]) -> list[dict[str, Any]] | None: ...


VISION_CRITICS: dict[str, Callable[[Settings], VisionCritic | None]] = {}


def get_vision_critic(settings: Settings) -> VisionCritic | None:
    """The configured optional vision critic; ``None`` (default) keeps V1 local-only."""
    provider = str(getattr(settings, "final_critic_vision_provider", "none") or "none").casefold()
    factory = VISION_CRITICS.get(provider)
    return factory(settings) if factory is not None else None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def max_repair_passes(settings: Settings) -> int:
    try:
        value = int(getattr(settings, "final_critic_max_repair_passes", DEFAULT_MAX_REPAIR_PASSES))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_REPAIR_PASSES
    return max(0, min(MAX_REPAIR_PASSES_LIMIT, value))


def user_locked_visual(scene: dict[str, Any]) -> bool:
    """A visual the user chose or locked; the critic may report it, never replace it."""
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    director = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
    return bool(scene.get("user_locked_visual") or media.get("manually_selected") or director.get("manually_selected"))


def scene_sample_times(start: float, end: float) -> list[float]:
    """Near the start, the middle and near the end of one rendered scene window."""
    duration = float(end) - float(start)
    if duration <= 0.1:
        return []
    inset = min(0.3, duration * 0.12)
    times = (start + inset, start + duration / 2, end - inset)
    return sorted({round(max(start + 0.03, min(end - 0.06, value)), 3) for value in times})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _media_text(media: dict[str, Any]) -> str:
    generation = media.get("generation") if isinstance(media.get("generation"), dict) else {}
    graphic = media.get("graphic") if isinstance(media.get("graphic"), dict) else {}
    values = [
        media.get("query"), media.get("title"), media.get("description"), *(media.get("tags") or []),
        generation.get("prompt"), generation.get("prompt_summary"),
        *(value for value in graphic.values() if isinstance(value, str)),
        *(value for value in graphic.get("steps") or [] if isinstance(value, str)),
    ]
    return " ".join(str(value) for value in values if value)


def _overlay_text(spec: dict[str, Any]) -> str:
    clean = normalise_overlay_spec(spec) or {}
    return " ".join(str(value) for key in ("steps", "left", "right", "text") for value in ([clean.get(key)] if isinstance(clean.get(key), str) else clean.get(key) or []))


def _verifier_ready(verifier: Any) -> bool:
    if verifier is None or isinstance(verifier, UnavailableVisualVerifier) or not hasattr(verifier, "score_video_frames"):
        return False
    if getattr(verifier, "status", "") == "available":
        return True
    available = getattr(verifier, "available", None)
    return bool(callable(available) and available())


def _probe_video(ffmpeg: str, video: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", completed.stderr)
    duration = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    probe: dict[str, Any] = {}
    if size:
        width, height = int(size.group(1)), int(size.group(2))
        probe.update(width=width, height=height, aspect_ok=abs(width / height - 9 / 16) < 0.01)
    if duration:
        probe["duration"] = round(int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3)), 3)
    return probe


def _render_file(state: dict[str, Any], settings: Settings, project_id: str) -> Path | None:
    root = settings.render_root.resolve()
    project_dir = (root / project_id).resolve()
    url = str((state.get("render") or {}).get("url") or "")
    if not url.startswith("/media/"):
        return None
    candidate = (root / url.removeprefix("/media/")).resolve()
    if candidate.is_file() and candidate.is_relative_to(project_dir):
        return candidate
    return None


def _small_gray(image: Image.Image) -> list[int]:
    return list(image.convert("L").resize((32, 56)).tobytes())


def _mean_abs_diff(first: Image.Image, second: Image.Image) -> float:
    left, right = _small_gray(first), _small_gray(second)
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / max(1, len(left))


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

class _Row:
    """Evidence for one rendered scene (frames stay in memory for this run only)."""

    def __init__(self, entry: dict[str, Any], scene: dict[str, Any] | None, number: int):
        self.entry = entry
        self.scene = scene or {}
        self.number = number
        self.scene_id = str(entry.get("scene_id") or "")
        self.frames: list[dict[str, Any]] = []
        self.images: list[Image.Image] = []
        self.dimensions: dict[str, dict[str, Any]] = {}
        self.issues: list[dict[str, Any]] = []
        self.semantic_score: float | None = None
        self.frame_scores: list[float] = []

    @property
    def media(self) -> dict[str, Any]:
        return self.entry.get("media") if isinstance(self.entry.get("media"), dict) else {}

    @property
    def identity(self) -> str:
        return str(self.media.get("identity") or "")

    @property
    def strategy(self) -> dict[str, Any]:
        value = self.scene.get("visual_director")
        return value if isinstance(value, dict) else {}

    @property
    def graphic(self) -> bool:
        return self.media.get("source") == GRAPHIC_ASSET_SOURCE or self.entry.get("composition") == "fullscreen_graphic"

    @property
    def still(self) -> bool:
        return self.media.get("kind") != "video"


class _Review:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        project_id: str,
        revision: int,
        settings: Settings,
        verifier: Any,
        pass_index: int,
        vision_critic: VisionCritic | None,
    ):
        self.state = state
        self.project_id = project_id
        self.revision = revision
        self.settings = settings
        self.verifier = verifier
        self.semantic_ready = _verifier_ready(verifier)
        self.pass_index = pass_index
        self.vision_critic = vision_critic
        self.rows: list[_Row] = []
        self.status = "not_reviewed"
        self.reason: str | None = None
        self.probe: dict[str, Any] = {}
        self._cross_scores: dict[tuple[str, str], float | None] = {}

    # -- evidence ----------------------------------------------------------

    def collect(self) -> bool:
        render = self.state.get("render") if isinstance(self.state.get("render"), dict) else {}
        layout = [row for row in render.get("layout") or [] if isinstance(row, dict)]
        video = _render_file(self.state, self.settings, self.project_id)
        ffmpeg = ffmpeg_path()
        if not layout:
            self.reason = "render_layout_unavailable"
            return False
        if video is None:
            self.reason = "rendered_video_unavailable"
            return False
        if not ffmpeg:
            self.reason = "ffmpeg_unavailable"
            return False
        self.probe = _probe_video(ffmpeg, video)
        root = self.settings.render_root.resolve()
        frame_dir = root / self.project_id / "critic" / f"v{self.revision}" / f"pass{self.pass_index}"
        if frame_dir.exists():
            shutil.rmtree(frame_dir, ignore_errors=True)
        frame_dir.mkdir(parents=True, exist_ok=True)
        scenes = {str(scene.get("id") or ""): scene for scene in self.state.get("scenes") or [] if isinstance(scene, dict)}
        for number, entry in enumerate(layout, 1):
            row = _Row(entry, scenes.get(str(entry.get("scene_id") or "")), number)
            for position, timestamp in enumerate(scene_sample_times(float(entry.get("start") or 0), float(entry.get("end") or 0))):
                destination = frame_dir / f"{row.scene_id or number}-{position}.jpg"
                if not extract_video_frame(ffmpeg, video, timestamp, destination, width=FRAME_WIDTH):
                    continue
                try:
                    with Image.open(destination) as image:
                        row.images.append(image.convert("RGB"))
                except (OSError, UnidentifiedImageError, ValueError):
                    continue
                row.frames.append({"time": timestamp, "path": destination.relative_to(root).as_posix()})
            self.rows.append(row)
        if not any(row.images for row in self.rows):
            self.reason = "frame_extraction_failed"
            return False
        return True

    # -- semantics ---------------------------------------------------------

    def _texts(self, row: _Row) -> list[str]:
        return visual_intent_text(row.scene, self.state) if row.scene else ["a relevant visual scene"]

    def _score_frames(self, images: list[Image.Image], texts: list[str], identity: str) -> Any | None:
        if not self.semantic_ready or not images:
            return None
        try:
            result = self.verifier.score_video_frames(images, texts, asset_identity=identity)
        except Exception:  # noqa: BLE001 - semantic review degrades to metadata
            self.semantic_ready = False
            return None
        return result if getattr(result, "status", "") == "verified" else None

    def cross_score(self, source: _Row, target: _Row) -> float | None:
        """How well ``source``'s rendered frames fit ``target``'s visual intent."""
        key = (source.scene_id, target.scene_id)
        if key not in self._cross_scores:
            result = self._score_frames(
                source.images, self._texts(target), f"critic:v{self.revision}:p{self.pass_index}:{source.scene_id}->{target.scene_id}"
            )
            value = None if result is None else (result.scene_score if result.scene_score is not None else result.score)
            self._cross_scores[key] = value
        return self._cross_scores[key]

    def _source_score(self, row: _Row) -> float | None:
        """The same verifier on the untouched source asset (before crop/motion/overlays)."""
        media = row.scene.get("media") if isinstance(row.scene.get("media"), dict) else {}
        relevance = media.get("relevance") if isinstance(media.get("relevance"), dict) else {}
        visual = relevance.get("visual") if isinstance(relevance.get("visual"), dict) else {}
        if row.still and self.semantic_ready and hasattr(self.verifier, "verify_local_image"):
            path = (self.settings.render_root.resolve() / str(media.get("cache_path") or "")).resolve()
            if path.is_file() and path.is_relative_to(self.settings.render_root.resolve()):
                try:
                    result = self.verifier.verify_local_image(path, self._texts(row), asset_identity=f"critic-source:{row.identity}:{row.scene_id}")
                except Exception:  # noqa: BLE001
                    result = None
                if result is not None and getattr(result, "status", "") == "verified":
                    return result.scene_score if result.scene_score is not None else result.score
        value = visual.get("scene_score", visual.get("score"))
        return float(value) if isinstance(value, (int, float)) else None

    # -- per-scene dimensions ---------------------------------------------

    def _issue(self, row: _Row, category: str, code: str, severity: str, message: str, **evidence: Any) -> dict[str, Any]:
        existing = next((item for item in row.issues if item["id"] == f"{row.scene_id}:{category}:{code}"), None)
        if existing is not None:
            return existing
        issue = {
            "id": f"{row.scene_id}:{category}:{code}",
            "scene_id": row.scene_id,
            "scene_number": row.number,
            "category": category,
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": evidence,
        }
        row.issues.append(issue)
        return issue

    def _semantic(self, row: _Row) -> None:
        if row.graphic:
            row.dimensions["semantic_match"] = {"rating": NOT_APPLICABLE, "reason": "fullscreen_graphic"}
            return
        result = self._score_frames(row.images, self._texts(row), f"critic:v{self.revision}:p{self.pass_index}:{row.scene_id}")
        if result is not None:
            score = result.scene_score if result.scene_score is not None else result.score
            row.semantic_score = float(score)
            row.frame_scores = [float(value) for value in result.frame_scores]
            rating = GOOD if score >= SEMANTIC_GOOD else WARNING if score >= SEMANTIC_POOR else POOR
            row.dimensions["semantic_match"] = {
                "rating": rating, "source": "openclip_rendered_frames", "score": round(float(score), 4),
                "subject_score": round(float(result.subject_score), 4) if result.subject_score is not None else None,
                "frame_scores": [round(value, 4) for value in row.frame_scores],
                "presentation_risk": bool(result.presentation_risk),
            }
            return
        media = row.scene.get("media") if isinstance(row.scene.get("media"), dict) else {}
        relevance = media.get("relevance") if isinstance(media.get("relevance"), dict) else {}
        decision = row.strategy.get("decision")
        if relevance.get("confidence") == "rejected" or decision == MISSING or not row.identity:
            rating = POOR
        elif decision == DEGRADED or row.media.get("asset_status") in _REUSE_STATUSES:
            rating = WARNING
        else:
            rating = GOOD
        row.dimensions["semantic_match"] = {"rating": rating, "source": "metadata", "decision": decision}

    def _visual_quality(self, row: _Row) -> None:
        reasons: list[str] = []
        for image in row.images:
            reasons.extend(_asset_quality(image.width, image.height, image)[1])
        dark = reasons.count("nearly_black") > len(row.images) / 2
        flat = reasons.count("low_contrast") > len(row.images) / 2
        text_heavy = bool(row.dimensions.get("semantic_match", {}).get("presentation_risk")) and not row.graphic
        rating = POOR if dark else WARNING if flat or text_heavy else GOOD
        row.dimensions["visual_quality"] = {"rating": rating, "nearly_black": dark, "low_contrast": flat, "text_heavy": text_heavy}

    def _subject_and_motion(self, row: _Row) -> None:
        motion = row.entry.get("motion") if isinstance(row.entry.get("motion"), dict) else None
        if row.graphic:
            row.dimensions["subject_visibility"] = {"rating": NOT_APPLICABLE}
            row.dimensions["motion"] = {"rating": NOT_APPLICABLE if not motion else GOOD, "type": (motion or {}).get("type")}
            return
        if row.semantic_score is None:
            row.dimensions["subject_visibility"] = {"rating": UNAVAILABLE, "reason": "no_local_visual_model"}
            row.dimensions["motion"] = {"rating": UNAVAILABLE if motion and motion.get("type") != "static" else NOT_APPLICABLE, "type": (motion or {}).get("type")}
            return
        source = self._source_score(row)
        drop = None if source is None else source - row.semantic_score
        if source is not None and source >= SEMANTIC_GOOD and row.semantic_score < SEMANTIC_GOOD and drop >= SUBJECT_DROP:
            rating = POOR
        elif drop is not None and drop >= SUBJECT_DROP:
            rating = WARNING
        else:
            rating = GOOD
        row.dimensions["subject_visibility"] = {
            "rating": rating, "source_score": None if source is None else round(source, 4),
            "rendered_score": round(row.semantic_score, 4), "drop": None if drop is None else round(drop, 4),
        }
        if not row.still or not motion:
            row.dimensions["motion"] = {"rating": NOT_APPLICABLE, "type": None if not row.still else "none"}
            return
        spread = (max(row.frame_scores) - min(row.frame_scores)) if len(row.frame_scores) >= 2 else 0.0
        awkward = motion.get("type") != "static" and spread >= MOTION_SPREAD and min(row.frame_scores) < SEMANTIC_GOOD
        row.dimensions["motion"] = {"rating": POOR if awkward else GOOD, "type": motion.get("type"), "frame_spread": round(spread, 4)}

    def _overlay(self, row: _Row) -> None:
        overlays = [item for item in row.entry.get("overlays") or [] if isinstance(item, dict)]
        if not overlays:
            row.dimensions["overlay_quality"] = {"rating": NOT_APPLICABLE}
            row.dimensions["caption_overlay_collision"] = {"rating": NOT_APPLICABLE}
            return
        bbox_area = max(float(item.get("bbox_area") or 0.0) for item in overlays)
        coverage = max(float(item.get("coverage") or 0.0) for item in overlays)
        if bbox_area >= OVERLAY_POOR_BBOX or coverage >= OVERLAY_POOR_COVERAGE:
            rating = POOR
        elif bbox_area >= OVERLAY_WARN_BBOX or coverage >= OVERLAY_WARN_COVERAGE:
            rating = WARNING
        else:
            rating = GOOD
        row.dimensions["overlay_quality"] = {
            "rating": rating, "bbox_area": round(bbox_area, 4), "coverage": round(coverage, 4),
            "style": overlays[0].get("style"), "placement": overlays[0].get("placement"),
        }
        band = caption_band(self.state)
        start, end = float(row.entry.get("start") or 0), float(row.entry.get("end") or 0)
        captions = self.state.get("captions") if isinstance(self.state.get("captions"), dict) else {}
        active = band is not None and any(
            isinstance(item, dict) and float(item.get("start") or 0) < end and float(item.get("end") or 0) > start
            for item in captions.get("items") or []
        )
        hits = [
            item for item in overlays
            if active and isinstance(item.get("bbox"), list) and item["bbox"][1] < band["bottom"] and item["bbox"][3] > band["top"]
        ]
        row.dimensions["caption_overlay_collision"] = {
            "rating": POOR if hits else GOOD, "caption_band": band, "overlay_bbox": [item.get("bbox") for item in overlays],
        }

    def _story_context(self, row: _Row) -> dict[str, Any]:
        if not row.scene:
            return {"story_role": None, "visual_role": "evidence", "story_stage": None, "reveal_allowed": True, "fact_ids": []}
        return scene_story_context(row.scene, self.state)

    def _reveal(self, row: _Row, story: dict[str, Any], answer_identities: set[str]) -> None:
        if story.get("reveal_allowed", True):
            row.dimensions["reveal_safety"] = {"rating": GOOD, "reason": "reveal_allowed"}
            return
        problems = reveal_problems(row.scene, self.state, overlays=row.entry.get("overlays") or [], window=(row.entry.get("start"), row.entry.get("end")))
        if row.identity and row.identity in answer_identities:
            problems.append({"component": "base_visual", "code": "primary_answer_visual_before_reveal"})
        row.dimensions["reveal_safety"] = {"rating": POOR if problems else GOOD, "problems": problems}
        for problem in problems:
            self._issue(
                row, "reveal_safety", problem["code"], "error",
                f"The {problem['component'].replace('_', ' ')} shows the protected answer before the Story Arc reveal.",
                component=problem["component"],
            )

    def evaluate(self) -> None:
        # Answer scenes that own their visual (not one borrowed from another fact).
        answer_rows = [
            row for row in self.rows
            if (self._story_context(row).get("visual_role") == "primary_answer" or row.scene.get("is_primary_answer"))
            and row.media.get("asset_status") not in _REUSE_STATUSES
        ]
        answer_identities = {row.identity for row in answer_rows if row.identity}
        for row in self.rows:
            story = self._story_context(row)
            row.dimensions["_story"] = story
            if not row.scene:
                # Retimed after the render: this window has no scene identity left
                # to judge or repair; never report guesses about it.
                row.dimensions.update({name: {"rating": UNAVAILABLE, "reason": "scene_retimed"} for name in DIMENSIONS})
                continue
            self._semantic(row)
            self._visual_quality(row)
            self._subject_and_motion(row)
            self._overlay(row)
            self._reveal(row, story, answer_identities)
            self._graphic(row, story)
            self._story_alignment(row, story, answer_rows)
            self._vision(row, story)
        self._adjacent()
        for row in self.rows:
            self._issues_from_dimensions(row)
        self.status = "reviewed"

    # -- graphics, story roles, adjacency ---------------------------------

    def base_candidates(self, row: _Row) -> list[tuple[float, _Row]]:
        """Reveal-safe project visuals that fit this scene's intent (best first)."""
        rejected = {str(value) for value in row.scene.get("rejected_media_identities") or []}
        ranked: list[tuple[float, _Row]] = []
        seen: set[str] = set()
        story = self._story_context(row)
        strategy = row.strategy or {"reveal_allowed": story.get("reveal_allowed", True)}
        # A secondary insight / final payoff stays visually distinct from the answer.
        distinct = story.get("visual_role") in {"secondary_insight", "final_payoff"} and not story.get("is_primary_answer")
        for other in self.rows:
            if distinct and (other.scene.get("is_primary_answer") or self._story_context(other).get("visual_role") == "primary_answer"):
                continue
            media = other.scene.get("media") if isinstance(other.scene.get("media"), dict) else {}
            if (
                other is row or other.graphic or not other.identity or other.identity in seen or other.identity in rejected
                or not media or not is_scene_asset_allowed(media) or not _reuse_safe(media, strategy)
                or reveal_problems(row.scene, self.state, media=media, overlays=[])
            ):
                continue
            seen.add(other.identity)
            same_block = bool(row.scene.get("block_id")) and other.scene.get("block_id") == row.scene.get("block_id")
            score = self.cross_score(other, row)
            if score is None:
                # Metadata only: the same fact's visual, or media described by
                # this scene's own visual-intent words.
                words = _coverage_tokens(" ".join(self._texts(row)))
                overlap = len(words & _coverage_tokens(_media_text(media))) / max(1, min(len(words), 6))
                fits = same_block or overlap >= 0.34
                score = SEMANTIC_GOOD + overlap if fits else 0.0
            if score >= SEMANTIC_GOOD:
                ranked.append((score + (0.02 if same_block else 0.0), other))
        return sorted(ranked, key=lambda item: -item[0])

    def _graphic(self, row: _Row, story: dict[str, Any]) -> None:
        if not row.graphic:
            row.dimensions["graphic_usage"] = {"rating": NOT_APPLICABLE}
            return
        bases = self.base_candidates(row)
        justified = row.strategy.get("composition_reason") == "no_acceptable_base_visual"
        if bases:
            rating, reason = POOR, "usable_base_visual_exists"
        else:
            rating, reason = (GOOD, "no_base_visual") if justified else (WARNING, "unjustified_fullscreen_graphic")
        row.dimensions["graphic_usage"] = {
            "rating": rating, "reason": reason, "justified": justified,
            "base_candidate": bases[0][1].scene_id if bases else None,
        }

    def _story_alignment(self, row: _Row, story: dict[str, Any], answer_rows: list[_Row]) -> None:
        role = story.get("visual_role")
        semantic = row.dimensions.get("semantic_match", {}).get("rating")
        status = str(row.media.get("asset_status") or "")
        findings: list[tuple[str, str, str]] = []
        if role == "hook" and semantic == WARNING:
            findings.append(("hook_not_immediate", "warning", "The hook visual is not clearly about the opening question."))
        if role in {"secondary_insight", "final_payoff"} and not story.get("is_primary_answer"):
            collapsed = [
                other for other in answer_rows
                if other is not row and other.identity and other.identity == row.identity
                and other.scene.get("block_id") != row.scene.get("block_id")
            ]
            if collapsed:
                findings.append(("repeats_primary_answer_visual", "error", "This scene reuses the primary answer's visual, so it does not read as its own point."))
        if role == "final_payoff" and status in _REUSE_STATUSES:
            findings.append(("payoff_generic_reuse", "warning", "The final payoff falls back to a reused project visual."))
        if role == "primary_answer" and status in _REUSE_STATUSES:
            findings.append(("answer_without_own_visual", "warning", "The primary answer reuses another fact's visual."))
        worst = min((_RATING_ORDER[POOR if severity == "error" else WARNING] for _code, severity, _message in findings), default=_RATING_ORDER[GOOD])
        rating = {value: key for key, value in _RATING_ORDER.items()}[worst]
        row.dimensions["story_alignment"] = {"rating": rating, "visual_role": role, "story_role": story.get("story_role"), "findings": [code for code, _s, _m in findings]}
        for code, severity, message in findings:
            self._issue(row, "story_alignment", code, severity, message)

    def _vision(self, row: _Row, story: dict[str, Any]) -> None:
        if self.vision_critic is None or not row.frames:
            return
        root = self.settings.render_root.resolve()
        try:
            findings = self.vision_critic.review_scene([root / item["path"] for item in row.frames], {"scene_id": row.scene_id, **story})
        except Exception:  # noqa: BLE001 - optional reviewer
            return
        for finding in findings or []:
            if isinstance(finding, dict) and finding.get("category") in DIMENSIONS:
                self._issue(row, str(finding["category"]), f"vision_{finding.get('code') or 'finding'}", "warning", str(finding.get("message") or "Vision critic finding."))

    def _adjacent(self) -> None:
        for row in self.rows:
            row.dimensions.setdefault("repetition", {"rating": GOOD})
            row.dimensions.setdefault("visual_continuity", {"rating": GOOD})
        # Repeated identities: deliberate continuation of one fact's base visual
        # is continuity; a repeat across unrelated facts is accidental.
        for later_index, later in enumerate(self.rows):
            for earlier in self.rows[:later_index]:
                if not later.identity or later.identity != earlier.identity or later.graphic or not later.scene or not earlier.scene:
                    continue
                if intentional_continuity(earlier.scene, later.scene):
                    later.dimensions["repetition"] = {"rating": GOOD, "reason": "intentional_continuity", "with": earlier.scene_id}
                    continue
                # Blame the scene that borrowed the footage, never its owner or a user choice.
                target, other = later, earlier
                if (_borrowed(earlier) and not _borrowed(later)) or (user_locked_visual(later.scene) and not user_locked_visual(earlier.scene)):
                    target, other = earlier, later
                # One short: the same footage for an unrelated fact is always noticed.
                target.dimensions["repetition"] = {"rating": POOR, "reason": "accidental_repeat", "with": other.scene_id, "identity": later.identity}
                self._issue(
                    target, "repetition", "accidental_repeat", "error",
                    f"Scene {target.number} repeats the visual of scene {other.number} without adding anything.",
                    identity=later.identity, with_scene=other.scene_id,
                )
                break
        for previous, row in zip(self.rows, self.rows[1:], strict=False):
            if not previous.images or not row.images or not previous.scene or not row.scene:
                continue
            difference = _mean_abs_diff(previous.images[-1], row.images[0])
            if previous.identity != row.identity and previous.graphic and row.graphic and difference < NEAR_IDENTICAL_DIFF:
                row.dimensions["repetition"] = {"rating": POOR, "reason": "near_identical_graphics", "with": previous.scene_id, "difference": round(difference, 2)}
                self._issue(row, "repetition", "near_identical_graphics", "error", f"Scenes {previous.number} and {row.number} are two near-identical full-screen graphics.", with_scene=previous.scene_id)
            same_block = bool(row.scene.get("block_id")) and row.scene.get("block_id") == previous.scene.get("block_id")
            weak = row.dimensions.get("semantic_match", {}).get("rating") in {WARNING, POOR}
            if same_block and previous.identity and row.identity and previous.identity != row.identity and weak and not previous.graphic:
                fit = self.cross_score(previous, row)
                if fit is not None and fit >= SEMANTIC_GOOD and (row.semantic_score is None or fit > row.semantic_score):
                    row.dimensions["visual_continuity"] = {"rating": POOR, "reason": "unnecessary_asset_switch", "with": previous.scene_id, "fit": round(fit, 4)}
                    self._issue(
                        row, "visual_continuity", "unnecessary_asset_switch", "error",
                        f"Scene {row.number} switches away from scene {previous.number}'s better-fitting visual in the middle of one fact.",
                        with_scene=previous.scene_id,
                    )

    def _issues_from_dimensions(self, row: _Row) -> None:
        dims = row.dimensions
        semantic = dims.get("semantic_match", {})
        subject = dims.get("subject_visibility", {})
        if semantic.get("rating") == POOR and subject.get("rating") != POOR:
            self._issue(row, "semantic_match", "wrong_media", "error", f"Scene {row.number} does not show what the narration is about.", score=semantic.get("score"), source=semantic.get("source"))
        elif semantic.get("rating") == WARNING:
            self._issue(row, "semantic_match", "weak_media", "warning", f"Scene {row.number}'s visual only loosely matches the narration.", score=semantic.get("score"), source=semantic.get("source"))
        if subject.get("rating") == POOR:
            self._issue(row, "subject_visibility", "subject_lost_in_render", "error", f"The subject of scene {row.number} is visible in the source but lost after crop, motion or overlay.", drop=subject.get("drop"))
        quality = dims.get("visual_quality", {})
        if quality.get("rating") == POOR:
            self._issue(row, "visual_quality", "unusable_frame", "error", f"Scene {row.number} renders nearly black.")
        elif quality.get("rating") == WARNING:
            self._issue(row, "visual_quality", "low_contrast_or_text_heavy", "warning", f"Scene {row.number} renders flat or text-heavy.")
        if dims.get("motion", {}).get("rating") == POOR:
            self._issue(row, "motion", "motion_loses_subject", "error", f"The still motion of scene {row.number} moves the subject out of view.")
        overlay = dims.get("overlay_quality", {})
        if overlay.get("rating") == POOR:
            self._issue(row, "overlay_quality", "overlay_too_dominant", "error", f"The overlay of scene {row.number} covers too much of the base visual.", bbox_area=overlay.get("bbox_area"), coverage=overlay.get("coverage"))
        elif overlay.get("rating") == WARNING:
            self._issue(row, "overlay_quality", "overlay_large", "warning", f"The overlay of scene {row.number} is on the large side.", bbox_area=overlay.get("bbox_area"))
        if dims.get("caption_overlay_collision", {}).get("rating") == POOR:
            self._issue(row, "caption_overlay_collision", "overlay_hits_captions", "error", f"The overlay of scene {row.number} collides with the captions.")
        graphic = dims.get("graphic_usage", {})
        if graphic.get("rating") == POOR:
            self._issue(row, "graphic_usage", "fullscreen_graphic_with_base", "error", f"Scene {row.number} is a full-screen graphic although a fitting base visual exists.", base_scene=graphic.get("base_candidate"))
        elif graphic.get("rating") == WARNING:
            self._issue(row, "graphic_usage", "fullscreen_graphic_unjustified", "warning", f"Scene {row.number} uses a full-screen graphic without a recorded reason.")
        if user_locked_visual(row.scene) and any(issue["severity"] == "error" for issue in row.issues):
            self._issue(row, "semantic_match" if semantic.get("rating") == POOR else row.issues[0]["category"], "manual_asset_weak", "warning", f"The visual you chose for scene {row.number} appears weak; it was not replaced automatically.")

    # -- persistence -------------------------------------------------------

    def scene_records(self) -> list[dict[str, Any]]:
        records = []
        for row in self.rows:
            story = row.dimensions.get("_story", {})
            dims = {key: value for key, value in row.dimensions.items() if not key.startswith("_")}
            values = [_RATING_VALUE[item["rating"]] for item in dims.values() if item.get("rating") in _RATING_VALUE]
            records.append({
                "scene_id": row.scene_id,
                "scene_number": row.number,
                "block_id": row.entry.get("block_id"),
                "fact_ids": list(story.get("fact_ids") or []),
                "story_role": story.get("story_role"),
                "visual_role": story.get("visual_role"),
                "story_stage": story.get("story_stage"),
                "reveal_allowed": bool(story.get("reveal_allowed", True)),
                "window": {"start": row.entry.get("start"), "end": row.entry.get("end")},
                "frames": row.frames,
                "media": {key: row.media.get(key) for key in ("identity", "source", "kind", "asset_status")},
                "composition": row.entry.get("composition"),
                "adjustments": row.entry.get("adjustments") or {},
                "user_locked": user_locked_visual(row.scene),
                "dimensions": dims,
                "score": round(sum(values) / len(values), 3) if values else None,
                "issue_ids": [issue["id"] for issue in row.issues],
            })
        return records

    def issues(self) -> list[dict[str, Any]]:
        return [issue for row in self.rows for issue in row.issues]

    def score(self) -> int | None:
        values = [record["score"] for record in self.scene_records() if record["score"] is not None]
        return round(100 * sum(values) / len(values)) if values else None

    def row(self, scene_id: str) -> _Row | None:
        return next((row for row in self.rows if row.scene_id == scene_id), None)


def _borrowed(row: _Row) -> bool:
    """A visual this scene reuses from another fact (the Visual Director's degraded reuse)."""
    return row.media.get("asset_status") in _REUSE_STATUSES


def intentional_continuity(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Deliberate base-visual continuation (same fact/block or a recorded choice)."""
    if first.get("block_id") and first.get("block_id") == second.get("block_id"):
        return True
    first_facts = {str(value) for value in first.get("story_unit_ids") or (first.get("visual_director") or {}).get("fact_ids") or []}
    second_facts = {str(value) for value in second.get("story_unit_ids") or (second.get("visual_director") or {}).get("fact_ids") or []}
    if first_facts & second_facts:
        return True
    for scene, other in ((second, first), (first, second)):
        continuity = scene.get("visual_continuity") if isinstance(scene.get("visual_continuity"), dict) else {}
        if continuity and (continuity.get("source_scene_id") == other.get("id") or scene.get("asset_status") in _CONTINUED_STATUSES):
            return True
    return False


def reveal_problems(
    scene: dict[str, Any],
    state: dict[str, Any],
    *,
    media: dict[str, Any] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    window: tuple[Any, Any] | None = None,
) -> list[dict[str, str]]:
    """What in a scene's composition would show the protected answer before the reveal.

    Story Arc first: nothing is checked once the scene's own story context
    allows the reveal.  Checks the base visual (real, generated, graphic or
    reused), the overlays drawn over it and ClipForge's own on-screen labels.
    """
    story = scene_story_context(scene, state) if scene else {"reveal_allowed": True}
    if story.get("reveal_allowed", True):
        return []
    media = media if media is not None else (scene.get("media") if isinstance(scene.get("media"), dict) else {})
    plan = build_visual_query_plan(scene, state)
    terms = protected_candidate_terms(state, plan)
    problems: list[dict[str, str]] = []
    if media:
        generation = media.get("generation") if isinstance(media.get("generation"), dict) else None
        if media.get("reveal_safe") is False or (generation is not None and not generation.get("reveal_safe", True)):
            problems.append({"component": "generated_image" if media_source(media) == GENERATED_ASSET_SOURCE else "base_visual", "code": "visual_recorded_reveal_unsafe"})
        elif (media.get("is_primary_answer") or media.get("story_role") == "primary_answer") and media_source(media) != GRAPHIC_ASSET_SOURCE:
            problems.append({"component": "reused_visual", "code": "primary_answer_visual_before_reveal"})
        elif terms and _mentions(_coverage_tokens(_media_text(media)), terms):
            component = "comparison_graphic" if media_source(media) == GRAPHIC_ASSET_SOURCE else "base_visual"
            problems.append({"component": component, "code": "visual_names_protected_answer"})
    for overlay in overlays or []:
        spec = overlay.get("spec") if isinstance(overlay, dict) and isinstance(overlay.get("spec"), dict) else {}
        if terms and _mentions(_coverage_tokens(_overlay_text(spec)), terms):
            problems.append({"component": "overlay", "code": "overlay_names_protected_answer"})
            break
    if window is not None and terms:
        start, end = float(window[0] or 0), float(window[1] or 0)
        for event in state.get("attention_events") or []:
            if not isinstance(event, dict):
                continue
            event_start = float(event.get("start") or 0)
            if event_start < end and event_start + float(event.get("duration") or 0) > start and _mentions(_coverage_tokens(str(event.get("text") or "")), terms):
                problems.append({"component": "label", "code": "label_names_protected_answer"})
                break
    return problems


# ---------------------------------------------------------------------------
# Repair planning
# ---------------------------------------------------------------------------

def _scene_by_id(state: dict[str, Any], scene_id: str) -> dict[str, Any] | None:
    return next((scene for scene in state.get("scenes") or [] if isinstance(scene, dict) and str(scene.get("id") or "") == scene_id), None)


def _recalculated_crop(review: _Review, row: _Row) -> dict[str, float] | None:
    """A focal point from the same crop windows smart crop uses, scored on the subject."""
    media = row.scene.get("media") if isinstance(row.scene.get("media"), dict) else {}
    if not row.still or media_source(media) == GRAPHIC_ASSET_SOURCE:
        return None
    root = review.settings.render_root.resolve()
    path = (root / str(media.get("cache_path") or "")).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        return None
    current = row.entry.get("crop") if isinstance(row.entry.get("crop"), dict) else {}
    current_x, current_y = float(current.get("center_x", 0.5)), float(current.get("center_y", 0.5))
    timeline = review.state.get("timeline") or {}
    target_ratio = int(timeline.get("width") or 1080) / max(1, int(timeline.get("height") or 1920))
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    windows = crop_windows(image.width, image.height, target_ratio)
    if not windows:
        return None
    best: tuple[float, tuple[float, float, float, float]] | None = None
    if review.semantic_ready and hasattr(review.verifier, "score_image"):
        texts = review._texts(row)
        for window in windows:
            left, top, width, height = window
            crop = image.crop((round(left * image.width), round(top * image.height), round((left + width) * image.width), round((top + height) * image.height)))
            try:
                score = float(review.verifier.score_image(crop, texts, asset_identity=f"critic-crop:{row.identity}:{window}"))
            except Exception:  # noqa: BLE001
                return None
            if best is None or score > best[0]:
                best = (score, window)
    if best is None:
        # Without a visual model the only safe change is back to the centre.
        if abs(current_x - 0.5) < CROP_SHIFT and abs(current_y - 0.5) < CROP_SHIFT:
            return None
        return {"center_x": 0.5, "center_y": 0.5, "source": "centered"}
    left, top, width, height = best[1]
    center = {"center_x": round(left + width / 2, 4), "center_y": round(top + height / 2, 4), "source": "subject_window", "score": round(best[0], 4)}
    if abs(center["center_x"] - current_x) < CROP_SHIFT and abs(center["center_y"] - current_y) < CROP_SHIFT:
        return None
    return center


def overlay_would_dominate(spec: dict[str, Any], state: dict[str, Any]) -> bool:
    """Measure an overlay spec as the renderer would draw it (full pill style)."""
    import tempfile

    from .renderer import overlay_footprint
    from .simple_graphics import GraphicSpecError, render_overlay

    timeline = state.get("timeline") or {}
    try:
        with tempfile.TemporaryDirectory(prefix="clipforge-critic-") as temp:
            path = render_overlay(spec, Path(temp) / "overlay.png", width=int(timeline.get("width") or 1080), height=int(timeline.get("height") or 1920))
            footprint = overlay_footprint(path)
    except (GraphicSpecError, OSError, ValueError):
        return False
    return float(footprint.get("bbox_area") or 0) >= OVERLAY_POOR_BBOX or float(footprint.get("coverage") or 0) >= OVERLAY_POOR_COVERAGE


def _overlay_graphic_spec(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """A full-screen graphic's information as a light overlay spec."""
    clean = normalise_graphic_spec(spec)
    if clean is None:
        return None
    if clean["kind"] == "process":
        return {"kind": "process", "steps": list(clean["steps"])}
    if clean["kind"] == "comparison":
        return {"kind": "comparison", "left": clean["left"], "right": clean["right"]}
    return normalise_overlay_spec({"kind": "label", "text": " ".join(part for part in (clean.get("value"), clean.get("label")) if part)})


def plan_repairs(review: _Review, earlier: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One targeted action per scene, chosen from that scene's own error issues.

    A media action that already found no alternative (or was blocked) earlier
    in this run is not retried.
    """
    exhausted = {
        (record.get("scene_id"), record.get("action"))
        for record in earlier or []
        if record.get("status") in {"no_alternative", "blocked", "reverted", "failed"}
    }
    actions: list[dict[str, Any]] = []
    by_scene: dict[str, list[dict[str, Any]]] = {}
    for issue in review.issues():
        if issue["severity"] == "error":
            by_scene.setdefault(issue["scene_id"], []).append(issue)
    for scene_id, issues in by_scene.items():
        row = review.row(scene_id)
        scene = _scene_by_id(review.state, scene_id)
        codes = {issue["code"]: issue for issue in issues}
        base = {"scene_id": scene_id, "scene_number": row.number if row else None, "issue_ids": [issue["id"] for issue in issues], "categories": sorted({issue["category"] for issue in issues})}
        if row is None or scene is None:
            actions.append({**base, "action": "report_only", "status": "blocked", "blocked_reason": "scene_changed_after_render"})
            continue
        locked = user_locked_visual(scene)
        media_action = _media_action(review, row, codes)
        if media_action is not None and (scene_id, media_action["action"]) in exhausted:
            actions.append({**base, **media_action, "status": "blocked", "blocked_reason": "no_alternative_found_earlier"})
            media_action = None
        if media_action is not None:
            if locked:
                actions.append({**base, **media_action, "status": "blocked", "blocked_reason": "user_locked_visual",
                                "message": "Manual asset appears weak; it was not replaced automatically."})
                media_action = None
            else:
                actions.append({**base, **media_action, "status": "planned"})
                continue
        composition = _composition_action(review, row, codes, scene)
        if composition is not None:
            actions.append({**base, **composition, "status": "planned"})
        elif not any(action["scene_id"] == scene_id for action in actions):
            actions.append({**base, "action": "report_only", "status": "blocked", "blocked_reason": "no_safe_targeted_repair"})
    return actions


def _media_action(review: _Review, row: _Row, codes: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    reveal = [issue for code, issue in codes.items() if issue["category"] == "reveal_safety"]
    if any(issue["evidence"].get("component") in {"base_visual", "generated_image", "reused_visual", "comparison_graphic"} for issue in reveal):
        return {"action": "replace_media", "reason": "reveal_safety", "reject": [row.identity] if row.identity else []}
    if "fullscreen_graphic_with_base" in codes or "near_identical_graphics" in codes:
        bases = review.base_candidates(row)
        spec = _overlay_graphic_spec((row.scene.get("media") or {}).get("graphic") or row.strategy.get("graphic"))
        if bases and spec is not None:
            return {
                "action": "convert_graphic_to_overlay", "reason": "graphic_usage", "base_scene_id": bases[0][1].scene_id,
                "base_identity": bases[0][1].identity, "overlay_spec": spec,
                # The information stays a small layer over the base visual.
                "overlay_adjustments": {"mode": "compact"} if overlay_would_dominate(spec, review.state) else {},
            }
    if "accidental_repeat" in codes or "repeats_primary_answer_visual" in codes:
        return {"action": "replace_media", "reason": "repetition" if "accidental_repeat" in codes else "story_alignment", "reject": [row.identity]}
    if "unnecessary_asset_switch" in codes:
        partner = review.row(codes["unnecessary_asset_switch"]["evidence"].get("with_scene") or "")
        if partner is not None and partner.identity:
            return {"action": "continue_base_visual", "reason": "visual_continuity", "base_scene_id": partner.scene_id, "base_identity": partner.identity}
    if "wrong_media" in codes or "unusable_frame" in codes:
        # A same-fact visual that already fits keeps continuity; otherwise the
        # normal Visual Director chain searches again (then generation, reuse).
        same_block = [
            candidate for _score, candidate in review.base_candidates(row)
            if candidate.scene.get("block_id") and candidate.scene.get("block_id") == row.scene.get("block_id")
        ]
        if same_block:
            return {"action": "continue_base_visual", "reason": "semantic_match", "base_scene_id": same_block[0].scene_id, "base_identity": same_block[0].identity}
        return {"action": "replace_media", "reason": "semantic_match", "reject": [row.identity] if row.identity else []}
    return None


def _composition_action(review: _Review, row: _Row, codes: dict[str, dict[str, Any]], scene: dict[str, Any]) -> dict[str, Any] | None:
    current = scene.get("render_adjustments") if isinstance(scene.get("render_adjustments"), dict) else {}
    adjustments: dict[str, Any] = {}
    reasons: list[str] = []
    overlay_now = current.get("overlay") if isinstance(current.get("overlay"), dict) else {}
    overlay: dict[str, Any] = {}
    if any(issue["category"] == "reveal_safety" and issue["evidence"].get("component") == "overlay" for issue in codes.values()):
        overlay["mode"] = "remove"
        reasons.append("reveal_safety")
    if "overlay_too_dominant" in codes:
        overlay.setdefault("mode", "compact")
        reasons.append("overlay_quality")
    if "overlay_hits_captions" in codes:
        band = caption_band(review.state) or {}
        overlay["placement"] = "upper" if band.get("position") == "lower" or float(band.get("top", 1)) > 0.5 else "lower"
        if overlay_now.get("placement") == overlay["placement"]:
            overlay.setdefault("mode", "compact")
        reasons.append("caption_overlay_collision")
    lost = "subject_lost_in_render" in codes
    if lost and row.dimensions.get("overlay_quality", {}).get("rating") in {POOR, WARNING}:
        overlay.setdefault("mode", "compact")
        reasons.append("subject_visibility")
    merged_overlay = {**overlay_now, **overlay}
    if overlay and merged_overlay != overlay_now:
        adjustments["overlay"] = merged_overlay
    if (lost or "motion_loses_subject" in codes) and row.still and current.get("motion") != "static":
        motion = row.entry.get("motion") if isinstance(row.entry.get("motion"), dict) else {}
        if motion.get("type") not in {None, "static"}:
            adjustments["motion"] = "static"
            reasons.append("motion" if "motion_loses_subject" in codes else "subject_visibility")
    if lost or "motion_loses_subject" in codes:
        crop = _recalculated_crop(review, row)
        if crop is not None and crop != current.get("crop"):
            adjustments["crop"] = crop
            reasons.append("crop")
    if not adjustments:
        return None
    return {"action": "adjust_composition", "reason": ",".join(dict.fromkeys(reasons)), "adjustments": adjustments}


# ---------------------------------------------------------------------------
# Applying repairs (existing Visual Director / media systems only)
# ---------------------------------------------------------------------------

def _scene_snapshot(scene: dict[str, Any]) -> dict[str, Any]:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    return {
        "media_identity": media.get("identity"),
        "media_source": media_source(media) if media else None,
        "asset_status": scene.get("asset_status"),
        "composition": (scene.get("visual_director") or {}).get("composition"),
        "adjustments": copy.deepcopy(scene.get("render_adjustments") or {}),
    }


def apply_repairs(
    actions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    prepare_media: Callable[[dict[str, Any]], Any],
    settings: Settings,
    pass_index: int,
) -> list[dict[str, Any]]:
    """Apply planned actions; media changes run through ``prepare_media`` once."""
    records: list[dict[str, Any]] = []
    previous: dict[str, dict[str, Any]] = {}
    counts_before = generation_counts(state)
    for action in actions:
        record = {**action, "pass": pass_index, "attempted_at": _now()}
        records.append(record)
        if action.get("status") != "planned":
            continue
        scene = _scene_by_id(state, action["scene_id"])
        if scene is None or (action["action"] in _MEDIA_ACTIONS and user_locked_visual(scene)):
            record.update(status="blocked", blocked_reason="user_locked_visual" if scene is not None else "scene_changed_after_render")
            continue
        record["before"] = _scene_snapshot(scene)
        previous[action["scene_id"]] = copy.deepcopy({key: scene.get(key) for key in ("media", "asset_status", "visual_director", "visual_continuity", "fallback_reason")})
        kind = action["action"]
        if kind == "replace_media":
            rejected = list(dict.fromkeys([*(scene.get("rejected_media_identities") or []), *[value for value in action.get("reject") or [] if value]]))
            scene["rejected_media_identities"] = rejected
            scene["asset_status"] = "replacement_required"
            scene.pop("visual_continuity", None)
            record["invalidated"] = ["media", "crop", "overlay", "motion"]
        elif kind in {"convert_graphic_to_overlay", "continue_base_visual"}:
            base = next(
                (dict(item["media"]) for item in state.get("scenes") or [] if isinstance(item.get("media"), dict) and str(item["media"].get("identity") or "") == action["base_identity"]),
                None,
            )
            strategy = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
            if base is None or reveal_problems(scene, state, media=base, overlays=[]) or not _reuse_safe(base, strategy or {"reveal_allowed": True}):
                record.update(status="blocked", blocked_reason="base_visual_not_reveal_safe" if base is not None else "base_visual_unavailable")
                continue
            base.pop("manually_selected", None)
            scene["media"] = base
            scene["asset_status"] = "block_visual_continued"
            scene.pop("fallback_reason", None)
            scene["visual_continuity"] = {"source_scene_id": action["base_scene_id"], "identity": action["base_identity"], "reason": f"final_critic_{action['reason']}"}
            if strategy:
                if kind == "convert_graphic_to_overlay":
                    strategy["overlay_spec"] = action["overlay_spec"]
                    strategy["composition"] = "base_with_overlay"
                    strategy["composition_reason"] = "final_critic_graphic_to_overlay"
                    if action.get("overlay_adjustments"):
                        scene["render_adjustments"] = {
                            **(scene.get("render_adjustments") or {}), "overlay": dict(action["overlay_adjustments"]), "source": "final_critic",
                        }
                strategy.update(resolved_type=REUSE_PREVIOUS_VISUAL, decision_reason=f"final_critic_{kind}")
            record["invalidated"] = ["media", "crop", "overlay", "motion"]
        elif kind == "adjust_composition":
            adjustments = dict(scene.get("render_adjustments") or {})
            adjustments.update(action["adjustments"])
            adjustments["source"] = "final_critic"
            scene["render_adjustments"] = adjustments
            record["invalidated"] = sorted(key for key in action["adjustments"] if key in {"overlay", "motion", "crop"})
        record["status"] = "applied"
    # One media pass for the whole repair: rejected scenes search again through
    # the normal chain (free real media, then bounded generation, reuse, a
    # graphic); every other scene keeps its cached media, and overlays are
    # re-attached from the Visual Director strategies.
    prepare_media(state)
    policy = generation_policy(state, settings)
    counts_after = generation_counts(state)
    for record in records:
        if record.get("status") != "applied" or record["scene_id"] not in previous:
            continue
        scene = _scene_by_id(state, record["scene_id"])
        if scene is None:
            continue
        after = _scene_snapshot(scene)
        record["after"] = after
        if record["action"] == "replace_media":
            new_identity = after["media_identity"]
            unsafe = reveal_problems(scene, state)
            if unsafe:
                # A repair may never introduce an earlier answer leak.
                scene.update(copy.deepcopy(previous[record["scene_id"]]))
                record.update(status="reverted", blocked_reason="replacement_not_reveal_safe", after=_scene_snapshot(scene))
            elif not new_identity or new_identity == record["before"]["media_identity"] or new_identity in (scene.get("rejected_media_identities") or []):
                record.update(status="no_alternative")
            record["generation"] = dict((scene.get("visual_director") or {}).get("generation") or {})
    record_budget = {
        "auto_generated_before": counts_before["auto_generated_images"],
        "auto_generated_after": counts_after["auto_generated_images"],
        "project_limit": policy["max_auto_generated_images_per_project"],
    }
    for record in records:
        record["generation_budget"] = record_budget
    return records


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _compact(review: _Review) -> dict[str, Any]:
    return {
        "pass": review.pass_index,
        "status": review.status,
        "overall_score": review.score(),
        "issues": review.issues(),
        "scene_scores": {row.scene_id: record["score"] for row, record in zip(review.rows, review.scene_records(), strict=True)},
    }


def _rating(row: _Row | None, category: str) -> str | None:
    return None if row is None else row.dimensions.get(category, {}).get("rating")


def _evaluate(records: list[dict[str, Any]], before: _Review, after: _Review) -> None:
    after_ids = {issue["id"] for issue in after.issues()}
    for record in records:
        if record.get("status") not in {"applied", "no_alternative"}:
            continue
        targeted = set(record.get("issue_ids") or [])
        resolved = sorted(targeted - after_ids)
        remaining = sorted(targeted & after_ids)
        after_row = after.row(record["scene_id"])
        new_reveal = [
            issue["id"] for issue in (after_row.issues if after_row else [])
            if issue["category"] == "reveal_safety" and issue["id"] not in targeted
        ]
        before_row = before.row(record["scene_id"])
        record["result"] = {
            "resolved": resolved,
            "remaining": remaining,
            "new_reveal_issues": new_reveal,
            "before": {category: _rating(before_row, category) for category in record.get("categories") or []},
            "after": {category: _rating(after_row, category) for category in record.get("categories") or []},
            "score_before": before_row.semantic_score if before_row else None,
            "score_after": after_row.semantic_score if after_row else None,
        }
        if new_reveal:
            record["outcome"] = "regressed"
        elif resolved and not remaining:
            record["outcome"] = "resolved"
        elif resolved:
            record["outcome"] = "partially_resolved"
        else:
            record["outcome"] = "unresolved"
        record["improved"] = record["outcome"] in {"resolved", "partially_resolved"}


def _summary(status: str, repaired: list[str], remaining: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    if status == "issues_remain":
        count = len(remaining)
        label = f"{count} issue remains" if count == 1 else f"{count} issues remain"
        if repaired:
            label = f"Repaired {len(repaired)} scene{'s' if len(repaired) != 1 else ''} · {label}"
    elif status == "repaired":
        label = f"Repaired {len(repaired)} scene{'s' if len(repaired) != 1 else ''}"
    elif status == "passed_with_warnings":
        label = f"Passed · {len(warnings)} note{'s' if len(warnings) != 1 else ''}"
    elif status == "passed":
        label = "Passed"
    else:
        label = "Not reviewed"
    return {"label": label, "repaired_scene_count": len(repaired), "remaining_issue_count": len(remaining), "warning_count": len(warnings)}


def disabled_review(revision: int) -> dict[str, Any]:
    """Record that this render was not reviewed (never an earlier render's review)."""
    return {"version": VERSION, "status": "disabled", "revision": revision, "reviewed_at": _now(), "summary": _summary("disabled", [], [], [])}


def run_final_quality_review(
    state: dict[str, Any],
    *,
    project_id: str,
    revision: int,
    settings: Settings,
    rerender: Callable[[dict[str, Any]], Any] | None = None,
    prepare_media: Callable[[dict[str, Any]], Any] | None = None,
    verifier: Any | None = None,
    max_passes: int | None = None,
) -> dict[str, Any]:
    """Review the rendered file, run at most ``max_passes`` repair renders, persist the result.

    ``rerender`` renders the (repaired) state to the same revision file and
    refreshes ``state["render"]``; ``prepare_media`` is the Visual Director's
    media pass.  Without either, the review only reports.  Never raises for
    review problems; a failed repair render restores the original render.
    """
    passes_allowed = max(0, min(MAX_REPAIR_PASSES_LIMIT, max_repair_passes(settings) if max_passes is None else int(max_passes)))
    reviewer = {"frames": "ffmpeg", "semantic": "metadata", "vision": "disabled"}
    if not getattr(settings, "final_critic_enabled", True):
        state["final_quality_review"] = disabled_review(revision)
        return state["final_quality_review"]
    verifier = verifier if verifier is not None else get_visual_verifier()
    vision = get_vision_critic(settings)
    if vision is not None:
        reviewer["vision"] = getattr(vision, "name", "optional")

    def review_pass(index: int) -> _Review:
        review = _Review(state, project_id=project_id, revision=revision, settings=settings, verifier=verifier, pass_index=index, vision_critic=vision)
        if review.collect():
            review.evaluate()
        return review

    review = review_pass(0)
    if review.status != "reviewed":
        state["final_quality_review"] = {
            "version": VERSION, "status": "not_reviewed", "reason": review.reason, "revision": revision,
            "reviewed_at": _now(), "reviewer": reviewer, "repair_pass_count": 0, "max_repair_passes": passes_allowed,
            "scenes": [], "issues": [], "repairs": [], "unresolved": [], "summary": _summary("not_reviewed", [], [], []),
        }
        return state["final_quality_review"]
    initial = review
    history = [_compact(review)]
    repairs: list[dict[str, Any]] = []
    pass_count = 0
    render_error: str | None = None
    while pass_count < passes_allowed and rerender is not None and prepare_media is not None:
        planned = plan_repairs(review, repairs)
        if not any(action["status"] == "planned" for action in planned):
            repairs.extend({**action, "pass": pass_count + 1} for action in planned)
            break
        video = _render_file(state, settings, project_id)
        snapshot = copy.deepcopy(state)
        backup = None
        if video is not None:
            backup = settings.render_root.resolve() / project_id / "critic" / f"v{revision}" / f"before-pass{pass_count + 1}.mp4"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(video, backup)
        try:
            records = apply_repairs(planned, state, prepare_media=prepare_media, settings=settings, pass_index=pass_count + 1)
            if not any(record.get("status") == "applied" for record in records):
                # Nothing on screen changed, so no repair render is needed.
                for record in records:
                    record.update(outcome="unresolved", improved=False)
                repairs.extend(records)
                break
            rerender(state)
        except Exception as exc:  # noqa: BLE001 - the original render stays valid
            state.clear()
            state.update(snapshot)
            if backup is not None and video is not None and backup.is_file():
                shutil.copy2(backup, video)
            render_error = str(exc)[:300] or type(exc).__name__
            repairs.extend({**action, "pass": pass_count + 1, "status": "failed", "blocked_reason": "repair_render_failed"} for action in planned if action["status"] == "planned")
            break
        finally:
            if backup is not None:
                backup.unlink(missing_ok=True)
        pass_count += 1
        after = review_pass(pass_count)
        if after.status != "reviewed":
            render_error = after.reason
            repairs.extend(records)
            break
        _evaluate(records, review, after)
        repairs.extend(records)
        history.append(_compact(after))
        review = after
    final_issues = review.issues()
    errors = [issue for issue in final_issues if issue["severity"] == "error"]
    warnings = [issue for issue in final_issues if issue["severity"] == "warning"]
    repaired = sorted({record["scene_id"] for record in repairs if record.get("outcome") in {"resolved", "partially_resolved"}})
    changed = sorted({record["scene_id"] for record in repairs if record.get("status") in {"applied", "no_alternative"} and record.get("after") and record.get("after") != record.get("before")})
    if errors:
        status = "issues_remain"
    elif repaired:
        status = "repaired"
    elif warnings:
        status = "passed_with_warnings"
    else:
        status = "passed"
    result = {
        "version": VERSION,
        "status": status,
        "revision": revision,
        "reviewed_revision": revision,
        "reviewed_at": _now(),
        "reviewer": {**reviewer, "semantic": "openclip" if review.semantic_ready else "metadata"},
        "render": review.probe,
        "overall_score": review.score(),
        "initial_score": initial.score(),
        "repair_pass_count": pass_count,
        "max_repair_passes": passes_allowed,
        "scenes": review.scene_records(),
        "issues": final_issues,
        "initial_issues": initial.issues() if pass_count else [],
        "repairs": repairs,
        "unresolved": [issue["id"] for issue in errors + warnings],
        "repaired_scenes": repaired,
        "changed_scenes": changed,
        "history": history,
        "user_overrides": [],
        "summary": _summary(status, repaired, errors, warnings),
    }
    if render_error:
        result["repair_error"] = render_error
    state["final_quality_review"] = result
    return result


def record_manual_change(state: dict[str, Any], scene: dict[str, Any], reason: str) -> None:
    """Remember that the user changed a reviewed scene (analytics and UI)."""
    review = state.get("final_quality_review")
    if not isinstance(review, dict):
        return
    scene_id = str(scene.get("id") or "")
    related = [issue["id"] for issue in review.get("issues") or [] if isinstance(issue, dict) and issue.get("scene_id") == scene_id]
    repaired = any(isinstance(item, dict) and item.get("scene_id") == scene_id and item.get("status") == "applied" for item in review.get("repairs") or [])
    review.setdefault("user_overrides", []).append({
        "scene_id": scene_id,
        "reason": reason,
        "at": _now(),
        "media_identity": (scene.get("media") or {}).get("identity"),
        "addressed_issue_ids": related,
        "after_auto_repair": repaired,
    })
