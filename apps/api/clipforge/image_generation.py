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
    """A generation attempt failed; ``category`` is safe to persist and show."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


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
            name = type(exc).__name__
            category = (
                "timeout" if "Timeout" in name
                else "invalid_credentials" if "Authentication" in name or "PermissionDenied" in name
                else "model_unavailable" if "NotFound" in name
                else "rate_limited" if "RateLimit" in name
                else "network_error" if "Connection" in name
                else "request_rejected" if "BadRequest" in name
                else "provider_error"
            )
            raise ImageGenerationError(category, "OpenAI image generation failed.") from None
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
