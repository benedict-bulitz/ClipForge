from __future__ import annotations

"""Optional LLM steps: question decomposition and evidence synthesis.

Both are bounded (one call each, counted in the research budget) and both
have deterministic fallbacks, so research works without an API key.  The
synthesis step only sees compact evidence units - never raw pages - and its
output is validated against the cited evidence (``package.validate_synthesized``).
"""

import json
import re
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from ..config import Settings
from ..story_arc import is_explanatory_question
from .models import SubQuestion
from .routing import PREFERENCES

MAX_SUB_QUESTIONS = 4
_QUESTION_WORDS = re.compile(
    r"^(?:why|what|how|when|where|who|which|explain|tell me|warum|wieso|weshalb|was|wie|wann|wo|wer|welche\w*|"
    r"erkläre|erklare)\s+",
    re.IGNORECASE,
)
_MECHANISM_TERMS = {"de": "Ursache Erklärung wie funktioniert", "en": "cause explanation how it works"}
_STRENGTHEN_TERMS = {"de": "wissenschaftliche Erklärung", "en": "scientific explanation"}
_CAPABILITY_TERMS = {"de": "möglich Forschung Fähigkeit Grenzen", "en": "possible research capability limits"}
_FOREIGN_QUERY_MARKERS = {
    "de": re.compile(r"(?i)\b(?:scientific explanation|cause|how it works|possible research|capability|limits)\b"),
    "en": re.compile(
        r"(?i)\b(?:wissenschaftlich\w*|erklärung\w*|ursache\w*|wie funktioniert|möglich\w*|forschung|"
        r"fähigkeit\w*|grenzen)\b"
    ),
}

DECOMPOSITION_INSTRUCTIONS = (
    "You plan web research for one short explainer video. Split the question into at most four research "
    "sub-questions that are NECESSARY to answer it - no tangents, no research tree. Always include kind 'core' "
    "(the direct answer). For a why/how question include kind 'mechanism' (the causal chain). Add 'detail' only "
    "for a concrete fact the explanation depends on, 'misconception' only when a common wrong belief exists, "
    "'current' only for time-sensitive questions. For each give a concise web search query (3-8 words, no "
    "question words, in the question's language) and, where international primary sources are likely better, "
    "an English query. Classify the question domain."
)
SYNTHESIS_INSTRUCTIONS = (
    "You turn retrieved evidence into a compact research package for a short explainer. Use ONLY the supplied "
    "evidence units. Every item must cite the evidence_ids it is based on, and must not add any fact, number, "
    "cause or name that the cited evidence does not state - simplify wording, never content. Write each item as "
    "one clear sentence in the requested language. core_answer: the direct answer to the question. what_happens: "
    "the observable phenomenon. mechanism_steps: the causal chain in order (why it happens, then how it works); "
    "leave it empty if the evidence states no cause - never infer one. numbers_dates: important figures exactly "
    "as stated. misconceptions: a common wrong belief the evidence corrects. caveats: a limitation that matters. "
    "viewer_takeaway: what the viewer should understand at the end. Prefer evidence from high-authority sources "
    "when units disagree, and leave out disputed points. Preserve epistemic uncertainty exactly: evidence that "
    "says may, could, might, possible, hypothesis, theorised or equivalent must remain explicitly uncertain in "
    "the core answer and caveats; never promote a hypothesis to a fact."
)


class _SubQuestionOut(BaseModel):
    kind: Literal["core", "mechanism", "detail", "misconception", "current"]
    question: str = Field(min_length=3, max_length=200)
    query: str = Field(min_length=2, max_length=120)
    english_query: str | None = Field(default=None, max_length=120)


class DecompositionOut(BaseModel):
    domain: Literal["science", "health", "history", "technology", "current_events", "everyday"]
    sub_questions: list[_SubQuestionOut] = Field(min_length=1, max_length=MAX_SUB_QUESTIONS)


class _ItemOut(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)


class SynthesisOut(BaseModel):
    core_answer: _ItemOut | None = None
    what_happens: _ItemOut | None = None
    mechanism_steps: list[_ItemOut] = Field(default_factory=list, max_length=4)
    numbers_dates: list[_ItemOut] = Field(default_factory=list, max_length=2)
    misconceptions: list[_ItemOut] = Field(default_factory=list, max_length=1)
    caveats: list[_ItemOut] = Field(default_factory=list, max_length=1)
    supporting: list[_ItemOut] = Field(default_factory=list, max_length=2)
    viewer_takeaway: _ItemOut | None = None


class ResearchLLM(Protocol):
    def decompose(self, question: str, language: str) -> DecompositionOut | None: ...

    def synthesize(self, payload: dict[str, Any]) -> SynthesisOut | None: ...


class OpenAIResearchLLM:
    """Worker-model calls; any provider error means "use the deterministic path"."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(api_key=settings.openai_api_key)

    def _parse(self, instructions: str, payload: dict[str, Any], schema: type[BaseModel]) -> Any:
        try:
            response = self._client.responses.parse(
                model=self._settings.openai_worker_model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                text_format=schema,
                store=False,
            )
            parsed = response.output_parsed
            return parsed if isinstance(parsed, schema) else None
        except (OpenAIError, ValueError, TypeError):
            return None

    def decompose(self, question: str, language: str) -> DecompositionOut | None:
        return self._parse(DECOMPOSITION_INSTRUCTIONS, {"question": question, "language": language}, DecompositionOut)

    def synthesize(self, payload: dict[str, Any]) -> SynthesisOut | None:
        return self._parse(SYNTHESIS_INSTRUCTIONS, payload, SynthesisOut)


def llm_for(settings: Settings) -> ResearchLLM | None:
    if settings.clipforge_ai_mode != "openai" or not settings.openai_api_key:
        return None
    return OpenAIResearchLLM(settings)


def topic_query(text: str) -> str:
    cleaned = _QUESTION_WORDS.sub("", str(text or "").strip())
    return cleaned.strip(" ?!.,") or str(text or "").strip()


def deterministic_sub_questions(question: str, query: str, language: str, *, focus: str | None = None) -> list[SubQuestion]:
    """core (+ mechanism for why/how questions) - the smallest useful decomposition."""
    lang = "de" if str(language).startswith("de") else "en"
    core_query = topic_query(query)[:160]
    if focus == "broaden":
        # The first pass found no direct answer: search the subject words only.
        content = [word for word in re.findall(r"[\wÄÖÜäöüß-]+", topic_query(question)) if len(word) > 3]
        core_query = " ".join(content[:6]) or core_query
    if focus == "strengthen":
        # A valid answer was found only in a weak snippet: look for an explanatory, citable source.
        core_query = f"{topic_query(question)[:120]} {_STRENGTHEN_TERMS[lang]}"
    if focus == "capability":
        # "Kann X Y?": the retry asks for evidence about X itself doing Y, not about related uses of X.
        core_query = f"{topic_query(question)[:120]} {_CAPABILITY_TERMS[lang]}"
    subs = [SubQuestion("q_core", "core", question, core_query)]
    if is_explanatory_question(question) or focus == "mechanism":
        base = topic_query(question)[:120]
        mechanism_query = f"{base} {_MECHANISM_TERMS[lang]}" if focus != "mechanism" else core_query
        if mechanism_query.casefold() != core_query.casefold():
            subs.append(SubQuestion("q_mechanism", "mechanism", f"{question} (cause / mechanism)", mechanism_query))
    return subs


def _query_in_research_language(candidate: str, fallback: str, language: str) -> str:
    """Keep generated queries in the requested language.

    This deliberately checks only planner-added research phrases: names and
    technical terms may legitimately come from another language.
    """
    lang = "de" if str(language).startswith("de") else "en"
    cleaned = topic_query(candidate)[:120]
    return topic_query(fallback)[:120] if _FOREIGN_QUERY_MARKERS[lang].search(cleaned) else cleaned


def sub_questions_from_llm(out: DecompositionOut, question: str, query: str, language: str) -> tuple[list[SubQuestion], str | None]:
    """Validated LLM decomposition (core first, unique kinds, bounded); falls back per field."""
    fallbacks = deterministic_sub_questions(question, query, language)
    fallback_by_kind = {sub.kind: sub.query for sub in fallbacks}
    default_query = fallback_by_kind.get("core", topic_query(query))
    subs: list[SubQuestion] = []
    seen: set[str] = set()
    for item in out.sub_questions:
        if item.kind in seen:
            continue
        seen.add(item.kind)
        planned_query = _query_in_research_language(
            item.query, fallback_by_kind.get(item.kind, default_query), language
        )
        subs.append(SubQuestion(f"q_{item.kind}", item.kind, item.question.strip(), planned_query))
        if item.english_query and str(language).startswith("de") and len(subs) < MAX_SUB_QUESTIONS and item.kind in {"core", "mechanism"}:
            subs.append(SubQuestion(f"q_{item.kind}_en", item.kind, item.question.strip(), topic_query(item.english_query)[:120]))
    if "core" not in seen:
        subs.insert(0, SubQuestion("q_core", "core", question, topic_query(query)[:160]))
    core = next(sub for sub in subs if sub.kind == "core")
    subs = [core, *[sub for sub in subs if sub is not core]][:MAX_SUB_QUESTIONS]
    domain = out.domain if out.domain in PREFERENCES else None
    return subs, domain
