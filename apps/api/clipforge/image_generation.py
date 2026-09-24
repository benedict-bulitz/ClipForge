"""Bounded, optional OpenAI image generation for scene-level visual fallbacks.

Generation is a paid fallback: callers decide *whether* to generate (budget,
policy, story constraints); this module only performs one request and reports
what happened.  Credentials come from the resolved ClipForge settings and are
never logged, persisted or included in error messages.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI, OpenAIError

from .config import Settings

# Display names only; the model identifier itself comes from
# ``Settings.generated_image_model`` (single configuration source).
MODEL_LABELS = {"gpt-image-2": "GPT Image 2", "gpt-image-1.5": "GPT Image 1.5", "gpt-image-1": "GPT Image 1"}
QUALITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "auto": "Auto"}
# low/medium/high/auto are the GPT image model qualities of the Images API.
SUPPORTED_QUALITIES = frozenset(QUALITY_LABELS)
# Standard portrait size supported by every GPT image model; 2:3 is cropped to
# 9:16 by the existing smart-crop step, and prompts keep the subject centered.
DEFAULT_PORTRAIT_SIZE = "1024x1536"
STANDARD_SIZES = ("1024x1024", "1024x1536", "1536x1024")
# Models documented by the installed SDK as accepting arbitrary WIDTHxHEIGHT
# (edges divisible by 16, aspect ratio between 1:3 and 3:1, max edge 3840).
_FLEXIBLE_SIZE_MODELS = ("gpt-image-2",)
_MAX_EDGE = 3840
MAX_PROMPT_CHARS = 1200


def resolve_image_size(model: str, requested: str | None) -> str:
    """A size the model accepts; otherwise the closest valid portrait size."""
    value = str(requested or "").strip().casefold()
    if value in STANDARD_SIZES:
        return value
    try:
        width, height = (int(part) for part in value.split("x", 1))
    except ValueError:
        return DEFAULT_PORTRAIT_SIZE
    flexible = any(model == name or model.startswith(f"{name}-") for name in _FLEXIBLE_SIZE_MODELS)
    if (
        flexible
        and width > 0 and height > 0
        and width % 16 == 0 and height % 16 == 0
        and max(width, height) <= _MAX_EDGE
        and 1 / 3 <= width / height <= 3
    ):
        return f"{width}x{height}"
    return DEFAULT_PORTRAIT_SIZE if height >= width else "1536x1024"


class ImageGenerationError(RuntimeError):
    """A generation attempt failed; ``category``/``detail`` are safe to persist and show."""

    def __init__(self, category: str, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.category = category
        self.detail = detail


# One place for user-facing, secret-free status messages (Change Media UI and
# persisted generation records).
GENERATION_MESSAGES = {
    "missing_api_key": "Image generation failed: OpenAI API key missing. Add it in Settings → Integrations.",
    "invalid_credentials": "Image generation failed: OpenAI rejected the API key.",
    "permission_denied": "Image generation failed: this OpenAI account has no access to {model} (organization verification or model access may be required).",
    "billing": "Image generation failed: OpenAI billing limit reached or credits exhausted.",
    "rate_limited": "Image generation failed: OpenAI rate limit reached. Try again in a moment.",
    "model_unavailable": "Image generation failed: model {model} is not available for this OpenAI account.",
    "unsupported_parameter": "Image generation failed: OpenAI rejected a request parameter{detail}.",
    "content_policy": "Image generation failed: OpenAI's safety system blocked this prompt.",
    "request_rejected": "Image generation failed: OpenAI rejected the request{detail}.",
    "timeout": "Image generation failed: the request to OpenAI timed out.",
    "network_error": "Image generation failed: OpenAI could not be reached (network).",
    "provider_error": "Image generation failed: OpenAI returned an error.",
    "empty_response": "Image generation failed: OpenAI returned no image.",
    "invalid_response": "Image generation failed: OpenAI returned unreadable image data.",
    "invalid_prompt": "Image generation failed: the prompt is empty.",
    "persistence_failed": "Image generation failed: the generated image could not be saved.",
    "rejected": "The generated image was rejected because it did not match this scene.",
    "protected_reveal": "This prompt would show the story's answer before its reveal; edit the prompt.",
    "no_safe_prompt": "No visual subject can be shown for this scene before the story's reveal.",
    "unchanged": "Generation returned the current media again; nothing was replaced.",
}
# Failures that would repeat for every scene in the same run.
PROVIDER_BLOCKING_ERRORS = frozenset({
    "missing_api_key", "invalid_credentials", "permission_denied", "billing", "model_unavailable",
    "network_error", "timeout", "rate_limited",
})


def generation_message(category: str | None, *, model: str = "", detail: str | None = None) -> str:
    template = GENERATION_MESSAGES.get(str(category or ""), GENERATION_MESSAGES["provider_error"])
    return template.format(model=model or "the image model", detail=f" ({detail})" if detail else "")


def _error_category(exc: OpenAIError) -> tuple[str, str | None]:
    """Classify an OpenAI SDK error from its type, status and error code/param only."""
    name = type(exc).__name__
    code = str(getattr(exc, "code", None) or "").casefold()
    param = str(getattr(exc, "param", None) or "")[:40] or None
    if "Timeout" in name:
        return "timeout", None
    if "Connection" in name:
        return "network_error", None
    if "Authentication" in name:
        return "invalid_credentials", None
    if code in {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}:
        return "billing", None
    if "PermissionDenied" in name:
        return "permission_denied", None
    if "NotFound" in name or code == "model_not_found" or param == "model":
        return "model_unavailable", None
    if "RateLimit" in name:
        return "rate_limited", None
    if code in {"moderation_blocked", "content_policy_violation"}:
        return "content_policy", None
    if code in {"unsupported_parameter", "unknown_parameter", "unsupported_value", "invalid_value"}:
        return "unsupported_parameter", param
    if "BadRequest" in name or "UnprocessableEntity" in name:
        return "request_rejected", param
    return "provider_error", None


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    model: str
    quality: str
    size: str
    output_format: str
    usage: dict[str, Any] = field(default_factory=dict)


def model_label(model: str) -> str:
    return MODEL_LABELS.get(model, model)


def quality_label(quality: str) -> str:
    return QUALITY_LABELS.get(quality, quality.title())


def _usage(response: Any) -> dict[str, Any]:
    """Token usage exactly as reported; nothing is estimated or priced."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    dump = getattr(usage, "model_dump", None)
    data = dump() if callable(dump) else dict(usage) if isinstance(usage, dict) else {}
    return {key: value for key, value in data.items() if isinstance(value, int | float | dict)}


def _openai_client(settings: Settings, timeout: float) -> Any:
    return OpenAI(api_key=settings.openai_api_key, timeout=timeout, max_retries=0)


# Indirection so the test suite can hard-disable real network clients.
OPENAI_CLIENT_FACTORY: Callable[[Settings, float], Any] = _openai_client


class OpenAIImageGenerator:
    """One-image-per-call wrapper around the OpenAI Images API."""

    provider = "generated_openai"

    def __init__(self, settings: Settings, *, client: Any | None = None):
        self._settings = settings
        self._client = client

    def __repr__(self) -> str:
        return "OpenAIImageGenerator(api_key=<redacted>)"

    @property
    def model(self) -> str:
        return self._settings.generated_image_model

    def generate(self, prompt: str, *, quality: str, size: str) -> GeneratedImage:
        prompt = " ".join(str(prompt or "").split())[:MAX_PROMPT_CHARS]
        if not prompt:
            raise ImageGenerationError("invalid_prompt", "No visual prompt could be built for this scene.")
        quality = quality if quality in SUPPORTED_QUALITIES else "low"
        size = resolve_image_size(self.model, size)
        try:
            client = self._client or OPENAI_CLIENT_FACTORY(
                self._settings, float(self._settings.generated_image_timeout_seconds)
            )
            response = client.images.generate(
                model=self.model,
                prompt=prompt,
                n=1,
                quality=quality,
                size=size,
                output_format="png",
            )
        except OpenAIError as exc:
            category, detail = _error_category(exc)
            raise ImageGenerationError(category, "OpenAI image generation failed.", detail=detail) from None
        except (OSError, ValueError, TypeError) as exc:
            raise ImageGenerationError("provider_error", f"OpenAI image generation failed: {type(exc).__name__}.") from None
        data = next(iter(getattr(response, "data", None) or []), None)
        encoded = getattr(data, "b64_json", None) if data is not None else None
        if not encoded:
            raise ImageGenerationError("empty_response", "OpenAI returned no image data.")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ImageGenerationError("invalid_response", "OpenAI returned unreadable image data.") from None
        return GeneratedImage(
            data=image,
            model=self.model,
            quality=str(getattr(response, "quality", None) or quality),
            size=str(getattr(response, "size", None) or size),
            output_format=str(getattr(response, "output_format", None) or "png"),
            usage=_usage(response),
        )


def get_image_generator(settings: Settings, *, automatic: bool = True) -> OpenAIImageGenerator | None:
    """A generator only when an OpenAI key is configured.

    Automatic fallback additionally requires ``generated_image_fallback_enabled``;
    a manual, user-confirmed request only needs the key.
    """
    if not settings.openai_api_key or (automatic and not settings.generated_image_fallback_enabled):
        return None
    return OpenAIImageGenerator(settings)
