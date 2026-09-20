from itertools import pairwise

from clipforge.attention import (
    plan_attention_events,
    replan_attention,
    resolve_attention_preferences,
)
from clipforge.dependencies import resolve_edit_scope
from clipforge.renderer import _write_ass_captions


def captions(items):
    return [{"text": text, "start": start, "end": start + 1.0} for start, text in items]


def scenes(duration=10):
    return [{"id": "scene-1", "start": 0, "end": duration}]


def test_density_modes_are_bounded_and_disabled_is_empty():
    items = captions([(0, "Blue light scatters strongly."), (2, "The sky appears blue."), (4, "Short wavelengths spread."), (6, "Sunsets turn red."), (8, "The atmosphere filters light.")])
    assert plan_attention_events(" ".join(item["text"] for item in items), items, scenes(), {"attention_density": "off"}) == []
    normal = plan_attention_events("x", items, scenes(), {"attention_density": "normal"})
    high = plan_attention_events("x", items, scenes(), {"attention_density": "high"})
    low = plan_attention_events("x", items, scenes(), {"attention_density": "low"})
    assert len(high) >= len(normal) >= len(low)
    assert len(high) <= 48


def test_events_are_semantic_non_periodic_and_scene_clamped():
    items = captions([(0, "Battery capacity fell by 42 percent."), (1.4, "First reduce expenses."), (3.2, "Then automate saving."), (5.0, "This prevents costly surprises.")])
    events = plan_attention_events(" ".join(item["text"] for item in items), items, [{"start": 0, "end": 2}, {"start": 2, "end": 6}], {"attention_density": "high"})
    assert any(event["type"] == "statistic_callout" and "42" in event["text"] for event in events)
    assert any(event["type"] == "comparison_label" for event in events)
    assert all(event["duration"] > 0 and event["start"] + event["duration"] <= (2 if event["scene_index"] == 0 else 6) for event in events)
    assert all(b["start"] - a["start"] >= 0.35 for a, b in pairwise(events))
    assert [event["start"] for event in events] != [round(index * 0.8, 3) for index in range(len(events))]


def test_statistics_are_extractively_gated_and_language_is_preserved():
    german = captions([(0, "Die Akkukapazität sank um 42 Prozent.")])
    events = plan_attention_events(german[0]["text"], german, scenes(2), {"attention_density": "normal"}, language="de")
    assert events[0]["type"] == "statistic_callout"
    assert events[0]["text"] == "42 Prozent"
    sparse = captions([(0, "Light changes.")])
    assert len(plan_attention_events(sparse[0]["text"], sparse, scenes(2), {"attention_density": "high"})) == 0


def test_custom_interval_is_normalized():
    assert resolve_attention_preferences({"attention_density": "custom", "attention_interval_seconds": 99})["target_interval_seconds"] == 8.0


def test_dense_opportunities_have_real_density_ordering():
    items = captions([(index * 0.55, f"Signal {index} changes the measured outcome.") for index in range(18)])
    counts = [len(plan_attention_events("dense", items, scenes(10), {"attention_density": mode})) for mode in ("high", "normal", "low")]
    assert counts[0] > counts[1] > counts[2]


def test_custom_interval_changes_dense_cadence():
    items = captions([(index * 0.55, f"Signal {index} changes the measured outcome.") for index in range(18)])
    counts = [len(plan_attention_events("dense", items, scenes(10), {"attention_density": "custom", "attention_interval_seconds": interval})) for interval in (0.8, 1.5, 3.0)]
    assert counts[0] > counts[1] > counts[2]


def test_symbol_and_comparative_extraction_are_safe():
    items = captions([(0, "Blaues Licht wird stärker gestreut."), (1.2, "Die Kosten steigen.")])
    events = plan_attention_events(" ".join(item["text"] for item in items), items, scenes(3), {"attention_density": "high"}, language="de")
    assert not any(event["type"] == "graphic_accent" for event in events)
    assert any(event["type"] == "icon_or_symbol" and event["text"] == "$" for event in events)


def test_activity_interval_never_fabricates_timer_only_graphics():
    items = captions([(0.1, "This sentence has no statistic, step, or supported symbol.")])
    events = plan_attention_events(items[0]["text"], items, scenes(4), {"attention_density": "custom", "attention_interval_seconds": 0.8})
    assert events == []
    assert not any(event["type"] in {"graphic_accent", "focus_pulse", "shape_pop", "sticker_pop", "punch_zoom"} for event in events)


def test_attention_events_reach_ass_as_separate_top_overlay(tmp_path):
    state = {
        "timeline": {"width": 1080, "height": 1920},
        "captions": {"enabled": True, "position": "lower", "items": [{"text": "Answer", "start": 0, "end": 1}]},
        "attention_events": [{"start": 0.4, "duration": 0.6, "type": "keyword_callout", "text": "BLUE LIGHT", "placement": "top"}],
    }
    ass = _write_ass_captions(state, 2, tmp_path).read_text()
    assert "Style: Attention" in ass
    assert "Attention" in ass and "BLUE LIGHT" in ass
    assert "0:00:00.40" in ass and "0:00:01.00" in ass


def test_captions_off_still_emits_attention_and_both_off_emits_no_dialogue(tmp_path):
    state = {"timeline": {"width": 1080, "height": 1920}, "captions": {"enabled": False, "items": []}, "attention_events": [{"start": 0.4, "duration": 0.6, "type": "statistic_callout", "text": "42%"}]}
    ass = _write_ass_captions(state, 2, tmp_path).read_text()
    assert "42%" in ass and "Dialogue: 1" in ass
    state["attention_events"] = []
    assert "Dialogue:" not in _write_ass_captions(state, 2, tmp_path).read_text()


def test_attention_plan_reports_actual_average_interval():
    state = {"intent": {"language": "en"}, "script": {"text": "One two three four."}, "captions": {"items": captions([(0, "One two"), (2, "three four")])}, "scenes": scenes(4), "attention_preferences": {"attention_density": "normal"}}
    events = replan_attention(state)
    assert state["attention_plan"]["event_count"] == len(events)
    assert state["attention_plan"]["actual_average_interval_seconds"] is None if len(events) < 2 else state["attention_plan"]["actual_average_interval_seconds"] is not None


def test_attention_setting_scope_only_reaches_render():
    changed = resolve_edit_scope("Set visual activity to frequent")
    assert "attention" in changed and "render" in changed
    assert "research" not in changed and "voice" not in changed and "assets" not in changed
