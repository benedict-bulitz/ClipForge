import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from .config import Settings
from .schemas import AdvancedOptions


class AIIntent(BaseModel):
    topic: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    language: str = Field(min_length=2)
    content_type: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    research_required: bool
    visual_style: str = Field(min_length=1)
    shortform: bool = True


class AIFact(BaseModel):
    claim: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    source_label: str | None = None
    source_url: str | None = None


class AIScriptBlock(BaseModel):
    role: str = Field(min_length=1)
    text: str = Field(min_length=1)


class AIProjectPlan(BaseModel):
    intent: AIIntent
    research_questions: list[str] = Field(min_length=0, max_length=6)
    facts: list[AIFact] = Field(min_length=0, max_length=10)
    answer_skeleton: list[str] = Field(min_length=2, max_length=8)
    script_blocks: list[AIScriptBlock] = Field(min_length=2, max_length=8)
    music_mood: str = Field(min_length=1)


@dataclass(frozen=True)
class AIPlanResult:
    plan: AIProjectPlan | None
    status: str
    error: str | None = None


def plan_with_openai(
    prompt: str, options: AdvancedOptions, settings: Settings
) -> AIPlanResult:
    """Create a schema-validated semantic plan and report provider failure explicitly."""
    if settings.clipforge_ai_mode != "openai":
        return AIPlanResult(None, "local_planner")
    if not settings.openai_api_key:
        return AIPlanResult(None, "missing_key", "OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    instructions = (
        "You are ClipForge's director. Build a compact short-form video plan. "
        "Honor every option. For factual topics, do not invent sources or claims; only attach a "
        "URL when you know the source precisely, and otherwise leave it null. Keep narration "
        "short enough for the requested maximum duration at roughly 155 words per minute. "
        "No greeting and no filler."
    )
    request = {
        "prompt": prompt,
        "options": options.model_dump(mode="json", exclude_none=True),
    }
    try:
        response = client.responses.parse(
            model=settings.openai_director_model,
            instructions=instructions,
            input=json.dumps(request, ensure_ascii=False),
            text_format=AIProjectPlan,
        )
        plan = response.output_parsed
        if not isinstance(plan, AIProjectPlan):
            return AIPlanResult(None, "provider_error", "OpenAI returned no parsed project plan")
        return AIPlanResult(plan, "connected")
    except (OpenAIError, ValueError, TypeError) as exc:
        return AIPlanResult(None, "provider_error", str(exc)[:240])


def ai_plan_to_dict(plan: AIProjectPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")
