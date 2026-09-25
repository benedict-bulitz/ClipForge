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
import tempfile
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
from .overlay_copy import (
    OVERLAY_SEMANTIC_CODES,
    assess_overlay,
    explanatory_overlay,
    fact_statement,
)
from .renderer import caption_band, ffmpeg_path
from .simple_graphics import normalise_graphic_spec, normalise_overlay_spec
from .smart_crop import crop_windows
from .thumbnails import _asset_quality, extract_video_frame
from .triple_hook import SOURCE as TRIPLE_HOOK_SOURCE
from .triple_hook import (
    hook_text_leaks,
    is_hook_scene,
    state_plan,
    validate_hook_text,
    visual_summary,
)
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

VERSION = 2
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
    "overlay_semantics",
    "hook_alignment",
)

# Thresholds reuse the media gate's OpenCLIP scale; rendered frames include
# captions/overlays, so "poor" is the gate's hard floor, not its pass mark.
SEMANTIC_GOOD = SCENE_VISUAL_THRESHOLD
SEMANTIC_POOR = VISUAL_THRESHOLD
# Story-critical scenes (primary answer, final payoff) need a clear match.
SEMANTIC_STRONG = SCENE_VISUAL_THRESHOLD + 0.02
CRITICAL_ROLES = {"primary_answer", "final_payoff"}
SUBJECT_DROP = 0.05
MOTION_SPREAD = 0.06
# Explicit occupancy limit of a drawn overlay in the final 9:16 frame (its
# bounding box and its opaque pixels, measured on the rendered overlay layer).
OVERLAY_MAX_BBOX, OVERLAY_MAX_COVERAGE = 0.10, 0.06
NEAR_IDENTICAL_DIFF = 18.0  # mean grey-level difference of two 32x56 thumbnails
CROP_SHIFT = 0.05
FRAME_WIDTH = 360
_REUSE_STATUSES = {"related_media_reused", "real_media_reused", "generated_media_reused"}
_CONTINUED_STATUSES = {"block_visual_continued"}
_MEDIA_ACTIONS = {"replace_media", "convert_graphic_to_overlay", "continue_base_visual"}
# Issues that drive a repair whatever their severity (a loose match is not "done").
REPAIRABLE_CODES = {
    "wrong_media", "weak_media", "text_heavy", "unusable_frame", "subject_lost_in_render", "motion_loses_subject",
    "overlay_too_dominant", "overlay_hits_captions", "fullscreen_graphic_with_base", "near_identical_graphics",
    "accidental_repeat", "repeats_primary_answer_visual", "answer_without_own_visual", "payoff_generic_reuse",
    "unnecessary_asset_switch",
    # Triple Hook V2 opening checks with a safe targeted repair.
    "dead_opening_frame", "hook_overlay_duplicates_narration", "hook_overlay_duplicates_captions",
    "hook_overlay_unreadable",
    *OVERLAY_SEMANTIC_CODES,
}
# Hook overlay problems are repaired by removing the label (never by rewriting it).
HOOK_OVERLAY_CODES = {"hook_overlay_duplicates_narration", "hook_overlay_duplicates_captions", "hook_overlay_unreadable"}
OPENING_DEAD_AIR_SECONDS = 1.0


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
        self.hook_summary: dict[str, Any] | None = None

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

    def required_score(self, row: _Row) -> float:
        """Rendered match a scene needs; stricter for the answer and the payoff."""
        story = row.dimensions.get("_story") or self._story_context(row)
        critical = story.get("visual_role") in CRITICAL_ROLES or story.get("is_primary_answer") or story.get("is_final_payoff")
        return SEMANTIC_STRONG if critical else SEMANTIC_GOOD

    def critical(self, row: _Row) -> bool:
        return self.required_score(row) > SEMANTIC_GOOD

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
            required = self.required_score(row)
            rating = GOOD if score >= required else WARNING if score >= SEMANTIC_POOR else POOR
            row.dimensions["semantic_match"] = {
                "rating": rating, "source": "openclip_rendered_frames", "score": round(float(score), 4), "required": round(required, 4),
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
        rating = POOR if dark or text_heavy else WARNING if flat else GOOD
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
        rating = POOR if bbox_area >= OVERLAY_MAX_BBOX or coverage >= OVERLAY_MAX_COVERAGE else GOOD
        row.dimensions["overlay_quality"] = {
            "rating": rating, "bbox_area": round(bbox_area, 4), "coverage": round(coverage, 4),
            "limit": {"bbox_area": OVERLAY_MAX_BBOX, "coverage": OVERLAY_MAX_COVERAGE},
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

    def _overlay_semantics(self, row: _Row) -> None:
        """Does the drawn text teach something?  Size and layout do not answer that.

        Checks the fact's planned overlay (all steps) and every drawn
        element against the complete Story Arc fact and the narration heard
        in this scene.
        """
        drawn = [item.get("spec") for item in row.entry.get("overlays") or [] if isinstance(item, dict) and isinstance(item.get("spec"), dict)]
        graphic = (row.scene.get("media") or {}).get("graphic") if row.graphic else None
        if not drawn and not isinstance(graphic, dict):
            row.dimensions["overlay_semantics"] = {"rating": NOT_APPLICABLE}
            return
        source = fact_statement(row.scene, self.state)
        narration = " ".join(str(row.scene.get("narration") or "").split())
        planned = row.strategy.get("overlay_spec") if isinstance(row.strategy.get("overlay_spec"), dict) else None
        specs = [spec for spec in ([planned] if planned and drawn else []) + drawn + ([graphic] if isinstance(graphic, dict) else []) if spec]
        found: dict[str, dict[str, str]] = {}
        for spec in specs:
            for issue in assess_overlay(spec, source=source, narration=narration):
                found.setdefault(issue["code"], issue)
        row.dimensions["overlay_semantics"] = {
            "rating": POOR if found else GOOD,
            "copy": [spec.get("steps") or [spec.get("left"), spec.get("right")] if spec.get("kind") != "label" else [spec.get("text")] for spec in specs][:2],
            "source": "full_fact" if source else "narration",
            "issues": sorted(found),
        }
        for code, issue in found.items():
            self._issue(row, "overlay_semantics", code, "error", f"The {'graphic' if row.graphic else 'overlay'} text of scene {row.number}: {issue['message']}", element=issue.get("element"))

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
            self._overlay_semantics(row)
            self._reveal(row, story, answer_identities)
            self._graphic(row, story)
            self._story_alignment(row, story, answer_rows)
            self._vision(row, story)
        self._adjacent()
        for row in self.rows:
            self._issues_from_dimensions(row)
        self._hook()
        self.status = "reviewed"

    # -- graphics, story roles, adjacency ---------------------------------

    def base_candidates(self, row: _Row, minimum: float | None = None) -> list[tuple[float, _Row]]:
        """Reveal-safe project visuals whose rendered frames fit this scene (best first).

        Never reuse merely because a visual exists: the candidate's own rendered
        frames must reach this scene's threshold (stricter for story-critical
        scenes), its role must be compatible and it must not be this scene's
        current (rejected) visual.
        """
        minimum = self.required_score(row) if minimum is None else minimum
        rejected = {str(value) for value in row.scene.get("rejected_media_identities") or []} | ({row.identity} if row.identity else set())
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
                score = minimum + overlap if fits else 0.0
            if score >= minimum:
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
        # A reused visual is acceptable only when it clearly fits this scene.
        fits = semantic == GOOD
        if role in {"secondary_insight", "final_payoff"} and not story.get("is_primary_answer"):
            collapsed = [
                other for other in answer_rows
                if other is not row and other.identity and other.identity == row.identity
                and other.scene.get("block_id") != row.scene.get("block_id")
            ]
            if collapsed:
                findings.append(("repeats_primary_answer_visual", "error", "This scene reuses the primary answer's visual, so it does not read as its own point."))
        if role == "final_payoff" and status in _REUSE_STATUSES and not fits:
            findings.append(("payoff_generic_reuse", "error", "The final payoff falls back to a reused project visual that does not fit it."))
        if role == "primary_answer" and status in _REUSE_STATUSES and not fits:
            findings.append(("answer_without_own_visual", "error", "The primary answer reuses another fact's visual that does not fit it."))
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
                if _progresses(earlier, later) and later.dimensions.get("semantic_match", {}).get("rating") == GOOD:
                    # Same base, but an evolving overlay communicates something new.
                    later.dimensions["repetition"] = {"rating": GOOD, "reason": "overlay_progression", "with": earlier.scene_id}
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
        elif semantic.get("rating") == WARNING and subject.get("rating") != POOR:
            self._issue(
                row, "semantic_match", "weak_media", "error" if self.critical(row) else "warning",
                f"Scene {row.number}'s visual only loosely matches the narration.", score=semantic.get("score"), source=semantic.get("source"),
            )
        if subject.get("rating") == POOR:
            self._issue(row, "subject_visibility", "subject_lost_in_render", "error", f"The subject of scene {row.number} is visible in the source but lost after crop, motion or overlay.", drop=subject.get("drop"))
        quality = dims.get("visual_quality", {})
        if quality.get("nearly_black"):
            self._issue(row, "visual_quality", "unusable_frame", "error", f"Scene {row.number} renders nearly black.")
        elif quality.get("text_heavy"):
            self._issue(row, "visual_quality", "text_heavy", "error", f"Scene {row.number} is dominated by text instead of a visual.")
        elif quality.get("rating") == WARNING:
            self._issue(row, "visual_quality", "low_contrast", "warning", f"Scene {row.number} renders flat.")
        if dims.get("motion", {}).get("rating") == POOR:
            self._issue(row, "motion", "motion_loses_subject", "error", f"The still motion of scene {row.number} moves the subject out of view.")
        overlay = dims.get("overlay_quality", {})
        if overlay.get("rating") == POOR:
            self._issue(row, "overlay_quality", "overlay_too_dominant", "error", f"The overlay of scene {row.number} covers too much of the base visual.", bbox_area=overlay.get("bbox_area"), coverage=overlay.get("coverage"))
        if dims.get("caption_overlay_collision", {}).get("rating") == POOR:
            self._issue(row, "caption_overlay_collision", "overlay_hits_captions", "error", f"The overlay of scene {row.number} collides with the captions.")
        graphic = dims.get("graphic_usage", {})
        if graphic.get("rating") == POOR:
            self._issue(row, "graphic_usage", "fullscreen_graphic_with_base", "error", f"Scene {row.number} is a full-screen graphic although a fitting base visual exists.", base_scene=graphic.get("base_candidate"))
        elif graphic.get("rating") == WARNING:
            self._issue(row, "graphic_usage", "fullscreen_graphic_unjustified", "warning", f"Scene {row.number} uses a full-screen graphic without a recorded reason.")
        if user_locked_visual(row.scene) and any(issue["severity"] == "error" for issue in row.issues):
            self._issue(row, "semantic_match" if semantic.get("rating") == POOR else row.issues[0]["category"], "manual_asset_weak", "warning", f"The visual you chose for scene {row.number} appears weak; it was not replaced automatically.")

    # -- Triple Hook V2: the rendered opening against the persisted plan -----

    def _hook(self) -> None:
        """Does the rendered opening deliver the selected triple hook?

        Reads the same persisted ``script.triple_hook`` that drove the
        script, the Visual Director and the overlay.  Visual match and subject
        visibility come from the per-scene dimensions (judged against the hook's
        own visual intent); this adds the hook-only checks: the on-screen hook
        was drawn, readable and not a copy of narration or captions, no reveal
        leaked in the spoken opening, and the video does not open on a dead
        frame or silence.  Only overlay removal and media replacement are ever
        repaired automatically; a verbal-hook failure is reported for
        regeneration, never re-voiced here.
        """
        plan = state_plan(self.state)
        for row in self.rows:
            row.dimensions.setdefault("hook_alignment", {"rating": NOT_APPLICABLE})
        if plan is None:
            return
        rows = [row for row in self.rows if row.scene and is_hook_scene(row.scene, self.state)]
        first = self.rows[0] if self.rows else None
        planned_text = str(plan.get("on_screen_text_hook") or "").strip()
        blocks = (self.state.get("script") or {}).get("blocks") or []
        spoken = next((str(block.get("text") or "") for block in blocks if isinstance(block, dict) and str(block.get("role") or "").casefold() == "hook"), "")
        captions = [item for item in (self.state.get("captions") or {}).get("items") or [] if isinstance(item, dict)]
        drawn_texts: list[str] = []
        for index, row in enumerate(rows):
            findings: list[tuple[str, str, str, dict[str, Any]]] = []
            labels = [
                str(item["spec"].get("text") or "") for item in row.entry.get("overlays") or []
                if isinstance(item, dict) and isinstance(item.get("spec"), dict) and item["spec"].get("kind") == "label"
            ]
            drawn_texts.extend(labels)
            adjustments = row.scene.get("render_adjustments") if isinstance(row.scene.get("render_adjustments"), dict) else {}
            removed = (adjustments.get("overlay") or {}).get("mode") == "remove"
            if planned_text and not labels and not removed and index == 0:
                findings.append(("hook_overlay_missing", "warning", "The planned on-screen hook was not drawn over the opening.", {}))
            start, end = float(row.entry.get("start") or 0), float(row.entry.get("end") or 0)
            heard = " ".join(
                str(item.get("text") or "") for item in captions
                if float(item.get("start") or 0) < end and float(item.get("end") or 0) > start
            )
            for label in labels:
                reason = validate_hook_text(label, spoken or str(plan.get("verbal_hook") or ""), self.state)
                if reason == "on_screen_duplicates_verbal":
                    findings.append(("hook_overlay_duplicates_narration", "error", "The on-screen hook repeats the spoken hook.", {"text": label}))
                elif reason in {"on_screen_too_long", "on_screen_fragment"}:
                    findings.append(("hook_overlay_unreadable", "error", "The on-screen hook is too long or not a complete phrase.", {"text": label}))
                elif heard and validate_hook_text(label, heard, self.state) == "on_screen_duplicates_verbal":
                    findings.append(("hook_overlay_duplicates_captions", "error", "The on-screen hook repeats the captions shown at the same time.", {"text": label}))
                if not (row.dimensions.get("_story") or {}).get("reveal_allowed", True) and hook_text_leaks(self.state, label):
                    self._issue(row, "reveal_safety", "hook_overlay_reveals_answer", "error", "The on-screen hook reveals the protected answer before the Story Arc reveal.", component="overlay")
            if index == 0:
                if spoken and hook_text_leaks(self.state, spoken):
                    findings.append(("hook_verbal_reveals_answer", "error", "The spoken hook reveals the protected answer; regenerate the hook.", {}))
                verbal = str(plan.get("verbal_hook") or "").strip()
                if verbal and spoken and " ".join(verbal.casefold().split()) != " ".join(spoken.casefold().split()):
                    findings.append(("hook_verbal_drift", "warning", "The spoken opening differs from the selected hook plan.", {}))
                intent = row.scene.get("visual_intent") if isinstance(row.scene.get("visual_intent"), dict) else {}
                if plan.get("status") != "fallback" and intent.get("source") != TRIPLE_HOOK_SOURCE:
                    findings.append(("hook_visual_not_applied", "warning", "The opening scene is not using the selected hook visual.", {}))
            semantic = row.dimensions.get("semantic_match", {})
            subject = row.dimensions.get("subject_visibility", {})
            ratings = [semantic.get("rating"), subject.get("rating"), row.dimensions.get("overlay_quality", {}).get("rating")]
            for code, severity, message, evidence in findings:
                self._issue(row, "hook_alignment", code, severity, f"Opening (scene {row.number}): {message}", **evidence)
            worst = POOR if any(severity == "error" for _c, severity, _m, _e in findings) or POOR in ratings else WARNING if findings or WARNING in ratings else GOOD
            row.dimensions["hook_alignment"] = {
                "rating": worst,
                "hook_id": plan.get("hook_id"),
                "visual_match": semantic.get("rating"),
                "visual_score": semantic.get("score"),
                "subject_visibility": subject.get("rating"),
                "overlay_text": labels,
                "findings": [code for code, _s, _m, _e in findings],
            }
        if first is not None and first.images:
            reasons = _asset_quality(first.images[0].width, first.images[0].height, first.images[0])[1]
            if "nearly_black" in reasons:
                self._issue(first, "hook_alignment", "dead_opening_frame", "error", "The video opens on a dead (nearly black) frame.")
                first.dimensions["hook_alignment"] = {**first.dimensions.get("hook_alignment", {}), "rating": POOR, "dead_opening_frame": True}
        timing = (self.state.get("captions") or {}).get("timing")
        opening_word = min((float(item.get("start") or 0) for item in captions if str(item.get("text") or "").strip()), default=None)
        if first is not None and timing == "word_aligned" and opening_word is not None and opening_word > OPENING_DEAD_AIR_SECONDS:
            self._issue(first, "hook_alignment", "opening_dead_air", "warning", f"The narration starts only after {opening_word:.1f}s.", seconds=round(opening_word, 2))
        hook_issues = [issue for row in rows + ([first] if first is not None and first not in rows else []) for issue in row.issues if issue["category"] == "hook_alignment" or issue["code"] == "hook_overlay_reveals_answer"]
        ratings = [row.dimensions.get("hook_alignment", {}).get("rating") for row in rows]
        self.hook_summary = {
            "hook_id": plan.get("hook_id"),
            "status": plan.get("status"),
            "planned": {
                "verbal_hook": plan.get("verbal_hook"),
                "on_screen_hook": planned_text or None,
                "visual": visual_summary(plan.get("visual_hook")),
                "strategy": plan.get("selected_strategy"),
            },
            "rendered": {
                "scene_ids": [row.scene_id for row in rows],
                "spoken_opening": spoken,
                "overlay_text": list(dict.fromkeys(drawn_texts)),
                "visual_match": [row.dimensions.get("semantic_match", {}).get("rating") for row in rows],
            },
            "rating": POOR if POOR in ratings or any(issue["severity"] == "error" for issue in hook_issues) else WARNING if WARNING in ratings or hook_issues else GOOD if rows else UNAVAILABLE,
            "issue_ids": [issue["id"] for issue in hook_issues],
        }

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


def _overlay_signature(row: _Row) -> list[Any]:
    return [item.get("spec") for item in row.entry.get("overlays") or [] if isinstance(item, dict)]


def _progresses(earlier: _Row, later: _Row) -> bool:
    """The later scene draws its own, different information over the shared base."""
    later_overlays = _overlay_signature(later)
    return bool(later_overlays) and later_overlays != _overlay_signature(earlier)


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
    for scene, other_facts in ((first, second_facts), (second, first_facts)):
        # The opening hook shows the planned visual of a fact the body then
        # explains: one subject continued, not an accidental repeat.
        intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
        if {str(value) for value in intent.get("source_fact_ids") or []} & other_facts:
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
    from .renderer import overlay_footprint
    from .simple_graphics import GraphicSpecError, render_overlay

    timeline = state.get("timeline") or {}
    try:
        with tempfile.TemporaryDirectory(prefix="clipforge-critic-") as temp:
            path = render_overlay(spec, Path(temp) / "overlay.png", width=int(timeline.get("width") or 1080), height=int(timeline.get("height") or 1920))
            footprint = overlay_footprint(path)
    except (GraphicSpecError, OSError, ValueError):
        return False
    return float(footprint.get("bbox_area") or 0) >= OVERLAY_MAX_BBOX or float(footprint.get("coverage") or 0) >= OVERLAY_MAX_COVERAGE


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


def _repairable(issue: dict[str, Any]) -> bool:
    return issue["severity"] == "error" or issue["code"] in REPAIRABLE_CODES


def plan_repairs(review: _Review, earlier: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One targeted action per scene, chosen from that scene's own issues.

    Story-critical scenes are planned first so they get the bounded paid
    fallback before anything else.  A media action that already found no
    alternative (or was blocked) earlier in this run is not retried.
    """
    exhausted = {
        (record.get("scene_id"), record.get("action"))
        for record in earlier or []
        if record.get("status") in {"no_alternative", "blocked", "reverted", "failed", "unresolved"}
    }
    actions: list[dict[str, Any]] = []
    by_scene: dict[str, list[dict[str, Any]]] = {}
    for issue in review.issues():
        if _repairable(issue):
            by_scene.setdefault(issue["scene_id"], []).append(issue)

    def priority(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, int]:
        row = review.row(item[0])
        errors = any(issue["severity"] == "error" for issue in item[1])
        return (0 if row is not None and review.critical(row) else 1, 0 if errors else 1, row.number if row else 0)

    for scene_id, issues in sorted(by_scene.items(), key=priority):
        row = review.row(scene_id)
        scene = _scene_by_id(review.state, scene_id)
        codes = {issue["code"]: issue for issue in issues}
        base = {"scene_id": scene_id, "scene_number": row.number if row else None, "issue_ids": [issue["id"] for issue in issues], "categories": sorted({issue["category"] for issue in issues})}
        if row is None or scene is None:
            actions.append({**base, "action": "report_only", "status": "blocked", "blocked_reason": "scene_changed_after_render"})
            continue
        locked = user_locked_visual(scene)
        composition = _composition_action(review, row, codes, scene)
        media_action = None if composition is not None and composition.get("reframe") else _media_action(review, row, codes)
        if media_action is not None and (scene_id, media_action["action"]) in exhausted:
            actions.append({**base, **media_action, "status": "blocked", "blocked_reason": "no_alternative_found_earlier"})
            media_action = None
        if media_action is not None:
            if locked:
                actions.append({**base, **media_action, "status": "blocked", "blocked_reason": "user_locked_visual",
                                "message": "Manual asset appears weak; it was not replaced automatically."})
            else:
                actions.append({**base, **media_action, "status": "planned"})
                continue
        if composition is not None:
            actions.append({**base, **composition, "status": "planned", "allow_media_escalation": not locked})
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
    for code, reason in (
        ("accidental_repeat", "repetition"), ("repeats_primary_answer_visual", "story_alignment"),
        ("answer_without_own_visual", "story_alignment"), ("payoff_generic_reuse", "story_alignment"),
    ):
        if code in codes:
            return {"action": "replace_media", "reason": reason, "reject": [row.identity] if row.identity else []}
    if "unnecessary_asset_switch" in codes:
        partner = review.row(codes["unnecessary_asset_switch"]["evidence"].get("with_scene") or "")
        if partner is not None and partner.identity:
            return {"action": "continue_base_visual", "reason": "visual_continuity", "base_scene_id": partner.scene_id, "base_identity": partner.identity}
    base_is_text = "text_heavy" in codes and not row.entry.get("overlays")
    if {"wrong_media", "weak_media", "unusable_frame", "dead_opening_frame"} & set(codes) or base_is_text:
        # A same-fact visual that already fits keeps continuity; otherwise the
        # scene-level escalation chain (real alternative, generated image,
        # fitting base + overlay, planned graphic).
        same_block = [
            candidate for _score, candidate in review.base_candidates(row)
            if candidate.scene.get("block_id") and candidate.scene.get("block_id") == row.scene.get("block_id")
        ]
        reason = "text_heavy" if base_is_text else "semantic_match"
        if same_block:
            return {"action": "continue_base_visual", "reason": reason, "base_scene_id": same_block[0].scene_id, "base_identity": same_block[0].identity}
        return {"action": "replace_media", "reason": reason, "reject": [row.identity] if row.identity else []}
    return None


def _composition_action(review: _Review, row: _Row, codes: dict[str, dict[str, Any]], scene: dict[str, Any]) -> dict[str, Any] | None:
    current = scene.get("render_adjustments") if isinstance(scene.get("render_adjustments"), dict) else {}
    overlay_now = current.get("overlay") if isinstance(current.get("overlay"), dict) else {}
    overlay: dict[str, Any] = {}
    reasons: list[str] = []
    if any(issue["category"] == "reveal_safety" and issue["evidence"].get("component") == "overlay" for issue in codes.values()):
        overlay["mode"] = "remove"
        reasons.append("reveal_safety")
    if "overlay_too_dominant" in codes or ("text_heavy" in codes and row.entry.get("overlays")):
        # Shorter copy, no panel, active step only: the base stays dominant.
        overlay.setdefault("mode", "compact")
        reasons.append("overlay_quality" if "overlay_too_dominant" in codes else "text_heavy")
    if "overlay_hits_captions" in codes:
        band = caption_band(review.state) or {}
        overlay["placement"] = "upper" if band.get("position") == "lower" or float(band.get("top", 1)) > 0.5 else "lower"
        if overlay_now.get("placement") == overlay["placement"]:
            overlay.setdefault("mode", "compact")
        reasons.append("caption_overlay_collision")
    rewrite = bool(set(codes) & set(OVERLAY_SEMANTIC_CODES))
    hook_label = ((scene.get("visual_director") or {}).get("overlay_spec") or {}).get("source") == TRIPLE_HOOK_SOURCE
    if HOOK_OVERLAY_CODES & set(codes) or (rewrite and hook_label):
        # The on-screen hook is optional: a label that repeats or cannot be
        # read is removed, never replaced by other copy.
        overlay["mode"] = "remove"
        reasons.append("hook_overlay")
        rewrite = False
    if rewrite:
        # Meaningless copy: rewrite only the overlay text from the full fact.
        reasons.append("overlay_semantics")
    lost = "subject_lost_in_render" in codes or "motion_loses_subject" in codes
    if lost and row.entry.get("overlays") and row.dimensions.get("overlay_quality", {}).get("coverage", 0) >= OVERLAY_MAX_COVERAGE / 2:
        overlay.setdefault("mode", "compact")
    merged_overlay = {**overlay_now, **overlay}
    action: dict[str, Any] = {"action": "adjust_composition", "adjustments": {}}
    if overlay and merged_overlay != overlay_now:
        action["adjustments"]["overlay"] = merged_overlay
    if lost and not row.graphic:
        # Reframe before replacing: crop, then less motion, then a frozen still.
        action["reframe"] = True
        reasons.append("subject_visibility" if "subject_lost_in_render" in codes else "motion")
    if rewrite:
        action["rewrite_overlay"] = True
    if not action["adjustments"] and not action.get("reframe") and not rewrite:
        return None
    action["reason"] = ",".join(dict.fromkeys(reasons))
    return action


# ---------------------------------------------------------------------------
# Applying repairs: bounded scene-level escalation through existing systems
# ---------------------------------------------------------------------------

_SNAPSHOT_KEYS = ("media", "asset_status", "visual_director", "visual_continuity", "fallback_reason", "overlays", "render_adjustments", "media_repair")


def _scene_snapshot(scene: dict[str, Any]) -> dict[str, Any]:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    return {
        "media_identity": media.get("identity"),
        "media_source": media_source(media) if media else None,
        "asset_status": scene.get("asset_status"),
        "composition": (scene.get("visual_director") or {}).get("composition"),
        "adjustments": copy.deepcopy(scene.get("render_adjustments") or {}),
    }


def _capture(scene: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({key: scene.get(key) for key in _SNAPSHOT_KEYS})


def _restore(scene: dict[str, Any], saved: dict[str, Any]) -> None:
    for key, value in copy.deepcopy(saved).items():
        if value is None:
            scene.pop(key, None)
        else:
            scene[key] = value


class _Repairer:
    """Applies one scene's action: each candidate is trial-rendered and scored first."""

    def __init__(self, review: _Review, *, prepare_media: Callable[[dict[str, Any]], Any], generator: Any | None, project_id: str):
        self.review = review
        self.state = review.state
        self.settings = review.settings
        self.prepare_media = prepare_media
        self.generator = generator
        self.project_id = project_id
        self._trials = 0
        # Visuals accepted in this pass per fact/block (for intentional continuity).
        self.accepted: dict[str, tuple[str, str]] = {}

    # -- trial render of one scene (the real renderer, one short segment) ----

    def trial(self, scene: dict[str, Any], row: _Row) -> dict[str, Any] | None:
        """Score what this scene would render as now; ``None`` without a local visual model."""
        from .renderer import RenderUnavailable, _create_visual_segment

        if not self.review.semantic_ready:
            return None
        ffmpeg = ffmpeg_path()
        scenes = self.state.get("scenes") or []
        index = next((position for position, item in enumerate(scenes) if item is scene), 0)
        duration = max(0.8, min(3.0, float(scene.get("end") or 0) - float(scene.get("start") or 0)))
        self._trials += 1
        images: list[Image.Image] = []
        kept = {key: copy.deepcopy(scene.get(key)) for key in ("smart_crop", "still_motion", "overlay_render")}
        try:
            with tempfile.TemporaryDirectory(prefix="clipforge-critic-trial-") as name:
                temp = Path(name)
                segment = _create_visual_segment(ffmpeg, self.state, scene, index, duration, temp, self.settings)
                for position, timestamp in enumerate(scene_sample_times(0.0, duration)):
                    frame = temp / f"trial-{position}.jpg"
                    if extract_video_frame(ffmpeg, segment, timestamp, frame, width=FRAME_WIDTH):
                        with Image.open(frame) as image:
                            images.append(image.convert("RGB"))
        except (RenderUnavailable, OSError, ValueError):
            return {"score": 0.0, "min_frame": 0.0, "status": "trial_render_failed"}
        finally:
            scene.pop("render_segment", None)
            for key, value in kept.items():
                if value is None:
                    scene.pop(key, None)
                else:
                    scene[key] = value
        identity = str((scene.get("media") or {}).get("identity") or "none")
        result = self.review._score_frames(images, self.review._texts(row), f"critic-trial:{row.scene_id}:{identity}:{self._trials}")
        if result is None:
            return None
        score = float(result.scene_score if result.scene_score is not None else result.score)
        frames = [float(value) for value in result.frame_scores] or [score]
        return {"score": round(score, 4), "min_frame": round(min(frames), 4), "presentation_risk": bool(result.presentation_risk)}

    # -- media steps -------------------------------------------------------

    def _strategy(self, scene: dict[str, Any]) -> dict[str, Any]:
        from .visual_director import plan_scene_strategy

        strategy = scene.get("visual_director")
        if not isinstance(strategy, dict) or not strategy:
            strategy = plan_scene_strategy(scene, self.state, build_visual_query_plan(scene, self.state))
            scene["visual_director"] = strategy
        return strategy

    def _reject(self, scene: dict[str, Any], identity: str | None) -> None:
        if identity:
            scene["rejected_media_identities"] = list(dict.fromkeys([*(scene.get("rejected_media_identities") or []), identity]))

    def _judge(self, scene: dict[str, Any], row: _Row, step: str, steps: list[dict[str, Any]], *, required: float, text_ok: bool) -> bool:
        """Trial-render the current candidate; ``True`` when it is good enough."""
        media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
        entry: dict[str, Any] = {"step": step, "identity": media.get("identity"), "source": media_source(media) if media else None}
        steps.append(entry)
        if reveal_problems(scene, self.state):
            entry.update(accepted=False, reason="not_reveal_safe")
            return False
        attach_overlays_for(self.state)
        trial = self.trial(scene, row)
        if trial is None:
            entry.update(accepted=True, reason="unverified_no_local_visual_model")
            return True
        entry.update(trial)
        ok = trial["score"] >= required and (text_ok or not trial.get("presentation_risk"))
        entry["accepted"] = ok
        if not ok:
            entry["reason"] = "remained_text_heavy" if trial["score"] >= required else "rendered_match_below_threshold"
        return ok

    def escalate_media(self, action: dict[str, Any], scene: dict[str, Any], row: _Row) -> dict[str, Any]:
        """Real alternative -> generated image -> fitting base (+overlay) -> planned graphic."""
        required = self.review.required_score(row)
        text_ok = action.get("reason") != "text_heavy"
        original = _capture(scene)
        steps: list[dict[str, Any]] = []
        best: tuple[float, dict[str, Any]] = (row.semantic_score if row.semantic_score is not None else -1.0, original)

        def remember() -> None:
            nonlocal best
            score = steps[-1].get("score")
            if isinstance(score, (int, float)) and score > best[0] and steps[-1].get("reason") != "not_reveal_safe":
                best = (float(score), _capture(scene))

        for identity in action.get("reject") or []:
            self._reject(scene, identity)
        strategy = self._strategy(scene)
        result = self._escalate(action, scene, row, strategy, required=required, text_ok=text_ok, original=original, steps=steps, remember=remember)
        if result.get("accepted_step") and scene.get("block_id"):
            self.accepted[str(scene["block_id"])] = (str((scene.get("media") or {}).get("identity") or ""), str(scene.get("id") or ""))
        if result.get("accepted_step") is None:
            _restore(scene, best[1])
            rejected = set(scene.get("rejected_media_identities") or [])
            kept = str((scene.get("media") or {}).get("identity") or "")
            if kept in rejected and best[1] is not original:
                scene["rejected_media_identities"] = [value for value in scene["rejected_media_identities"] if value != kept]
            changed = kept != str((original.get("media") or {}).get("identity") or "")
            reason = next((step["reason"] for step in reversed(steps) if step.get("step") == "generated_image" and step.get("reason")), None)
            result = {
                "status": "applied" if changed else "unresolved",
                "accepted_step": "best_available" if changed else None,
                "unresolved_reason": "no_sufficiently_relevant_visual" if reason in {None, "rendered_match_below_threshold"} else reason,
                "steps": steps,
            }
        return result

    def _escalate(
        self, action: dict[str, Any], scene: dict[str, Any], row: _Row, strategy: dict[str, Any], *,
        required: float, text_ok: bool, original: dict[str, Any], steps: list[dict[str, Any]], remember: Callable[[], None],
    ) -> dict[str, Any]:
        from .visual_director import (
            GENERATE_FALLBACK,
            GENERATED_IMAGE,
            auto_generation_block_reason,
            generate_scene_image,
            record_decision,
            render_scene_graphic,
            visual_reveal_safe,
        )

        # 0. The same fact's visual, just repaired in this pass: continuity first.
        partner = self.accepted.get(str(scene.get("block_id") or ""))
        if partner and partner[1] != scene.get("id") and partner[0] not in set(scene.get("rejected_media_identities") or []):
            base_action = {"base_identity": partner[0], "base_scene_id": partner[1], "reason": "visual_continuity"}
            if self._apply_base(scene, base_action, strategy) and self._judge(scene, row, "continue_repaired_fact_visual", steps, required=required, text_ok=text_ok):
                return {"status": "applied", "accepted_step": "continue_base_visual", "steps": steps}
            if steps:
                remember()
            _restore(scene, original)
        # 1. The Visual Director's own chain for this scene, without falling back
        #    to another scene's visual: free real media (the rejected identities
        #    excluded), then its bounded generated image when nothing real passes.
        if action["action"] in {"continue_base_visual", "convert_graphic_to_overlay"}:
            if self._apply_base(scene, action, strategy) and self._judge(scene, row, action["action"], steps, required=required, text_ok=text_ok):
                return {"status": "applied", "accepted_step": action["action"], "steps": steps}
            if steps:
                remember()
            _restore(scene, original)
        scene["asset_status"] = "replacement_required"
        scene["media_repair"] = {"no_reuse": True}
        scene.pop("visual_continuity", None)
        self.prepare_media(self.state)
        scene.pop("media_repair", None)
        strategy = self._strategy(scene)
        media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
        identity = str(media.get("identity") or "")
        fresh = identity and identity != (original.get("media") or {}).get("identity") and identity not in set(action.get("reject") or [])
        generated_now = media_source(media) == GENERATED_ASSET_SOURCE and fresh
        if fresh:
            if self._judge(scene, row, "generated_image" if generated_now else "real_alternative", steps, required=required, text_ok=text_ok):
                return {"status": "applied", "accepted_step": steps[-1]["step"], "steps": steps}
            remember()
            self._reject(scene, identity)
        else:
            steps.append({"step": "real_alternative", "accepted": False, "reason": "no_accepted_real_alternative"})
        # 2. One bounded generated image, through the same budget checks.
        if not generated_now:
            blocked = auto_generation_block_reason(self.state, scene, self.settings, self.generator)
            if blocked:
                steps.append({"step": "generated_image", "accepted": False, "reason": blocked})
            else:
                metadata, record = generate_scene_image(
                    scene, self.state, strategy, project_id=self.project_id, settings=self.settings, generator=self.generator,
                    verifier=self.review.verifier, trigger="auto", reason="final_critic_escalation",
                )
                strategy.setdefault("generation", {}).update(status=record["status"], model=record.get("model"), quality=record.get("quality"))
                if metadata is None:
                    steps.append({"step": "generated_image", "accepted": False, "reason": record.get("error") or record["status"]})
                else:
                    metadata.update(story_role=strategy.get("story_role"), is_primary_answer=bool(strategy.get("is_primary_answer")))
                    metadata["reveal_safe"] = bool(metadata.get("reveal_safe", True)) and visual_reveal_safe(
                        self.state, strategy, scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
                    )
                    scene["media"] = metadata
                    scene["asset_status"] = "generated_image_ready"
                    scene.pop("fallback_reason", None)
                    record_decision(scene, strategy, GENERATE_FALLBACK, GENERATED_IMAGE, "final_critic_escalation")
                    if self._judge(scene, row, "generated_image", steps, required=required, text_ok=text_ok):
                        return {"status": "applied", "accepted_step": "generated_image", "steps": steps}
                    remember()
                    self._reject(scene, str(metadata.get("identity") or ""))
        # 3. A project visual that clearly fits this scene (rendered-frame score),
        #    with the scene's information drawn as an overlay.
        for _score, candidate in self.review.base_candidates(row, minimum=required):
            if candidate.identity in set(scene.get("rejected_media_identities") or []):
                continue
            base_action = {"base_identity": candidate.identity, "base_scene_id": candidate.scene_id, "reason": action.get("reason") or "semantic_match"}
            if self._apply_base(scene, base_action, strategy) and self._judge(scene, row, "fitting_base_visual", steps, required=required, text_ok=text_ok):
                return {"status": "applied", "accepted_step": "fitting_base_visual", "steps": steps}
            remember()
            _restore(scene, original)
            break
        # 4. The scene's planned explanatory graphic (only where one exists, and
        #    never as the answer to a text-heavy scene).
        if strategy.get("graphic") and text_ok:
            metadata = render_scene_graphic(scene, self.state, strategy, project_id=self.project_id, settings=self.settings)
            if metadata is not None and not reveal_problems(scene, self.state, media=metadata, overlays=[]):
                scene["media"] = metadata
                scene["asset_status"] = "graphic_ready"
                strategy.update(composition="fullscreen_graphic", composition_reason="no_acceptable_base_visual")
                record_decision(scene, strategy, DEGRADED, strategy.get("planned_type"), "final_critic_escalation")
                steps.append({"step": "planned_graphic", "identity": metadata["identity"], "accepted": True})
                return {"status": "applied", "accepted_step": "planned_graphic", "steps": steps}
        # Nothing clearly better: the caller keeps the best-scoring option.
        return {"status": "unresolved", "accepted_step": None, "steps": steps}

    def _apply_base(self, scene: dict[str, Any], action: dict[str, Any], strategy: dict[str, Any]) -> bool:
        base = next(
            (dict(item["media"]) for item in self.state.get("scenes") or [] if isinstance(item.get("media"), dict) and str(item["media"].get("identity") or "") == action["base_identity"]),
            None,
        )
        if base is None or reveal_problems(scene, self.state, media=base, overlays=[]) or not _reuse_safe(base, strategy or {"reveal_allowed": True}):
            return False
        base.pop("manually_selected", None)
        scene["media"] = base
        scene["asset_status"] = "block_visual_continued"
        scene.pop("fallback_reason", None)
        scene["visual_continuity"] = {"source_scene_id": action["base_scene_id"], "identity": action["base_identity"], "reason": f"final_critic_{action.get('reason')}"}
        if action.get("overlay_spec"):
            strategy["overlay_spec"] = action["overlay_spec"]
            strategy["composition"] = "base_with_overlay"
            strategy["composition_reason"] = "final_critic_graphic_to_overlay"
        elif not strategy.get("overlay_spec") and strategy.get("graphic"):
            spec = _overlay_graphic_spec(strategy["graphic"])
            if spec is not None:
                strategy["overlay_spec"] = spec
        if action.get("overlay_adjustments") or (strategy.get("overlay_spec") and overlay_would_dominate(strategy["overlay_spec"], self.state)):
            scene["render_adjustments"] = {**(scene.get("render_adjustments") or {}), "overlay": {"mode": "compact"}, "source": "final_critic"}
        strategy.update(resolved_type=REUSE_PREVIOUS_VISUAL, decision_reason="final_critic_base_visual")
        return True

    # -- overlay copy --------------------------------------------------------

    def rewrite_overlay(self, scene: dict[str, Any], row: _Row) -> dict[str, Any]:
        """New copy from the complete fact for every scene of that fact; the base visual stays."""
        from .simple_graphics import normalise_graphic_spec as graphic_spec
        from .visual_director import render_scene_graphic

        strategy = self._strategy(scene)
        source = fact_statement(scene, self.state)
        narration = " ".join(str(scene.get("narration") or "").split())
        story = self.review._story_context(row)
        spec = explanatory_overlay(scene, self.state, self.settings, story.get("visual_role"))
        if spec is not None and (assess_overlay(spec, source=source, narration=narration) or reveal_problems(scene, self.state, overlays=[{"spec": spec}])):
            spec = None
        block = scene.get("block_id")
        siblings = [item for item in self.state.get("scenes") or [] if block and item.get("block_id") == block] or [scene]
        for item in siblings:
            target = item.get("visual_director") if isinstance(item.get("visual_director"), dict) else None
            if target is None:
                continue
            target["overlay_spec"] = dict(spec) if spec else None
            target["overlay_copy_source"] = (spec or {}).get("source", "removed")
        if row.graphic:
            graphic = graphic_spec({"kind": "process", "steps": list(spec["steps"])}) if spec else None
            if graphic is None:
                return {"status": "unresolved", "accepted_step": None, "unresolved_reason": "no_clear_relation_in_fact", "steps": [{"step": "rewrite_graphic_copy", "accepted": False}]}
            strategy["graphic"] = graphic
            metadata = render_scene_graphic(scene, self.state, strategy, project_id=self.project_id, settings=self.settings)
            if metadata is None:
                return {"status": "unresolved", "accepted_step": None, "steps": [{"step": "rewrite_graphic_copy", "accepted": False}]}
            scene["media"] = metadata
            return {"status": "applied", "accepted_step": "rewrite_overlay_from_fact", "steps": [{"step": "rewrite_graphic_copy", "copy": graphic["steps"], "accepted": True}]}
        attach_overlays_for(self.state)
        if spec is None:
            return {"status": "applied", "accepted_step": "remove_meaningless_overlay", "steps": [{"step": "remove_meaningless_overlay", "accepted": True}]}
        return {"status": "applied", "accepted_step": "rewrite_overlay_from_fact", "steps": [{"step": "rewrite_overlay_from_fact", "copy": spec["steps"], "source": spec.get("source"), "accepted": True}]}

    # -- framing steps -----------------------------------------------------

    def reframe(self, action: dict[str, Any], scene: dict[str, Any], row: _Row) -> dict[str, Any]:
        """Crop, then less motion, then a frozen still; media only if framing still fails."""
        required = max(SEMANTIC_GOOD, self.review.required_score(row))
        steps: list[dict[str, Any]] = []
        best: tuple[float, dict[str, Any]] | None = None
        adjustments = dict(scene.get("render_adjustments") or {})
        adjustments.update(action.get("adjustments") or {})
        candidates: list[tuple[str, dict[str, Any]]] = []
        crop = _recalculated_crop(self.review, row)
        if crop is not None:
            candidates.append(("recompute_focal_crop", {"crop": crop}))
        motion = row.entry.get("motion") if isinstance(row.entry.get("motion"), dict) else {}
        if row.still and motion.get("type") not in {None, "static"} and not motion.get("reduced"):
            candidates.append(("reduce_motion", {"motion": "reduced"}))
        if row.still and motion.get("type") not in {None, "static"}:
            candidates.append(("freeze_still", {"motion": "static"}))
        if not candidates and action.get("adjustments"):
            candidates.append(("overlay_only", {}))
        for step, change in candidates:
            adjustments.update(change)
            scene["render_adjustments"] = {**adjustments, "source": "final_critic"}
            attach_overlays_for(self.state)
            trial = self.trial(scene, row)
            entry = {"step": step, **change, **(trial or {})}
            steps.append(entry)
            if trial is None:
                entry["accepted"] = True
                return {"status": "applied", "accepted_step": step, "steps": steps}
            # The subject must stay visible in every sampled frame.
            entry["accepted"] = trial["min_frame"] >= required
            if best is None or trial["min_frame"] > best[0]:
                best = (trial["min_frame"], copy.deepcopy(scene["render_adjustments"]))
            if entry["accepted"]:
                return {"status": "applied", "accepted_step": step, "steps": steps}
        if best is not None:
            scene["render_adjustments"] = best[1]
        if action.get("allow_media_escalation") and steps:
            result = self.escalate_media({"action": "replace_media", "reason": "semantic_match", "reject": [row.identity]}, scene, row)
            result["steps"] = steps + result["steps"]
            if result.get("accepted_step"):
                return result
            return {**result, "status": "applied" if best is not None else result["status"]}
        return {"status": "applied" if steps else "unresolved", "accepted_step": None, "unresolved_reason": "framing_still_fails", "steps": steps}


def attach_overlays_for(state: dict[str, Any]) -> None:
    from .media import attach_project_overlays

    attach_project_overlays([scene for scene in state.get("scenes") or [] if isinstance(scene, dict)])


def apply_repairs(
    actions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    review: _Review,
    prepare_media: Callable[[dict[str, Any]], Any],
    generator: Any | None,
    settings: Settings,
    project_id: str,
    pass_index: int,
) -> list[dict[str, Any]]:
    """Apply planned actions scene by scene (bounded escalation inside each action)."""
    records: list[dict[str, Any]] = []
    counts_before = generation_counts(state)
    repairer = _Repairer(review, prepare_media=prepare_media, generator=generator, project_id=project_id)
    # Retiming after the render dropped the overlays; restore them first.
    attach_overlays_for(state)
    for action in actions:
        record = {**action, "pass": pass_index, "attempted_at": _now(), "repair_attempted": action.get("status") == "planned"}
        records.append(record)
        if action.get("status") != "planned":
            record["repair_effective"] = False
            continue
        scene = _scene_by_id(state, action["scene_id"])
        row = review.row(action["scene_id"])
        if scene is None or row is None or (action["action"] in _MEDIA_ACTIONS and user_locked_visual(scene)):
            record.update(status="blocked", repair_effective=False, blocked_reason="user_locked_visual" if scene is not None else "scene_changed_after_render")
            continue
        record["before"] = _scene_snapshot(scene)
        record["before_score"] = row.semantic_score
        record["before_overlay"] = copy.deepcopy((scene.get("visual_director") or {}).get("overlay_spec"))
        kind = action["action"]
        overlay_codes = {issue_id.split(":")[-1] for issue_id in action.get("issue_ids") or []} & set(OVERLAY_SEMANTIC_CODES)
        if ((scene.get("visual_director") or {}).get("overlay_spec") or {}).get("source") == TRIPLE_HOOK_SOURCE:
            overlay_codes = set()  # the hook label is removed by its composition action
        rewritten = repairer.rewrite_overlay(scene, row) if overlay_codes or action.get("rewrite_overlay") else None
        if kind in _MEDIA_ACTIONS:
            outcome = repairer.escalate_media(action, scene, row)
            if rewritten:
                outcome["steps"] = rewritten["steps"] + outcome.get("steps", [])
            record["invalidated"] = ["media", "crop", "overlay", "motion"]
        else:
            if action.get("adjustments"):
                scene["render_adjustments"] = {**(scene.get("render_adjustments") or {}), **action["adjustments"], "source": "final_critic"}
            if action.get("reframe"):
                outcome = repairer.reframe(action, scene, row)
                if rewritten:
                    outcome["steps"] = rewritten["steps"] + outcome.get("steps", [])
            elif rewritten is not None:
                outcome = rewritten
            else:
                outcome = {"status": "applied", "accepted_step": "overlay_adjustment", "steps": [{"step": "overlay_adjustment", **action["adjustments"]}]}
            record["invalidated"] = sorted({key for key in (scene.get("render_adjustments") or {}) if key in {"overlay", "motion", "crop"}})
        record.update(outcome)
        record["after"] = _scene_snapshot(scene)
        record["after_overlay"] = copy.deepcopy((scene.get("visual_director") or {}).get("overlay_spec"))
        record["generation"] = dict((scene.get("visual_director") or {}).get("generation") or {})
        trial_scores = [step.get("score") for step in outcome.get("steps") or [] if isinstance(step.get("score"), (int, float))]
        record["trial_score"] = max(trial_scores) if trial_scores else None
    # Scenes whose media vanished with retiming get their normal media pass;
    # every other scene is cached, and overlays follow the strategies.
    prepare_media(state)
    policy = generation_policy(state, settings)
    counts_after = generation_counts(state)
    budget = {
        "auto_generated_before": counts_before["auto_generated_images"],
        "auto_generated_after": counts_after["auto_generated_images"],
        "project_limit": policy["max_auto_generated_images_per_project"],
    }
    for record in records:
        record["generation_budget"] = budget
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


_SCENE_QUALITY_CODES = {"wrong_media", "weak_media", "text_heavy", "subject_lost_in_render", "motion_loses_subject", "unusable_frame"}

_STEP_MESSAGES = {
    "real_alternative": "replaced a weak visual",
    "generated_image": "replaced a weak visual with a generated image",
    "fitting_base_visual": "reused a visual that fits this scene, with its information as an overlay",
    "planned_graphic": "used the planned explanatory graphic",
    "best_available": "switched to the best available visual",
    "continue_base_visual": "kept the fact's visual for continuity",
    "convert_graphic_to_overlay": "turned the full-screen graphic into an overlay on a real visual",
    "recompute_focal_crop": "re-centred the crop on the subject",
    "reduce_motion": "reduced the camera motion",
    "freeze_still": "disabled unsafe camera motion",
    "overlay_only": "adjusted the overlay",
    "rewrite_overlay_from_fact": "rewrote the overlay text from the full fact",
    "remove_meaningless_overlay": "removed an overlay that taught nothing",
}
_UNRESOLVED_MESSAGES = {
    "no_sufficiently_relevant_visual": "no sufficiently relevant visual found",
    "project_budget_exhausted": "no relevant visual found and the AI image budget is used up",
    "scene_attempts_exhausted": "no relevant visual found; this scene already used its AI image attempt",
    "disabled": "no relevant visual found; the AI image fallback is off",
    "unavailable_no_api_key": "no relevant visual found; the AI image fallback is not configured",
    "remained_text_heavy": "the replacement remained text-heavy",
    "framing_still_fails": "the subject still leaves the frame",
    "user_locked_visual": "your chosen visual looks weak; it was not replaced automatically",
    "no_alternative_found_earlier": "no better visual was found",
    "no_safe_targeted_repair": "no safe automatic repair exists",
    "repair_render_failed": "the repair render failed; the original was kept",
    "no_clear_relation_in_fact": "no clear relation could be shown for this fact",
}



def _result_message(record: dict[str, Any]) -> str:
    if record.get("repair_effective"):
        if record.get("action") == "adjust_composition" and record.get("accepted_step") in {None, "overlay_adjustment"}:
            overlay = (record.get("adjustments") or {}).get("overlay") or {}
            parts = ["removed the overlay" if overlay.get("mode") == "remove" else "simplified the overlay" if overlay.get("mode") == "compact" else ""]
            if overlay.get("placement"):
                parts.append("moved the overlay away from the captions")
            return ", ".join(part for part in parts if part) or "adjusted the overlay"
        return _STEP_MESSAGES.get(str(record.get("accepted_step") or record.get("action")), "repaired")
    reason = record.get("unresolved_reason") or record.get("blocked_reason")
    if reason in _UNRESOLVED_MESSAGES:
        return _UNRESOLVED_MESSAGES[reason]
    remaining = record.get("remaining_issue") or []
    if set(OVERLAY_SEMANTIC_CODES) & set(remaining):
        return "the overlay text is still not meaningful"
    if "text_heavy" in remaining:
        return "the scene is still text-heavy"
    if {"wrong_media", "weak_media"} & set(remaining):
        return "the visual still does not clearly match the narration"
    if {"subject_lost_in_render", "motion_loses_subject"} & set(remaining):
        return "the subject still leaves the frame"
    return "the repair did not resolve the issue"


def _evaluate(records: list[dict[str, Any]], before: _Review, after: _Review) -> None:
    """A repair is effective only when its triggering issue is gone from the new render."""
    after_ids = {issue["id"] for issue in after.issues()}
    for record in records:
        if not record.get("repair_attempted") or record.get("status") == "blocked":
            continue
        targeted = set(record.get("issue_ids") or [])
        after_row = after.row(record["scene_id"])
        before_row = before.row(record["scene_id"])
        after_issues = after_row.issues if after_row else []
        remaining = {issue["code"] for issue in after_issues if issue["id"] in targeted}
        if record.get("action") in _MEDIA_ACTIONS or record.get("reframe"):
            # A new visual or framing must also leave the scene clearly matching.
            remaining |= {issue["code"] for issue in after_issues if issue["code"] in _SCENE_QUALITY_CODES}
        new_reveal = [issue["id"] for issue in after_issues if issue["category"] == "reveal_safety" and issue["id"] not in targeted]
        before_score = before_row.semantic_score if before_row else None
        after_score = after_row.semantic_score if after_row else None
        effective = record.get("status") == "applied" and not remaining and not new_reveal
        if new_reveal or (before_score is not None and after_score is not None and after_score < before_score - 0.02):
            outcome = "regressed"
        elif effective:
            outcome = "resolved"
        else:
            outcome = "unresolved"
        record.update(
            repair_effective=effective,
            improved=effective,
            outcome=outcome,
            before_score=before_score,
            after_score=after_score,
            remaining_issue=sorted(remaining),
            result={
                "resolved": sorted(targeted - after_ids),
                "remaining": sorted(targeted & after_ids),
                "new_reveal_issues": new_reveal,
                "before": {category: _rating(before_row, category) for category in record.get("categories") or []},
                "after": {category: _rating(after_row, category) for category in record.get("categories") or []},
            },
        )


def _unresolved_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if issue["severity"] == "error" or (issue["severity"] == "warning" and issue["code"] in REPAIRABLE_CODES)]


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
    generator: Any | None = None,
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
    if generator is None:
        from .image_generation import get_image_generator

        generator = get_image_generator(settings)
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
            records = apply_repairs(
                planned, state, review=review, prepare_media=prepare_media, generator=generator,
                settings=settings, project_id=project_id, pass_index=pass_count + 1,
            )
            if not any(record.get("status") == "applied" for record in records):
                # Nothing on screen changed, so no repair render is needed.
                for record in records:
                    record.update(outcome="unresolved", improved=False, repair_effective=False)
                    record["result_message"] = _result_message(record)
                repairs.extend(records)
                break
            rerender(state)
        except Exception as exc:  # noqa: BLE001 - the original render stays valid
            state.clear()
            state.update(snapshot)
            if backup is not None and video is not None and backup.is_file():
                shutil.copy2(backup, video)
            render_error = str(exc)[:300] or type(exc).__name__
            repairs.extend(
                {**action, "pass": pass_count + 1, "status": "failed", "blocked_reason": "repair_render_failed", "repair_attempted": True, "repair_effective": False}
                for action in planned if action["status"] == "planned"
            )
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
    errors = _unresolved_issues(final_issues)
    warnings = [issue for issue in final_issues if issue["severity"] == "warning" and issue not in errors]
    for record in repairs:
        record.setdefault("repair_effective", False)
        record["result_message"] = _result_message(record)
    # Only scenes whose triggering issue is actually gone count as repaired.
    repaired = sorted({record["scene_id"] for record in repairs if record.get("repair_effective")} - {issue["scene_id"] for issue in errors})
    changed = sorted({record["scene_id"] for record in repairs if record.get("status") == "applied" and record.get("after") and record.get("after") != record.get("before")})
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
        "unresolved": [issue["id"] for issue in errors],
        "repaired_scenes": repaired,
        "changed_scenes": changed,
        "history": history,
        "user_overrides": [],
        "summary": _summary(status, repaired, errors, warnings),
    }
    if review.hook_summary is not None:
        result["hook"] = review.hook_summary
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
