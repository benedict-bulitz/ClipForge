from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings


class ThumbnailGenerationError(RuntimeError):
    """A project cover could not be built from its persisted media."""


PLATFORMS = ("tiktok", "instagram", "youtube")
WIDTH, HEIGHT = 1080, 1920


def _project_directory(project_id: str, settings: Settings) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", project_id):
        raise ThumbnailGenerationError("The project storage identity is invalid.")
    root = settings.render_root.resolve()
    directory = (root / project_id).resolve()
    if directory.parent != root:
        raise ThumbnailGenerationError("The project storage path is unsafe.")
    return directory


def _cover_text(state: dict[str, Any]) -> str:
    topic = " ".join(str(state.get("intent", {}).get("topic") or state.get("prompt") or "").split())
    topic = topic.rstrip("?.!")
    words = topic.split()
    if not words:
        return "CLIPFORGE"
    text = " ".join(words[:6])
    if len(text) > 34:
        text = text[:34].rsplit(" ", 1)[0]
    return text.upper()


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _source_images(state: dict[str, Any], project_dir: Path) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for scene in state.get("scenes", []):
        media = scene.get("media") if isinstance(scene.get("media"), dict) else None
        if not media or media.get("kind") != "photo":
            continue
        cache_path = str(media.get("cache_path") or "")
        if not cache_path:
            continue
        candidate = (project_dir.parent / cache_path).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(project_dir):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        sources.append((str(scene.get("id") or f"scene-{len(sources) + 1}"), candidate))
    return sources


def _compose(source: Path, destination: Path, text: str, variant: str) -> None:
    try:
        with Image.open(source) as original:
            image = ImageOps.fit(original.convert("RGB"), (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise ThumbnailGenerationError("A project image could not be opened.") from exc
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, HEIGHT * 0.62, WIDTH, HEIGHT), fill=(0, 0, 0, 155))
    draw.text(
        (64, HEIGHT * 0.72),
        text,
        fill=(255, 255, 255, 255),
        font=_font(74 if len(text) < 24 else 58),
        spacing=10,
        stroke_width=2,
        stroke_fill=(0, 0, 0, 220),
    )
    draw.text((64, HEIGHT - 92), variant.upper(), fill=(255, 104, 56, 255), font=_font(24))
    output = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.jpg")
    output.save(temporary, format="JPEG", quality=88, optimize=True)
    temporary.replace(destination)


def build_project_thumbnails(
    state: dict[str, Any], project_id: str, settings: Settings
) -> dict[str, Any]:
    """Build deterministic cover variants from the project's own photo assets."""
    project_dir = _project_directory(project_id, settings)
    sources = _source_images(state, project_dir)
    if not sources:
        return {
            "status": "unavailable",
            "error": "No suitable project image is available for a cover.",
            "selected_variant_id": None,
            "variants": [],
        }
    text = _cover_text(state)
    variants: list[dict[str, Any]] = []
    for index, platform in enumerate(PLATFORMS):
        scene_id, source = sources[index % len(sources)]
        variant_id = f"{platform}-cover-{index + 1}"
        relative = Path(project_id) / "thumbnails" / f"{variant_id}.jpg"
        destination = settings.render_root.resolve() / relative
        _compose(source, destination, text, platform)
        variants.append(
            {
                "id": variant_id,
                "platform": platform,
                "url": f"/media/{relative.as_posix()}",
                "source_scene_id": scene_id,
                "text": text,
                "width": WIDTH,
                "height": HEIGHT,
            }
        )
    return {
        "status": "available",
        "error": None,
        "selected_variant_id": variants[0]["id"],
        "variants": variants,
    }
