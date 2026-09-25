import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from clipforge.ai import AIHookGenerationResult
from clipforge.config import Settings
from clipforge.hooks import STRATEGIES
from clipforge.pipeline import _generate_body_with_v2_or_fallback, build_initial_state
from clipforge.renderer import RenderResult, _create_voice
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import (
    SCRIPT_WRITER_V2_INSTRUCTIONS,
    OpenAIScriptWriterProvider,
    ScriptBlockV2,
    ScriptDraftV2,
    ScriptWriterFact,
    ScriptWriterRequest,
    ScriptWriterResult,
    ScriptWriterValidationError,
    generate_script_v2,
    validate_script_draft,
)
from clipforge.services import _render_state


def fact(
    fact_id: str = "fact_01",
    claim: str = "Sharp corners concentrate mechanical stress.",
    verification: str = "supported",
) -> ScriptWriterFact:
    return ScriptWriterFact(
        id=fact_id,
        claim=claim,
        verification=verification,
        confidence=0.96,
        priority="MUST_KNOW",
    )


def request(language: str = "de") -> ScriptWriterRequest:
    return ScriptWriterRequest(
        prompt="Warum haben Flugzeugfenster unten ein kleines Loch?",
        language=language,
        tone="fast documentary",
        audience="general",
        content_type="factual_explainer",
        facts=[fact()],
        research_summary="The opening in the inner pane helps equalize pressure.",
        target_duration={"max_seconds": 60},
        writing_requirements=["Use natural spoken narration."],
    )


def draft(language: str = "de", *, payoff_facts: list[str] | None = None) -> ScriptDraftV2:
    return ScriptDraftV2(
        language=language,
        blocks=[
            ScriptBlockV2(
                role="answer",
                text="Das kleine Loch hilft, den Druck zwischen den Scheiben auszugleichen.",
                fact_ids=["fact_01"],
            ),
            ScriptBlockV2(
                role="payoff",
                text="Es ist also ein Teil der Sicherheitskonstruktion.",
                fact_ids=payoff_facts or [],
            ),
        ],
    )


def test_valid_german_and_english_drafts() -> None:
    validate_script_draft(draft(), request())
    english_request = request("en")
    english = ScriptDraftV2(
        language="en",
        blocks=[
            ScriptBlockV2(
                role="answer",
                text="The hole helps equalize pressure between the window panes.",
                fact_ids=["fact_01"],
            ),
            ScriptBlockV2(role="payoff", text="It is part of the safety design."),
        ],
    )
    validate_script_draft(english, english_request)


@pytest.mark.parametrize("role", ["hook", "transition", "setup", "detail", "context"])
def test_invalid_roles_are_rejected(role: str) -> None:
    with pytest.raises(ValidationError):
        ScriptBlockV2(role=role, text="A sentence.", fact_ids=["fact_01"])


def test_empty_block_body_and_excessive_blocks_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ScriptBlockV2(role="answer", text="", fact_ids=["fact_01"])
    with pytest.raises(ValidationError):
        ScriptDraftV2(
            language="de",
            blocks=[ScriptBlockV2(role="payoff", text="Enough.")],
        )
    too_many = [ScriptBlockV2(role="payoff", text=f"Point {i}.") for i in range(7)]
    with pytest.raises(ValidationError):
        ScriptDraftV2(language="de", blocks=too_many)
    with pytest.raises(ValidationError):
        ScriptBlockV2(role="payoff", text="x" * 1_201)


def test_fact_references_are_grounded_and_payoff_may_be_unreferenced() -> None:
    validate_script_draft(draft(), request())
    with pytest.raises(ScriptWriterValidationError, match="unknown fact ID"):
        validate_script_draft(
            ScriptDraftV2(
                language="de",
                blocks=[
                    ScriptBlockV2(role="answer", text="Eine Erklärung.", fact_ids=["missing"]),
                    ScriptBlockV2(role="payoff", text="Das ist der Grund."),
                ],
            ),
            request(),
        )
    with pytest.raises(ScriptWriterValidationError, match="unsupported fact ID"):
        validate_script_draft(
            draft(),
            request().model_copy(update={"facts": [fact(verification="conflicting")]}),
        )


@pytest.mark.parametrize(
    "text",
    [
        "Quelle: Travelbook erklärt das.",
        "See https://example.com for more.",
        "[Quelle](https://example.com)",
        "<p>Eine Erklärung.</p>",
        "Answer: Eine Erklärung.",
        "Eine Erklärung:.",
        "Eine Erklärung!!",
        "Eine Erklärung?.",
    ],
)
def test_contamination_and_malformed_punctuation_are_rejected(text: str) -> None:
    with pytest.raises(ScriptWriterValidationError):
        validate_script_draft(
            ScriptDraftV2(
                language="de",
                blocks=[
                    ScriptBlockV2(role="answer", text=text, fact_ids=["fact_01"]),
                    ScriptBlockV2(role="payoff", text="Das ist der Grund."),
                ],
            ),
            request(),
        )


def test_exact_duplicate_blocks_are_rejected() -> None:
    duplicate = ScriptBlockV2(
        role="answer",
        text="Der Druck wird ausgeglichen.",
        fact_ids=["fact_01"],
    )
    with pytest.raises(ScriptWriterValidationError, match="duplicates"):
        validate_script_draft(
            ScriptDraftV2(language="de", blocks=[duplicate, duplicate]),
            request(),
        )


class FakeProvider:
    def generate(self, _request: ScriptWriterRequest) -> ScriptWriterResult:
        return ScriptWriterResult(draft(), "connected")


def test_provider_failure_has_no_python_fallback() -> None:
    class FailedProvider:
        def generate(self, _request: ScriptWriterRequest) -> ScriptWriterResult:
            return ScriptWriterResult(None, "provider_error", "offline")

    result = generate_script_v2(request(), FailedProvider())
    assert result.status == "provider_error"
    assert result.draft is None


def test_writer_instructions_prioritize_complete_direct_explanations() -> None:
    instructions = SCRIPT_WRITER_V2_INSTRUCTIONS.casefold()
    for phrase in (
        "not reassurance",
        "complete causal chain",
        "do not repeat the same mechanism",
        "shortest complete explanation",
        "do not pad",
        "payoff is optional",
        "useful concluding information",
    ):
        assert phrase in instructions


def test_openai_provider_uses_director_model_and_source_free_contract() -> None:
    parsed = draft()
    captured: dict[str, object] = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=parsed)

    fake_client = SimpleNamespace(responses=FakeResponses())
    settings = Settings(
        clipforge_ai_mode="openai",
        openai_api_key="test-key",
        openai_director_model="configured-director",
    )
    with patch("clipforge.script_writer.OpenAI", return_value=fake_client):
        result = OpenAIScriptWriterProvider(settings).generate(request())

    assert result.status == "connected"
    assert result.draft == parsed
    assert captured["model"] == "configured-director"
    assert captured["text_format"] is ScriptDraftV2
    assert captured["instructions"] == SCRIPT_WRITER_V2_INSTRUCTIONS
    payload = json.loads(str(captured["input"]))
    assert payload["facts"][0]["id"] == "fact_01"
    assert "https://" not in str(payload)
    assert "Travelbook" not in str(payload)
    assert "hook" not in str(payload).casefold()
    assert "visual" not in str(payload).casefold()
    assert "media" not in str(payload).casefold()


@pytest.mark.parametrize("requirement", [
    "10–14 year old", "first listen", "everyday German", "one idea at a time",
    "technical term immediately", "explain the idea first", "scientific distinctions",
    "not facts", "nested clauses", "not from fixed templates",
])
def test_writer_readability_contract(requirement: str) -> None:
    assert requirement in SCRIPT_WRITER_V2_INSTRUCTIONS


def test_hook_readability_is_in_actual_director_prompt() -> None:
    from clipforge.ai import DIRECTOR_INSTRUCTIONS

    for requirement in (
        "every hook candidate", "10–14 year old", "first listen", "everyday German",
        "no unexplained jargon", "Simplify a difficult question", "factual meaning",
        "clarity takes priority", "rigid hook templates",
    ):
        assert requirement in DIRECTOR_INSTRUCTIONS


def test_openai_provider_returns_explicit_failure_for_invalid_result() -> None:
    fake_client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=lambda **_kwargs: SimpleNamespace(
                output_parsed=ScriptDraftV2(
                    language="de",
                    blocks=[
                        ScriptBlockV2(
                            role="answer", text="Nicht belegt.", fact_ids=["missing"]
                        ),
                        ScriptBlockV2(role="payoff", text="Ende."),
                    ],
                )
            )
        )
    )
    settings = Settings(openai_api_key="test-key")
    with patch("clipforge.script_writer.OpenAI", return_value=fake_client):
        result = OpenAIScriptWriterProvider(settings).generate(request())
    assert result.status == "validation_error"
    assert result.draft is None


def _plan_for_integration(prompt: str, language: str = "de"):
    class Plan:
        def model_dump(self, mode="json"):
            return {
                "intent": {
                    "topic": prompt.rstrip("?"),
                    "intent": "explain",
                    "question": prompt,
                    "language": language,
                    "content_type": "factual_explainer",
                    "tone": "fast_documentary",
                    "research_required": True,
                    "visual_style": "documentary_graphics",
                    "shortform": True,
                },
                "research_questions": ["What is the mechanism?"],
                "facts": [],
                "answer_skeleton": ["HOOK", "ANSWER"],
                "script_blocks": [
                    {"role": "hook", "text": "Legacy opening."},
                    {"role": "answer", "text": "Legacy body."},
                ],
                "music_mood": "documentary",
                "hook_candidates": [
                    {"strategy": "curiosity_gap", "text": "Was macht das kleine Loch im Fenster?"}
                ],
                "selected_hook_strategy": "evidence_insight",
                "visual_intents": [],
            }

    return Plan()


def _integration_facts():
    source = {"label": "Test source", "url": "https://source.test"}
    return [
        {
            "claim": "Das kleine Loch gleicht den Druck zwischen den Fensterscheiben aus.",
            "confidence": 0.95,
            "importance": 0.95,
            "sources": [source],
            "verification": "source_snippet",
        }
    ], [source]


class IntegrationProvider:
    name = "fixture-v2"

    def __init__(self, result: ScriptWriterResult):
        self.result = result
        self.requests: list[ScriptWriterRequest] = []

    def generate(self, request: ScriptWriterRequest) -> ScriptWriterResult:
        self.requests.append(request)
        return self.result


def test_fresh_generation_uses_v2_body_and_preserves_legacy_hook(monkeypatch) -> None:
    prompt = "Warum haben Flugzeugfenster unten ein kleines Loch?"
    facts, sources = _integration_facts()
    provider = IntegrationProvider(
        ScriptWriterResult(
            ScriptDraftV2(
                language="de",
                blocks=[
                    ScriptBlockV2(
                        role="answer",
                        text="Das Loch gleicht den Druck zwischen den Scheiben aus.",
                        fact_ids=["fact_01"],
                    ),
                    ScriptBlockV2(
                        role="payoff",
                        text="So bleibt die äußere Scheibe besser geschützt.",
                    ),
                ],
            ),
            "connected",
        )
    )
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(
            facts, sources, "verified_sources", "fixture"
        ),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult",
            (),
            {"plan": _plan_for_integration(prompt), "status": "connected", "error": None},
        )(),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.generate_hook_candidates_with_openai",
        lambda *_args, **_kwargs: AIHookGenerationResult(
            [{"strategy": "direct_reframe", "text": "Das kleine Loch ist nicht kaputt, sondern gleicht den Druck aus."}],
            "direct_reframe",
            "connected",
        ),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="openai", openai_api_key="test-key"),
        script_writer_provider=provider,
    )
    assert len(provider.requests) == 1
    assert state["script"]["script_writer_v2"]["status"] == "v2_success"
    assert state["script"]["blocks"][0]["role"] == "hook"
    assert state["script"]["blocks"][0]["text"] != (
        "Das Loch gleicht den Druck zwischen den Scheiben aus."
    )
    assert "Das Loch gleicht" in state["script"]["text"]
    assert state["script"]["blocks"][1]["fact_ids"] == ["fact_01"]
    assert [block["role"] for block in state["script"]["blocks"][1:]] == ["answer", "payoff"]


@pytest.mark.parametrize(
    ("prompt", "body", "provider_hook", "strategy", "provider_hook_wins"),
    [
        (
            # The dull-knife claim is not in the research: the documented
            # strategy engine rejects it and selects a supported hook.
            "Warum tränen unsere Augen beim Zwiebelschneiden?",
            "Beim Schneiden setzt die Zwiebel reizende Stoffe frei.",
            "Schneidest du mit einem stumpfen Messer? Das kann deine Augen stärker reizen.",
            "ego_challenge",
            False,
        ),
        (
            "Wieso kriegen wir Gänsehaut?",
            "Kleine Muskeln an den Haarwurzeln stellen deine Haare auf.",
            "Deine Haare stellen sich auf – aber warum eigentlich?",
            "curiosity_gap",
            True,
        ),
    ],
)
def test_production_pipeline_uses_shared_post_body_hook_for_narration_tts_and_captions(
    monkeypatch, tmp_path, prompt, body, provider_hook, strategy, provider_hook_wins
) -> None:
    source = {"label": "Test source", "url": "https://source.test"}
    facts = [{
        "claim": body,
        "confidence": 0.95,
        "importance": 0.95,
        "sources": [source],
        "verification": "source_snippet",
    }]
    provider = IntegrationProvider(ScriptWriterResult(ScriptDraftV2(
        language="de",
        blocks=[
            ScriptBlockV2(role="answer", text=body, fact_ids=["fact_01"]),
            ScriptBlockV2(role="payoff", text="Das erklärt den Effekt.", fact_ids=[]),
        ],
    ), "connected"))
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(facts, [source], "verified_sources", "fixture"),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult", (), {"plan": _plan_for_integration(prompt), "status": "connected", "error": None}
        )(),
    )
    hook_inputs: list[str] = []

    def generate_hook(_prompt, _intent, _facts, finalized_body, _settings):
        hook_inputs.append(finalized_body)
        return AIHookGenerationResult(
            [{"strategy": strategy, "text": provider_hook}], strategy, "connected"
        )

    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", generate_hook)
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        # This is the normal runtime shape: V2 uses OpenAI when configured,
        # independently of the legacy planner mode. The hook must still flow
        # into the persisted script, TTS input, captions, and render state.
        Settings(clipforge_ai_mode="local", openai_api_key="test-key", render_root=tmp_path),
        script_writer_provider=provider,
    )
    assert hook_inputs and hook_inputs[0].startswith(body)
    hook = state["script"]["selected_hook"]
    assert (hook == provider_hook) is provider_hook_wins
    assert state["script"]["selected_hook_strategy"] == state["script"]["triple_hook"]["selected_strategy"]
    if provider_hook_wins:
        assert state["script"]["selected_hook_strategy"] == strategy
        assert state["script"]["blocks"][1]["text"] == body
    assert state["script"]["selected_hook_strategy"] in STRATEGIES
    assert state["script"]["blocks"][0]["text"] == hook == state["script"]["triple_hook"]["verbal_hook"]
    assert state["script"]["text"].startswith(hook)
    assert state["captions"]["items"][0]["text"].startswith(hook.split()[0])

    captured: dict[str, object] = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"w" * 5000})()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = type("Audio", (), {"speech": Speech()})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", FakeOpenAI)
    state["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(state, tmp_path, Settings(openai_api_key="test-key", render_root=tmp_path))
    assert str(captured["input"]).startswith(hook)

    rendered_input: dict[str, str] = {}
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("clipforge.services.run_ai_review", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "clipforge.services.render_video",
        lambda render_state, *_args, **_kwargs: (
            rendered_input.update(text=render_state["script"]["text"])
            or RenderResult("/media/test.mp4", 12.0, "openai", 100)
        ),
    )
    rendered = _render_state(
        state, "project-test", 2, Settings(openai_api_key="test-key", render_root=tmp_path)
    )
    assert rendered_input["text"].startswith(hook)
    assert rendered["script"]["blocks"][0]["text"] == hook


@pytest.mark.parametrize(
    "result",
    [
        ScriptWriterResult(None, "provider_error", "offline"),
        ScriptWriterResult(None, "validation_error", "unknown fact ID"),
    ],
)
def test_fresh_generation_falls_back_to_legacy_body(monkeypatch, result) -> None:
    prompt = "Warum haben Flugzeugfenster unten ein kleines Loch?"
    facts, sources = _integration_facts()
    provider = IntegrationProvider(result)
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(
            facts, sources, "verified_sources", "fixture"
        ),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult",
            (),
            {"plan": _plan_for_integration(prompt), "status": "connected", "error": None},
        )(),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="openai", openai_api_key="test-key"),
        script_writer_provider=provider,
    )
    assert state["script"]["script_writer_v2"]["status"] == "legacy_fallback"
    assert state["script"]["script_writer_v2"]["error"] == result.error
    assert "Legacy body." in state["script"]["text"]


def test_local_mode_with_openai_key_uses_script_writer_provider(monkeypatch) -> None:
    prompt = "Warum haben Flugzeugfenster unten ein kleines Loch?"
    facts, sources = _integration_facts()
    provider = IntegrationProvider(
        ScriptWriterResult(
            ScriptDraftV2(
                language="de",
                blocks=[
                    ScriptBlockV2(
                        role="answer",
                        text="Das Loch gleicht den Druck aus.",
                        fact_ids=["fact_01"],
                    ),
                    ScriptBlockV2(role="payoff", text="Darum ist es kein Schaden."),
                ],
            ),
            "connected",
        )
    )
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(
            facts, sources, "verified_sources", "fixture"
        ),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult",
            (),
            {"plan": _plan_for_integration(prompt), "status": "connected", "error": None},
        )(),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        script_writer_provider=provider,
    )
    assert len(provider.requests) == 1
    assert state["script"]["script_writer_v2"]["status"] == "v2_success"
    assert "Das Loch gleicht" in state["script"]["text"]


def test_no_openai_key_keeps_legacy_fallback(monkeypatch) -> None:
    prompt = "Warum haben Flugzeugfenster unten ein kleines Loch?"
    facts, sources = _integration_facts()
    provider = IntegrationProvider(
        ScriptWriterResult(None, "provider_error", "unexpected")
    )
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(
            facts, sources, "verified_sources", "fixture"
        ),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult",
            (),
            {"plan": _plan_for_integration(prompt), "status": "connected", "error": None},
        )(),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key=None),
        script_writer_provider=provider,
    )
    assert provider.requests == []
    assert state["script"]["script_writer_v2"]["status"] == "legacy_fallback"
    assert state["script"]["script_writer_v2"]["reason"] == "unsupported_generation_mode"


def test_large_production_shaped_research_is_bounded_before_v2_validation() -> None:
    provider = IntegrationProvider(ScriptWriterResult(None, "provider_error", "offline"))
    facts = [
        {
            "id": f"fact_{index:02d}",
            "claim": (
                "Flugzeugfenster bestehen aus mehreren Scheiben und diese technische "
                "Konstruktion unterstützt den Druckausgleich. "
                + ("Zusätzliche überprüfte Erklärung. " * 12)
            ),
            "verification": "source_snippet",
            "confidence": 0.9,
            "priority": "MUST_KNOW",
        }
        for index in range(1, 7)
    ]
    blocks, diagnostics = _generate_body_with_v2_or_fallback(
        "Warum haben Flugzeugfenster unten ein kleines Loch?",
        {
            "language": "de",
            "tone": "fast_documentary",
            "content_type": "factual_explainer",
        },
        AdvancedOptions(language="de", max_duration=60),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        facts,
        [{"role": "answer", "text": "Legacy body."}],
        provider=provider,
    )

    assert blocks == [{"role": "answer", "text": "Legacy body."}]
    assert diagnostics["attempted"] is True
    assert diagnostics["reason"] == "provider_failed"
    assert len(provider.requests) == 1
    assert len(provider.requests[0].research_summary or "") <= 2_000


def test_invalid_v2_request_falls_back_without_calling_provider() -> None:
    provider = IntegrationProvider(ScriptWriterResult(None, "provider_error", "unexpected"))
    blocks, diagnostics = _generate_body_with_v2_or_fallback(
        "Warum haben Flugzeugfenster unten ein kleines Loch?",
        {
            "language": "",
            "tone": "fast_documentary",
            "content_type": "factual_explainer",
        },
        AdvancedOptions(language="de"),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        [
            {
                "claim": "Das Loch gleicht den Druck zwischen den Scheiben aus.",
                "verification": "supported",
            }
        ],
        [{"role": "answer", "text": "Legacy body."}],
        provider=provider,
    )

    assert blocks == [{"role": "answer", "text": "Legacy body."}]
    assert diagnostics["attempted"] is True
    assert diagnostics["status"] == "legacy_fallback"
    assert diagnostics["reason"] == "request_validation_failed"
    assert diagnostics["error"]
    assert provider.requests == []
