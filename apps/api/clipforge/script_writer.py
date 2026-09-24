from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings
from .narration import contamination_issues

ScriptBlockRole = Literal["answer", "explanation", "support", "payoff"]
SupportedVerification = Literal["supported", "source_attributed"]


class ScriptWriterFact(BaseModel):
    """Normalized evidence made available to the body writer."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    claim: str = Field(min_length=1, max_length=1_000)
    verification: Literal[
        "supported",
        "source_attributed",
        "uncertain",
        "conflicting",
        "unsupported",
        "unverified_model_synthesis",
    ]
    confidence: float | None = Field(default=None, ge=0, le=1)
    priority: str | None = Field(default=None, max_length=40)


class ScriptWriterTargetDuration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_seconds: int | None = Field(default=None, ge=1, le=180)
    max_seconds: int | None = Field(default=None, ge=1, le=180)

    @field_validator("max_seconds")
    @classmethod
    def max_must_cover_minimum(cls, value: int | None, info):
        minimum = info.data.get("min_seconds")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("max_seconds cannot be less than min_seconds")
        return value


class ScriptWriterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=3, max_length=4_000)
    language: str = Field(min_length=2, max_length=8)
    tone: str = Field(min_length=1, max_length=80)
    audience: str = Field(min_length=1, max_length=120)
    content_type: str = Field(min_length=1, max_length=80)
    facts: list[ScriptWriterFact] = Field(default_factory=list, max_length=10)
    research_summary: str | None = Field(default=None, max_length=2_000)
    target_duration: ScriptWriterTargetDuration | None = None
    writing_requirements: list[str] = Field(default_factory=list, max_length=12)
    payoff_plan: dict[str, object] | None = None
    format_plan: dict[str, object] | None = None
    novelty_plan: dict[str, object] | None = None

    @model_validator(mode="after")
    def require_normalized_evidence(self):
        contaminated: list[str] = []
        for fact in self.facts:
            if contamination_issues(fact.claim):
                contaminated.append(f"fact {fact.id}")
        if self.research_summary and contamination_issues(self.research_summary):
            contaminated.append("research_summary")
        if contaminated:
            raise ValueError(
                "Script Writer evidence must be normalized and source-free: "
                + ", ".join(contaminated)
            )
        return self


class ScriptBlockV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ScriptBlockRole
    text: str = Field(min_length=1, max_length=1_200)
    fact_ids: list[str] = Field(default_factory=list, max_length=6)


class ScriptDraftV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=2, max_length=8)
    blocks: list[ScriptBlockV2] = Field(min_length=2, max_length=6)


class ScriptWriterValidationError(ValueError):
    """Raised when a provider draft violates the safe script contract."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(frozen=True)
class ScriptWriterResult:
    draft: ScriptDraftV2 | None
    status: Literal["connected", "provider_error", "validation_error"]
    error: str | None = None


class ScriptWriterProvider(Protocol):
    def generate(self, request: ScriptWriterRequest) -> ScriptWriterResult: ...


SCRIPT_WRITER_V2_INSTRUCTIONS = (
    "You are ClipForge's Script Writer V2. Write a complete body-only short-form explanation "
    "from the supplied user prompt and normalized verified facts. Do not write a hook, opening "
    "teaser, or clickbait. Follow the supplied payoff_plan when it asks for a protected payoff: give "
    "the useful supporting information needed to understand it, then land the payoff without filler. "
    "Otherwise begin with the actual answer, not reassurance or a restatement of the question. Never "
    "delay a reveal by a fixed time, repeat information to hold it back, or append a generic CTA/outro. "
    "Use the supplied format_plan as lightweight structure guidance without adding filler or unsupported claims. "
    "Use the supplied novelty_plan only to prioritize supported explanatory or comparative value; never invent "
    "novelty claims, obscure trivia, or unsupported surprise. "
    "Use only the supplied facts for "
    "substantive factual claims and attach the corresponding fact IDs to each factual block. "
    "Write natural spoken narration with logical order, short understandable sentences, and concise "
    "transitions. Explain the complete causal chain needed to understand the answer, but do not "
    "repeat the same mechanism in multiple blocks. Prefer useful information over reassurance; "
    "avoid generic comfort, filler, article-like phrasing, redundant restatements, artificial "
    "drama, and weak concluding flourishes. Use the shortest complete explanation for a simple "
    "factual question, while allowing more detail when the topic genuinely requires it. Do not "
    "pad toward optional duration guidance or invent content to fill a minimum. A payoff is "
    "optional in substance: use it only for useful concluding information, such as a supported "
    "secondary function, and omit a catchy or motivational ending when none is needed. Do not "
    "use source language, editorial attribution, URLs, citations, Markdown, or HTML. Use only "
    "these roles: answer, explanation, support, and payoff. A payoff may have no fact ID only "
    "when it adds no new factual claim. Never expose fact IDs in spoken text. Return only the "
    "requested structured output. "
    "Readability target: would a typical 10–14 year old understand each sentence on first listen, "
    "without prior knowledge? For German narration use everyday German, short natural sentences, "
    "active voice, concrete wording, and one idea at a time. Avoid unnecessary jargon, academic "
    "or bureaucratic phrasing, abstract synonyms, long noun constructions, and nested clauses. "
    "Explain an unfamiliar but necessary technical term immediately in simple words; preferably "
    "explain the idea first, then name the term. Preserve scientific distinctions and factual "
    "meaning: simplify language, not facts. Do not sound childish or add filler, reassurance, "
    "tangents, or extra length merely to explain familiar ideas. Choose wording freely, not from "
    "fixed templates. Silently check readability before returning the draft."
)


def _request_for_model(request: ScriptWriterRequest) -> dict:
    """Return the deliberately narrow, source-free model input contract."""
    return {
        "prompt": request.prompt,
        "language": request.language,
        "tone": request.tone,
        "audience": request.audience,
        "content_type": request.content_type,
        "facts": [
            {
                "id": fact.id,
                "claim": fact.claim,
                "verification": fact.verification,
                "confidence": fact.confidence,
                "priority": fact.priority,
            }
            for fact in request.facts
        ],
        "research_summary": request.research_summary,
        "target_duration": request.target_duration.model_dump(mode="json")
        if request.target_duration
        else None,
        "writing_requirements": request.writing_requirements,
        "payoff_plan": request.payoff_plan,
        "format_plan": request.format_plan,
        "novelty_plan": request.novelty_plan,
    }


def validate_script_draft(
    draft: ScriptDraftV2, request: ScriptWriterRequest | None = None
) -> ScriptDraftV2:
    """Validate hard safety and grounding invariants without rewriting prose."""
    issues: list[str] = []
    if not draft.blocks:
        issues.append("script body is empty")

    fact_map = {fact.id: fact for fact in (request.facts if request else [])}
    supported = {"supported", "source_attributed"}
    seen_text: set[str] = set()
    previous_text: str | None = None
    for index, block in enumerate(draft.blocks):
        text = " ".join(block.text.split()).strip()
        if not text:
            issues.append(f"block {index + 1} is empty")
            continue
        normalized = text.casefold()
        if normalized in seen_text:
            issues.append(f"block {index + 1} repeats narration exactly")
        if previous_text == normalized:
            issues.append(f"block {index + 1} duplicates the adjacent block")
        seen_text.add(normalized)
        previous_text = normalized

        contamination = contamination_issues(text)
        if contamination:
            issues.append(f"block {index + 1} contains {', '.join(contamination)}")
        if re.search(r"(?<!\.)[.!?]{2,}|:\.|[!?]\.", text):
            issues.append(f"block {index + 1} contains malformed punctuation")
        if block.role != "payoff" and not block.fact_ids:
            issues.append(f"block {index + 1} has no factual support")
        for fact_id in block.fact_ids:
            fact = fact_map.get(fact_id)
            if fact is None:
                issues.append(f"block {index + 1} references unknown fact ID {fact_id!r}")
            elif fact.verification not in supported:
                issues.append(f"block {index + 1} references unsupported fact ID {fact_id!r}")

    if request and draft.language.casefold() != request.language.casefold():
        issues.append("draft language does not match request language")
    if issues:
        raise ScriptWriterValidationError(issues)
    return draft


class OpenAIScriptWriterProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = OpenAI(api_key=settings.openai_api_key)

    def generate(self, request: ScriptWriterRequest) -> ScriptWriterResult:
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_director_model,
                instructions=SCRIPT_WRITER_V2_INSTRUCTIONS,
                input=json.dumps(_request_for_model(request), ensure_ascii=False),
                text_format=ScriptDraftV2,
                store=False,
            )
            draft = response.output_parsed
            if not isinstance(draft, ScriptDraftV2):
                return ScriptWriterResult(
                    None, "provider_error", "OpenAI returned no parsed ScriptDraftV2"
                )
            validate_script_draft(draft, request)
            return ScriptWriterResult(draft, "connected")
        except ScriptWriterValidationError as exc:
            return ScriptWriterResult(None, "validation_error", str(exc))
        except (OpenAIError, ValueError, TypeError) as exc:
            return ScriptWriterResult(None, "provider_error", str(exc)[:240])


def generate_script_v2(
    request: ScriptWriterRequest, provider: ScriptWriterProvider
) -> ScriptWriterResult:
    """Validate the request, invoke an isolated provider, and never invent fallback prose."""
    return provider.generate(request)
