from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .language import LanguageChoice
from .voice import VoiceId, VoicePresentation, VoiceTone


class AdvancedOptions(BaseModel):
    language: LanguageChoice = "auto"
    voice: str | None = None
    voice_id: VoiceId | None = None
    voice_presentation: VoicePresentation | None = None
    voice_tone: VoiceTone | None = None
    voice_speed: float | None = Field(default=None, ge=0.7, le=1.4)
    style: Literal["documentary", "cinematic", "editorial"] | None = None
    platform: str = "auto"
    min_duration: int | None = Field(default=None, ge=10, le=180)
    max_duration: int = Field(default=60, ge=10, le=180)
    pacing: Literal["slow", "balanced", "fast"] = "fast"
    attention_density: Literal["off", "low", "normal", "high", "custom"] = "normal"
    attention_interval_seconds: float | None = Field(default=None, ge=0.6, le=8.0)
    research: Literal["auto", "on", "off"] = "auto"
    captions_enabled: bool = True
    caption_style: Literal["clean", "bold", "minimal", "pop", "boxed", "outline", "karaoke"] = "karaoke"
    caption_position: Literal["upper", "center", "lower"] = "lower"
    caption_font_size: int = Field(default=72, ge=32, le=112)
    caption_text_color: str = Field(default="#ffffff", pattern=r"^#[0-9A-Fa-f]{6}$")
    caption_highlight_color: str = Field(default="#ff6838", pattern=r"^#[0-9A-Fa-f]{6}$")
    caption_words_per_group: int = Field(default=4, ge=2, le=8)
    music_enabled: bool = False
    music_mood: Literal["ambient", "documentary", "tech", "cinematic"] = "ambient"
    music_volume: float = Field(default=0.14, ge=0, le=0.5)
    music_ducking: bool = True
    music_fades: bool = True
    aspect_ratio: Literal["9:16", "1:1", "16:9"] = "9:16"

    @model_validator(mode="after")
    def validate_duration_bounds(self):
        if self.min_duration is not None and self.min_duration > self.max_duration:
            raise ValueError("Minimum duration cannot exceed maximum duration.")
        return self

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language_choice(cls, value: object) -> object:
        if isinstance(value, str):
            return {
                "german": "de",
                "deutsch": "de",
                "english": "en",
                "englisch": "en",
                "": "auto",
            }.get(value.casefold(), value.casefold())
        return value

    @field_validator("caption_style", mode="before")
    @classmethod
    def normalize_caption_style(cls, value: object) -> object:
        if isinstance(value, str):
            return {
                "bold_clean": "bold",
                "bold clean": "bold",
                "minimal_lower_third": "minimal",
                "": "karaoke",
            }.get(value.casefold(), value.casefold())
        return value


class ProjectCreate(BaseModel):
    prompt: str = Field(min_length=3, max_length=4_000)
    mode: Literal["auto"] = "auto"
    options: AdvancedOptions = Field(default_factory=AdvancedOptions)

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_prompt(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class GenerationJobRead(BaseModel):
    id: str
    project_id: str
    base_revision: int | None
    status: Literal["queued", "running", "completed", "failed"]
    current_stage: str
    stage_label: str
    progress: float = Field(ge=0, le=1)
    completed_units: int | None
    total_units: int | None
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    elapsed_seconds: float = Field(ge=0)
    estimated_remaining_seconds: float | None = Field(default=None, ge=0)
    failure_category: str | None
    failure_message: str | None

class EditCreate(BaseModel):
    instruction: str = Field(min_length=2, max_length=2_000)
    base_revision: int | None = Field(default=None, ge=1)

    @field_validator("instruction", mode="before")
    @classmethod
    def normalize_instruction(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class RenderCreate(BaseModel):
    base_revision: int | None = Field(default=None, ge=1)


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int | None = Field(default=None, ge=1)


class ChatCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ChatMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: Literal["user", "assistant"]
    content: str
    tool_metadata: dict[str, Any]
    created_at: datetime


class ChatTurnRead(BaseModel):
    messages: list[ChatMessageRead]
    project: "ProjectRead"


class VoicePreviewCreate(BaseModel):
    text: str | None = Field(default=None, max_length=280)
    language: Literal["en", "de"] = "en"
    voice_id: VoiceId | None = None
    presentation: VoicePresentation = "neutral"
    tone: VoiceTone = "warm"
    speed: float = Field(default=1.0, ge=0.7, le=1.4)

    @field_validator("text", mode="before")
    @classmethod
    def normalize_preview_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        clean = " ".join(value.split())
        return clean or None


class VoicePreviewRead(BaseModel):
    url: str
    provider: Literal["openai", "macos_say"]
    cached: bool
    cache_key: str


class RevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    parent_revision: int | None
    instruction: str
    kind: Literal["initial", "user", "system"]
    changed_components: list[str]
    state: dict[str, Any]
    created_at: datetime


class ProjectRead(BaseModel):
    id: str
    original_prompt: str
    title: str
    status: str
    current_revision: int
    active_tip_revision: int
    can_undo: bool
    can_redo: bool
    created_at: datetime
    updated_at: datetime
    revision: RevisionRead
    revisions: list[dict[str, Any]]


class SceneMediaCandidateRead(BaseModel):
    token: str
    provider: str
    provider_id: str
    kind: Literal["video", "photo"]
    preview_url: str
    source_url: str
    creator: str
    creator_url: str | None = None
    query: str
    width: int
    height: int
    duration: float | None = None
    selected: bool = False


class SceneMediaCandidatesRead(BaseModel):
    scene_number: int
    preferred_kind: Literal["video", "photo"]
    candidates: list[SceneMediaCandidateRead]


class SceneMediaCandidateApply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=8, max_length=160)
    base_revision: int = Field(ge=1)


class ProjectExportRead(BaseModel):
    success: bool
    exported_filename: str
    display_path: str
    file_size: int
    cleanup_status: Literal["complete", "warning"]
    cleanup_warnings: list[str]
    exported_at: str
    media_url: str
    already_exported: bool = False
    project: ProjectRead


class HealthRead(BaseModel):
    status: str
    service: str
    ai_mode: str


IntegrationProvider = Literal["openai", "brave", "pexels"]
IntegrationStatus = Literal[
    "configured",
    "connected",
    "not_configured",
    "invalid_credentials",
    "rate_limited",
    "network_error",
    "provider_error",
    "storage_error",
]


class IntegrationKeyCreate(BaseModel):
    api_key: SecretStr


class IntegrationRead(BaseModel):
    provider: IntegrationProvider
    configured: bool
    last_four: str | None
    status: IntegrationStatus
    source: Literal["keyring", "environment"] | None = None
    message: str | None = None


class EnvImportResult(BaseModel):
    provider: IntegrationProvider
    result: Literal["imported", "skipped"]
    status: IntegrationStatus
    configured: bool
    last_four: str | None
    source: Literal["keyring", "environment"] | None = None


class EnvImportRead(BaseModel):
    results: list[EnvImportResult]
