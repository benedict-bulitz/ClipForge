from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdvancedOptions(BaseModel):
    language: str | None = None
    voice: str | None = None
    style: str | None = None
    platform: str = "auto"
    max_duration: int | None = Field(default=None, ge=10, le=180)
    caption_style: str | None = None
    music: str | None = None
    aspect_ratio: Literal["9:16", "1:1", "16:9"] = "9:16"


class ProjectCreate(BaseModel):
    prompt: str = Field(min_length=3, max_length=4_000)
    mode: Literal["auto"] = "auto"
    options: AdvancedOptions = Field(default_factory=AdvancedOptions)

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_prompt(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class EditCreate(BaseModel):
    instruction: str = Field(min_length=2, max_length=2_000)
    base_revision: int | None = Field(default=None, ge=1)

    @field_validator("instruction", mode="before")
    @classmethod
    def normalize_instruction(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class RenderCreate(BaseModel):
    base_revision: int | None = Field(default=None, ge=1)


class RevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    parent_revision: int | None
    instruction: str
    changed_components: list[str]
    state: dict[str, Any]
    created_at: datetime


class ProjectRead(BaseModel):
    id: str
    original_prompt: str
    title: str
    status: str
    current_revision: int
    created_at: datetime
    updated_at: datetime
    revision: RevisionRead
    revisions: list[dict[str, Any]]


class HealthRead(BaseModel):
    status: str
    service: str
    ai_mode: str
