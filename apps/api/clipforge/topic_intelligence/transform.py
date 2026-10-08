from __future__ import annotations

"""Topic -> compelling, truth-seeking German question (bounded, validated).

Raw trends ("Schlafträgheit", a video title, a headline) are rarely good video
prompts.  Two paths feed the same candidate model:

* ``curated_transform``: the semantic curator's grounded question and
  judgement (one batched AI call per <= 20 topics, see ``semantic``).
* ``deterministic_transform``: without AI, keep/extract real questions and use
  concrete templates only where the encyclopedia says what a subject is.

Every question is validated deterministically either way: natural German, a
real question, no embedded answer, no clickbait, no number the evidence lacks.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..language import detect_text_language
from .candidate import Signal, TopicGroup
from .text import (
    NICHE_PRIORS,
    classify_niche,
    extract_question,
    question_issues,
    question_mechanism,
)

# Topics per evaluation round (one curator request per round when AI is enabled).
MAX_BATCH = 20
# Bumped whenever the question step changes, so pools built by an older one are not reused.
TRANSFORMATION_VERSION = "tq4"

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

@dataclass
class Transformed:
    key: str
    question: str
    niche: str
    method: Literal["curator", "source_question", "converted_headline", "template", "evergreen_seed", "none"]
    assessment: dict[str, float] = field(default_factory=dict)
    assessment_confidence: Literal["low", "medium", "high"] = "low"
    flags: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    angle: str = ""
    # Local extraction notes (extracted_clause, converted_headline, needs_title_context, shouting, ...).
    notes: set[str] = field(default_factory=set)
    # The curator's judgement of the final question (None = not curated).
    semantic: Signal | None = None


VISUAL_BY_NICHE = {
    "weltraum": 0.85, "natur_tiere": 0.85, "wetter_klima": 0.85, "technik": 0.8, "essen_trinken": 0.8,
    "geografie": 0.8, "alltag_phaenomene": 0.75, "koerper_gesundheit": 0.7, "wissenschaft": 0.7,
    "geschichte": 0.7, "psychologie": 0.55, "unknown": 0.55, "wirtschaft_geld": 0.5, "gesellschaft": 0.5,
    "sprache_kultur": 0.45,
}


def _evidence_text(group: TopicGroup) -> str:
    # Editorial seed questions are part of a subject's evidence (their numbers are not invented here).
    return " ".join(f"{item.title} {item.description} {' '.join(item.seed_questions)}" for item in group.sightings)


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
    has_article = any(item.kind in {"article", "evergreen"} for item in group.sightings)
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
    order = {"video": 0, "news": 1, "article": 2, "evergreen": 3}
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


def evergreen_seed(group: TopicGroup, avoid: Callable[[str], bool] | None = None) -> str | None:
    """The strongest editorial seed question not yet used (V2: one topic, several possible questions).

    Strength = the curiosity structure of the question (paradox > what-if > why > how ...);
    ties keep the catalog order.  Seeds with a weak short shape are never chosen.
    """
    from .signals import QUESTION_FORM_VALUES
    from .text import short_shape_flags

    seeds = [seed for seed in group.seed_questions if not (avoid and avoid(seed)) and not short_shape_flags(seed)]
    if not seeds:
        return None
    return max(seeds, key=lambda seed: (QUESTION_FORM_VALUES.get(question_mechanism(seed), 0.5), -seeds.index(seed)))


def deterministic_transform(group: TopicGroup, avoid: Callable[[str], bool] | None = None) -> Transformed:
    niche, _strength = classify_niche(group.title, group.description())
    seed = evergreen_seed(group, avoid) if group.evergreen else None
    if seed is not None:
        question, method, notes = seed, "evergreen_seed", set()
    else:
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


def _curated_issues(question: str, evidence: str) -> list[str]:
    """Hard deterministic checks on a curated question.

    The fixed list of question openers is a prefilter for *extracted* headlines;
    the curator judges natural spoken German itself (``natural_spoken_german``), so
    a verb-first question it wrote ("Verarbeiten Hunde Wörter ...?") is not
    rejected for its opener - only for English or for not being a question.
    """
    issues = question_issues(question, evidence=evidence)
    if "not_natural_german" in issues and question.endswith("?") and detect_text_language(question) != "en":
        issues.remove("not_natural_german")
    return issues


def curated_transform(group: TopicGroup, judgement: dict[str, Any], semantic_signal: Signal) -> Transformed:
    """A candidate from the curator's grounded question; deterministic checks still apply."""
    question = " ".join(str(judgement.get("question") or "").split())
    niche = str(judgement.get("niche") or "")
    niche = niche if niche in NICHE_PRIORS else classify_niche(question or group.title, group.title, group.description())[0]
    flags = _group_flags(group)
    if not judgement.get("usable") or not question:
        return Transformed(group.key, question, niche, "curator", flags=flags, issues=["not_usable_for_knowledge_short"], semantic=semantic_signal)
    def ten(name: str, default: int = 5) -> float:
        return round(max(0, min(10, int(judgement.get(name, default)))) / 10, 3)
    assessment = {
        "curiosity_gap": ten("curiosity_gap"),
        "clear_payoff": ten("clear_factual_payoff"),
        "substance": ten("knowledge_short_fit"),
        "premise_clarity": ten("self_contained_clarity"),
        "information_gain": ten("knowledge_short_fit"),
        "visual_potential": ten("visual_potential"),
        "researchability": 0.8 if any(item.kind in {"article", "evergreen"} for item in group.sightings) else 0.6,
        "dach_relevance": ten("dach_relevance"),
        "broad_appeal": ten("universal_12plus_relevance"),
        "accessibility": ten("prior_knowledge_free"),
    }
    _local, _method, notes = _template_question(group, niche)
    return Transformed(
        group.key,
        question,
        niche,
        "curator",
        assessment,
        "medium",
        flags,
        _curated_issues(question, _evidence_text(group)),
        str(judgement.get("reason") or "")[:120],
        notes=notes if question == _local else set(),
        semantic=semantic_signal,
    )
