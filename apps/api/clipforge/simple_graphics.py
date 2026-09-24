"""Small deterministic explanatory graphics for Visual Director V2.

Three layouts only: a large number/statistic, an A-vs-B comparison and a short
process chain.  Text comes from the scene's own spec (never invented here);
layouts keep the lower quarter free for captions and the top band free for
attention callouts.  No topic-specific templates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

GRAPHIC_KINDS = ("number", "comparison", "process")
MAX_LABEL_CHARS = 42
MAX_STEPS = 3

_BACKGROUND_TOP = (18, 20, 27)
_BACKGROUND_BOTTOM = (37, 45, 60)
_ACCENT = (255, 104, 56)
_TEXT = (247, 245, 240)
_MUTED = (182, 188, 199)
_CARD = (49, 58, 76)
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
)


class GraphicSpecError(ValueError):
    pass


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _clean(value: object, limit: int = MAX_LABEL_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit].strip(" ,.;:-")


def normalise_graphic_spec(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validated, bounded copy of a graphic spec, or ``None`` when unusable."""
    if not isinstance(spec, dict) or spec.get("kind") not in GRAPHIC_KINDS:
        return None
    kind = spec["kind"]
    if kind == "number":
        value = _clean(spec.get("value"), 18)
        return {"kind": kind, "value": value, "label": _clean(spec.get("label"))} if value else None
    if kind == "comparison":
        left, right = _clean(spec.get("left")), _clean(spec.get("right"))
        if not left or not right or left.casefold() == right.casefold():
            return None
        return {"kind": kind, "left": left, "right": right}
    steps = [_clean(step) for step in spec.get("steps") or [] if _clean(step)][:MAX_STEPS]
    return {"kind": kind, "steps": steps} if len(steps) >= 2 else None


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _fitted_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, minimum: int) -> ImageFont.ImageFont:
    size = start
    while size > minimum:
        font = _font(size)
        if _text_size(draw, text, font)[0] <= max_width:
            return font
        size -= 4
    return _font(minimum)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int = 2) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and _text_size(draw, trial, font)[0] <= max_width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines[:max_lines]


def _centered(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, center_x: int, top: int, fill: tuple[int, int, int]) -> int:
    width, height = _text_size(draw, text, font)
    left, text_top, _right, _bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - width // 2 - left, top - text_top), text, font=font, fill=fill)
    return height


def _background(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), _BACKGROUND_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(top + (bottom - top) * ratio) for top, bottom in zip(_BACKGROUND_TOP, _BACKGROUND_BOTTOM, strict=True))
        draw.line([(0, y), (width, y)], fill=color)
    return image


def _shared_card_font(draw: ImageDraw.ImageDraw, labels: list[str], width: int) -> ImageFont.ImageFont:
    """One font size for every card so labels read as a set."""
    inner = width - round(width * 0.09) * 2 - round(width * 0.1)
    fonts = [_fitted_font(draw, label, inner, round(width * 0.085), round(width * 0.045)) for label in labels]
    return min(fonts, key=lambda font: getattr(font, "size", 0))


def _card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], label: str, width: int, font: ImageFont.ImageFont) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=round(width * 0.04), fill=_CARD, outline=_ACCENT, width=max(3, width // 270))
    inner = right - left - round(width * 0.1)
    lines = _wrap(draw, label, font, inner)
    line_height = _text_size(draw, "Ag", font)[1] + round(width * 0.015)
    y = top + ((bottom - top) - line_height * len(lines)) // 2
    for line in lines:
        _centered(draw, line, font, (left + right) // 2, y, _TEXT)
        y += line_height


def render_simple_graphic(spec: dict[str, Any], destination: Path, *, width: int, height: int) -> Path:
    """Render one PNG at the timeline resolution; deterministic for a given spec."""
    clean = normalise_graphic_spec(spec)
    if clean is None:
        raise GraphicSpecError("Unsupported or empty graphic spec.")
    image = _background(width, height)
    draw = ImageDraw.Draw(image)
    center_x = width // 2
    margin = round(width * 0.09)
    usable = width - margin * 2
    # Content band: below the attention-callout area, above the caption area.
    top, bottom = round(height * 0.16), round(height * 0.68)
    if clean["kind"] == "number":
        number_font = _fitted_font(draw, clean["value"], usable, round(width * 0.26), round(width * 0.08))
        label_font = _font(round(width * 0.065))
        label_lines = _wrap(draw, clean["label"], label_font, usable) if clean["label"] else []
        number_height = _text_size(draw, clean["value"], number_font)[1]
        label_height = (_text_size(draw, "Ag", label_font)[1] + round(width * 0.02)) * len(label_lines)
        gap = round(width * 0.09)
        y = top + ((bottom - top) - number_height - gap - label_height) // 2
        _centered(draw, clean["value"], number_font, center_x, y, _ACCENT)
        y += number_height + gap // 2
        draw.rounded_rectangle((center_x - margin, y, center_x + margin, y + max(6, width // 150)), radius=4, fill=_TEXT)
        y += gap // 2 + max(6, width // 150)
        for line in label_lines:
            _centered(draw, line, label_font, center_x, y, _TEXT)
            y += _text_size(draw, "Ag", label_font)[1] + round(width * 0.02)
    elif clean["kind"] == "comparison":
        badge = round(width * 0.09)
        card_height = ((bottom - top) - badge * 2) // 2
        first = (margin, top, width - margin, top + card_height)
        second = (margin, bottom - card_height, width - margin, bottom)
        font = _shared_card_font(draw, [clean["left"], clean["right"]], width)
        _card(draw, first, clean["left"], width, font)
        _card(draw, second, clean["right"], width, font)
        middle = (first[3] + second[1]) // 2
        draw.ellipse((center_x - badge, middle - badge, center_x + badge, middle + badge), fill=_ACCENT)
        _centered(draw, "VS", _font(round(badge * 0.8)), center_x, middle - round(badge * 0.4), _TEXT)
    else:
        steps = clean["steps"]
        arrow = round(width * 0.07)
        card_height = min(round(height * 0.13), ((bottom - top) - arrow * 2 * (len(steps) - 1)) // len(steps))
        total = card_height * len(steps) + arrow * 2 * (len(steps) - 1)
        y = top + ((bottom - top) - total) // 2
        font = _shared_card_font(draw, steps, width)
        for index, step in enumerate(steps):
            _card(draw, (margin, y, width - margin, y + card_height), step, width, font)
            y += card_height
            if index < len(steps) - 1:
                tip = y + arrow * 2 - round(arrow * 0.4)
                draw.polygon(
                    [(center_x - arrow, y + round(arrow * 0.4)), (center_x + arrow, y + round(arrow * 0.4)), (center_x, tip)],
                    fill=_ACCENT,
                )
                y += arrow * 2
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination
