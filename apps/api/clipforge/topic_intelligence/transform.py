"""Topic -> compelling, truth-seeking German question (bounded, validated).

Raw trends ("Schlafträgheit", a video title, a headline) are rarely good video
prompts.  With the OpenAI director configured, ONE structured worker-model call
rewrites a whole batch and assesses knowledge-short suitability; otherwise a
deterministic path keeps source questions and builds plain template questions.
Every question is validated deterministically either way: natural German, a
real question, no embedded answer, no clickbait, no number the evidence lacks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..language import detect_text_language
from .candidate import TopicGroup
from .text import (
    NICHE_PRIORS,
    classify_niche,
    compact,
    extract_question,
    question_issues,
    question_mechanism,
)

TRANSFORM_CLIENT_FACTORY: Any = OpenAI
# Topics per transformation call (one LLM request per batch when OpenAI is enabled).
MAX_BATCH = 20
# Bumped whenever the question step changes, so pools built by an older one are not reused.
TRANSFORMATION_VERSION = "tq3"

FLAG_VALUES = (
    "opinion",
    "vague",
    "needs_long_context",
    "unverifiable",
    "trivial",
    "no_clear_payoff",
    "clickbait_source",
    "person_centric",
    "tragedy_or_breaking_news",
    "politics",
    "entertainment",
    "not_dach_relevant",
)
# Flags that make a topic unusable for ClipForge, vs. ones that only penalize.
REJECT_FLAGS = frozenset({"opinion", "unverifiable", "tragedy_or_breaking_news", "person_centric", "not_dach_relevant"})

ASSESSMENT_KEYS = (
    "curiosity_gap",
    "clear_payoff",
    "substance",
    "premise_clarity",
    "information_gain",
    "visual_potential",
    "researchability",
    "dach_relevance",
    "broad_appeal",
    "accessibility",
)

TRANSFORM_INSTRUCTIONS = (
    "You are ClipForge's topic editor for a GERMAN short-form knowledge channel (YouTube Shorts, TikTok, Reels; "
    "15-40 second explainers) whose audience lives in Germany, Austria and Switzerland. For every supplied topic "
    "decide whether it can become one strong knowledge short and, if so, rewrite it as ONE natural German question "
    "a curious viewer would ask. Rules for the question: everyday spoken German a 14-year-old understands, concrete "
    "rather than broad, immediately understandable without context, truth-seeking (it asks, it does not claim), no "
    "answer or explanation inside the question, no invented premise (use only what the supplied evidence says; never "
    "add numbers, dates, records or claims that are not in the evidence), no clickbait words, no emoji, at most 18 "
    "words, ending with a question mark. Prefer the underlying explainable phenomenon over the news event (e.g. raw "
    "topic 'Schlafträgheit' -> 'Warum fühle ich mich nach einem kurzen Mittagsschlaf manchmal schlechter als "
    "vorher?'). Do not translate international trends blindly: rate dach_relevance by whether German-speaking "
    "viewers would genuinely care. Score each dimension 0-10: curiosity_gap, clear_payoff (a clear, satisfying "
    "answer exists), substance (enough for 15-40 seconds, not trivial), premise_clarity, information_gain, "
    "visual_potential (real footage/photos can show it), researchability (verifiable from reliable sources), "
    "dach_relevance, broad_appeal (would an average German viewer WITHOUT special interest want the answer?), "
    "accessibility (the premise is understandable immediately, without knowing a specific place, project, date, "
    "flight, code or person). Never use a generic wrapper such as 'Was steckt eigentlich hinter X?', 'Was ist X?' "
    "or 'Wie funktioniert eigentlich X?' around a bare name; prefer a concrete curiosity mechanism (Warum ..., "
    "Wieso ..., Wie kann es sein, dass ..., Was würde passieren, wenn ..., Warum passiert X, obwohl Y ...) ONLY when "
    "the evidence supports it - never manufacture curiosity. If the topic is a calendar date, an isolated event id, "
    "an obscure project or a name that needs context, either find the broadly interesting, supported phenomenon behind "
    "it or set usable=false. Flag problems with the allowed flags only: opinion, vague, needs_long_context, unverifiable, "
    "trivial, no_clear_payoff, person_centric (gossip or a person's biography), tragedy_or_breaking_news (deaths, "
    "accidents, attacks), politics (party politics, elections), entertainment (a show, match or release itself), "
    "not_dach_relevant. Set usable=false when no honest knowledge question exists. angle: max 12 German words on "
    "what the video would explain. Return the supplied id unchanged. Structured output only."
)


class AITopicAssessment(BaseModel):
    id: str
    usable: bool
    question: str = Field(default="", max_length=220)
    niche: str = "unknown"
    angle: str = Field(default="", max_length=160)
    curiosity_gap: int = Field(default=5, ge=0, le=10)
    clear_payoff: int = Field(default=5, ge=0, le=10)
    substance: int = Field(default=5, ge=0, le=10)
    premise_clarity: int = Field(default=5, ge=0, le=10)
    information_gain: int = Field(default=5, ge=0, le=10)
    visual_potential: int = Field(default=5, ge=0, le=10)
    researchability: int = Field(default=5, ge=0, le=10)
    dach_relevance: int = Field(default=5, ge=0, le=10)
    broad_appeal: int = Field(default=5, ge=0, le=10)
    accessibility: int = Field(default=5, ge=0, le=10)
    flags: list[str] = Field(default_factory=list)


class AITopicBatch(BaseModel):
    items: list[AITopicAssessment]


@dataclass
class Transformed:
    key: str
    question: str
    niche: str
    method: Literal["llm", "source_question", "converted_headline", "template", "none"]
    assessment: dict[str, float] = field(default_factory=dict)
    assessment_confidence: Literal["low", "medium", "high"] = "low"
    flags: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    angle: str = ""
    # Local extraction notes (extracted_clause, converted_headline, needs_title_context, shouting, ...).
    notes: set[str] = field(default_factory=set)


VISUAL_BY_NICHE = {
    "weltraum": 0.85, "natur_tiere": 0.85, "wetter_klima": 0.85, "technik": 0.8, "essen_trinken": 0.8,
    "geografie": 0.8, "alltag_phaenomene": 0.75, "koerper_gesundheit": 0.7, "wissenschaft": 0.7,
    "geschichte": 0.7, "psychologie": 0.55, "unknown": 0.55, "wirtschaft_geld": 0.5, "gesellschaft": 0.5,
    "sprache_kultur": 0.45,
}


def _evidence_text(group: TopicGroup) -> str:
    return " ".join(f"{item.title} {item.description}" for item in group.sightings)


def _group_flags(group: TopicGroup) -> list[str]:
    mapping = {
        "person": "person_centric",
        "tragedy": "tragedy_or_breaking_news",
        "politics": "politics",
        "entertainment_or_sport": "entertainment",
        "disambiguation": "vague",
    }
    return sorted({mapping[flag] for flag in group.flags if flag in mapping})


def _heuristic_assessment(question: str, niche: str, group: TopicGroup, *, template: bool) -> dict[str, float]:
    mechanism = question_mechanism(question)
    curiosity = {"paradox": 0.8, "what_if": 0.8, "why": 0.75, "how": 0.7, "yes_no": 0.62}.get(mechanism, 0.5)
    payoff = {"paradox": 0.7, "what_if": 0.65, "why": 0.7, "how": 0.7, "yes_no": 0.6}.get(mechanism, 0.5)
    has_article = any(item.kind == "article" for item in group.sightings)
    return {
        "curiosity_gap": curiosity,
        "clear_payoff": payoff,
        "substance": 0.5 if template else 0.6,
        "premise_clarity": 0.55 if template else 0.65,
        "information_gain": 0.5,
        "visual_potential": VISUAL_BY_NICHE.get(niche, 0.4),
        "researchability": 0.8 if has_article else 0.55,
        # Every V1 source is a German-market source (de.wikipedia, YouTube DE, Brave DE).
        "dach_relevance": 0.75,
    }


# Conditions one "gets" (Schluckauf, Muskelkater) - not reflexes/actions (Gähnen), which would read badly.
_BODY_REACTION = ("kontraktion", "symptom", "reizung", "beschwerde", "muskelschmerz")
_PHENOMENON = ("erscheinung", "phänomen", "phaenomen", "wetterereignis", "niederschlag", "naturereignis", "effekt")
_DEVICE = ("gerät", "geraet", "maschine", "verfahren", "technologie", "antrieb")


def _template_question(group: TopicGroup, niche: str) -> tuple[str, str, set[str]]:
    """(question, method, notes) without an LLM: keep/extract a real question, else a concrete template.

    Never a generic "Was steckt eigentlich hinter X?" wrapper: a topic without a
    supported concrete question is not transformed at all.
    """
    evidence = _evidence_text(group)
    order = {"video": 0, "news": 1, "article": 2}
    for item in sorted(group.sightings, key=lambda sighting: (order[sighting.kind], sighting.title)):
        question, notes = extract_question(item.title)
        if question and detect_text_language(question) != "en" and not question_issues(question, evidence=evidence):
            method = "converted_headline" if "converted_headline" in notes else "source_question"
            return question, method, notes
    articles = [item for item in group.sightings if item.kind == "article"]
    if not articles:
        return "", "none", set()
    subject = articles[0].title.strip()
    description = f"{articles[0].description}".casefold()
    noun = _with_article(subject, articles[0].description)
    # Concrete forms only where the encyclopedia itself says what the subject is.
    if any(marker in description for marker in _BODY_REACTION):
        return f"Warum bekommen wir {subject}?", "template", set()
    if noun and any(marker in description for marker in _PHENOMENON):
        return f"Wie entsteht eigentlich {noun}?", "template", set()
    if noun and niche == "technik" and any(marker in description for marker in _DEVICE):
        return f"Wie funktioniert eigentlich {noun}?", "template", set()
    return "", "none", set()


def _with_article(subject: str, text: str) -> str | None:
    """'ein Regenbogen' / 'eine Sternschnuppe' from the extract's own article ('Der Regenbogen ist ...').

    A mass noun used without an article ('Hagel ist ...') stays bare; an unknown
    grammatical form yields None rather than broken German.
    """
    name = re.escape(subject)
    match = re.search(rf"\b(Der|Das|Ein|Die|Eine)\s+{name}\b", str(text or ""))
    if match:
        return f"{'eine' if match.group(1) in {'Die', 'Eine'} else 'ein'} {subject}"
    if re.search(rf"(?:^|\s){name}\s+(?:ist|bezeichnet|nennt man)\b", str(text or "")):
        return subject
    return None


def deterministic_transform(group: TopicGroup) -> Transformed:
    niche, _strength = classify_niche(group.title, group.description())
    question, method, notes = _template_question(group, niche)
    flags = _group_flags(group)
    if notes & {"shouting", "exclamation"}:
        flags = sorted({*flags, "clickbait_source"})  # styling removed; the premise still counts less
    if method == "none":
        return Transformed(group.key, "", niche, "none", flags=flags, issues=["no_question_transformation"])
    niche = classify_niche(question, group.title, group.description())[0]
    issues = question_issues(question, evidence=_evidence_text(group))
    return Transformed(
        group.key,
        question,
        niche,
        method,  # type: ignore[arg-type]
        _heuristic_assessment(question, niche, group, template=method == "template"),
        "low",
        flags,
        issues,
        notes=notes,
    )


def _llm_request(groups: list[TopicGroup]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"t{index}",
            "topic": group.title,
            "evidence": [
                {"source": item.source, "kind": item.kind, "title": compact(item.title, 160), "text": compact(item.description, 300)}
                for item in group.sightings[:3]
            ],
        }
        for index, group in enumerate(groups)
    ]


def _from_llm(group: TopicGroup, item: AITopicAssessment) -> Transformed:
    niche = item.niche if item.niche in NICHE_PRIORS else classify_niche(group.title, item.question)[0]
    flags = sorted({flag for flag in item.flags if flag in FLAG_VALUES} | set(_group_flags(group)))
    question = " ".join(item.question.split())
    if not item.usable or not question:
        return Transformed(group.key, question, niche, "llm", flags=flags, issues=["not_usable_for_knowledge_short"], angle=item.angle)
    assessment = {key: round(getattr(item, key) / 10, 3) for key in ASSESSMENT_KEYS}
    return Transformed(
        group.key,
        question,
        niche,
        "llm",
        assessment,
        "medium",
        flags,
        question_issues(question, evidence=_evidence_text(group)),
        compact(item.angle, 120),
    )


def transform_topics(
    groups: list[TopicGroup], settings: Settings, *, allow_llm: bool = True,
) -> tuple[list[Transformed], str, str | None]:
    """(results in input order, method used, error).  One bounded LLM call at most.

    ``allow_llm=False`` when the pool's AI budget must be kept for validation.
    """
    groups = groups[:MAX_BATCH]
    if not groups:
        return [], "none", None
    if not allow_llm or settings.clipforge_ai_mode != "openai" or not settings.openai_api_key:
        return [deterministic_transform(group) for group in groups], "template", None
    try:
        response = TRANSFORM_CLIENT_FACTORY(api_key=settings.openai_api_key).responses.parse(
            model=settings.openai_worker_model,
            instructions=TRANSFORM_INSTRUCTIONS,
            input=json.dumps({"market": {"language": "de", "region": "DE", "broader": "DACH"}, "topics": _llm_request(groups)}, ensure_ascii=False),
            text_format=AITopicBatch,
            max_output_tokens=6000,
            store=False,
        )
        parsed = response.output_parsed
        if not isinstance(parsed, AITopicBatch):
            raise TypeError("no parsed topic batch")
    except (OpenAIError, ValidationError, ValueError, TypeError) as exc:
        # The question step failed: fall back to the honest deterministic path.
        return [deterministic_transform(group) for group in groups], "template", f"{type(exc).__name__}: {str(exc)[:160]}"
    by_id = {item.id: item for item in parsed.items}
    results = []
    for index, group in enumerate(groups):
        item = by_id.get(f"t{index}")
        results.append(_from_llm(group, item) if item is not None else deterministic_transform(group))
    return results, "llm", None
