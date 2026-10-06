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
    # Research Pipeline V2 ("v1" = the previous snippet-only path).  Hard
    # per-run budgets; browser rendering (Scrapling DynamicFetcher, needs the
    # optional ``scrapling[fetchers]`` extra + Playwright) stays off by default.
    research_pipeline: str = "v2"
    research_max_searches: int = 6
    research_max_documents: int = 6
    research_max_llm_calls: int = 2
    research_browser_fetch: bool = False
    research_max_browser_fetches: int = 1
    research_fetch_timeout_seconds: float = 8.0
    research_deadline_seconds: float = 45.0
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    europeana_api_key: str | None = None
    # Visual Director V2: paid generated-image fallback after free media fails.
    generated_image_fallback_enabled: bool = True
    generated_image_model: str = "gpt-image-2"
    generated_image_quality: str = "low"
    generated_image_size: str = "1024x1536"
    generated_image_timeout_seconds: float = 90.0
    max_auto_generated_images_per_project: int = 3
    max_generation_attempts_per_scene: int = 1
    # Turn a Story Arc fact into a concrete visual (worker model) before
    # generating an image; deterministic fallback when disabled or offline.
    visual_prompt_translation_enabled: bool = True
    # Final Video Critic: review the rendered video, then at most this many
    # automatic targeted repair passes (0 = report only, hard limit 2).
    final_critic_enabled: bool = True
    final_critic_max_repair_passes: int = 1
    # Optional stronger vision critic; "none" keeps V1 local/free only.
    final_critic_vision_provider: str = "none"
    shortform_max_duration: int = 180
    # YouTube Learning Loop: OAuth client (keyring overrides the environment),
    # the local callback registered with that client, and the minimum number
    # of comparable ClipForge Shorts before any "above/below normal" claim.
    youtube_oauth_client_id: str | None = None
    youtube_oauth_client_secret: str | None = None
    youtube_oauth_redirect_uri: str = "http://localhost:8000/api/youtube/oauth/callback"
    youtube_baseline_min_sample: int = 5
    # Multi-platform publishing.  Developer-app credentials come from the
    # keyring (Settings -> Integrations) or the environment; per-account
    # tokens live only in the keyring.  Redirect URIs must be registered
    # exactly like this in the TikTok / Meta developer apps.
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    tiktok_oauth_redirect_uri: str = "http://localhost:8000/api/publishing/tiktok/oauth/callback"
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    instagram_oauth_redirect_uri: str = "http://localhost:8000/api/publishing/instagram/oauth/callback"
    meta_graph_version: str = "v25.0"
    # ClipForge-owned schedules (Instagram/TikTok) run only while this backend
    # runs: how often due work is checked, how late a missed slot may still
    # be published after a restart, and the bounded retry budget.
    publishing_scheduler_enabled: bool = True
    publishing_scheduler_interval_seconds: float = 20.0
    publishing_missed_grace_minutes: int = 15
    publishing_max_attempts: int = 5
    # Topic Intelligence ("Generate Next Video"): how long a scored candidate
    # pool is reused, the hard YouTube quota budget of one discovery refresh
    # (search.list costs 100 units, everything else 1), how many search.list
    # competition probes a refresh may spend, and optional JSON weight
    # overrides for the scoring authority (e.g. '{"trend": 0.25}').
    topic_pool_ttl_minutes: int = 45
    topic_youtube_quota_budget: int = 400
    topic_youtube_search_probes: int = 2
    topic_score_weights: str | None = None
    # Semantic question validation (needs OPENAI_API_KEY; independent of the
    # director's AI mode). Off or unavailable -> strict local acceptance.
    topic_semantic_validation: bool = True
    # Topics per curator request (bounded by the AI request budget; smaller = faster requests).
    topic_curator_batch_size: int = 10
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
    "EUROPEANA_API_KEY": "europeana_api_key",
    "YOUTUBE_OAUTH_CLIENT_ID": "youtube_oauth_client_id",
    "YOUTUBE_OAUTH_CLIENT_SECRET": "youtube_oauth_client_secret",
    "TIKTOK_CLIENT_KEY": "tiktok_client_key",
    "TIKTOK_CLIENT_SECRET": "tiktok_client_secret",
    "META_APP_ID": "meta_app_id",
    "META_APP_SECRET": "meta_app_secret",
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
