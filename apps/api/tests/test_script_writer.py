import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from clipforge.config import Settings
from clipforge.script_writer import (
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
    payload = json.loads(str(captured["input"]))
    assert payload["facts"][0]["id"] == "fact_01"
    assert "https://" not in str(payload)
    assert "Travelbook" not in str(payload)
    assert "hook" not in str(payload).casefold()
    assert "visual" not in str(payload).casefold()
    assert "media" not in str(payload).casefold()


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
