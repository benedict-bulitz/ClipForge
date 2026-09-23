import json
from types import SimpleNamespace

import pytest

from clipforge.hook_library import (
    HookLibraryError,
    analyze_hook_opportunities,
    eligible_strategy_ids,
    fallback_templates,
    generation_playbook,
    load_hook_library,
    strategy_families,
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


def test_generation_playbook_exposes_manifest_guidance_not_templates():
    playbook = generation_playbook([sourced("42% of surveyed readers changed their habits.")])
    assert playbook["library_id"] == "clipforge-hook-library-deine-hooks"
    assert playbook["strategy_families"] == strategy_families()
    assert "fallback_templates" not in playbook
    assert playbook["template_policy"].startswith("Do not copy")


def test_live_director_request_receives_the_manifest(monkeypatch):
    from clipforge.ai import AIProjectPlan, plan_with_openai
    from clipforge.config import Settings
    from clipforge.schemas import AdvancedOptions

    captured = {}
    plan = AIProjectPlan.model_validate({
        "intent": {"topic": "onions", "intent": "explain", "question": "Why do onions sting?", "language": "en", "content_type": "factual_explainer", "tone": "clear", "research_required": True, "visual_style": "documentary", "shortform": True},
        "research_questions": [], "facts": [], "answer_skeleton": ["ANSWER", "SUPPORT"],
        "script_blocks": [{"role": "hook", "text": "Onions can sting your eyes."}, {"role": "answer", "text": "They release an irritant."}],
        "music_mood": "documentary", "hook_candidates": [], "visual_intents": [],
    })
    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_: SimpleNamespace(
        responses=SimpleNamespace(parse=lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(output_parsed=plan)))
    ))
    result = plan_with_openai(
        "Why do onions make us cry?", AdvancedOptions(),
        Settings(clipforge_ai_mode="openai", openai_api_key="test-key"),
        evidence=["Onions release an irritant gas when cut."],
    )
    assert result.status == "connected"
    request = json.loads(captured["input"])
    assert request["hook_playbook"]["library_id"] == "clipforge-hook-library-deine-hooks"
    assert request["hook_playbook"]["strategy_families"]


def test_shared_post_body_hook_generator_receives_final_body_and_manifest(monkeypatch):
    from clipforge.ai import (
        AIHookGenerationResponse,
        generate_hook_candidates_with_openai,
    )
    from clipforge.config import Settings

    captured = {}
    response = AIHookGenerationResponse.model_validate({
        "hook_candidates": [{
            "strategy": "curiosity_gap",
            "text": "Was reizt deine Augen beim Zwiebelschneiden wirklich?",
        }],
        "selected_hook_strategy": "curiosity_gap",
    })
    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_: SimpleNamespace(
        responses=SimpleNamespace(parse=lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(output_parsed=response)))
    ))
    result = generate_hook_candidates_with_openai(
        "Warum tränen unsere Augen beim Zwiebelschneiden?",
        {"topic": "Zwiebeln", "question": "Warum tränen unsere Augen beim Zwiebelschneiden?", "language": "de"},
        [sourced("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.")],
        "Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.",
        # Script Writer V2 is intentionally available whenever the configured
        # OpenAI provider has a key, even with the legacy planner set to local.
        # The post-body hook call must use that same production route.
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
    )
    assert result.status == "connected"
    assert result.candidates == [response.hook_candidates[0].model_dump()]
    request = json.loads(captured["input"])
    assert request["final_body"].startswith("Beim Schneiden")
    assert request["hook_playbook"]["library_id"] == "clipforge-hook-library-deine-hooks"


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
