from functools import lru_cache
from pathlib import Path

from keyring.errors import KeyringError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .security.secrets import SecretName, SecretStore


class Settings(BaseSettings):
    app_name: str = "ClipForge API"
    environment: str = "development"
    database_url: str = "sqlite:///./clipforge.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:3000"
    clipforge_ai_mode: str = "local"
    openai_api_key: str | None = None
    openai_director_model: str = "gpt-5.6-terra"
    openai_worker_model: str = "gpt-5.6-luna"
    openai_escalation_model: str = "gpt-5.6-sol"
    openai_tts_model: str = "gpt-4o-mini-tts"
    editor_agent_provider: str = "auto"
    editor_agent_fast_model: str | None = None
    editor_agent_strong_model: str | None = None
    editor_agent_ollama_base_url: str = "http://localhost:11434/v1"
    editor_agent_ollama_model: str = "qwen3:8b"
    editor_agent_history_limit: int = 16
    editor_agent_strong_word_threshold: int = 36
    editor_agent_max_tool_calls: int = 4
    caption_alignment_provider: str = "auto"
    caption_alignment_model: str = "tiny"
    brave_search_api_key: str | None = None
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    # Visual Director V2: paid generated-image fallback after free media fails.
    generated_image_fallback_enabled: bool = True
    generated_image_model: str = "gpt-image-2.5-flare"
    generated_image_quality: str = "low"
    generated_image_size: str = "1024x1536"
    generated_image_timeout_seconds: float = 90.0
    max_auto_generated_images_per_project: int = 3
    max_generation_attempts_per_scene: int = 1
    shortform_max_duration: int = 180
    render_root: Path = Path("./projects")
    downloads_root: Path | None = None

    model_config = SettingsConfigDict(env_file=("../../.env", ".env"), extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def resolved_downloads_root(self) -> Path:
        """Use a configured test/runtime directory or the repository-local Downloads folder."""
        return (self.downloads_root or Path(__file__).resolve().parents[3] / "Downloads").resolve()


SECRET_SETTING_FIELDS: dict[SecretName, str] = {
    "OPENAI_API_KEY": "openai_api_key",
    "BRAVE_SEARCH_API_KEY": "brave_search_api_key",
    "PEXELS_API_KEY": "pexels_api_key",
}


def load_environment_settings() -> Settings:
    """Load settings from process environment and configured .env files only."""
    return Settings()


def resolve_settings(
    env_settings: Settings | None = None,
    secret_store: SecretStore | None = None,
) -> Settings:
    """Resolve settings with keyring secrets taking precedence over environment values."""
    settings = env_settings if env_settings is not None else load_environment_settings()
    store = secret_store if secret_store is not None else SecretStore()
    secure_values: dict[str, str] = {}

    for secret_name, field_name in SECRET_SETTING_FIELDS.items():
        try:
            value = store.get_secret(secret_name)
        except KeyringError:
            value = None
        if value is not None:
            secure_values[field_name] = value

    return settings.model_copy(update=secure_values)


@lru_cache
def get_settings() -> Settings:
    return resolve_settings()


def refresh_settings() -> Settings:
    """Invalidate and rebuild the process-wide resolved settings snapshot."""
    get_settings.cache_clear()
    return get_settings()
