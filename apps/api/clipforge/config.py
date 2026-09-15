from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    brave_search_api_key: str | None = None
    pexels_api_key: str | None = None
    shortform_max_duration: int = 180
    render_root: Path = Path("./projects")

    model_config = SettingsConfigDict(env_file=("../../.env", ".env"), extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
