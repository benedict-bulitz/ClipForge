"""Platform-specific, persisted social hashtag suggestions.

This module deliberately has no trend provider.  It produces content-relevant
metadata only; a future provider can add a trend signal to each platform entry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings

Platform = Literal["tiktok", "instagram", "youtube"]
PLATFORMS: tuple[Platform, ...] = ("tiktok", "instagram", "youtube")
_SPAM = {"#fyp", "#foryou", "#viral", "#trending", "#xyzbca", "#fun", "#cool", "#video"}
_STOPWORDS = {
    "und", "der", "die", "das", "den", "dem", "ein", "eine", "einer", "ist", "sind", "mit",
    "für", "von", "wie", "warum", "was", "the", "and", "with", "why", "what", "this", "that",
}


def normalize_hashtag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    if re.search(r"\s", token):
        return None
    if not token.startswith("#"):
        token = f"#{token}"
    if len(token) < 2 or len(token) > 80 or re.search(r"\s", token):
        return None
    if not re.fullmatch(r"#[\w-]+", token, flags=re.UNICODE):
        return None
    return token


def normalize_hashtags(values: Iterable[object], *, reject_spam: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = normalize_hashtag(value)
        if token is None or (reject_spam and token.casefold() in _SPAM):
            continue
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            result.append(token)
    return result[:8]


class PlatformHashtags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(min_length=2, max_length=8)

    @field_validator("hashtags")
    @classmethod
    def validate_hashtags(cls, value: list[str]) -> list[str]:
        normalized = normalize_hashtags(value, reject_spam=True)
        if len(normalized) < 2:
            raise ValueError("at least two valid, content-relevant hashtags are required")
        return normalized


class SocialHashtagOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiktok: PlatformHashtags
    instagram: PlatformHashtags
    youtube: PlatformHashtags


class SocialMetadataProvider(Protocol):
    def generate(self, content: dict) -> SocialHashtagOutput: ...


_INSTRUCTIONS = """Generate independent social metadata for TikTok, Instagram, and YouTube Shorts.
Base them only on the final topic, language, content type, and final narration. Each platform must be
considered independently, though useful overlap is allowed. Return a strong accurate title, a concise
platform-appropriate description, and only as many relevant hashtags as this specific platform and topic
justify (never pad to a fixed count). Prefer accurate topic/entity/concept terms.
Do not use #fyp, #foryou, #viral, #trending, #xyzbca, generic spam, trend claims, scores, descriptions,
or whitespace in a tag. Return only the requested structured data."""


class OpenAISocialMetadataProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = OpenAI(api_key=settings.openai_api_key)

    def generate(self, content: dict) -> SocialHashtagOutput:
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_director_model,
                instructions=_INSTRUCTIONS,
                input=json.dumps(content, ensure_ascii=False),
                text_format=SocialHashtagOutput,
                store=False,
            )
        except OpenAIError as exc:
            raise SocialMetadataError(str(exc)) from exc
        parsed = response.output_parsed
        if not isinstance(parsed, SocialHashtagOutput):
            raise SocialMetadataError("No structured hashtag output was returned.")
        return parsed


class SocialMetadataError(RuntimeError):
    pass


def _terms(content: dict) -> list[str]:
    source = f"{content.get('topic', '')} {content.get('script', '')}"
    words = re.findall(r"[\wÀ-ÖØ-öø-ÿ-]{3,}", source, flags=re.UNICODE)
    unique: list[str] = []
    seen: set[str] = set()
    for word in words:
        key = word.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        unique.append(word[:40])
    return unique[:5] or ["Wissen"]


def _local_output(content: dict) -> SocialHashtagOutput:
    terms = [f"#{term}" for term in _terms(content)]
    language = str(content.get("language") or "en").casefold()
    # These are platform-specific editorial contexts, not trend assertions.
    suffixes = (
        ("#KurzErklärt", "#Wissen") if language == "de" else ("#QuickLearn", "#Learn")
    )
    instagram = ("#WissensSnack", "#Entdecken") if language == "de" else ("#LearnSomething", "#Discover")
    youtube = ("#WissensShorts", "#Shorts") if language == "de" else ("#EducationalShorts", "#Shorts")
    topic = str(content.get("topic") or "Wissensvideo").strip().rstrip("?")
    language_suffix = " – kurz erklärt" if language == "de" else " – explained"
    return SocialHashtagOutput(
        tiktok=PlatformHashtags(title=f"{topic}{language_suffix}", description=f"Die kurze Antwort auf: {topic}.", hashtags=normalize_hashtags([*terms[:3], *suffixes], reject_spam=True)),
        instagram=PlatformHashtags(title=topic, description=f"{topic}. Speichere dir die Erklärung für später.", hashtags=normalize_hashtags([*terms[:2], *instagram], reject_spam=True)),
        youtube=PlatformHashtags(title=f"{topic} | Wissens-Short" if language == "de" else f"{topic} | Short", description=f"Was steckt hinter {topic}? Hier ist die kompakte Erklärung.", hashtags=normalize_hashtags([*terms[:2], *youtube], reject_spam=True)),
    )


def content_from_state(state: dict) -> dict:
    intent = state.get("intent") or {}
    script = state.get("script") or {}
    return {
        "topic": str(intent.get("topic") or ""),
        "script": str(script.get("text") or ""),
        "language": str(intent.get("language") or "en"),
        "content_type": str(intent.get("content_type") or "short video"),
    }


def generate_social_metadata(state: dict, settings: Settings, *, provider: SocialMetadataProvider | None = None) -> dict:
    content = content_from_state(state)
    try:
        output = provider.generate(content) if provider else (
            OpenAISocialMetadataProvider(settings).generate(content)
            if settings.clipforge_ai_mode == "openai" and settings.openai_api_key
            else _local_output(content)
        )
        return {
            "status": "available",
            "source": "generated",
            "platforms": {
                platform: {
                    "title": getattr(output, platform).title,
                    "description": getattr(output, platform).description,
                    "hashtags": list(getattr(output, platform).hashtags),
                    "manual": False,
                }
                for platform in PLATFORMS
            },
        }
    except (SocialMetadataError, ValueError, TypeError) as exc:
        return {"status": "unavailable", "source": "generated", "error": str(exc), "platforms": {}}
