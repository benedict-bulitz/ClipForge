"""Question-relative answer grounding: does a sentence answer *this* question?

Lexical relevance only says a sentence is about the same topic.  A core answer
must also preserve what the question asks:

* the ENTITIES the question is about (the sky *of Mars*, not the night sky;
  *künstliche Intelligenz* = "KI"), with compound words counted only when
  their modifier is part of the question ("Marshimmel" yes, "Nachthimmel" no);
* the RELATION the question type requests: a cause or mechanism for why/how,
  the asked predicate for can/does questions ("empfinden", not "analysiert ...
  Schmerz bei Patienten"), a time for when-questions;
* no substitute shapes: advice for an explanation, a naming/definition for a
  cause, an observer relation (X detects Y in someone else) for X doing Y,
  a restatement of the phenomenon.

Deterministic and topic-free: German nouns are capitalised, so the
question's entities come from grammar; other languages fall back to content
words with a coverage quota.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..question_intent import answer_mode
from ..story_arc import is_explanatory_question
from .evidence import KIND_PATTERNS, words

_TOKEN = re.compile(r"[A-Za-zÄÖÜäöüß0-9][\wÄÖÜäöüß-]*")
_LEAD = {
    "warum", "wieso", "weshalb", "weswegen", "wodurch", "wie", "was", "wann", "wo", "wer", "welche", "welcher", "welches",
    "kann", "können", "könnte", "ist", "sind", "gibt", "hat", "haben", "wird", "werden", "why", "how", "what", "when",
    "where", "who", "which", "can", "could", "is", "are", "does", "do", "did", "will",
}
_QTYPE = (
    ("can", re.compile(r"(?i)^\s*(?:kann|können|könnte|könnten|darf|can|could|is it possible|ist es möglich)\b")),
    ("when", re.compile(r"(?i)^\s*(?:wann|seit wann|when|since when)\b")),
    ("where", re.compile(r"(?i)^\s*(?:wo|woher|wohin|where)\b")),
    ("what", re.compile(r"(?i)^\s*(?:was ist|was sind|was bedeutet|what is|what are|what does .* mean)\b")),
    ("which", re.compile(r"(?i)^\s*(?:welche\w*|which|wer|who)\b")),
)
# Function words that never carry a question's relation (adverbs, particles).
_FILLER = {
    "überhaupt", "eigentlich", "immer", "erst", "oft", "völlig", "nicht", "manchmal", "genau", "wirklich", "denn", "ohne",
    "dass", "uns", "man", "eine", "einem", "einen", "einer", "ein", "der", "die", "das", "dem", "den", "des", "auf", "in",
    "nach", "vor", "mit", "bei", "von", "und", "oder", "zu", "so", "noch", "schon", "dabei", "even", "really", "actually",
    "always", "only", "just", "the", "a", "an", "of", "on", "in", "to", "and", "or", "it", "its", "at", "after", "before",
    "wurde", "wurden", "wird", "werden", "war", "waren", "ist", "sind", "hat", "haben", "hatte", "kann", "können", "lässt",
}
_ADJ_SUFFIX = ("lich", "isch", "ig", "en", "er", "es", "em", "e", "n", "s", "ish")
_ADVICE = re.compile(
    r"(?i)\b(?:hilft|helfen|hilfreich|tipps?|solltest|sollten sie|sollte man|vermeide\w*|versuch(?:e|t)? |"
    r"helps?|helpful|tips?|you should|try to|avoid\w*)\b"
)
_NAMING = re.compile(
    r"(?i)\b(?:(?:wird|werden|wurde|wurden)\b[^.]{0,60}\b(?:bezeichnet|genannt)|nennt man|bezeichnet man|heißt|"
    r"spitzname|beiname|is (?:often |also )?(?:called|known as|nicknamed)|are (?:often |also )?(?:called|known as)|nickname)\b"
)
_OBSERVER = re.compile(
    r"(?i)\b(?:erkenn\w*|analysier\w*|analyse\w*|mess\w*|misst|detektier\w*|überwach\w*|auswert\w*|interpretier\w*|"
    r"registrier\w*|identifizier\w*|aufspür\w*|find\w* heraus|detect\w*|analy[sz]\w*|measur\w*|monitor\w*|recogni[sz]\w*|"
    r"identif\w*|track\w*|find\w* out|search\w*|such\w*)\b"
)
_CAUSAL = KIND_PATTERNS["mechanism"]
_DATE = KIND_PATTERNS["date"]
_CONNECTIVES = {
    "weil", "denn", "deshalb", "daher", "darum", "deswegen", "dadurch", "sodass", "damit", "wodurch", "indem", "because",
    "since", "therefore", "thus", "hence", "dort", "there",
}


def _fold(word: str) -> str:
    return word.casefold().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def _verb_stem(word: str) -> str:
    word = _fold(word)
    if word.startswith("ge") and len(word) > 5:
        word = word[2:]
    for suffix in ("ungen", "ung", "est", "et", "en", "st", "er", "t", "e", "n", "s", "ing", "ed"):
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def inflects(term: str, word: str) -> bool:
    """Same word in another form or as the head of a longer word ("Mikrowelle" ~ "Mikrowellen", "rot" ~ "rötlich")."""
    a, b = _fold(term), _fold(word)
    if a == b or _verb_stem(a) == _verb_stem(b):
        return True
    if len(a) >= 4 and b.startswith(a):
        return True
    return any(b == a + suffix for suffix in _ADJ_SUFFIX)


@dataclass(frozen=True)
class Entity:
    name: str
    head: str
    forms: frozenset[str]  # extra aliases: acronym, other words of a multiword name

    def matched(self, tokens: list[str], others: set[str], acronyms: set[str]) -> bool:
        if _fold(self.head) in acronyms or self.forms & acronyms:
            return True
        for token in tokens:
            if inflects(self.head, token) or any(inflects(form, token) for form in self.forms if len(form) >= 4):
                return True
            # Compound with the entity as its head counts only when the
            # modifier is itself part of the question ("Marshimmel", not "Nachthimmel").
            folded, head = _fold(token), _fold(self.head)
            if len(head) >= 4 and folded.endswith(head) and len(folded) > len(head):
                modifier = folded[: -len(head)].rstrip("s-")
                if any(inflects(other, modifier) for other in others if _fold(other) != head):
                    return True
        return False


@dataclass(frozen=True)
class QuestionFrame:
    question: str
    qtype: str  # why | how | can | when | where | what | which | other
    language: str
    entities: tuple[Entity, ...]
    predicate: frozenset[str]
    terms: frozenset[str]
    explanation_asked: bool
    asks_observation: bool
    asks_naming: bool
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def required_entities(self) -> int:
        count = len(self.entities)
        if self.language != "de":
            return math.ceil(0.6 * count)
        return count if count <= 2 else count - 1

    def as_dict(self) -> dict[str, object]:
        return {
            "question_type": self.qtype,
            "entities": [entity.name for entity in self.entities],
            "required_entities": self.required_entities,
            "predicate": sorted(self.predicate),
        }


def _acronyms(tokens: list[str]) -> set[str]:
    folded = [_fold(token) for token in tokens]
    found = {token for token in folded if 2 <= len(token) <= 4}
    for size in (2, 3):
        for index in range(len(folded) - size + 1):
            found.add("".join(word[0] for word in folded[index:index + size]))
    return found


def question_frame(question: str, language: str = "de") -> QuestionFrame:
    text = str(question or "").strip()
    lang = "de" if str(language).startswith("de") else "en"
    explanatory = is_explanatory_question(text)
    qtype = "other"
    if explanatory:
        qtype = "how" if re.match(r"(?i)^\s*(?:wie|how)\b", text) else "why"
    else:
        qtype = next((name for name, pattern in _QTYPE if pattern.search(text)), "other")
    tokens = _TOKEN.findall(text)
    if tokens and tokens[0].casefold() in _LEAD:
        tokens = tokens[1:]
    entities: list[Entity] = []
    predicate: set[str] = set()
    if lang == "de":
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lower = token.casefold()
            if token[:1].isupper() and lower not in _LEAD and lower not in _FILLER:
                phrase = [token]
                # Only a proper adjective ("Berliner", "Wiener") fuses with the next noun into one name;
                # "KI Bilder" stays subject + object.
                while (index + 1 < len(tokens) and tokens[index + 1][:1].isupper() and phrase[-1].endswith("er")
                       and tokens[index + 1].casefold() not in _FILLER):
                    index += 1
                    phrase.append(tokens[index])
                modifier = tokens[index - len(phrase)] if index - len(phrase) >= 0 else ""
                forms = {_fold(word) for word in phrase[:-1]}
                if modifier and modifier[:1].islower() and modifier.casefold() not in _FILLER and len(modifier) > 3 \
                        and modifier.casefold() not in _LEAD and re.search(r"(?:e|en|er|es)$", modifier):
                    # "künstliche Intelligenz": the adjective is part of the name (acronym "KI").
                    forms.add(_fold(modifier[0] + phrase[-1][0]))
                    forms.add(_fold(modifier))
                    predicate.discard(modifier.casefold())
                if len(phrase) > 1:
                    forms.add("".join(_fold(word)[0] for word in phrase))
                entities.append(Entity(" ".join(phrase), phrase[-1], frozenset(forms)))
            elif lower not in _FILLER and lower not in _LEAD and len(lower) >= 3:
                predicate.add(lower)
            index += 1
        # Adjectives absorbed into a name are not the asked relation.
        absorbed = {form for entity in entities for form in entity.forms}
        predicate = {word for word in predicate if _fold(word) not in absorbed}
    else:
        content = [token for token in tokens if token.casefold() not in _FILLER and token.casefold() not in _LEAD and len(token) >= 3]
        entities = [Entity(token, token, frozenset()) for token in content]
        predicate = set()
    terms = frozenset(words(text))
    return QuestionFrame(
        question=text,
        qtype=qtype,
        language=lang,
        entities=tuple(entities),
        predicate=frozenset(predicate),
        terms=terms,
        explanation_asked=explanatory or answer_mode(text) == "explanation" and qtype in {"why", "how"},
        asks_observation=bool(_OBSERVER.search(text)),
        asks_naming=bool(_NAMING.search(text)) or bool(re.search(r"(?i)\b(?:heißt|genannt|called|named)\b", text)),
    )


def entity_coverage(frame: QuestionFrame, text: str) -> tuple[int, list[str]]:
    tokens = _TOKEN.findall(str(text or ""))
    others = {entity.head for entity in frame.entities} | set(frame.predicate)
    acronyms = _acronyms(tokens)
    covered = [entity.name for entity in frame.entities if entity.matched(tokens, others, acronyms)]
    return len(covered), covered


def _new_content(frame: QuestionFrame, text: str) -> int:
    """Content words the sentence adds beyond the question's own words (and connectives)."""
    asked = list(frame.terms) + [entity.head for entity in frame.entities]
    return sum(
        1 for word in words(text)
        if word not in _CONNECTIVES and not any(inflects(term, word) or inflects(word, term) for term in asked)
    )


def _shared_shape_issues(frame: QuestionFrame, text: str) -> list[str]:
    issues: list[str] = []
    if frame.explanation_asked and _ADVICE.search(text):
        issues.append("advice_not_explanation")
    if _OBSERVER.search(text) and not frame.asks_observation:
        issues.append("observer_relation")  # X detects / analyses Y - not X doing Y
    return issues


def core_issues(frame: QuestionFrame, text: str) -> list[str]:
    """Why ``text`` cannot be the core answer to ``frame`` (empty: it can)."""
    issues = _shared_shape_issues(frame, text)
    covered, _names = entity_coverage(frame, text)
    if frame.entities and covered < frame.required_entities:
        issues.append("entity_mismatch")
    if frame.qtype in {"why", "how"}:
        if not _CAUSAL.search(text):
            issues.append("no_cause_or_mechanism")
        if _NAMING.search(text) and not frame.asks_naming:
            issues.append("naming_not_cause")
        if _new_content(frame, text) < 3:
            issues.append("restates_phenomenon")
    elif frame.qtype == "can" or (frame.qtype == "other" and frame.predicate):
        if frame.predicate and not any(
            inflects(term, token) or inflects(token, term) for term in frame.predicate for token in _TOKEN.findall(text)
        ):
            issues.append("predicate_mismatch")
    elif frame.qtype == "when" and not _DATE.search(text):
        issues.append("no_time")
    return issues


def topical_issues(frame: QuestionFrame, text: str, context: str = "") -> list[str]:
    """Why ``text`` (read with its paragraph) cannot support this question at all."""
    issues = _shared_shape_issues(frame, text)
    covered, _names = entity_coverage(frame, f"{text} {context}")
    if frame.entities and covered < frame.required_entities:
        issues.append("entity_mismatch")
    return issues


def mechanism_issues(frame: QuestionFrame, text: str, context: str = "") -> list[str]:
    """A mechanism step must be causal, on the asked entities (with its paragraph), and not advice/naming."""
    issues = topical_issues(frame, text, context)
    if not _CAUSAL.search(text):
        issues.append("no_cause_or_mechanism")
    if _NAMING.search(text) and not frame.asks_naming:
        issues.append("naming_not_cause")
    return issues
