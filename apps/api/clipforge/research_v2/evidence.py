"""Evidence units: compact, attributable sentences that answer a sub-question.

A unit is one complete sentence (plus the preceding sentence when it starts
with a pronoun that needs it) taken verbatim from a retrieved source, with
its kind (mechanism, number, date, comparison, caveat, misconception,
definition, observation), its relevance to the research sub-questions and a
short excerpt for traceability.  Relevance is a gate: a sentence that does
not talk about the question's subject is never evidence, however
authoritative its page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..question_intent import answer_mode, gives_advice
from .models import SubQuestion

MAX_UNITS_PER_SOURCE = 8
MAX_UNITS = 48
MIN_WORDS, MAX_WORDS = 7, 55

_WORD = re.compile(r"[a-zäöüß0-9]+")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "who", "why", "how", "are", "was", "were",
    "has", "have", "had", "than", "into", "its", "their", "there", "about", "your", "you", "they", "them", "does",
    "did", "not", "but", "can", "will", "would", "when", "also", "been", "being", "more", "most", "such", "some",
    "der", "die", "das", "den", "dem", "des", "und", "für", "mit", "von", "wer", "warum", "wieso", "weshalb",
    "wie", "ist", "sind", "hat", "haben", "hatte", "als", "auf", "aus", "bei", "ein", "eine", "einer", "eines",
    "einem", "einen", "sich", "auch", "noch", "nur", "oder", "dass", "nicht", "aber", "doch", "wird", "werden",
    "wurde", "wurden", "kann", "können", "eigentlich", "wirklich", "wenn", "dann", "denn", "man", "zum", "zur",
    "durch", "über", "unter", "nach", "vor", "bis", "um", "so", "sehr", "viel", "viele", "mehr", "immer", "dies",
    "diese", "dieser", "dieses", "these", "those", "then", "very", "much", "many", "other", "andere", "anderen",
    "bleibt", "bleiben", "gibt", "geben", "macht", "machen", "überhaupt", "welche", "welcher", "welches", "welchen",
}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9\"„“(])")
_ANAPHOR = re.compile(
    r"^(?:(?:die|der|das)\s+(?:dabei|dadurch)|dies\w*|das|diese[rsmn]?|er|sie|es|dabei|dadurch|deshalb|daher|damit|dafür|darum|dazu|so|hierbei|this|that|these|those|it|they|he|she|"
    r"thus|hence|as a result|im gegensatz dazu|demgegenüber|dagegen|in contrast|by contrast|(?:trotz|wegen|aufgrund|neben|bei|mit|nach|despite|because of|with) (?:dies\w*|dessen|deren|this|that|these))\b",
    re.IGNORECASE,
)
# A pronoun that can stand for an earlier-named thing (mirrors answer_relation._PRONOUN).
_PRONOUN = re.compile(r"(?i)\b(?:sie|er|es|ihn|ihm|ihre[nmrs]?|seine[nmrs]?|diese[rsmn]?|it|its|they|them|their)\b")
# A place/thing named only in the previous sentence ("erscheint der Himmel dort ...").
_BACK_REFERENCE = re.compile(r"(?i)\b(?:dort|dorthin|ebendort|there)\b")
PROCESS_PATTERN = re.compile(
    r"\b(?:react\w*|forms?|form(?:ed|ing)|produc\w*|creat\w*|oxidi[sz]\w*|comes? from|results? from|is due to|are due to|"
    r"reagier\w*|bildet|bilden|gebildet|erzeug\w*|oxidier\w*|(?:kommt|kommen|stammt|stammen) von|"
    r"(?:ist|sind) (?:auf [^.;]{1,80} zurückzuführen|zurückzuführen auf)|liegt an|liegen an|"
    r"(?:farbe|färbung|eigenschaft|zustand)\b[^.;]{0,50}\bhat\b[^.;]{1,80}\bvon|"
    r"absorb\w*|scatter\w*|reflect\w*|convert\w*|transfer\w*|absorbier\w*|streu\w*|reflektier\w*|"
    r"umgewandelt|wandelt|überträgt|übertragen|erwärm\w*|erhitz\w*|entsteh\w*|"
    r"bind\w*|lagern?\b[^.;]{0,60}\bein|abbrechen|zer(?:setzt|setzen|fallen)|"
    r"überlager\w*|auslösch\w*|verstärk\w*|veränder\w*|beeinfluss\w*|lösen\b[^.;]{0,60}\baus|"
    r"bewegt|bewegen|erzeug\w*|bestimm\w*|detektier\w*|erkenn\w*|"
    r"decompos\w*|degrad\w*|trap\w*|bind\w*|cancel\w*|interfer\w*|"
    r"chang\w*|generat\w*|trigger\w*|determin\w*|detect\w*|measur\w*)\b",
    re.IGNORECASE,
)
KIND_PATTERNS: dict[str, re.Pattern[str]] = {
    "misconception": re.compile(
        r"(?i)\b(?:myth\w*|misconception\w*|misunderstand\w*|common belief|widely believed|contrary to|"
        r"not because|irrtum\w*|irrglaube\w*|mythos|mythen|missverständnis\w*|fälschlich\w*|entgegen\b|"
        r"nicht etwa|nicht,? weil|glauben viele|viele (?:glauben|denken|meinen)|many (?:people )?(?:believe|think)|"
        r"wird oft behauptet|weit verbreitet\w*)\b"
    ),
    "mechanism": re.compile(
        r"(?i)\b(?:because|since|due to|caused? by|causes?|leads? to|results? in|so that|therefore|thus|hence|"
        r"which means|as a result|by (?:\w+ing)|in order to|absorb\w*|scatter\w*|reflect\w*|convert\w*|transfer\w*|"
        r"weil|denn|dadurch|deshalb|daher|darum|deswegen|sodass|so dass|führt zu|führen zu|verursach\w*|bewirk\w*|"
        r"entsteh\w*|indem|wodurch|aufgrund|wegen|absorbier\w*|streu\w*|reflektier\w*|umgewandelt|wandelt|"
        r"überträgt|übertragen|erwärm\w*|erhitz\w*|damit|um\b[^.,;]{1,80}\bzu\s+\w+|ziel war|grund dafür|"
        r"aus angst|reason\w*|motiv\w*|fear of|to (?:stop|prevent|keep)|"
        r"da\s+(?:sie|er|es|die|der|das)\b|(?:ursache|grund)\b[^.;]{0,100}\b(?:ist|sind)|"
        r"liegt daran|durch\b[^.;]{1,80}\b(?:druck|unterschied|änderung)\w*)\b|" + PROCESS_PATTERN.pattern
    ),
    "number": re.compile(r"(?i)\b\d[\d.,]*\s*(?:%|prozent|percent|°c|grad|kelvin|k\b|km|m\b|cm|mm|nm|kg|g\b|"
                         r"ghz|mhz|hz|watt|w\b|kw|millionen|milliarden|million|billion|tausend|thousand|mal\b|times\b)|"
                         r"\b\d{1,3}(?:[.,]\d{3})+\b|\b\d+[.,]\d+\b"),
    # A year in date position ("im Jahr 1961", "seit 2004") or a calendar date - never a bare quantity ("1000 Inseln").
    "date": re.compile(r"\b(?:im jahre?|in|seit|bis|ab|um|anno|von|year|since|until|by|from)\s+(?:1[0-9]{3}|20[0-9]{2})\b|"
                       r"\b(?:1[0-9]{3}|20[0-9]{2})\s+(?:wurde|wurden|begann|endete|kam|was|were|began|ended)\b|"
                       r"\b\d{1,2}\.\s*(?:januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember)\b|"
                       r"\b\d{4}-\d{2}-\d{2}\b", re.IGNORECASE),
    "comparison": re.compile(r"(?i)\b(?:than|compared|unlike|whereas|versus|im vergleich|anders als|während|als die|als der|als das)\b"),
    "caveat": re.compile(
        r"(?i)\b(?:however|although|only if|depends|not always|in some cases|unclear|disputed|debated|may|might|could|"
        r"possible|possibly|hypothes(?:is|es|ised|ized)|theor(?:y|ies|ised|ized)|suggests?|proposed|"
        r"jedoch|allerdings|obwohl|nur wenn|hängt .{0,30}ab|nicht immer|teilweise|unklar|umstritten|möglicherweise|"
        r"vermutlich|könnte\w*|hypothese\w*|theorie\w*|theoretisier\w*|deutet\w*|vorgeschlagen)\b"
    ),
    "definition": re.compile(r"(?i)\b(?:is an?|refers to|is the|bezeichnet|ist ein\w*|nennt man|versteht man)\b"),
}
UNCERTAINTY_PATTERN = KIND_PATTERNS["caveat"]
_KIND_ORDER = ("misconception", "mechanism", "number", "date", "comparison", "caveat", "definition")
_ADVICE = re.compile(r"(?i)^(?:wer\s|if you want|to (?:avoid|prevent)\b|um\b[^.]{0,60}\bzu (?:vermeiden|verhindern)|tipp:|tip:)")
_CURRENT = re.compile(
    r"(?i)\b(?:currently|at present|as of|latest|today|this year|recent(?:ly)?|now\b|record|derzeit|aktuell\w*|"
    r"zurzeit|momentan|heute|neueste\w*|jüngste\w*|rekord\w*|bisher)\b"
)
_CHROME = re.compile(
    r"(?i)(?:cookies?|newsletter|abonnier|subscribe|klicken sie|click here|weiterlesen|read more|foto:|bild:|"
    r"getty|dpa-infocom|©|alle rechte|all rights reserved|kommentar\w*|anzeige|werbung|share|teilen|"
    r"in diesem (?:artikel|beitrag|video|ratgeber)|in this (?:article|post|video|guide)|erfährst du|you(?:'ll| will) learn|"
    r"jetzt kaufen|buy now|affiliate|testsieger)"
)


def words(text: object) -> set[str]:
    return {word for word in _WORD.findall(str(text or "").casefold()) if len(word) >= 3 and word not in _STOP}


def _stem(word: str) -> str:
    for suffix in ("ungen", "ung", "ern", "en", "er", "es", "e", "s", "n", "ing", "ed", "ly"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def related(first: str, second: str) -> bool:
    """Inflection/compound tolerant word match ("Mikrowelle" ~ "Mikrowellenherd")."""
    if first == second:
        return True
    a, b = _stem(first), _stem(second)
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 5 and (long.startswith(short) or (len(short) >= 6 and long.endswith(short)))


def matched_terms(terms: set[str] | frozenset[str], text_words: set[str]) -> set[str]:
    return {term for term in terms if any(related(term, word) for word in text_words)}


def numbers_in(text: object) -> set[str]:
    """Normalised numbers (``1.000`` / ``1,000`` -> ``1000``; ``2,45`` -> ``2.45``)."""
    found: set[str] = set()
    for raw in re.findall(r"\d[\d.,]*\d|\d", str(text or "")):
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            found.add(re.sub(r"[.,]", "", raw))
        else:
            found.add(raw.replace(",", "."))
    return found


_NO_BREAK = re.compile(
    r"(?i)(?:\b\d{1,2}\.|\b(?:z|d|u|v|o|s|bzw|ca|etc|evtl|ggf|inkl|nr|st|dr|prof|vgl|sog|jh|jhd|mio|mrd|bzgl|"
    r"e\.g|i\.e|vs|mr|mrs|ms|no|approx|fig|al)\.)$"
)


def sentences(paragraph: str) -> list[str]:
    """Sentences; never split after an ordinal ("am 13. August") or a common abbreviation."""
    merged: list[str] = []
    for part in _SENTENCE_SPLIT.split(paragraph.strip()):
        part = part.strip()
        if not part:
            continue
        if merged and _NO_BREAK.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def kinds_of(text: str) -> list[str]:
    return [kind for kind in _KIND_ORDER if KIND_PATTERNS[kind].search(text)] or ["observation"]


def is_time_sensitive(text: str) -> bool:
    return bool(_CURRENT.search(text))


@dataclass
class EvidenceUnit:
    id: str
    text: str
    excerpt: str
    source_id: str
    sub_question: str
    kinds: list[str]
    relevance: float
    matched: list[str]
    basis: str = "full_text"  # full_text | snippet
    # Exact origin of the statement. ``basis`` remains the compact strength
    # class used by ranking; provenance survives into the package.
    provenance: str | None = None  # full_text | abstract | search_snippet | metadata
    numbers: list[str] = field(default_factory=list)
    time_sensitive: bool = False
    # The sentence just before this one in its paragraph (empty when the unit
    # already includes it, or opens the paragraph): the only antecedent a
    # pronoun in this sentence may refer to.
    antecedent: str = ""
    paragraph_initial: bool = False
    # The source's opening subject paragraph, when it names the asked entity.
    # Resolves component references across paragraphs; never supplies a cause.
    subject_context: str = ""

    @property
    def kind(self) -> str:
        return self.kinds[0]

    def __post_init__(self) -> None:
        if self.provenance is None:
            self.provenance = "full_text" if self.basis == "full_text" else "search_snippet"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text[:400],
            "excerpt": self.excerpt[:400],
            "subject_context": self.subject_context[:400],
            "source_id": self.source_id,
            "sub_question": self.sub_question,
            "kind": self.kind,
            "kinds": self.kinds,
            "relevance": round(self.relevance, 3),
            "basis": self.basis,
            "provenance": self.provenance,
            "numbers": self.numbers[:6],
            "time_sensitive": self.time_sensitive,
        }


def question_terms(text: str) -> set[str]:
    return words(text)


def _relevance(
    text_words: set[str], core_terms: set[str], sub: SubQuestion, sub_terms: set[str], context_words: set[str]
) -> tuple[float, set[str]]:
    """Share of the core subject terms the sentence (or its paragraph) talks about."""
    if not core_terms:
        return 0.0, set()
    hits = matched_terms(core_terms, text_words)
    context_hits = matched_terms(core_terms, context_words) - hits
    extra = matched_terms(sub_terms - core_terms, text_words) if sub_terms - core_terms else set()
    score = (len(hits) + 0.5 * len(context_hits)) / len(core_terms)
    if extra:
        score += 0.15
    return min(1.0, score), hits | extra


def units_from_paragraphs(
    paragraphs: list[str],
    *,
    source_id: str,
    sub_questions: list[SubQuestion],
    core_terms: set[str],
    start: int,
    basis: str = "full_text",
    provenance: str | None = None,
    min_relevance: float = 0.3,
    title: str = "",
) -> list[EvidenceUnit]:
    """Relevant evidence units of one source (at most MAX_UNITS_PER_SOURCE)."""
    # Local import: answer_relation shares the kind patterns defined in this module.
    from .answer_relation import (
        core_issues,
        entity_coverage,
        mechanism_issues,
        question_frame,
        relation_hits,
        topical_issues,
    )

    frame = None
    if sub_questions:
        question = sub_questions[0].question
        german = re.match(r"(?i)^\s*(?:warum|wie|wieso|weshalb|weswegen|wodurch|was|wann|wo|wer|welche\w*|kann|können|ist|sind)\b", question)
        frame = question_frame(question, "de" if german else "en")
    sub_terms = {sub.id: words(sub.question) | words(sub.query) for sub in sub_questions}
    subject_context = ""
    if frame is not None and frame.entities and paragraphs:
        opening = paragraphs[0][:400]
        if not opening.rstrip().endswith("?") and frame.entities[0].name in entity_coverage(frame, opening)[1]:
            subject_context = opening
    # A "why" question is not answered by advice ("Wer umrührt, ...").
    explanation_only = answer_mode(sub_questions[0].question) == "explanation" if sub_questions else False
    candidates: list[tuple[float, EvidenceUnit]] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        parts = sentences(paragraph)
        paragraph_words = words(paragraph)
        for index, sentence in enumerate(parts):
            text = sentence
            antecedent = parts[index - 1] if index > 0 else ""
            refers_back = _ANAPHOR.match(sentence) or _BACK_REFERENCE.search(sentence)
            if refers_back and index > 0 and len((parts[index - 1] + " " + sentence).split()) <= MAX_WORDS:
                text = f"{parts[index - 1]} {sentence}"  # the claim keeps the context it refers to
                antecedent = ""
            count = len(text.split())
            kinds = kinds_of(text)
            short_process = count >= 4 and PROCESS_PATTERN.search(text)
            if (count < MIN_WORDS and not short_process) or count > MAX_WORDS or "?" in text[-2:] or _CHROME.search(text):
                continue
            if explanation_only and (gives_advice(text) or _ADVICE.search(text)):
                continue
            key = text.casefold()[:160]
            if key in seen:
                continue
            text_words = words(text)
            if _PRONOUN.search(sentence):
                # "Errichtet wurde er 1955, um ...": a pronoun sentence is about what it refers to -
                # the previous sentence, or the page title when it opens the paragraph.
                text_words |= words(antecedent or (title if index == 0 else ""))
            best: tuple[float, SubQuestion, set[str]] | None = None
            for sub in sub_questions:
                score, hits = _relevance(text_words, core_terms, sub, sub_terms[sub.id], paragraph_words)
                kinds = kinds_of(text)
                if "mechanism" in kinds and not hits:
                    hits = matched_terms(core_terms, paragraph_words)
                if sub.kind == "mechanism" and "mechanism" in kinds:
                    score += 0.1
                if sub.kind == "misconception" and "misconception" in kinds:
                    score += 0.1
                if best is None or score > best[0]:
                    best = (score, sub, hits)
            if best is None or best[0] < min_relevance or not best[2]:
                continue
            seen.add(key)
            score, sub, hits = best
            kinds = kinds_of(text)
            excerpt = paragraph if len(paragraph) <= 400 else paragraph[max(0, paragraph.find(sentence) - 120):][:400]
            unit = EvidenceUnit(
                id="",
                text=text,
                excerpt=excerpt,
                source_id=source_id,
                sub_question=sub.id,
                kinds=kinds,
                relevance=min(1.0, score),
                matched=sorted(hits),
                basis=basis,
                provenance=provenance,
                numbers=sorted(numbers_in(text)),
                time_sensitive=is_time_sensitive(text),
                antecedent=antecedent,
                paragraph_initial=index == 0,
                subject_context=subject_context,
            )
            # Prefer explanatory and specific sentences within a source.
            direct = frame is not None and not core_issues(frame, text, antecedent or (title if index == 0 else ""), context=f"{paragraph} {subject_context}")
            process = frame is not None and "mechanism" in kinds and not mechanism_issues(frame, text, paragraph)
            property_support = frame is not None and relation_hits(frame, text) and not topical_issues(frame, text, paragraph)
            weight = (2 if direct else 1 if process or property_support else 0) + score + (0.2 if "mechanism" in kinds else 0) + (0.1 if {"number", "misconception"} & set(kinds) else 0)
            candidates.append((weight, unit))
    candidates.sort(key=lambda item: -item[0])
    # A sentence that also appears inside a unit carrying its follow-up
    # sentence is the same statement twice: keep the one with context.
    texts = [unit.text for _weight, unit in candidates]
    candidates = [item for item in candidates if not any(item[1].text != other and item[1].text in other for other in texts)]
    kept = [unit for _weight, unit in candidates[:MAX_UNITS_PER_SOURCE]]
    for offset, unit in enumerate(kept):
        unit.id = f"ev_{start + offset:02d}"
    return kept
