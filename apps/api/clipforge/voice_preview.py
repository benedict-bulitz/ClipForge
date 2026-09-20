import hashlib
import json
import shutil
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock

from .config import Settings
from .renderer import _create_voice
from .schemas import VoicePreviewCreate
from .voice import initial_voice

DEFAULT_PREVIEW_TEXT = (
    "This is how your ClipForge narration will sound. "
    "You can adjust the voice, tone, and speaking speed before creating the video."
)
PREVIEW_CACHE_MAX_FILES = 100
PREVIEW_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
PREVIEW_RATE_LIMIT = 6
PREVIEW_RATE_WINDOW_SECONDS = 60

_preview_requests: dict[str, deque[float]] = defaultdict(deque)
_preview_lock = Lock()


class PreviewRateLimited(RuntimeError):
    pass


def enforce_preview_rate_limit(client_id: str, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _preview_lock:
        requests = _preview_requests[client_id]
        while requests and current - requests[0] >= PREVIEW_RATE_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= PREVIEW_RATE_LIMIT:
            raise PreviewRateLimited("Too many voice previews. Please wait a minute and try again.")
        requests.append(current)


def reset_preview_rate_limits() -> None:
    with _preview_lock:
        _preview_requests.clear()


def generate_voice_preview(
    payload: VoicePreviewCreate, settings: Settings
) -> dict[str, str | bool]:
    text = payload.text or DEFAULT_PREVIEW_TEXT
    voice = initial_voice(
        voice_id=payload.voice_id,
        presentation=payload.presentation,
        tone=payload.tone,
        speed=payload.speed,
    )
    provider = "openai" if settings.openai_api_key else "macos_say"
    cache_key = _preview_cache_key(
        provider=provider,
        model=settings.openai_tts_model if provider == "openai" else "system",
        text=text,
        language=payload.language,
        voice=voice,
    )
    cache_root = settings.render_root.resolve() / "voice-previews"
    cache_root.mkdir(parents=True, exist_ok=True)
    _cleanup_preview_cache(cache_root)
    cached_path = _cached_preview_path(cache_root, cache_key)
    if cached_path:
        return {
            "url": f"/media/voice-previews/{cached_path.name}",
            "provider": provider,
            "cached": True,
            "cache_key": cache_key,
        }

    state = {
        "script": {"text": text, "word_count": len(text.split())},
        "intent": {"language": payload.language},
        "duration": {
            "estimated_seconds": max(2.0, len(text.split()) / 155 * 60),
        },
        "voice": voice,
    }
    with tempfile.TemporaryDirectory(prefix="clipforge-preview-") as temp_name:
        generated, generated_provider = _create_voice(state, Path(temp_name), settings)
        actual_cache_key = _preview_cache_key(
            provider=generated_provider,
            model=settings.openai_tts_model if generated_provider == "openai" else "system",
            text=text,
            language=payload.language,
            voice=voice,
        )
        output = cache_root / f"{actual_cache_key}{generated.suffix}"
        partial = output.with_suffix(output.suffix + ".part")
        shutil.copy2(generated, partial)
        partial.replace(output)
    return {
        "url": f"/media/voice-previews/{output.name}",
        "provider": generated_provider,
        "cached": False,
        "cache_key": actual_cache_key,
    }


def _preview_cache_key(
    *, provider: str, model: str, text: str, language: str, voice: dict
) -> str:
    cache_input = {
        "provider": provider,
        "model": model,
        "text": text,
        "language": language,
        "voice": voice,
    }
    return hashlib.sha256(
        json.dumps(cache_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


def _cached_preview_path(cache_root: Path, cache_key: str) -> Path | None:
    for suffix in (".wav", ".aiff"):
        cached_path = cache_root / f"{cache_key}{suffix}"
        if cached_path.exists() and cached_path.stat().st_size > 4096:
            return cached_path
    return None


def _cleanup_preview_cache(cache_root: Path) -> None:
    now = time.time()
    files = sorted(
        (path for path in cache_root.iterdir() if path.is_file() and ".part" not in path.name),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for index, path in enumerate(files):
        if index >= PREVIEW_CACHE_MAX_FILES or now - path.stat().st_mtime > PREVIEW_CACHE_MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
