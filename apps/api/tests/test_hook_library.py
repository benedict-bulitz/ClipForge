import json

import pytest

from clipforge.hook_library import (
    HookLibraryError,
    analyze_hook_opportunities,
    eligible_strategy_ids,
    fallback_templates,
    load_hook_library,
    strategy_guidance,
)


def sourced(claim: str) -> dict:
    return {
        "claim": claim,
        "verification": "source_snippet",
        "sources": [{"label": "Source", "url": "https://source.test"}],
    }


def test_hook_library_loads_from_config_and_is_cached():
    first = load_hook_library()
    second = load_hook_library()
    assert first is second
    assert len(first["strategy_families"]) == 5
    assert len(first["fallback_templates"]) == 50


def test_hook_library_exposes_valid_strategy_and_template_lookups():
    templates = fallback_templates("shock_number")
    assert templates
    assert all(item["family"] == "shock_number" for item in templates)
    guidance = strategy_guidance(
        analyze_hook_opportunities(
            [sourced("42% of surveyed readers changed their habits.")]
        )
    )
    assert any(item["id"] == "shock_number" for item in guidance)


def test_opportunity_matching_requires_evidence_for_statistics_and_trends():
    statistic_profile = analyze_hook_opportunities(
        [sourced("42% of surveyed readers changed their habits.")]
    )
    trend_profile = analyze_hook_opportunities(
        [sourced("Readers are increasingly choosing digital books.")]
    )
    empty_profile = analyze_hook_opportunities([])
    assert "shock_number" in eligible_strategy_ids(statistic_profile)
    assert "social_proof" in eligible_strategy_ids(trend_profile)
    assert "shock_number" not in eligible_strategy_ids(empty_profile)
    assert "social_proof" not in eligible_strategy_ids(empty_profile)


def test_malformed_library_fails_clearly(monkeypatch, tmp_path):
    path = tmp_path / "hook_library.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr("clipforge.hook_library._library_path", lambda: path)
    load_hook_library.cache_clear()
    with pytest.raises(HookLibraryError, match="invalid JSON"):
        load_hook_library()
    load_hook_library.cache_clear()


def test_invalid_strategy_record_fails_validation(monkeypatch, tmp_path):
    path = tmp_path / "hook_library.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "strategy_families": [
                    {
                        "id": "broken",
                        "primary_pattern": "x",
                        "use_when": [],
                        "avoid_when": [],
                        "evidence_policy": "x",
                    },
                    {
                        "id": "broken",
                        "primary_pattern": "x",
                        "use_when": [],
                        "avoid_when": [],
                        "evidence_policy": "x",
                    },
                ],
                "fallback_templates": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("clipforge.hook_library._library_path", lambda: path)
    load_hook_library.cache_clear()
    with pytest.raises(HookLibraryError, match="Duplicate hook strategy id"):
        load_hook_library()
    load_hook_library.cache_clear()
