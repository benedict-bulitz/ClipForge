"""Overlay copy: the smallest useful visual relationship of a fact, and its check.

An informational overlay must teach something on its own.  Copy is built from
the complete fact (Story Arc unit claims first, then the complete script
block(s) of the fact, the scene narration only as a last resort) — never from
the scene fragment the Story Arc happened to cut — as a relation:

* CAUSE → EFFECT / THING → RESULT (split at a causal or relational word),
* STEP 1 → STEP 2 (→ STEP 3) for a stated process,

with the reporting frame ("researchers suspect that …") removed and the
uncertainty kept as a "?" on the result.  ``assess_overlay`` is the one
deterministic meaning check used by the Visual Director (before an overlay is
planned) and by the Final Video Critic (on what was actually drawn).

Only language grammar is listed here (articles, conjunctions, hedges,
reporting verbs, copulas, causal verbs) — no topic vocabulary.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

MAX_ELEMENT_WORDS = 6
TARGET_ELEMENT_WORDS = 5
MAX_ELEMENT_CHARS = 38  # within the overlay renderer's 40-character label (a "?" may follow)
MAX_ELEMENTS = 3

_WORD = re.compile(r"[\wÀ-ÖØ-öø-ÿ'-]+", re.UNICODE)

ARTICLES = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "eines",
    "the", "a", "an",
}
CONJUNCTIONS = {"und", "oder", "aber", "sowie", "and", "or", "but", "nor"}
SUBORDINATORS = {
    "dass", "weil", "ob", "wenn", "falls", "damit", "sodass", "wobei", "während", "obwohl", "denn",
    "that", "because", "whether", "if", "which", "who", "while", "although", "since",
}
PREPOSITIONS = {
    "zu", "zum", "zur", "mit", "bei", "beim", "für", "von", "vom", "im", "in", "an", "am", "auf", "aus", "nach",
    "über", "unter", "durch", "gegen", "ohne", "um", "vor",
    "to", "of", "with", "for", "from", "on", "at", "by", "into", "about", "under", "over", "through", "without",
}
# Words that need something after them to mean anything.
MODIFIERS = {
    "weniger", "mehr", "besser", "schlechter", "größer", "kleiner", "stärker", "schwächer", "sehr", "so", "noch", "auch",
    "nicht", "kein", "keine", "als", "wie",
    "less", "more", "better", "worse", "larger", "smaller", "stronger", "weaker", "very", "not", "no", "than", "as",
}
PRONOUNS = {"sich", "uns", "dir", "ihm", "ihr", "ihnen", "es", "man", "wir", "us", "you", "it", "we", "they", "them"}
HEDGES = {
    "möglicherweise", "vielleicht", "wahrscheinlich", "vermutlich", "eventuell", "offenbar", "anscheinend",
    "possibly", "maybe", "perhaps", "probably", "likely", "apparently",
}
# Reporting frames: who claims it, not what is claimed.
REPORTING = {
    "forschende", "forscher", "forscherin", "forscherinnen", "wissenschaftler", "wissenschaftlerin",
    "wissenschaftlerinnen", "experten", "expertinnen", "studie", "studien", "vermuten", "vermutet", "glauben",
    "glaubt", "annehmen", "nehmen", "geht", "gehen", "davon", "aus", "könnte", "könnten", "sein", "sagen",
    "sagt", "zeigen", "zeigt", "laut", "meinen", "meint",
    "researchers", "researcher", "scientists", "scientist", "experts", "study", "studies", "suspect", "suspects",
    "believe", "believes", "think", "thinks", "assume", "assumes", "say", "says", "show", "shows", "suggest",
    "suggests", "according", "might", "could", "may", "be", "possible",
}
FILLER = REPORTING | HEDGES | {"es", "man", "dabei", "dafür", "dazu", "so", "also", "eigentlich", "genau", "wirklich"}
# Relation words: the overlay's arrow replaces them.
CAUSAL = {
    "hilft", "helfen", "sorgt", "sorgen", "führt", "führen", "verursacht", "verursachen", "bewirkt", "bewirken",
    "ermöglicht", "ermöglichen", "verbessert", "verbessern", "erhöht", "erhöhen", "verringert", "verringern",
    "senkt", "senken", "schützt", "schützen", "dadurch", "sodass", "deshalb", "daher", "darum", "deswegen",
    "helps", "help", "causes", "cause", "leads", "lead", "results", "improves", "improve", "increases",
    "increase", "reduces", "reduce", "lowers", "lower", "allows", "allow", "enables", "enable", "protects",
    "protect", "therefore", "thus", "hence",
}
REVERSED = {"weil", "da", "because", "since"}  # effect <- cause
CHANGE_VERBS = {
    "verbessert", "verbessern", "erhöht", "erhöhen", "verringert", "verringern", "senkt", "senken", "schützt", "schützen",
    "improves", "improve", "increases", "increase", "reduces", "reduce", "lowers", "lower", "protects", "protect",
}
COPULAS = {"ist", "sind", "wird", "werden", "bleibt", "bleiben", "hat", "haben", "is", "are", "becomes", "become", "has", "have", "gets", "get"}
_LEADING_DROP = ARTICLES | CONJUNCTIONS | SUBORDINATORS | PRONOUNS | HEDGES | {"dabei", "dafür", "dazu", "so", "zu", "to", "resulting", "in"}
_TRAILING_DROP = ARTICLES | CONJUNCTIONS | SUBORDINATORS | {"dabei", "dafür", "dazu", "uns", "us"}
_FUNCTION = ARTICLES | CONJUNCTIONS | SUBORDINATORS | PREPOSITIONS | PRONOUNS | HEDGES | REPORTING | {"dabei", "dafür", "dazu", "so"}

OVERLAY_SEMANTIC_CODES = ("incomplete_overlay", "narration_fragment", "duplicate_narration", "low_information_gain", "overlay_too_long")


def _words(text: object) -> list[str]:
    return _WORD.findall(str(text or ""))


def _lower(words: list[str]) -> list[str]:
    return [word.casefold() for word in words]


def _content(words: list[str]) -> list[str]:
    return [word for word in _lower(words) if word not in _FUNCTION and word not in MODIFIERS and word not in COPULAS]


# ---------------------------------------------------------------------------
# The fact behind a scene
# ---------------------------------------------------------------------------

def fact_statement(scene: dict[str, Any], state: dict[str, Any]) -> str:
    """The complete fact behind a scene (never the Story Arc's scene fragment).

    Priority: Story Arc unit claims; all script blocks carrying the scene's
    fact ids (a fact split into several sentences); its own complete block;
    the scene narration only when nothing else exists.
    """
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else {}
    claims = {str(unit.get("id")): str(unit.get("claim") or "") for unit in arc.get("units") or [] if isinstance(unit, dict)}
    unit_ids = [str(value) for value in scene.get("story_unit_ids") or (scene.get("visual_director") or {}).get("fact_ids") or []]
    joined = " ".join(claims[unit_id] for unit_id in unit_ids if claims.get(unit_id)).strip()
    if joined:
        return joined
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    blocks = [block for block in script.get("blocks") or [] if isinstance(block, dict)]
    own = next((block for block in blocks if str(block.get("id") or "") == str(scene.get("block_id") or "")), {})
    fact_ids = {str(value) for value in own.get("fact_ids") or []}
    if fact_ids:
        text = " ".join(str(block.get("text") or "") for block in blocks if fact_ids & {str(value) for value in block.get("fact_ids") or []})
        if text.strip():
            return " ".join(text.split())
    return " ".join(str(own.get("text") or scene.get("narration") or "").split())


# ---------------------------------------------------------------------------
# Deterministic relation extraction
# ---------------------------------------------------------------------------

def _strip_frame(statement: str) -> tuple[str, bool]:
    """Remove "X suspect(s) that …" / leading reporting clauses; report uncertainty."""
    words = _lower(_words(statement))
    hedged = bool(set(words) & (HEDGES | REPORTING))
    text = statement.strip()
    match = re.search(r"(?i)\b(dass|that)\b", text)
    if match:
        before = _lower(_words(text[: match.start()]))
        # "<who> <reports/suspects> [since when] that …": only the claim is shown.
        if before and set(before) & REPORTING and not set(before) & (CAUSAL | COPULAS):
            text = text[match.end():]
    parts = [part for part in re.split(r"[,:]", text, maxsplit=1)]
    if len(parts) == 2 and all(word in REPORTING or word in PRONOUNS or word in ARTICLES for word in _lower(_words(parts[0]))):
        text = parts[1]
    return text.strip(" ,.;:!?"), hedged


def _clean(tokens: list[str]) -> list[str]:
    tokens = [token for token in tokens if token.casefold() not in HEDGES]
    while tokens and tokens[0].casefold() in _LEADING_DROP:
        tokens = tokens[1:]
    while tokens and tokens[-1].casefold() in _TRAILING_DROP:
        tokens = tokens[:-1]
    return tokens


def _compress(tokens: list[str]) -> list[str]:
    """Shorten a clause to a label: inner articles, then prepositional phrases, go first."""
    def long(values: list[str]) -> bool:
        return len(values) > TARGET_ELEMENT_WORDS or len(" ".join(values)) > MAX_ELEMENT_CHARS

    if not long(tokens):
        return tokens
    tokens = [token for index, token in enumerate(tokens) if index == 0 or token.casefold() not in ARTICLES]
    while long(tokens):
        start = next((index for index, token in enumerate(tokens) if index > 0 and token.casefold() in PREPOSITIONS - {"zu", "to"}), None)
        if start is None:
            break
        end = start + 1
        # The phrase runs to its noun (German: the capitalised word) or one word.
        while end < len(tokens) - 1 and not tokens[end][:1].isupper() and tokens[end].casefold() not in COPULAS | CAUSAL:
            end += 1
        tokens = tokens[:start] + tokens[end + 1:]
    # Last resort: attributive adjectives in front of a (capitalised) noun.
    index = 1
    while long(tokens) and index < len(tokens) - 1:
        word = tokens[index]
        if word[:1].islower() and tokens[index + 1][:1].isupper() and word.casefold().endswith(("e", "en", "er", "es", "em")) \
                and word.casefold() not in COPULAS | CAUSAL | _FUNCTION | MODIFIERS:
            tokens = tokens[:index] + tokens[index + 1:]
            continue
        index += 1
    return tokens


def _element(tokens: list[str]) -> str:
    tokens = _clean(tokens)
    # Verb-final subordinate clause ("… beim Greifen hilft"): lead with the verb.
    if len(tokens) >= 3 and tokens[-1].casefold() in CAUSAL:
        tokens = [tokens[-1], *tokens[:-1]]
    elif len(tokens) >= 3 and tokens[-1].casefold() in COPULAS:
        tokens = tokens[:-1]
    # Inverted main clause after a removed connector (verb, "sich", article, noun):
    # a lowercase verb, then article + capitalised noun -> subject first.
    if tokens and tokens[0][:1].islower() and tokens[0].casefold() not in _FUNCTION:
        for index in range(1, min(4, len(tokens) - 1)):
            if tokens[index].casefold() in ARTICLES and tokens[index + 1][:1].isupper():
                tokens = [tokens[index + 1], *tokens[:index], *tokens[index + 2:]]
                break
    text = " ".join(_compress(tokens)).strip(" ,.;:-")
    return text[:1].upper() + text[1:] if text else ""


def _split_relation(core: str) -> list[str] | None:
    sentences = [part for part in re.split(r"(?<=[.!?;])\s+|;\s*", core) if part.strip()]
    tokens: list[str] = []
    for sentence in sentences:
        tokens.extend([*re.findall(r"[\wÀ-ÖØ-öø-ÿ'-]+|[,;]", sentence), ";"])
    parts: list[list[str]] = [[]]
    reverse_at: int | None = None
    for token in tokens:
        low = token.casefold()
        if low in CAUSAL or low in REVERSED or token in {";"}:
            if parts[-1]:
                if low in REVERSED:
                    reverse_at = len(parts)
                parts.append([token] if low in CHANGE_VERBS else [])
            continue
        if token == ",":
            if parts[-1] and _content(parts[-1]) and len(parts[-1]) >= 1 and not _dangling_end(parts[-1]):
                parts.append([])
            continue
        parts[-1].append(token)
    elements = [_element(part) for part in parts]
    elements = [element for element in elements if _content(_words(element))]
    if reverse_at is not None and len(elements) == 2:
        elements.reverse()
    return elements if 2 <= len(elements) <= MAX_ELEMENTS else None


def _split_copula(core: str) -> list[str] | None:
    tokens = re.findall(r"[\wÀ-ÖØ-öø-ÿ'-]+", core)
    for index, token in enumerate(tokens[1:-1], 1):
        if token.casefold() in COPULAS:
            elements = [_element(tokens[:index]), _element(tokens[index + 1:])]
            if all(_content(_words(element)) for element in elements):
                return elements
    return None


def _dangling_end(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[-1].casefold() in (ARTICLES | CONJUNCTIONS | SUBORDINATORS | PREPOSITIONS | MODIFIERS)


def relation_elements(statement: str) -> tuple[list[str], bool] | None:
    """``([element, ...], hedged)`` for a fact, or ``None`` when no clear relation exists."""
    core, hedged = _strip_frame(statement)
    for candidate in (_split_relation(core), _split_copula(core)):
        if candidate and not assess_elements(candidate, source=statement):
            return candidate, hedged
    return None


def overlay_spec_for(statement: str) -> dict[str, Any] | None:
    """A process/relation overlay spec from a complete fact, or ``None``."""
    found = relation_elements(statement)
    if found is None:
        return None
    elements, hedged = found
    spec: dict[str, Any] = {"kind": "process", "steps": elements, "source": "fact_relation"}
    if len(elements) == 2:
        spec["relation"] = True
    if hedged:
        # The graphic shows the hypothesised relation, never as a proven fact.
        spec["steps"] = [*elements[:-1], f"{elements[-1]}?"]
        spec["hedged"] = True
    return spec


# ---------------------------------------------------------------------------
# The meaning check (Visual Director and Final Video Critic)
# ---------------------------------------------------------------------------

def spec_elements(spec: dict[str, Any] | None) -> list[str]:
    spec = spec or {}
    if spec.get("kind") == "comparison":
        return [str(spec.get("left") or ""), str(spec.get("right") or "")]
    if spec.get("kind") == "label":
        return [str(spec.get("text") or "")]
    return [str(value) for value in spec.get("steps") or []]


def _source_clauses(source: str) -> list[list[str]]:
    return [_lower(_words(clause)) for clause in re.split(r"[,.;:!?]|\b(?:dass|that|weil|because)\b", source, flags=re.IGNORECASE) if clause.strip()]


def _cut_mid_clause(words: list[str], source: str) -> bool:
    """The element ends on a word its source clause continues after (it was chopped)."""
    low = _lower(words)
    for clause in _source_clauses(source):
        for start in range(len(clause) - len(low) + 1):
            if clause[start:start + len(low)] == low:
                following = clause[start + len(low):]
                if following and following[0] not in COPULAS and following[0] not in CAUSAL:
                    return True
    return False


_FRAMING = ARTICLES | CONJUNCTIONS | SUBORDINATORS | REPORTING | HEDGES | PRONOUNS | {"dabei", "dafür", "dazu"}


def _is_chopped(elements: list[str], source: str) -> bool:
    """The elements are the source cut into consecutive pieces at arbitrary points.

    A split between complete clauses (punctuation) or at a relation word (a
    causal verb or copula, which the arrow stands for) is a real step or
    relation; a split inside a clause is chopped narration.
    """
    tokens = [token.casefold() for token in re.findall(r"[\wÀ-ÖØ-öø-ÿ'-]+|[,.;:!?]", source)]
    words_at = [index for index, token in enumerate(tokens) if token not in ",.;:!?"]
    words = [tokens[index] for index in words_at]
    spans: list[tuple[int, int]] = []
    position = 0
    for element in elements:
        target = _lower(_words(element.rstrip("?")))
        found = next((start for start in range(position, len(words) - len(target) + 1) if words[start:start + len(target)] == target), None)
        if found is None or not target:
            return False
        spans.append((found, found + len(target)))
        position = found + len(target)
    chopped = False
    for (_start, end), (next_start, _next_end) in pairwise(spans):
        between = words[end:next_start]
        if any(word not in _FRAMING for word in between) and not any(word in CAUSAL or word in COPULAS for word in between):
            return False  # real content was left out between them: a summary, not a cut
        relation = any(word in CAUSAL or word in COPULAS for word in between) or words[next_start] in CAUSAL or words[next_start] in COPULAS
        punctuation = any(token in ",.;:!?" for token in tokens[words_at[end - 1] + 1:words_at[next_start]])
        complete = punctuation and _clause_complete(spans, words_at, tokens, end, next_start)
        if not relation and not complete:
            chopped = True
    return chopped


def _clause_complete(spans: list[tuple[int, int]], words_at: list[int], tokens: list[str], end: int, next_start: int) -> bool:
    """Both sides of a punctuation split end/start at clause edges."""
    def edge_after(word_index: int) -> bool:
        if word_index >= len(words_at):
            return True
        following = tokens[words_at[word_index - 1] + 1:words_at[word_index]] if word_index > 0 else []
        return any(token in ",.;:!?" for token in following)

    next_end = next((span_end for span_start, span_end in spans if span_start == next_start), next_start)
    return edge_after(end) and edge_after(next_end)


def assess_elements(elements: list[str], *, source: str = "", narration: str = "") -> list[dict[str, str]]:
    """Why these overlay elements would not teach anything on their own (empty = fine)."""
    issues: list[dict[str, str]] = []

    def add(code: str, element: str, message: str) -> None:
        if not any(item["code"] == code for item in issues):
            issues.append({"code": code, "element": element, "message": message})

    cleaned = [element.strip().rstrip("?").strip() for element in elements if str(element).strip()]
    if not cleaned:
        return [{"code": "low_information_gain", "element": "", "message": "The overlay has no content."}]
    for element in cleaned:
        words = _words(element)
        low = _lower(words)
        content = _content(words)
        if not content or sum(word in FILLER for word in low) * 2 >= len(low):
            add("low_information_gain", element, f"“{element}” only says who claims something, not what.")
            continue
        if low[0] in SUBORDINATORS or low[0] in CONJUNCTIONS:
            add("incomplete_overlay", element, f"“{element}” starts in the middle of a sentence.")
        if low[-1] in (ARTICLES | CONJUNCTIONS | SUBORDINATORS | PREPOSITIONS):
            add("incomplete_overlay", element, f"“{element}” stops before the information it depends on.")
        elif low[-1] in MODIFIERS and (not source or _cut_mid_clause(words, source)):
            add("incomplete_overlay", element, f"“{element}” ends on “{words[-1]}” without what it refers to.")
        if len(words) > MAX_ELEMENT_WORDS or len(element) > MAX_ELEMENT_CHARS:
            verbatim = " ".join(low) in " ".join(_lower(_words(narration or source)))
            add("duplicate_narration" if verbatim else "overlay_too_long", element, f"“{element}” is a sentence, not a short visual label.")
    if len({element.casefold() for element in cleaned}) < len(cleaned):
        add("low_information_gain", cleaned[0], "The overlay repeats the same element.")
    if source and len(cleaned) >= 2 and _is_chopped(cleaned, source):
        add("narration_fragment", " / ".join(cleaned), "The overlay is the narration cut into boxes, not a relation.")
    # Transcript on screen: the elements are verbatim runs of what is being
    # said and together cover (nearly) all of it.  A summary may share words.
    spoken = _lower(_words(narration))
    spoken_text = " " + " ".join(spoken) + " "
    verbatim = [element for element in cleaned if f" {' '.join(_lower(_words(element)))} " in spoken_text]
    covered = sum(len(_words(element)) for element in verbatim)
    if len(spoken) >= 5 and len(verbatim) == len(cleaned) and covered >= 0.8 * len(spoken):
        add("duplicate_narration", " / ".join(cleaned), "The overlay repeats what the narration says at that moment.")
    return issues


def assess_overlay(spec: dict[str, Any] | None, *, source: str = "", narration: str = "") -> list[dict[str, str]]:
    elements = spec_elements(spec)
    if (spec or {}).get("kind") == "comparison":
        # "A vs B" names two things; only emptiness or filler can fail it.
        return [issue for issue in assess_elements(elements) if issue["code"] in {"low_information_gain", "overlay_too_long"}]
    return assess_elements(elements, source=source, narration=narration)


# ---------------------------------------------------------------------------
# Optional worker-model summary (same check applies; deterministic fallback)
# ---------------------------------------------------------------------------

SUMMARY_INSTRUCTIONS = (
    "You write the on-screen overlay for one fact of a factual short video. Return the smallest useful "
    "visual relationship that helps a viewer understand the fact without hearing the narration: CAUSE -> "
    "EFFECT, THING -> RESULT, STEP 1 -> STEP 2, NUMBER + MEANING, or A vs B. Use 2 or 3 elements of 2-6 "
    "words each, in the fact's language. Never copy the narration, never start with who claims it "
    "(researchers, studies), no unfinished phrases. If the fact is a hypothesis, set hedged=true. If no "
    "useful relation exists, return no elements."
)


class OverlaySummary(BaseModel):
    elements: list[str] = Field(default_factory=list, max_length=MAX_ELEMENTS)
    hedged: bool = False


def _client(settings: Any) -> Any:
    return OpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=0)


# Indirection so the test suite can hard-disable real network clients.
SUMMARY_CLIENT_FACTORY: Callable[[Any], Any] = _client
_CACHE: OrderedDict[str, dict[str, Any] | None] = OrderedDict()


def clear_summary_cache() -> None:
    _CACHE.clear()


def summarize_fact(statement: str, *, settings: Any, story_role: str | None = None) -> dict[str, Any] | None:
    """A model-written overlay spec that passes ``assess_overlay``; ``None`` otherwise (never raises)."""
    statement = " ".join(str(statement or "").split())[:600]
    if not statement or settings is None or not getattr(settings, "openai_api_key", None) or not getattr(settings, "visual_prompt_translation_enabled", False):
        return None
    key = hashlib.sha256(json.dumps([getattr(settings, "openai_worker_model", ""), statement, story_role]).encode()).hexdigest()
    if key in _CACHE:
        return dict(_CACHE[key]) if _CACHE[key] else None
    result: dict[str, Any] | None = None
    try:
        response = SUMMARY_CLIENT_FACTORY(settings).responses.parse(
            model=settings.openai_worker_model,
            instructions=SUMMARY_INSTRUCTIONS,
            input=json.dumps({"fact": statement, "story_role": story_role}, ensure_ascii=False),
            text_format=OverlaySummary,
            max_output_tokens=200,
            store=False,
        )
        parsed = response.output_parsed
    except (OpenAIError, OSError, ValueError, TypeError, AttributeError):
        parsed = None
    if isinstance(parsed, OverlaySummary):
        elements = [" ".join(str(item).split())[:60] for item in parsed.elements if str(item).strip()][:MAX_ELEMENTS]
        if 2 <= len(elements) and not assess_elements(elements, source=statement):
            result = {"kind": "process", "steps": elements, "source": "model_summary"}
            if len(elements) == 2:
                result["relation"] = True
            if parsed.hedged:
                result["steps"] = [*elements[:-1], f"{elements[-1].rstrip('?')}?"]
                result["hedged"] = True
    _CACHE[key] = result
    while len(_CACHE) > 128:
        _CACHE.popitem(last=False)
    return dict(result) if result else None


def explanatory_overlay(scene: dict[str, Any], state: dict[str, Any], settings: Any | None = None, story_role: str | None = None) -> dict[str, Any] | None:
    """The overlay spec for a scene's fact: model summary when configured, else deterministic."""
    statement = fact_statement(scene, state)
    return summarize_fact(statement, settings=settings, story_role=story_role) or overlay_spec_for(statement)
