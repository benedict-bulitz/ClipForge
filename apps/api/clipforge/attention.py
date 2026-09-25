"""Deterministic, bounded visual attention planning."""
from __future__ import annotations

import re
from typing import Any

INTERVALS = {"low": 2.8, "normal": 1.5, "high": 0.8}
MIN_SPACING = 0.35
MAX_EVENTS = 48
_NUMBER = re.compile(r"(?<!\w)(?:\d+(?:[.,]\d+)?\s*%?|\d+(?:[.,]\d+)?\s*(?:percent|prozent))\b", re.IGNORECASE)
_STEP = re.compile(r"(?i)^(?:first|second|third|then|next|finally|zuerst|erstens|zweitens|danach|zuletzt)\b")
_STOP = {"the", "and", "that", "this", "with", "from", "into", "eine", "einer", "einem", "und", "der", "die", "das", "mit", "für", "von", "wird", "werden"}
_DANGLING = {"stronger", "weaker", "higher", "lower", "more", "less", "stärker", "schwächer", "höher", "niedriger", "mehr", "weniger"}
_SYMBOLS = (
    (re.compile(r"(?i)\b(?:money|cost|saving|savings|dollar|geld|kosten|sparen|ersparnis)\b"), "$"),
    (re.compile(r"(?i)\b(?:increase|increases|growth|grows|rise|rises|increase|zunahme|wachstum|steigt|steigen|anstieg)\b"), "↑"),
    (re.compile(r"(?i)\b(?:decrease|decreases|fall|falls|drop|drops|decrease|abnahme|fällt|sinkt|rückgang)\b"), "↓"),
    (re.compile(r"(?i)\b(?:warning|risk|danger|warnung|risiko|gefahr)\b"), "!"),
    (re.compile(r"(?i)\b(?:time|duration|deadline|zeit|dauer|frist)\b"), "◷"),
)


def resolve_attention_preferences(options: dict[str, Any] | None = None) -> dict[str, Any]:
    source = options or {}
    mode = str(source.get("attention_density") or source.get("density_mode") or "normal").casefold()
    if mode not in {"off", "low", "normal", "high", "custom"}:
        mode = "normal"
    interval = source.get("attention_interval_seconds", source.get("target_interval_seconds"))
    if mode == "custom":
        try:
            interval = max(0.6, min(8.0, float(interval)))
        except (TypeError, ValueError):
            interval = 1.5
    else:
        interval = INTERVALS.get(mode)
    return {"enabled": mode != "off", "density_mode": mode, "target_interval_seconds": interval}


def _words(text: str) -> list[str]:
    return re.findall(r"[\wÄÖÜäöüß%.,]+", text)


def _event_text(text: str, *, german: bool) -> str:
    words = [word.strip(".,!?;:") for word in _words(text)]
    meaningful = [word for word in words if len(word) > 2 and word.casefold() not in _STOP]
    chosen = meaningful[:3]
    while len(chosen) > 1 and chosen[-1].casefold().strip(".,!?;:") in _DANGLING:
        chosen.pop()
    return " ".join(chosen)


def _symbol_for(text: str) -> str | None:
    return next((symbol for pattern, symbol in _SYMBOLS if pattern.search(text)), None)


def _subtitle_duplicate(callout: str, subtitle: str, *, statistic: bool = False) -> bool:
    if statistic:
        return False
    left = {word.casefold().strip(".,!?;:") for word in _words(callout) if len(word) > 2}
    right = {word.casefold().strip(".,!?;:") for word in _words(subtitle) if len(word) > 2}
    return bool(left and right and len(left & right) / min(len(left), len(right)) >= 0.75)


def _overlaps_subtitle(callout: str, start: float, end: float, captions: list[dict[str, Any]], *, statistic: bool = False) -> bool:
    """Reject subtitle-like callouts that would duplicate visible caption text."""
    if statistic:
        return False
    for caption in captions:
        caption_start = float(caption.get("start", 0) or 0)
        caption_end = float(caption.get("end", caption_start) or caption_start)
        if start < caption_end and end > caption_start and _subtitle_duplicate(callout, str(caption.get("text") or "")):
            return True
    return False


def _scene_for_time(scenes: list[dict[str, Any]], start: float, end: float) -> int | None:
    for index, scene in enumerate(scenes):
        scene_start, scene_end = float(scene.get("start", 0)), float(scene.get("end", 0))
        if start < scene_end and end > scene_start:
            return index
    return None


def plan_attention_events(
    narration: str,
    captions: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    preferences: dict[str, Any] | None = None,
    *,
    language: str = "en",
) -> list[dict[str, Any]]:
    prefs = resolve_attention_preferences(preferences)
    if not prefs["enabled"] or not narration.strip() or not scenes:
        return []
    target = float(prefs["target_interval_seconds"] or 1.5)
    german = language == "de"
    opportunities: list[dict[str, Any]] = []
    for item in captions:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = float(item.get("start", 0) or 0)
        end = float(item.get("end", start) or start)
        if end <= start:
            continue
        number = _NUMBER.search(text)
        if number:
            event_type, value = "statistic_callout", number.group(0).strip()
            unit = re.match(r"\s*(?:percent|prozent)\b", text[number.end():], re.IGNORECASE)
            if unit:
                value = f"{value} {unit.group(0).strip()}"
        elif _STEP.search(text):
            event_type, value = "comparison_label", _event_text(text, german=german)
        else:
            symbol = _symbol_for(text)
            value = symbol or _event_text(text, german=german)
            event_type = "icon_or_symbol" if symbol else ("keyword_callout" if value else "text_emphasis")
        if event_type not in {"statistic_callout", "comparison_label", "icon_or_symbol"} and _overlaps_subtitle(value, start, end, captions, statistic=event_type == "statistic_callout"):
            # A generic caption fragment is not a meaningful visual event.
            continue
        if not value and event_type != "graphic_accent":
            continue
        scene_index = _scene_for_time(scenes, start, end)
        if scene_index is None:
            continue
        scene_start, scene_end = float(scenes[scene_index].get("start", 0)), float(scenes[scene_index].get("end", end))
        start = max(scene_start, start)
        end = min(scene_end, end, start + 1.0)
        if end - start < 0.25:
            continue
        opportunities.append({"scene_index": scene_index, "start": start, "duration": end - start, "type": event_type, "text": value, "importance": 1.0 if number else (0.9 if event_type in {"comparison_label", "icon_or_symbol"} else 0.6), "placement": "top", "style": event_type})
    opportunities.sort(key=lambda item: (float(item["start"]), -float(item["importance"])))
    selected: list[dict[str, Any]] = []
    used_text: set[str] = set()
    semantic_by_scene: dict[int, list[dict[str, Any]]] = {}
    for item in opportunities:
        semantic_by_scene.setdefault(int(item["scene_index"]), []).append(item)
    for scene_index, scene in enumerate(scenes):
        scene_start, scene_end = float(scene.get("start", 0)), float(scene.get("end", 0))
        scene_opportunities = semantic_by_scene.get(scene_index, [])
        # The interval is a soft activity preference, never a timer that
        # fabricates shapes. Select meaningful opportunities near the desired
        # cadence, but leave the shot alone when none exists.
        for candidate in scene_opportunities:
            text_key = str(candidate.get("text") or "").casefold().strip()
            if text_key and text_key in used_text:
                continue
            candidate = dict(candidate)
            candidate["start"] = max(scene_start, min(scene_end - 0.12, float(candidate["start"])))
            candidate["duration"] = min(float(candidate.get("duration") or 0.35), scene_end - candidate["start"], 0.7)
            if candidate["duration"] < 0.2 or (selected and candidate["start"] - float(selected[-1]["start"]) < max(MIN_SPACING, target * 0.75)):
                continue
            used_text.add(text_key)
            selected.append(candidate)
    return [dict(event, id=f"attention_{index + 1:03d}", start=round(float(event["start"]), 3), duration=round(float(event["duration"]), 3)) for index, event in enumerate(selected)]


def replan_attention(state: dict[str, Any]) -> list[dict[str, Any]]:
    preferences = resolve_attention_preferences(state.get("attention_preferences") or state.get("options") or {})
    state["attention_preferences"] = preferences
    events = plan_attention_events(
        str(state.get("script", {}).get("text") or ""),
        list(state.get("captions", {}).get("items") or []),
        list(state.get("scenes") or []),
        preferences,
        language=str(state.get("intent", {}).get("language") or "en"),
    )
    from .triple_hook import hook_overlay_spec, is_hook_scene

    if hook_overlay_spec(state) is not None:
        # The on-screen hook is the one text of the opening window.
        scenes = list(state.get("scenes") or [])
        reserved = {index for index, scene in enumerate(scenes) if isinstance(scene, dict) and is_hook_scene(scene, state)}
        kept = [event for event in events if event.get("scene_index") not in reserved]
        if len(kept) != len(events):
            events = [dict(event, id=f"attention_{index + 1:03d}") for index, event in enumerate(kept)]
    state["attention_events"] = events
    intervals = [events[index]["start"] - events[index - 1]["start"] for index in range(1, len(events))]
    scene_cuts = max(0, len(state.get("scenes") or []) - 1)
    visual_changes = sorted({0.0, *(float(event["start"]) for event in events), *(float(scene.get("start", 0)) for scene in state.get("scenes") or [])})
    visual_gaps = [visual_changes[index] - visual_changes[index - 1] for index in range(1, len(visual_changes))]
    state["attention_plan"] = {
        "status": "disabled" if not preferences["enabled"] else "ready",
        "requested_interval_seconds": preferences["target_interval_seconds"],
        "event_count": len(events),
        "actual_average_interval_seconds": round(sum(intervals) / len(intervals), 3) if intervals else None,
        "scene_cut_count": scene_cuts,
        "micro_event_count": sum(1 for event in events if event.get("type") in {"focus_pulse", "sticker_pop", "shape_pop", "punch_zoom"}),
        "maximum_dead_gap_seconds": round(max(visual_gaps, default=0.0), 3),
        "average_visual_gap_seconds": round(sum(visual_gaps) / len(visual_gaps), 3) if visual_gaps else None,
    }
    return events
