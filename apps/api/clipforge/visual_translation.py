"""Translate a scene's information into one photographable background visual.

The image model must be told what the viewer can *see*, not what the
narration *says*: "Forscher vermuten, dass runzlige Haut beim Greifen hilft"
has no visual subject in its words ("Forscher", "vermuten", "dass").  This
module asks the configured worker model, with the full Story Arc fact, for a
concrete subject/action/setting.  It is only used on the paid-generation path
(an OpenAI key is present there anyway); results are cached per statement.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from .config import Settings

MAX_CACHE = 128

TRANSLATION_INSTRUCTIONS = (
    "You direct background visuals for factual short videos. Given one statement from the script "
    "(any language) and its context, describe ONE concrete, photographable background image in English "
    "that shows what the statement is about. Answer the question: what can the viewer actually SEE that "
    "represents this information? Name physical subjects, their visible state or action, and a setting. "
    "Never use abstract or reporting words (researchers, suspect, study, reason, why, maybe, that) as the "
    "subject unless people doing research are literally the topic. No text, labels, diagrams, infographics, "
    "charts, logos or UI. Keep anatomy and physics plausible. Never depict anything listed in "
    "must_not_show. Keep each field short."
)


class VisualTranslation(BaseModel):
    main_subject: str = Field(min_length=2, max_length=120)
    visible_state_or_action: str = Field(default="", max_length=140)
    setting: str = Field(default="", max_length=100)
    details: list[str] = Field(default_factory=list, max_length=4)


def _client(settings: Settings) -> Any:
    return OpenAI(api_key=settings.openai_api_key, timeout=25.0, max_retries=0)


# Indirection so the test suite can hard-disable real network clients.
TRANSLATOR_CLIENT_FACTORY: Callable[[Settings], Any] = _client
_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()


def clear_translation_cache() -> None:
    _CACHE.clear()


def translate_statement(
    statement: str,
    *,
    settings: Settings,
    topic: str = "",
    subjects: list[str] | None = None,
    story_role: str | None = None,
    must_not_show: list[str] | None = None,
) -> dict[str, Any] | None:
    """A concrete visual for ``statement``; ``None`` when unavailable (never raises)."""
    statement = " ".join(str(statement or "").split())[:600]
    if not statement or not settings.openai_api_key or not settings.visual_prompt_translation_enabled:
        return None
    request = {
        "statement": statement,
        "topic": " ".join(str(topic or "").split())[:200],
        "canonical_visual_subjects": list(subjects or [])[:6],
        "story_role": story_role,
        "must_not_show": list(must_not_show or [])[:6],
    }
    key = hashlib.sha256(json.dumps([settings.openai_worker_model, request], sort_keys=True).encode()).hexdigest()
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return dict(_CACHE[key])
    try:
        response = TRANSLATOR_CLIENT_FACTORY(settings).responses.parse(
            model=settings.openai_worker_model,
            instructions=TRANSLATION_INSTRUCTIONS,
            input=json.dumps(request, ensure_ascii=False),
            text_format=VisualTranslation,
            max_output_tokens=300,
            store=False,
        )
        parsed = response.output_parsed
    except (OpenAIError, OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(parsed, VisualTranslation):
        return None
    result = {
        "main_subject": parsed.main_subject.strip(),
        "visible_state_or_action": parsed.visible_state_or_action.strip(),
        "setting": parsed.setting.strip(),
        "details": [str(item).strip()[:60] for item in parsed.details if str(item).strip()][:4],
        "source": "llm_translation",
        "model": settings.openai_worker_model,
    }
    _CACHE[key] = result
    while len(_CACHE) > MAX_CACHE:
        _CACHE.popitem(last=False)
    return dict(result)
