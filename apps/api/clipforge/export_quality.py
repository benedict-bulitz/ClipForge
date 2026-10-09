from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("clipforge.export_quality")

def run_export_quality_checks(state: dict[str, Any], render_path: Path, max_duration: int = 180) -> list[dict[str, str]]:
    """Export Quality Assurance V1: Detect technical defects in finished Knowledge Shorts."""
    issues = []
    if not render_path.exists() or not render_path.is_file():
        issues.append({"code": "file_missing", "severity": "error", "message": "Rendered file does not exist."})
        return issues
        
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        issues.append({"code": "missing_dependencies", "severity": "warning", "message": "FFprobe/FFmpeg unavailable for quality checks."})
        return issues

    try:
        cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(render_path)]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            issues.append({"code": "corrupt_media", "severity": "error", "message": "File is corrupt or unreadable."})
            return issues
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        issues.append({"code": "ffprobe_error", "severity": "error", "message": f"FFprobe failed: {exc}"})
        return issues

    format_info = payload.get("format", {})
    streams = payload.get("streams", [])
    
    # 2. Duration
    try:
        actual_duration = float(format_info.get("duration", 0))
    except ValueError:
        actual_duration = 0.0

    if actual_duration > max_duration + 1.0:
        issues.append({"code": "duration_exceeds_limit", "severity": "error", "message": f"Video duration {actual_duration:.1f}s exceeds limit {max_duration}s."})
    elif actual_duration < 1.0:
        issues.append({"code": "duration_too_short", "severity": "error", "message": f"Video duration {actual_duration:.1f}s is abnormally short."})
        
    timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    expected_duration = timeline.get("duration")
    if expected_duration and abs(actual_duration - float(expected_duration)) > 2.0:
        issues.append({"code": "duration_mismatch", "severity": "error", "message": f"Output duration {actual_duration:.1f}s mismatches timeline {expected_duration:.1f}s."})

    # 9. Narration/video duration inconsistencies
    voice = state.get("voice") if isinstance(state.get("voice"), dict) else {}
    if voice.get("enabled", True):
        voice_blocks = voice.get("blocks", [])
        if voice_blocks:
            last_block = voice_blocks[-1]
            narration_end = last_block.get("end", 0.0)
            # Narration should not end way after the video or finish way before the video ends (unless intentional music outro)
            if narration_end > actual_duration + 0.5:
                issues.append({"code": "narration_overflow", "severity": "error", "message": f"Narration ends at {narration_end:.1f}s but video is only {actual_duration:.1f}s long."})

    # 3. Dimensions
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        issues.append({"code": "missing_video_stream", "severity": "error", "message": "No video stream found."})
    else:
        actual_width = video_stream.get("width")
        actual_height = video_stream.get("height")
        expected_width = timeline.get("width", 1080)
        expected_height = timeline.get("height", 1920)
        
        if actual_width != expected_width or actual_height != expected_height:
            issues.append({
                "code": "dimension_mismatch", 
                "severity": "error", 
                "message": f"Dimensions {actual_width}x{actual_height} do not match configured {expected_width}x{expected_height}."
            })

    # 4. Audio stream
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if voice.get("enabled", True) and not audio_stream:
        issues.append({"code": "missing_audio_stream", "severity": "error", "message": "Audio stream missing but narration is enabled."})

    # 5, 6, 7. Silence, Black frames, Frozen frames
    try:
        filter_cmd = [
            ffmpeg, "-hide_banner", "-v", "info", "-i", str(render_path),
            "-vf", "blackdetect=d=0.5:pix_th=0.01,freezedetect=n=0.003:d=2",
            "-af", "silencedetect=noise=-50dB:d=2",
            "-f", "null", "-"
        ]
        analyze = subprocess.run(filter_cmd, capture_output=True, text=True, timeout=60, check=False)
        stderr = analyze.stderr
        
        if "black_duration:" in stderr:
            issues.append({"code": "unintended_black_frames", "severity": "warning", "message": "Detected unintended black frames in the video."})

        if "silence_duration:" in stderr:
            issues.append({"code": "unexpected_audio_silence", "severity": "warning", "message": "Detected unexpected long audio silence."})
            
        if "lavfi.freezedetect.freeze_start" in stderr:
            issues.append({"code": "suspicious_frozen_frames", "severity": "warning", "message": "Detected suspicious frozen frames in the video."})

    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not run ffmpeg analyze filters: {exc}")

    # 8. Missing or corrupt media
    # We check if any scene in state failed to find its media
    scenes = state.get("scenes", [])
    for scene in scenes:
        if scene.get("asset_status") == "missing_media" or (isinstance(scene.get("media"), dict) and scene["media"].get("status") == "missing"):
            issues.append({"code": "missing_media", "severity": "error", "message": f"Scene {scene.get('id')} has missing media."})
            break

    return issues

