"""Small, bounded subject-aware crop analysis for vertical rendering."""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any

from PIL import Image

from .visual_verifier import get_visual_verifier, visual_intent_text

MAX_CROP_CACHE = 128
_CROP_CACHE: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()


def crop_windows(width: int, height: int, target_ratio: float) -> list[tuple[float, float, float, float]]:
    """Return five (or fewer) normalized windows, never exceeding the source."""
    if width <= 0 or height <= 0 or target_ratio <= 0:
        return []
    source_ratio = width / height
    if source_ratio >= target_ratio:
        crop_width, crop_height = height * target_ratio, float(height)
        centers = (0.2, 0.35, 0.5, 0.65, 0.8)
        max_left = max(0.0, 1.0 - crop_width / width)
        return [(min(max_left, max(0.0, center * (1.0 - crop_width / width))), 0.0, crop_width / width, 1.0) for center in centers]
    crop_width, crop_height = float(width), width / target_ratio
    centers = (0.35, 0.5, 0.65)
    max_top = max(0.0, 1.0 - crop_height / height)
    return [(0.0, min(max_top, max(0.0, center * (1.0 - crop_height / height))), 1.0, crop_height / height) for center in centers]


def _crop(image: Image.Image, window: tuple[float, float, float, float]) -> Image.Image:
    left, top, width, height = window
    box = (round(left * image.width), round(top * image.height), round((left + width) * image.width), round((top + height) * image.height))
    return image.crop(box).resize((224, 398))


def _cache_key(scene: dict[str, Any], media: dict[str, Any], target_ratio: float, model: str) -> tuple[str, str, str, str]:
    identity = str(media.get("identity") or media.get("cache_path") or "asset")
    intent = "|".join(visual_intent_text(scene))
    intent_hash = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
    return identity, intent_hash, model, f"{target_ratio:.6f}"


def _put_cache(key: tuple[str, str, str, str], value: dict[str, Any]) -> None:
    _CROP_CACHE[key] = value
    _CROP_CACHE.move_to_end(key)
    while len(_CROP_CACHE) > MAX_CROP_CACHE:
        _CROP_CACHE.popitem(last=False)


def analyze_scene_media(scene: dict[str, Any], state: dict[str, Any], settings: Any, *, verifier: Any | None = None) -> dict[str, Any] | None:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    if not media or media.get("kind") not in {"photo", "video"}:
        return None
    if (scene.get("visual_intent") or {}).get("visual_strategy") == "diagram_or_card":
        return None
    path = (settings.render_root.resolve() / str(media.get("cache_path") or "")).resolve()
    root = settings.render_root.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    visual = verifier or get_visual_verifier()
    model = str(getattr(visual, "model_identity", "metadata-only"))
    target_ratio = int(state["timeline"]["width"]) / int(state["timeline"]["height"])
    key = _cache_key(scene, media, target_ratio, model)
    if key in _CROP_CACHE:
        _CROP_CACHE.move_to_end(key)
        return dict(_CROP_CACHE[key])
    try:
        if getattr(visual, "status", "") != "available":
            return {"status": "fallback", "mode": "center", "center_x": 0.5, "center_y": 0.5, "confidence": 0.0, "model": model}
        if media.get("kind") == "video" and (getattr(visual, "_model", None) is None or path.stat().st_size < 1024):
            return {"status": "fallback", "mode": "center", "center_x": 0.5, "center_y": 0.5, "confidence": 0.0, "model": model}
        image = Image.open(path).convert("RGB") if media.get("kind") == "photo" else None
        frames = [image] if image is not None else visual._extract_frames(path)[:3]
        if not frames:
            return {"status": "fallback", "mode": "center", "center_x": 0.5, "center_y": 0.5, "confidence": 0.0, "model": model}
        windows = crop_windows(frames[0].width, frames[0].height, target_ratio)
        prompts = visual_intent_text(scene)
        scores = []
        for window in windows:
            frame_scores = [visual.score_image(_crop(frame, window), prompts, asset_identity=f"{media.get('identity', path)}:{window}") for frame in frames]
            scores.append((sum(frame_scores) / len(frame_scores), window))
        best_score, best = max(scores, key=lambda row: row[0])
        result = {"status": "verified", "mode": "smart", "center_x": round(best[0] + best[2] / 2, 4), "center_y": round(best[1] + best[3] / 2, 4), "crop_width": round(best[2], 4), "crop_height": round(best[3], 4), "score": round(best_score, 6), "confidence": round(max(0.0, min(1.0, (best_score + 1) / 2)), 4), "model": model}
        _put_cache(key, result)
        return result
    except (OSError, RuntimeError, ValueError, TypeError):
        return {"status": "fallback", "mode": "center", "center_x": 0.5, "center_y": 0.5, "confidence": 0.0, "model": model}
