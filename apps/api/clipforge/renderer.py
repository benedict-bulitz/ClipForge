import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from PIL import Image, UnidentifiedImageError

from .alignment import (
    align_narration,
    alignment_readiness,
    group_aligned_words,
    phrase_fallback_items,
)
from .attention import replan_attention
from .config import Settings
from .media import GRAPHIC_ASSET_SOURCE, _reuse_safe, is_scene_asset_allowed, media_source
from .music import attach_discovered_track, resolve_track_path
from .narration import clean_narration_text, contamination_issues
from .progress import ProgressCallback, report_progress
from .simple_graphics import GraphicSpecError, compact_overlay_spec, render_overlay
from .smart_crop import analyze_scene_media
from .voice import OPENAI_VOICES, tts_instructions


class RenderUnavailable(RuntimeError):
    pass


class VoiceGenerationError(RenderUnavailable):
    """A safe, user-actionable failure from the selected voice provider."""

    def __init__(self, message: str, *, category: str, status_code: int = 503):
        super().__init__(message)
        self.category = category
        self.status_code = status_code


@dataclass(frozen=True)
class RenderResult:
    url: str
    actual_seconds: float
    voice_provider: str
    file_size: int
    # What each scene of the file shows: its time window, media, crop, still
    # motion and the overlays actually drawn (with their frame footprint).
    layout: tuple[dict[str, Any], ...] = ()


# Encoded scene segments are reused across renders of a project when nothing
# that affects their pixels changed (a targeted repair re-encodes only the
# repaired scenes; captions and audio are always composed again).
SEGMENT_CACHE_DIRECTORY = "segment-cache"
# Caption layout shared by the ASS writer and the layout metadata.
CAPTION_MARGIN_RATIO = 0.12
CAPTION_LINE_HEIGHT = 1.25
CAPTION_MAX_LINES = 2


def ffmpeg_path() -> str | None:
    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if Path(path).exists() else None
    except (OSError, RuntimeError):
        return shutil.which("ffmpeg")


def _run_process(command: list[str], *, timeout: int, failure: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RenderUnavailable(failure) from exc


def readiness(settings: Settings) -> dict[str, dict[str, str | bool | None]]:
    system_voice = shutil.which("say")
    ffmpeg = ffmpeg_path()
    alignment = alignment_readiness(settings)
    return {
        "director": {
            "ready": settings.clipforge_ai_mode != "openai" or bool(settings.openai_api_key),
            "status": "OpenAI" if settings.openai_api_key else "Local planner",
            "key": "OPENAI_API_KEY",
            "url": "https://platform.openai.com/api-keys",
        },
        "research": {
            "ready": True,
            "status": "Brave Search" if settings.brave_search_api_key else "Wikipedia fallback",
            "key": "BRAVE_SEARCH_API_KEY",
            "url": "https://api-dashboard.search.brave.com/app/keys",
        },
        "media": {
            "ready": True,
            "status": "Pexels + Wikimedia" if settings.pexels_api_key else "Wikimedia fallback",
            "key": "PEXELS_API_KEY",
            "url": "https://www.pexels.com/api/new/",
        },
        "voice": {
            "ready": bool(settings.openai_api_key or system_voice),
            "status": "OpenAI TTS" if settings.openai_api_key else ("macOS voice" if system_voice else "Provider needed"),
            "key": "OPENAI_API_KEY",
            "url": "https://platform.openai.com/api-keys",
            "alternative_url": "https://huggingface.co/hexgrad/Kokoro-82M",
        },
        "alignment": {
            "ready": alignment.ready,
            "status": alignment.status,
            "key": None,
            "url": "https://pypi.org/project/faster-whisper/",
        },
        "render": {
            "ready": bool(ffmpeg),
            "status": "Bundled FFmpeg" if ffmpeg else "FFmpeg missing",
            "key": None,
            "url": "https://ffmpeg.org/download.html",
        },
        "storage": {
            "ready": True,
            "status": "Local storage",
            "key": "CLOUDFLARE_R2_*",
            "url": "https://developers.cloudflare.com/r2/get-started/",
        },
    }


def render_video(
    state: dict,
    project_id: str,
    revision_number: int,
    settings: Settings,
    *,
    progress: ProgressCallback | None = None,
) -> RenderResult:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderUnavailable("FFmpeg is unavailable. Open Build readiness for installation help.")
    if state["render"]["status"] == "blocked_by_research":
        raise RenderUnavailable("This factual project has no attributable research yet.")
    raw_text = str(state.get("script", {}).get("text") or "")
    if clean_narration_text(raw_text) != raw_text or contamination_issues(raw_text):
        raise RenderUnavailable("Narration validation failed before rendering.")

    output_dir = settings.render_root.resolve() / project_id / "renders" / f"v{revision_number}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "clipforge.mp4"
    with tempfile.TemporaryDirectory(prefix="clipforge-") as temp_name:
        temp = Path(temp_name)
        report_progress(progress, "voice", "Generating narration", phase="start")
        audio, provider = _cached_voice(
            state, project_id, temp, settings, progress=progress
        )
        actual = _audio_duration(ffmpeg, audio)
        maximum = float(state["duration"]["max_seconds"])
        if actual > maximum + 0.1:
            raise RenderUnavailable(
                "Generated narration exceeds the maximum duration; shorten it at a sentence boundary."
            )
        target = max(float(state["duration"].get("effective_minimum_seconds") or 10), actual)
        captions = state.setdefault("captions", {})
        if captions.get("enabled", True):
            report_progress(
                progress, "alignment", "Timing captions to speech", phase="start"
            )
            alignment = align_narration(
                audio,
                state["script"]["text"],
                target,
                state["intent"]["language"],
                settings,
            )
            captions["timing"] = alignment.status
            captions["alignment_provider"] = alignment.provider
            captions["diagnostic"] = alignment.diagnostic
            group_size = int(captions.get("words_per_group", 4))
            captions["items"] = (
                group_aligned_words(alignment.words, group_size, state["script"]["text"])
                if alignment.words
                else phrase_fallback_items(state["script"]["text"], target, group_size)
            )
            replan_attention(state)
            report_progress(
                progress, "alignment", "Timing captions to speech", phase="complete"
            )
        else:
            captions.update(items=[], timing="disabled", alignment_provider="disabled")
            report_progress(
                progress, "alignment", "Timing captions to speech", phase="skipped"
            )
        scenes = state["scenes"]
        total_weight = max(1.0, sum(max(0.1, scene["end"] - scene["start"]) for scene in scenes))
        fps = int(state["timeline"]["fps"])
        # Whole frames per scene: the file's scene windows are then exact and
        # an unchanged scene encodes identically in a later (repair) render.
        frames = [max(1, round(target * max(0.1, scene["end"] - scene["start"]) / total_weight * fps)) for scene in scenes]
        if frames:
            # The last scene absorbs rounding so the picture spans the narration.
            frames[-1] = max(1, round(target * fps) - sum(frames[:-1]))
        durations = [count / fps for count in frames]
        report_progress(
            progress,
            "rendering",
            "Preparing scene video",
            phase="start",
            completed_units=0,
            total_units=len(scenes),
        )
        segment_paths = []
        segment_cache = settings.render_root.resolve() / project_id / SEGMENT_CACHE_DIRECTORY
        layout: list[dict[str, Any]] = []
        cursor = 0.0
        cache_files: list[Path] = []
        for index, (scene, duration) in enumerate(
            zip(scenes, durations, strict=True)
        ):
            segment_paths.append(
                _create_visual_segment(
                    ffmpeg, state, scene, index, duration, temp, settings, cache_dir=segment_cache
                )
            )
            layout.append(_scene_layout(scene, index, cursor, min(target, cursor + duration)))
            cursor += duration
            segment = scene.pop("render_segment", None) or {}
            if segment.get("cache_file"):
                cache_files.append(Path(segment["cache_file"]))
            report_progress(
                progress,
                "rendering",
                "Preparing scene video",
                completed_units=index + 1,
                total_units=len(scenes),
            )
        report_progress(
            progress,
            "rendering",
            "Preparing scene video",
            phase="complete",
            completed_units=len(scenes),
            total_units=len(scenes),
        )
        concat = temp / "segments.txt"
        lines: list[str] = []
        for segment_path in segment_paths:
            escaped = str(segment_path).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        caption_file = _write_ass_captions(state, target, temp)
        command = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-i",
            str(audio),
        ]
        music_enabled = bool(
            state.get("music", {}).get("enabled") or state.get("music", {}).get("requested_enabled")
        )
        report_progress(
            progress,
            "music",
            "Music will be mixed on export",
            phase="skipped",
        )
        music_state = state.setdefault("music", {})
        if not music_enabled:
            music_state["status"] = "disabled"
        command.extend(["-af", f"volume={float(state.get('voice', {}).get('volume', 1)):.3f},apad=pad_dur={target:.3f}"])
        command.extend([
            "-t",
            f"{target:.3f}",
        ])
        if (state.get("captions", {}).get("enabled", True) and state.get("captions", {}).get("items")) or state.get("attention_events"):
            command.extend(["-vf", f"ass={caption_file}"])
        command.extend([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ])
        report_progress(
            progress,
            "finalizing",
            "Finalizing and checking the video",
            phase="start",
        )
        completed = _run_process(
            command, timeout=180, failure="Video composition did not finish in time."
        )
        if completed.returncode != 0:
            output.unlink(missing_ok=True)
            raise RenderUnavailable(completed.stderr.strip()[-500:] or "FFmpeg render failed")
        verify = _run_process(
            [ffmpeg, "-v", "error", "-i", str(output), "-f", "null", "-"],
            timeout=60,
            failure="The rendered video could not be verified.",
        )
        if verify.returncode != 0 or not output.exists() or output.stat().st_size < 10_000:
            output.unlink(missing_ok=True)
            raise RenderUnavailable(verify.stderr.strip()[-500:] or "Rendered file failed quality checks")
        _prune_segment_cache(segment_cache, cache_files)
        report_progress(
            progress,
            "finalizing",
            "Finalizing and checking the video",
            phase="complete",
        )
        # Retain independent sources outside the export cleanup directories.
        layers = settings.render_root.resolve() / project_id / "audio-layers" / f"v{revision_number}"
        layers.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, layers / "picture.mp4")
        shutil.copy2(audio, layers / "narration.wav")
        state.setdefault("voice", {})["mix_source"] = (layers / "narration.wav").relative_to(settings.render_root.resolve()).as_posix()
        state["voice"]["picture_source"] = (layers / "picture.mp4").relative_to(settings.render_root.resolve()).as_posix()
    relative = output.relative_to(settings.render_root.resolve()).as_posix()
    return RenderResult(
        url=f"/media/{relative}",
        actual_seconds=round(target, 2),
        voice_provider=provider,
        file_size=output.stat().st_size,
        layout=tuple(layout),
    )


def _scene_layout(scene: dict, index: int, start: float, end: float) -> dict[str, Any]:
    """Renderer layout metadata for one scene window of the finished file."""
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    strategy = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
    crop = scene.get("smart_crop") if isinstance(scene.get("smart_crop"), dict) else {}
    segment = scene.get("render_segment") if isinstance(scene.get("render_segment"), dict) else {}
    return {
        "scene_id": str(scene.get("id") or f"scene_{index + 1:02d}"),
        "index": index,
        "block_id": str(scene.get("block_id") or ""),
        "start": round(start, 3),
        "end": round(end, 3),
        "media": {
            "identity": str(media.get("identity") or ""),
            "source": media_source(media) if media else "",
            "kind": str(media.get("kind") or ""),
            "cache_path": str(media.get("cache_path") or ""),
            "asset_status": str(scene.get("asset_status") or ""),
        },
        "composition": strategy.get("composition"),
        "crop": {
            key: crop.get(key) for key in ("status", "mode", "center_x", "center_y", "confidence") if key in crop
        },
        "motion": dict(scene["still_motion"]) if isinstance(scene.get("still_motion"), dict) and media.get("kind") != "video" else None,
        "overlays": [dict(item) for item in segment.get("overlays") or []],
        "adjustments": dict(scene.get("render_adjustments") or {}),
        "segment_cache": segment.get("cache", "disabled"),
    }


def _prune_segment_cache(cache_dir: Path, kept: list[Path]) -> None:
    """Keep only the segments of the latest render (bounded disk use)."""
    if not cache_dir.is_dir():
        return
    keep = {path.resolve() for path in kept}
    for item in cache_dir.glob("*.mp4"):
        if item.resolve() not in keep:
            item.unlink(missing_ok=True)


def caption_band(state: dict) -> dict[str, Any] | None:
    """Normalised vertical band the burned-in captions can occupy (``None`` = no captions)."""
    captions = state.get("captions") if isinstance(state.get("captions"), dict) else {}
    if not captions.get("enabled", True):
        return None
    height = max(1, int((state.get("timeline") or {}).get("height") or 1920))
    font_size = max(24, min(112, int(captions.get("font_size", 72))))
    block = min(0.5, CAPTION_MAX_LINES * font_size * CAPTION_LINE_HEIGHT / height)
    position = str(captions.get("position") or "lower")
    if position == "upper":
        top = CAPTION_MARGIN_RATIO
        bottom = top + block
    elif position == "center":
        top, bottom = 0.5 - block / 2, 0.5 + block / 2
    else:
        position = "lower"
        bottom = 1.0 - CAPTION_MARGIN_RATIO
        top = bottom - block
    return {"position": position, "top": round(top, 4), "bottom": round(bottom, 4)}


def _write_ass_captions(state: dict, target: float, temp: Path) -> Path:
    width = int(state["timeline"]["width"])
    height = int(state["timeline"]["height"])
    captions = state.get("captions", {})
    font_size = max(24, min(112, int(captions.get("font_size", 72))))
    normal_color = _ass_color(captions.get("text_color"), "ffffff")
    active_color = _ass_color(captions.get("highlight_color"), "ff6838")
    style = caption_style_config(str(captions.get("style") or "karaoke"))
    alignment = {"upper": 8, "center": 5}.get(captions.get("position"), 2)
    margin = round(height * CAPTION_MARGIN_RATIO)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{normal_color},{active_color},&H90000000,{style['back_color']},{style['bold']},0,0,0,100,100,{style['spacing']},0,{style['border_style']},{style['outline']},{style['shadow']},{alignment},70,70,{margin},1
Style: Attention,Arial,{max(28, round(font_size * 0.62))},{active_color},{active_color},&H90000000,&H90000000,1,0,0,0,100,100,0,0,1,3,1,8,70,70,{round(height * 0.08)},1
Style: AttentionStat,Arial,{max(32, round(font_size * 0.72))},{active_color},{active_color},&H90000000,&H90000000,1,0,0,0,100,100,0,0,1,3,1,8,70,70,{round(height * 0.08)},1
Style: AttentionSymbol,Arial,{max(34, round(font_size * 0.82))},{active_color},{active_color},&H90000000,&H90000000,1,0,0,0,100,100,0,0,1,2,1,7,70,70,{round(height * 0.08)},1
Style: AttentionCompare,Arial,{max(26, round(font_size * 0.55))},{normal_color},{normal_color},&H90000000,&H90000000,1,0,0,0,100,100,0,0,1,2,1,8,70,70,{round(height * 0.08)},1
Style: AttentionAccent,Arial,12,{active_color},{active_color},&H90000000,&H90000000,0,0,0,0,100,100,0,0,1,0,0,8,70,70,{round(height * 0.08)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    items = (captions.get("items") or []) if captions.get("enabled", True) else []
    events = []
    for item in items:
        start = max(0.0, min(target, float(item.get("start") or 0)))
        end = max(start + 0.08, min(target, float(item.get("end") or target)))
        text = _ass_escape(str(item.get("text") or ""))
        if style["uppercase"]:
            text = text.upper()
        timed_words = (
            item.get("words")
            if item.get("timing") == "word_aligned" and isinstance(item.get("words"), list)
            else []
        )
        if timed_words:
            for active_index, word in enumerate(timed_words):
                word_start = max(start, float(word.get("start", start)))
                word_end = min(end, max(word_start + 0.04, float(word.get("end", end))))
                rendered = []
                for index, candidate in enumerate(timed_words):
                    candidate_text = _ass_escape(str(candidate.get("text") or ""))
                    if style["uppercase"]:
                        candidate_text = candidate_text.upper()
                    color = active_color if index == active_index else normal_color
                    rendered.append(f"{{\\c{color}}}{candidate_text}")
                events.append(
                    f"Dialogue: 0,{_ass_time(word_start)},{_ass_time(word_end)},Default,,0,0,0,,"
                    + " ".join(rendered)
                )
            continue
        if text:
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
            )
    for item in state.get("attention_events", []) or []:
        start = max(0.0, min(target, float(item.get("start") or 0)))
        end = max(start + 0.08, min(target, start + float(item.get("duration") or 0)))
        text = _ass_escape(str(item.get("text") or "")).strip()
        event_type = str(item.get("type") or "")
        if event_type == "graphic_accent":
            # Legacy states may still contain this type; never render the old
            # persistent-looking green bar.
            continue
        if event_type == "focus_pulse":
            text = f"{{\\an5\\c{active_color}\\p1}}m 0 40 b 0 18 18 0 40 0 b 62 0 80 18 80 40 b 80 62 62 80 40 80 b 18 80 0 62 0 40{{\\p0}}"
        elif event_type == "shape_pop":
            text = f"{{\\an5\\c{active_color}\\p1}}m 0 32 l 32 0 l 64 32 l 32 64{{\\p0}}"
        elif event_type == "sticker_pop":
            text = f"{{\\an5\\c{active_color}\\bord2}}✦"
        elif event_type == "punch_zoom":
            text = f"{{\\an5\\c{active_color}\\p1}}m 0 10 l 10 0 m 54 0 l 64 10 m 64 54 l 54 64 m 10 64 l 0 54{{\\p0}}"
        if not text or end <= start:
            continue
        event_style = {"statistic_callout": "AttentionStat", "icon_or_symbol": "AttentionSymbol", "comparison_label": "AttentionCompare", "graphic_accent": "AttentionAccent"}.get(event_type, "Attention")
        anchor = "{\\an8}"
        scene_index = item.get("scene_index")
        if event_type == "focus_pulse" and isinstance(scene_index, int):
            scenes = state.get("scenes") or []
            if 0 <= scene_index < len(scenes):
                crop = scenes[scene_index].get("smart_crop") or {}
                if crop.get("confidence", 0) >= 0.55:
                    anchor = f"{{\\an5\\pos({round(width * float(crop.get('center_x', .5)))},{round(height * float(crop.get('center_y', .5)))})}}"
        events.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},{event_style},,0,0,0,,{anchor}{text}")
    output = temp / "captions.ass"
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_color(value: object, fallback: str) -> str:
    color = str(value or f"#{fallback}").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        color = fallback
    return f"&H00{color[4:6]}{color[2:4]}{color[0:2]}"


def caption_style_config(style: str) -> dict[str, int | bool | str]:
    styles: dict[str, dict[str, int | bool | str]] = {
        "clean": {"bold": 0, "border_style": 1, "outline": 3, "shadow": 1, "spacing": 0, "back_color": "&H50000000", "uppercase": False},
        "bold": {"bold": -1, "border_style": 1, "outline": 5, "shadow": 2, "spacing": 0, "back_color": "&H60000000", "uppercase": True},
        "minimal": {"bold": 0, "border_style": 1, "outline": 1, "shadow": 0, "spacing": 1, "back_color": "&H00000000", "uppercase": False},
        "pop": {"bold": -1, "border_style": 1, "outline": 6, "shadow": 3, "spacing": 1, "back_color": "&H60000000", "uppercase": True},
        "boxed": {"bold": -1, "border_style": 3, "outline": 9, "shadow": 0, "spacing": 0, "back_color": "&H80000000", "uppercase": False},
        "outline": {"bold": -1, "border_style": 1, "outline": 7, "shadow": 0, "spacing": 0, "back_color": "&H00000000", "uppercase": False},
        "karaoke": {"bold": -1, "border_style": 1, "outline": 4, "shadow": 2, "spacing": 0, "back_color": "&H50000000", "uppercase": False},
    }
    aliases = {"bold_clean": "bold", "minimal_lower_third": "minimal"}
    return styles.get(aliases.get(style, style), styles["karaoke"])


MUSIC_MIN_DB = -30.0
MUSIC_DUCK_DB = -4.0


def music_volume_gain(volume: object, *, ducking: bool = False) -> tuple[float, float]:
    """Map the persisted 0..1 UI value to perceptual dB and linear gain."""
    value = max(0.0, min(1.0, float(volume)))
    if value == 0:
        return 0.0, float("-inf")
    db = MUSIC_MIN_DB + (0.0 - MUSIC_MIN_DB) * value**0.5
    if ducking:
        db += MUSIC_DUCK_DB
    return 10 ** (db / 20.0), db


def music_render_config(state: dict) -> dict[str, object]:
    music = state.get("music", {})
    volume = max(0.0, min(1.0, float(music.get("volume", 0.14))))
    ducking = bool(music.get("ducking", True))
    effective_volume, effective_db = music_volume_gain(volume, ducking=ducking)
    _base_gain, volume_db = music_volume_gain(volume)
    return {
        "enabled": bool(music.get("enabled", False)),
        "mood": str(music.get("mood") or "ambient"),
        "volume": volume,
        "volume_db": volume_db,
        "ducking": ducking,
        "ducking_db": MUSIC_DUCK_DB if ducking else 0.0,
        "effective_volume": effective_volume,
        "effective_db": effective_db,
        "fades": bool(music.get("fades", True)),
        "track_id": str((music.get("track") or {}).get("id") or ""),
        "voice_volume": max(0.0, min(1.0, float(state.get("voice", {}).get("volume", 1)))),
    }


def music_filter_graph(config: dict[str, object], duration: float) -> str:
    fades = ""
    if config["fades"]:
        fades = (
            ",afade=t=in:st=0:d=0.8,"
            f"afade=t=out:st={max(0.0, duration - 1):.3f}:d=1"
        )
    return (
        # Export has exactly two inputs: the base render (0, including
        # narration) and the looped music asset (1).
        f"[1:a]volume={float(config['effective_volume']):.3f}{fades}[bed];"
        f"[0:a]volume={float(config.get('voice_volume', 1)):.3f},apad=pad_dur={duration:.3f}[voice];"
        "[voice][bed]amix=inputs=2:duration=longest:normalize=0[mixed]"
    )


def music_input_args(track: Path, duration: float) -> list[str]:
    """Loop a real track and trim its input cleanly to the narration duration."""
    return ["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", str(track)]


def replace_scene_video(state: dict, project_id: str, title: str, scene_number: int, revision: int, settings: Settings) -> None:
    """Replace one picture interval while copying the finished audio unchanged."""
    from .exporter import ExportUnavailable, exported_video_path

    root = settings.render_root.resolve()
    project_root = (root / project_id).resolve()
    url = str(state.get("render", {}).get("url", ""))
    source = (root / url.removeprefix("/media/")).resolve()
    if not url.startswith("/media/") or not source.is_relative_to(project_root) or not source.is_file():
        try:
            source = exported_video_path(project_id, title, state.get("export", {}), settings)
        except ExportUnavailable as exc:
            raise RenderUnavailable("Render this project before replacing a scene.") from exc
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderUnavailable("FFmpeg is unavailable.")
    scene = state["scenes"][scene_number - 1]
    start, end = float(scene["start"]), float(scene["end"])
    output = project_root / "renders" / f"v{revision}" / "clipforge.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipforge-replace-") as name:
        temp = Path(name)
        segment = _create_visual_segment(ffmpeg, state, scene, scene_number - 1, end - start, temp, settings, require_real_media=True)
        scene.pop("render_segment", None)
        # Render the existing caption/attention timeline onto just the replacement.
        captions = _write_ass_captions(state, float(state["timeline"]["duration"]), temp)
        graph = f"[1:v]setpts=PTS-STARTPTS+{start:.6f}/TB"
        if state.get("captions", {}).get("enabled", True) or state.get("attention_events"):
            graph += f",ass={captions}"
        graph += f"[replacement];[0:v][replacement]overlay=eof_action=pass:repeatlast=0:enable='gte(t,{start:.6f})*lt(t,{end:.6f})'[video]"
        command = [ffmpeg, "-y", "-v", "error", "-i", str(source), "-i", str(segment), "-filter_complex", graph, "-map", "[video]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy", "-movflags", "+faststart", str(output)]
        result = _run_process(command, timeout=180, failure="Scene replacement timed out.")
        if result.returncode != 0 or not output.is_file():
            raise RenderUnavailable("Scene replacement could not be rendered.")
    # Audio remixes must use the newly replaced picture even after export cleanup.
    retained = project_root / "replacements" / f"picture-v{revision}.mp4"
    retained.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output, retained)
    state.setdefault("voice", {})["picture_source"] = retained.relative_to(root).as_posix()
    state.pop("export", None)
    state["render"].update(status="complete", stale=False, exported=False, revision=revision, url=f"/media/{output.relative_to(root).as_posix()}", file_size=output.stat().st_size)


def _create_music_track(
    _ffmpeg: str, state: dict, _duration: float, _temp: Path
) -> Path | None:
    if not state.get("music", {}).get("enabled") and not state.get("music", {}).get("requested_enabled"):
        return None
    # A selected track must be a real, validated local audio asset.  Never
    # synthesize a tone as a fallback when the library is empty or stale.
    return attach_discovered_track(state)


def remix_project_audio(state: dict, project_id: str, revision: int, settings: Settings) -> None:
    """Copy the finished picture and mix immutable narration/music sources."""
    root = settings.render_root.resolve()
    project_root = (root / project_id).resolve()
    if project_root.parent != root:
        raise RenderUnavailable("Invalid project audio path.")
    voice = state.setdefault("voice", {})
    source = (root / str(voice.get("mix_source", ""))).resolve()
    picture = (root / str(voice.get("picture_source", ""))).resolve()
    if not source.is_file() or not picture.is_file():
        # Upgrade pre-controls projects only when their original narration exists.
        candidates = list((project_root / "audio").glob("narration-*"))
        url = str(state.get("render", {}).get("url", ""))
        picture = (root / url.removeprefix("/media/")).resolve()
        if len(candidates) != 1 or not url.startswith("/media/") or not picture.is_file():
            raise RenderUnavailable("Original audio is unavailable. Render this project once to enable audio controls.")
        source = candidates[0].resolve()
    if not source.is_relative_to(project_root) or not picture.is_relative_to(project_root):
        raise RenderUnavailable("Invalid project audio sources.")
    layers = project_root / "audio-layers" / f"v{revision}"
    layers.mkdir(parents=True, exist_ok=True)
    if not voice.get("mix_source"):
        shutil.copy2(source, layers / "narration.wav")
        shutil.copy2(picture, layers / "picture.mp4")
        source, picture = layers / "narration.wav", layers / "picture.mp4"
        voice.update(mix_source=source.relative_to(root).as_posix(), picture_source=picture.relative_to(root).as_posix())
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderUnavailable("FFmpeg is unavailable.")
    duration = _audio_duration(ffmpeg, picture)
    output = project_root / "renders" / f"v{revision}" / "clipforge.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-v", "error", "-i", str(picture), "-i", str(source)]
    music = state.setdefault("music", {})
    track = resolve_track_path(music) if music.get("enabled") else None
    if music.get("enabled") and track is None:
        raise RenderUnavailable("The selected music file is unavailable.")
    if track:
        command.extend(music_input_args(track, duration))
        command.extend(["-filter_complex", music_filter_graph(music_render_config(state), duration), "-map", "0:v:0", "-map", "[mixed]"])
    else:
        command.extend(["-map", "0:v:0", "-map", "1:a:0", "-af", f"volume={voice['volume']:.3f},apad"])
    command.extend(["-t", f"{duration:.3f}", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(output)])
    result = _run_process(command, timeout=180, failure="Audio mix timed out.")
    if result.returncode != 0 or not output.is_file():
        raise RenderUnavailable("Audio mix failed.")
    music["status"] = "mixed" if track else "disabled"
    state.pop("export", None)
    state["render"].update(status="complete", stale=False, exported=False, url=f"/media/{output.relative_to(root).as_posix()}", file_size=output.stat().st_size, revision=revision)


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _create_voice(state: dict, temp: Path, settings: Settings) -> tuple[Path, str]:
    raw_text = str(state["script"]["text"])
    text = clean_narration_text(raw_text)
    if text != raw_text or contamination_issues(raw_text):
        raise RenderUnavailable("Narration validation failed before voice generation.")
    text = text[:4096]
    requested_provider = str(state.get("voice", {}).get("provider") or "").casefold()
    if requested_provider in {"", "auto", "openai_or_system", "cached"}:
        requested_provider = "openai" if settings.openai_api_key else "system"
    elif requested_provider == "macos_say" and settings.openai_api_key and str(state.get("voice", {}).get("model") or "system") == "system":
        # Legacy states created before provider persistence used macos_say as a
        # placeholder; upgrade them to the configured provider on rerender.
        requested_provider = "openai"
    if requested_provider == "openai":
        if not settings.openai_api_key:
            raise VoiceGenerationError(
                "OpenAI voice is selected but no OpenAI key is configured.",
                category="openai_not_configured",
                status_code=503,
            )
        output = temp / "voice.wav"
        try:
            voice_id = str(state["voice"].get("voice_id") or "marin")
            if voice_id not in OPENAI_VOICES:
                voice_id = "marin"
            model = str(state["voice"].get("model") or settings.openai_tts_model)
            state["voice"].update(provider="openai", model=model)
            response = OpenAI(api_key=settings.openai_api_key).audio.speech.create(
                model=model,
                voice=voice_id,
                input=text,
                instructions=tts_instructions(state["voice"], state["intent"]["language"]),
                response_format="wav",
                speed=max(0.25, min(4.0, float(state["voice"].get("speed") or 1.0))),
            )
            if len(response.content) <= 4096:
                raise OSError("Narration response was empty")
            output.write_bytes(response.content)
            state["voice"].update(cached=False, audio_source="generated")
            return output, "openai"
        except (OpenAIError, OSError) as exc:
            raise _openai_voice_error(exc) from exc
    if requested_provider not in {"system", "macos_say"}:
        raise RenderUnavailable(f"Unsupported voice provider: {requested_provider}")
    state["voice"].update(provider="macos_say", model="system")
    say = shutil.which("say")
    if not say:
        raise RenderUnavailable("No voice provider is available.")
    output = temp / "voice.aiff"
    voice = _system_voice(say, state["voice"], state["intent"]["language"])
    rate = max(
        140,
        round(state["script"]["word_count"] / max(1, state["duration"]["estimated_seconds"]) * 60),
    )
    rate = round(rate * max(0.7, min(1.4, float(state["voice"].get("speed") or 1.0))))
    command = [say]
    if voice:
        command.extend(["-v", voice])
    command.extend(["-r", str(rate), "-o", str(output), text])
    completed = _run_process(
        command,
        timeout=90,
        failure="System narration did not finish in time.",
    )
    if completed.returncode != 0 or not output.exists() or output.stat().st_size <= 4096:
        raise RenderUnavailable(completed.stderr.strip() or "System voice generation failed")
    state["voice"].update(cached=False, audio_source="generated")
    # Keep the renderer result label backwards-compatible while persisted state
    # clearly records the intentional local/system provider.
    return output, "macos_say"


def _openai_voice_error(exc: OpenAIError | OSError) -> VoiceGenerationError:
    if isinstance(exc, AuthenticationError):
        return VoiceGenerationError(
            "OpenAI voice authentication failed. Update the OpenAI key in Settings.",
            category="openai_authentication_error",
            status_code=401,
        )
    if isinstance(exc, PermissionDeniedError):
        return VoiceGenerationError(
            "OpenAI rejected voice access for this key. Check its project permissions.",
            category="openai_permission_error",
            status_code=403,
        )
    if isinstance(exc, RateLimitError):
        return VoiceGenerationError(
            "OpenAI voice generation is unavailable because the account is rate-limited or out of quota. Check billing and usage, then try again.",
            category="openai_quota_error",
            status_code=429,
        )
    if isinstance(exc, BadRequestError):
        return VoiceGenerationError(
            "OpenAI rejected the selected voice or speech settings. Check the model and voice, then try again.",
            category="openai_voice_rejected",
            status_code=422,
        )
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return VoiceGenerationError(
            "OpenAI voice generation could not be reached. Try again when the provider connection is available.",
            category="openai_provider_unavailable",
        )
    if isinstance(exc, OSError):
        return VoiceGenerationError(
            "OpenAI returned unusable voice audio. Try again or check the configured speech model.",
            category="openai_invalid_audio",
        )
    return VoiceGenerationError(
        "OpenAI voice generation failed. Check the configured voice provider and try again.",
        category="openai_voice_error",
    )


def _cached_voice(
    state: dict,
    project_id: str,
    temp: Path,
    settings: Settings,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[Path, str]:
    requested_provider = str(state.get("voice", {}).get("provider") or "").casefold()
    if requested_provider in {"", "auto", "openai_or_system", "cached"}:
        requested_provider = "openai" if settings.openai_api_key else "system"
    elif requested_provider == "macos_say" and settings.openai_api_key and str(state.get("voice", {}).get("model") or "system") == "system":
        requested_provider = "openai"
    model = str(state.get("voice", {}).get("model") or (settings.openai_tts_model if requested_provider == "openai" else "system"))
    voice_input = {
        "provider": requested_provider,
        "model": model,
        "script": state["script"]["text"],
        "language": state["intent"]["language"],
        "voice_id": state["voice"].get("voice_id"),
        "tone": state["voice"].get("tone"),
        "presentation": state["voice"].get("gender_presentation"),
        "speed": state["voice"].get("speed"),
        "instructions": tts_instructions(state["voice"], state["intent"]["language"]),
    }
    key = hashlib.sha256(
        json.dumps(voice_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    cache_root = settings.render_root.resolve() / project_id / "audio"
    for suffix in (".wav", ".aiff"):
        cached = cache_root / f"narration-{key}{suffix}"
        if cached.exists() and cached.stat().st_size > 4096:
            state["voice"].update(provider=requested_provider, model=model, cached=True, audio_source="cache")
            report_progress(
                progress,
                "voice",
                "Generating narration",
                phase="complete",
                cached=True,
            )
            return cached, requested_provider
    generated, provider = _create_voice(state, temp, settings)
    cache_root.mkdir(parents=True, exist_ok=True)
    cached = cache_root / f"narration-{key}{generated.suffix}"
    shutil.copy2(generated, cached)
    report_progress(
        progress, "voice", "Generating narration", phase="complete"
    )
    return cached, provider


def _system_voice(say: str, voice: dict, language: str) -> str | None:
    try:
        result = subprocess.run(
            [say, "-v", "?"], capture_output=True, text=True, timeout=10, check=False
        )
        available = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return None
    presentation = voice.get("gender_presentation")
    if language == "de":
        preferred = (
            ["Markus", "Yannick", "Martin"]
            if presentation == "masculine"
            else ["Anna", "Petra"]
        )
    else:
        preferred = (
            ["Daniel", "Alex", "Reed"]
            if presentation == "masculine"
            else ["Samantha", "Ava", "Karen"]
        )
    return next((candidate for candidate in preferred if candidate in available), None)


def _scene_media_path(scene: dict, settings: Settings) -> tuple[Path | None, str]:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    cache_path = media.get("cache_path")
    kind = str(media.get("kind") or "")
    if not cache_path or not is_scene_asset_allowed(media):
        return None, "real_media_unavailable"
    root = settings.render_root.resolve()
    candidate = (root / str(cache_path)).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None, "real_media_unavailable"
    return candidate, kind


def overlay_placement(state: dict, center_y: float) -> str:
    """Keep overlays out of the caption band and off the focal subject."""
    captions = state.get("captions") if isinstance(state.get("captions"), dict) else {}
    if captions.get("enabled", True) and captions.get("position") == "upper":
        return "lower"
    return "lower" if center_y < 0.42 else "upper"


def overlay_footprint(path: Path) -> dict[str, Any]:
    """Share of the frame an overlay occupies: its bounding box and its drawn pixels."""
    try:
        with Image.open(path) as image:
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            if alpha is None:
                return {"bbox": [0.0, 0.0, 1.0, 1.0], "bbox_area": 1.0, "coverage": 1.0}
            width, height = image.size
            bbox = alpha.getbbox()
            drawn = alpha.point(lambda value: 255 if value > 40 else 0).histogram()[255]
    except (OSError, UnidentifiedImageError, ValueError):
        return {}
    if bbox is None:
        return {"bbox": None, "bbox_area": 0.0, "coverage": 0.0}
    left, top, right, bottom = bbox
    return {
        "bbox": [round(left / width, 4), round(top / height, 4), round(right / width, 4), round(bottom / height, 4)],
        "bbox_area": round((right - left) * (bottom - top) / (width * height), 4),
        "coverage": round(drawn / (width * height), 4),
    }


def _render_scene_overlays(
    state: dict, scene: dict, index: int, temp: Path, width: int, height: int, center_y: float
) -> list[Path]:
    overlays = [item for item in scene.get("overlays") or [] if isinstance(item, dict) and isinstance(item.get("spec"), dict)]
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    adjustments = scene.get("render_adjustments") if isinstance(scene.get("render_adjustments"), dict) else {}
    adjust = adjustments.get("overlay") if isinstance(adjustments.get("overlay"), dict) else {}
    segment = scene.setdefault("render_segment", {})
    segment["overlays"] = []
    if not overlays or media_source(media) == GRAPHIC_ASSET_SOURCE or adjust.get("mode") == "remove":
        return []
    placement = adjust.get("placement") if adjust.get("placement") in {"upper", "lower"} else overlay_placement(state, center_y)
    compact = adjust.get("mode") == "compact"
    style = "minimal" if compact else "pill"
    paths: list[Path] = []
    for position, overlay in enumerate(overlays):
        spec = compact_overlay_spec(overlay["spec"]) if compact else overlay["spec"]
        try:
            path = render_overlay(spec, temp / f"overlay-{index:02d}-{position}.png", width=width, height=height, placement=placement, style=style)
        except (GraphicSpecError, OSError, ValueError):
            continue  # an overlay is never worth failing the scene
        paths.append(path)
        segment["overlays"].append({
            "kind": spec.get("kind") if isinstance(spec, dict) else overlay.get("kind"),
            "spec": spec,
            "placement": placement,
            "style": style,
            **overlay_footprint(path),
        })
    if paths:
        scene["overlay_render"] = {"placement": placement, "count": len(paths)}
    return paths


_ZOOM_STEPS = {"fast_cut": 0.0012, "dynamic": 0.0010, "subtle_pan": 0.0006, "slow_push": 0.0003}
# Horizontal drift of the zoomed window across the 9:16 work frame (it always
# shows >= 92% of it, so the focal subject stays in view), ending on the side
# of the focal point.
_PAN_RANGE = (0.2, 0.8)


def still_motion_plan(scene: dict, index: int, motion: str, center_x: float, frames: int) -> dict:
    """Deterministic, subtle Ken Burns treatment for photos and generated stills.

    Scenes alternate push-in, pan across the focal point and pull-out so a run
    of stills does not feel identical.  A still without a motion setting stays
    static; simple graphics only get a very gentle push so text stays inside.
    """
    step = _ZOOM_STEPS.get(motion, 0.0)
    if not step:
        return {"type": "static", "max_zoom": 1.0, "zoom": "min(zoom+0.0000,1.0)", "x": f"{center_x:.4f}"}
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    if media_source(media) == GRAPHIC_ASSET_SOURCE:
        return {"type": "push_in", "max_zoom": 1.03, "zoom": f"min(zoom+{min(step, 0.0004):.4f},1.03)", "x": "0.5000"}
    max_zoom = 1.08
    pattern = ("push_in", "pan", "pull_out")[index % 3]
    if pattern == "pull_out":
        return {
            "type": "pull_out",
            "max_zoom": max_zoom,
            "zoom": f"if(eq(on,0),{max_zoom},max(zoom-{step:.4f},1.0))",
            "x": f"{center_x:.4f}",
        }
    if pattern == "pan":
        start, end = _PAN_RANGE if center_x >= 0.5 else _PAN_RANGE[::-1]
        return {
            "type": "pan",
            "max_zoom": max_zoom,
            "zoom": f"{max_zoom - 0.02:.2f}",
            "x": f"({start:.4f}+{end - start:.4f}*on/{max(1, frames - 1)})",
        }
    return {"type": "push_in", "max_zoom": max_zoom, "zoom": f"min(zoom+{step:.4f},{max_zoom})", "x": f"{center_x:.4f}"}


def _create_visual_segment(
    ffmpeg: str,
    state: dict,
    scene: dict,
    index: int,
    duration: float,
    temp: Path,
    settings: Settings,
    *,
    require_real_media: bool = False,
    cache_dir: Path | None = None,
) -> Path:
    width = int(state["timeline"]["width"])
    height = int(state["timeline"]["height"])
    fps = int(state["timeline"]["fps"])
    source, kind = _scene_media_path(scene, settings)
    if source is None:
        if require_real_media:
            raise RenderUnavailable("The replacement media is unavailable. Choose another real image or video.")
        strategy = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
        for other in state.get("scenes", []):
            if not isinstance(other.get("media"), dict) or not _reuse_safe(other["media"], strategy):
                continue
            source, kind = _scene_media_path(other, settings)
            if source is not None:
                scene["media"] = dict(other["media"])
                scene["asset_status"] = "real_media_reused"
                break
        if source is None:
            raise RenderUnavailable("No real scene media is available. Retry media discovery; text cards are disabled.")
    output = temp / f"segment-{index:02d}.mp4"
    adjustments = scene.get("render_adjustments") if isinstance(scene.get("render_adjustments"), dict) else {}
    smart_crop = analyze_scene_media(scene, state, settings)
    crop_override = adjustments.get("crop") if isinstance(adjustments.get("crop"), dict) else None
    if crop_override and media_source(scene.get("media") or {}) != GRAPHIC_ASSET_SOURCE:
        # A targeted repair (Final Video Critic) chose this focal point.
        smart_crop = {
            **(smart_crop or {}),
            "status": "adjusted",
            "mode": "critic_adjusted",
            "center_x": min(1.0, max(0.0, float(crop_override.get("center_x", 0.5)))),
            "center_y": min(1.0, max(0.0, float(crop_override.get("center_y", 0.5)))),
        }
    if smart_crop:
        scene["smart_crop"] = smart_crop
    center_x = min(1.0, max(0.0, float((smart_crop or {}).get("center_x", 0.5))))
    center_y = min(1.0, max(0.0, float((smart_crop or {}).get("center_y", 0.5))))
    base = [ffmpeg, "-y", "-v", "error"]
    if kind == "video":
        # Integer crop origins stay fixed for the whole clip so source motion is
        # preserved without sub-pixel re-rounding wobble.
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:x='trunc((in_w-{width})*{center_x:.4f})':"
            f"y='trunc((in_h-{height})*{center_y:.4f})',fps={fps},"
            f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p"
        )
        inputs, base_filter = ["-i", str(source), "-an"], video_filter
    else:
        frames = max(1, round(duration * fps))
        motion = "" if adjustments.get("motion") == "static" else str(scene.get("motion") or "")
        plan = still_motion_plan(scene, index, motion, center_x, frames)
        scene["still_motion"] = {key: plan[key] for key in ("type", "max_zoom")}
        max_zoom = plan["max_zoom"]
        # Keep motion bounded and render it at 2x before the final downscale. At
        # output resolution, integer zoompan windows turn smooth motion into a
        # visible 1px staircase even when their origins are truncated.
        render_scale = 2
        work_w = int(width * max_zoom * render_scale)
        work_h = int(height * max_zoom * render_scale)
        output_w = width * render_scale
        output_h = height * render_scale
        # zoompan crops iw/zoom x ih/zoom of its *input* and scales that to
        # ``s``; cropping to the exact target aspect around the focal point
        # first keeps non-9:16 photos and generated images undistorted.
        zoom_filter = (
            f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
            f"crop={work_w}:{work_h}:x='trunc((in_w-{work_w})*{center_x:.4f})':"
            f"y='trunc((in_h-{work_h})*{center_y:.4f})',"
            f"zoompan=z='{plan['zoom']}':"
            f"x='trunc((iw-iw/zoom)*{plan['x']})':"
            f"y='trunc((ih-ih/zoom)*{center_y:.4f})':"
            f"d={frames}:s={output_w}x{output_h}:fps={fps},"
            f"scale={width}:{height}:flags=lanczos,format=yuv420p"
        )
        inputs, base_filter = ["-loop", "1", "-i", str(source)], zoom_filter
    overlay_paths = _render_scene_overlays(state, scene, index, temp, width, height, center_y)
    if overlay_paths:
        # Base visual + transparent information overlays, composited per scene;
        # captions are burned in later over the whole timeline.
        graph = f"[0:v]{base_filter}[base]"
        last = "base"
        for position, _path in enumerate(overlay_paths, 1):
            graph += f";[{last}][{position}:v]overlay=0:0:format=auto[ov{position}]"
            last = f"ov{position}"
        graph += f";[{last}]format=yuv420p[vout]"
        overlay_inputs = [arg for path in overlay_paths for arg in ("-loop", "1", "-i", str(path))]
        command = [*base, *inputs, *overlay_inputs, "-filter_complex", graph, "-map", "[vout]"]
    else:
        command = [*base, *inputs, "-vf", base_filter]
    command.extend(
        [
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    segment = scene.setdefault("render_segment", {})
    cached = _segment_cache_path(cache_dir, command, source, overlay_paths)
    segment["cache_file"] = str(cached) if cached is not None else None
    if cached is not None and cached.is_file() and cached.stat().st_size > 0:
        segment["cache"] = "hit"
        return cached
    command.append(str(output))
    completed = _run_process(
        command, timeout=120, failure="A visual scene did not finish rendering."
    )
    if completed.returncode != 0 or not output.exists():
        output.unlink(missing_ok=True)
        raise RenderUnavailable(completed.stderr.strip()[-500:] or "A visual scene could not be rendered.")
    segment["cache"] = "miss" if cached is not None else "disabled"
    if cached is not None:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            staging = cached.with_suffix(".partial")
            shutil.copy2(output, staging)
            staging.replace(cached)
        except OSError:
            pass  # the cache is an optimisation only
    return output


def _segment_cache_path(cache_dir: Path | None, command: list[str], source: Path, overlays: list[Path]) -> Path | None:
    """Content key of one segment: everything that decides its pixels."""
    if cache_dir is None:
        return None
    try:
        stat = source.stat()
        replacements = {str(source): f"source:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"}
        for path in overlays:
            replacements[str(path)] = "overlay:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    material = [replacements.get(part, part) for part in command[1:]]
    key = hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()[:24]
    return cache_dir / f"{key}.mp4"


def _audio_duration(ffmpeg: str, audio: Path) -> float:
    probe = _run_process(
        [ffmpeg, "-i", str(audio)],
        timeout=30,
        failure="Generated narration could not be measured.",
    )
    match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        raise RenderUnavailable("Could not measure generated narration.")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _draw_scene(state: dict, scene: dict, index: int, temp: Path) -> Path:
    """Legacy entrypoint: synthetic text visuals are permanently disabled."""
    raise RenderUnavailable("Synthetic scene cards are disabled. Real image/video media is required.")
