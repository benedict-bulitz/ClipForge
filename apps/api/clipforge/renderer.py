import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from openai import OpenAI, OpenAIError
from PIL import Image, ImageDraw, ImageFont

from .config import Settings


class RenderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderResult:
    url: str
    actual_seconds: float
    voice_provider: str
    file_size: int


def ffmpeg_path() -> str | None:
    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if Path(path).exists() else None
    except (OSError, RuntimeError):
        return shutil.which("ffmpeg")


def readiness(settings: Settings) -> dict[str, dict[str, str | bool | None]]:
    system_voice = shutil.which("say")
    ffmpeg = ffmpeg_path()
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
            "status": "Pexels" if settings.pexels_api_key else "Generated scene cards",
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
    state: dict, project_id: str, revision_number: int, settings: Settings
) -> RenderResult:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderUnavailable("FFmpeg is unavailable. Open Build readiness for installation help.")
    if state["render"]["status"] == "blocked_by_research":
        raise RenderUnavailable("This factual project has no attributable research yet.")

    output_dir = settings.render_root.resolve() / project_id / "renders" / f"v{revision_number}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "clipforge.mp4"
    with tempfile.TemporaryDirectory(prefix="clipforge-") as temp_name:
        temp = Path(temp_name)
        audio, provider = _create_voice(state, temp, settings)
        actual = _audio_duration(ffmpeg, audio)
        target = min(float(state["duration"]["max_seconds"]), max(4.0, actual))
        scenes = state["scenes"]
        total_weight = max(1.0, sum(max(0.1, scene["end"] - scene["start"]) for scene in scenes))
        durations = [target * max(0.1, scene["end"] - scene["start"]) / total_weight for scene in scenes]
        image_paths = [
            _draw_scene(state, scene, index, temp)
            for index, scene in enumerate(scenes)
        ]
        concat = temp / "scenes.txt"
        lines: list[str] = []
        for image_path, duration in zip(image_paths, durations, strict=True):
            escaped = str(image_path).replace("'", "'\\''")
            lines.extend([f"file '{escaped}'", f"duration {duration:.3f}"])
        lines.append(f"file '{str(image_paths[-1]).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
            "-t",
            f"{target:.3f}",
            "-vf",
            f"fps={state['timeline']['fps']},format=yuv420p",
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
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=180, check=False
        )
        if completed.returncode != 0:
            raise RenderUnavailable(completed.stderr.strip()[-500:] or "FFmpeg render failed")
        verify = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(output), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if verify.returncode != 0 or not output.exists() or output.stat().st_size < 10_000:
            raise RenderUnavailable(verify.stderr.strip()[-500:] or "Rendered file failed quality checks")
    relative = output.relative_to(settings.render_root.resolve()).as_posix()
    return RenderResult(
        url=f"/media/{relative}",
        actual_seconds=round(target, 2),
        voice_provider=provider,
        file_size=output.stat().st_size,
    )


def _create_voice(state: dict, temp: Path, settings: Settings) -> tuple[Path, str]:
    text = state["script"]["text"][:4096]
    if settings.openai_api_key:
        output = temp / "voice.wav"
        try:
            response = OpenAI(api_key=settings.openai_api_key).audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="marin",
                input=text,
                instructions=f"Natural {state['intent']['language']} narration. {state['voice']['profile']}.",
                response_format="wav",
            )
            output.write_bytes(response.content)
            return output, "openai"
        except (OpenAIError, OSError) as exc:
            if not shutil.which("say"):
                raise RenderUnavailable(f"OpenAI TTS failed: {exc}") from exc
    say = shutil.which("say")
    if not say:
        raise RenderUnavailable("No voice provider is available.")
    output = temp / "voice.aiff"
    voice = "Anna" if state["intent"]["language"] == "de" else "Samantha"
    rate = max(
        140,
        round(state["script"]["word_count"] / max(1, state["duration"]["estimated_seconds"]) * 60),
    )
    completed = subprocess.run(
        [say, "-v", voice, "-r", str(rate), "-o", str(output), text],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0 or not output.exists():
        raise RenderUnavailable(completed.stderr.strip() or "System voice generation failed")
    return output, "macos_say"


def _audio_duration(ffmpeg: str, audio: Path) -> float:
    probe = subprocess.run(
        [ffmpeg, "-i", str(audio)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        raise RenderUnavailable("Could not measure generated narration.")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _draw_scene(state: dict, scene: dict, index: int, temp: Path) -> Path:
    width = int(state["timeline"]["width"])
    height = int(state["timeline"]["height"])
    palettes = [
        ("#171714", "#ff6838"),
        ("#243246", "#82a6c8"),
        ("#4f341f", "#e3b362"),
        ("#27382f", "#89a887"),
    ]
    background, accent = palettes[index % len(palettes)]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    unit = min(width, height)
    draw.ellipse(
        (width * 0.52, -unit * 0.14, width * 1.12, unit * 0.46),
        fill=accent,
    )
    draw.rectangle((width * 0.07, height * 0.1, width * 0.085, height * 0.26), fill=accent)
    font = _font(max(30, int(unit * 0.063)), bold=True)
    small = _font(max(18, int(unit * 0.018)), bold=True)
    role = scene.get("block_id", f"scene {index + 1}").replace("voice_block_", "SCENE ")
    draw.text((width * 0.08, height * 0.1), role.upper(), font=small, fill="#f7f5ee")
    chars = 18 if width < height else 38
    wrapped = textwrap.fill(scene["narration"], width=chars)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=int(unit * 0.018))
    text_height = bbox[3] - bbox[1]
    draw.multiline_text(
        (width * 0.08, (height - text_height) * 0.58),
        wrapped,
        font=font,
        fill="#ffffff",
        spacing=int(unit * 0.018),
    )
    output = temp / f"scene-{index:02d}.png"
    image.save(output, optimize=True)
    return output


def _font(size: int, *, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()
