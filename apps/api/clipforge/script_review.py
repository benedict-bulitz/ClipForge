from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .script_writer import (
    ScriptDraftV2,
    ScriptWriterFact,
    ScriptWriterRequest,
    ScriptWriterValidationError,
    validate_script_draft,
)


class ScriptReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=48)
    message: str = Field(min_length=2, max_length=240)


class ScriptReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["approve", "revise"]
    issues: list[ScriptReviewIssue] = Field(default_factory=list, max_length=10)
    draft: ScriptDraftV2 | None = None


@dataclass(frozen=True)
class ScriptReviewRequest:
    prompt: str
    language: str
    tone: str
    audience: str
    content_type: str
    draft: ScriptDraftV2
    facts: list[ScriptWriterFact]
    target_duration: dict[str, int | None] | None = None
    writing_requirements: list[str] | None = None

    def model_input(self) -> dict:
        return {
            "prompt": self.prompt,
            "language": self.language,
            "tone": self.tone,
            "audience": self.audience,
            "content_type": self.content_type,
            "draft": self.draft.model_dump(mode="json"),
            "facts": [fact.model_dump(mode="json") for fact in self.facts],
            "target_duration": self.target_duration,
            "writing_requirements": self.writing_requirements or [],
        }


@dataclass(frozen=True)
class ScriptReviewResult:
    response: ScriptReviewResponse | None
    status: Literal["approved", "revised", "provider_error", "validation_error"]
    error: str | None = None


class ScriptReviewProvider(Protocol):
    name: str

    def review(self, request: ScriptReviewRequest) -> ScriptReviewResult: ...


SCRIPT_REVIEW_V2_INSTRUCTIONS = (
    "You are ClipForge's dedicated Script Review V2. Review only the body of a short-form "
    "factual explanation, not its hook or visuals. Judge whether it answers the prompt directly, "
    "preserves the causal explanation needed to understand the answer, uses natural spoken "
    "language, follows a logical order, and avoids unnecessary reassurance, filler, redundant "
    "restatement, repeated causal explanations, list-like writing, awkward transitions, "
    "article-like wording, unsupported additions, malformed speech, artificial payoffs, and "
    "tangents. Check whether the answer is delayed by a preamble and whether its length is "
    "proportionate to the informational complexity. Do not shorten merely to shorten: preserve "
    "useful causal context and every supported detail needed for a complete explanation. For a "
    "simple factual question, prefer the shortest complete answer; for a complex question, allow "
    "necessary detail. A payoff is not required and must contain useful concluding information, "
    "not a generic flourish. If the draft is sound, approve it. If it needs improvement, return "
    "a complete revised draft using only answer, explanation, support, and payoff roles. Preserve "
    "valid fact IDs for every factual block. Do not add hooks, sources, URLs, HTML, Markdown, or "
    "source attribution. Return only the requested structured output."
)


class OpenAIScriptReviewProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_worker_model

    def review(self, request: ScriptReviewRequest) -> ScriptReviewResult:
        try:
            response = self._client.responses.parse(
                model=self._model,
                instructions=SCRIPT_REVIEW_V2_INSTRUCTIONS,
                input=json.dumps(request.model_input(), ensure_ascii=False),
                text_format=ScriptReviewResponse,
                max_output_tokens=1_500,
                store=False,
            )
            parsed = response.output_parsed
            if not isinstance(parsed, ScriptReviewResponse):
                return ScriptReviewResult(None, "provider_error", "No parsed Script Review V2 result")
            if parsed.status == "approve":
                if parsed.draft is not None:
                    return ScriptReviewResult(
                        None, "validation_error", "Approved review must not replace the draft"
                    )
                return ScriptReviewResult(parsed, "approved")
            if parsed.draft is None:
                return ScriptReviewResult(
                    None, "validation_error", "Revised review must include a draft"
                )
            validate_script_draft(parsed.draft, ScriptWriterRequest(
                prompt=request.prompt,
                language=request.language,
                tone=request.tone,
                audience=request.audience,
                content_type=request.content_type,
                facts=request.facts,
                target_duration=request.target_duration,
                writing_requirements=request.writing_requirements or [],
            ))
            return ScriptReviewResult(parsed, "revised")
        except ScriptWriterValidationError as exc:
            return ScriptReviewResult(None, "validation_error", str(exc)[:240])
        except (OpenAIError, ValueError, TypeError) as exc:
            return ScriptReviewResult(None, "provider_error", str(exc)[:240])


def review_script_v2(
    request: ScriptReviewRequest, provider: ScriptReviewProvider
) -> ScriptReviewResult:
    return provider.review(request)


def compact_script_draft(draft: ScriptDraftV2) -> list[dict]:
    return [
        {"role": block.role, "text": block.text, "fact_ids": list(block.fact_ids)}
        for block in draft.blocks
    ]
