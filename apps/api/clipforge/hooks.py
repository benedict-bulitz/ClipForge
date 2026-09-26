"""Canonical verbal hook strategies and deterministic truthfulness checks.

The strategy taxonomy is the documented Hook Strategy framework (DAS
HOOK-MANIFEST, ``config/hook_library.json``) in its canonical ClipForge form.
Document family IDs are aliases of these canonical strategies; nothing else
is a valid verbal hook strategy.  Selection itself lives in ``verbal_hook``
(the one verbal-hook authority); this module only validates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .narration import clean_narration_text

CANONICAL_STRATEGIES = (
    "curiosity_gap",
    "counterintuitive_insight",
    "direct_reframe",
    "evidence_insight",
    "common_mistake",
    "verified_statistic",
    "social_proof_or_trend",
    "high_stakes_consequence",
    "ego_challenge",
)
STRATEGIES = set(CANONICAL_STRATEGIES)
# Document family IDs and older internal names -> canonical strategy.
STRATEGY_ALIASES = {
    "shock_number": "verified_statistic",
    "social_proof": "social_proof_or_trend",
    "fomo": "social_proof_or_trend",
    "hot_take": "counterintuitive_insight",
    "direct_confrontation": "direct_reframe",
    "direct_challenge": "direct_reframe",
}


def canonical_strategy(value: object) -> str | None:
    """The canonical document strategy for a name, or ``None`` for anything invented."""
    name = re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())
    if name in STRATEGIES:
        return name
    return STRATEGY_ALIASES.get(name)


_TREND_WORDS = re.compile(r"(?i)\b(?:currently|lately|increasingly|suddenly|trending|viral|adopt(?:ed|ion)|switching|more and more|aktuell|zunehmend|immer mehr|im trend)\b")
_PREVALENCE = re.compile(r"(?i)\b(?:most people|most creators|almost everyone|everyone|the most common mistake|everyone makes|experts usually|top performers|die meisten (?:menschen|leute|von uns|deutschen|eltern|kinder|nutzer|kunden|leser|zuschauer|machen|denken|glauben|wissen|halten|sagen|tun|kennen)|fast alle|jeder|der häufigste fehler|experten machen meistens|erfolgreiche menschen)\b")
_NUMBER = re.compile(r"(?<!\w)(?:\d+(?:[.,]\d+)?\s*%?|\d+(?:[.,]\d+)?\s*(?:million|billion|milliarde[n]?|millionen?))\b", re.IGNORECASE)
_CLICHE = re.compile(r"(?i)(?:they don't want you to know|you(?:'|’)ve been lied to|nobody talks about this|this will change everything|experts hate this|everything you know is wrong|das wollen sie dir nicht sagen|du wurdest belogen)")
_ATTACK = re.compile(r"(?i)\b(?:lazy|stupid|idiot|loser|du bist faul|dumm|versager)\b")
_META = re.compile(r"(?i)\b(?:in this video|today we(?:'|’)re going to|here(?:'|’)s the answer|in diesem video|heute zeige ich)\b")
_META_FILLER = re.compile(r"(?i)(?:that is the key to the answer|that answers the question|that's the answer|das beantwortet die frage|genau das ist die antwort|hier ist der grund|deshalb ist die antwort|und genau das erklärt es)")
_OPENING_SPECIALIST_TERM = re.compile(r"(?i)^\s*(?:piloerektion)\b")
_STOP = {"the", "and", "why", "what", "how", "are", "is", "was", "were", "for", "from", "with", "that", "this", "your", "you", "der", "die", "das", "und", "warum", "wie", "ist", "sind", "für", "von", "mit", "dass", "dies"}


@dataclass(frozen=True)
class HookCandidate:
    strategy: str
    text: str
    score: float
    reason: str


def _question(intent: dict[str, Any]) -> str:
    question = clean_narration_text(intent.get("question") or "").strip().rstrip(".!?")
    return re.sub(r"(?i)^(?:please\s+)?(?:explain|tell me|show me|erkläre|erklaere|erzähl mir|erzaehl mir)\s+", "", question)


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-zäöüß]{3,}", text.casefold()) if word not in _STOP}


# A research snippet with its source page is sourced evidence (as for the
# Script Writer and the verbal hook authority).
_SOURCED = {"source_attributed", "source_snippet"}


def _evidence_facts(facts: list[dict[str, Any]]) -> list[str]:
    return [clean_narration_text(fact.get("claim")) for fact in facts if clean_narration_text(fact.get("claim"))]


def _supported_numbers(facts: list[dict[str, Any]]) -> set[str]:
    return {m.group(0).lower().replace(",", ".") for f in facts if f.get("sources") and f.get("verification") in _SOURCED for m in _NUMBER.finditer(str(f.get("claim") or ""))}


def _number_supported(text: str, facts: list[dict[str, Any]], intent: dict[str, Any] | None, *, research_scoped: bool = False) -> bool:
    # ``research_scoped``: the facts are this project's own research, so the
    # topic check (against numbers borrowed from unrelated sources) is moot;
    # the number must still match a sourced claim the wording shares words with.
    topic = set() if research_scoped else _words(str((intent or {}).get("topic") or "") + " " + _question(intent or {}))
    for match in _NUMBER.finditer(text):
        number = match.group(0).lower().replace(",", ".")
        sourced = [f for f in facts if f.get("sources") and f.get("verification") in _SOURCED]
        matches = [f for f in sourced if number in str(f.get("claim") or "").lower().replace(",", ".")]
        if not matches:
            # A hedged rounding ("rund 270.000") of a sourced figure is still
            # that figure, said simply; the hedge must make it true.
            value, hedge = parse_number(match.group(0)), hedge_kind(text[: match.start()])
            matches = [f for f in sourced if value is not None and hedge and rounded_number_supported(value, hedge, str(f.get("claim") or ""))]
        if not matches:
            return False
        candidate_words = _words(text)
        if topic and not any(topic & _words(str(f.get("claim") or "")) and _words(str(f.get("claim") or "")) & candidate_words for f in matches):
            return False
        if research_scoped and not any(_words(str(f.get("claim") or "")) & candidate_words for f in matches):
            return False
    return True


_HEDGES = (
    ("above", r"mehr als|more than|über|over"),
    ("below", r"fast|knapp|nearly|almost|just under|knapp unter"),
    ("near", r"rund|etwa|ungefähr|circa|ca\.|about|around|roughly|approximately"),
)


def parse_number(raw: str) -> float | None:
    """A spoken figure as a value ("267.570" / "267,570" / "6,4" / "12 %")."""
    value = re.sub(r"\s*(?:%|prozent|percent)$", "", str(raw or "").strip(), flags=re.IGNORECASE).strip()
    if re.fullmatch(r"\d{1,3}(?:[.,\u202f ]\d{3})+", value):
        return float(re.sub(r"\D", "", value))
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def hedge_kind(before: str) -> str | None:
    """What the hedge right before a number claims: above, below or near."""
    for kind, pattern in _HEDGES:
        if re.search(rf"(?i)\b(?:{pattern})\s*$", before):
            return kind
    return None


def rounded_number_supported(value: float, hedge: str, claim: str) -> bool:
    """``hedge`` + ``value`` is true of a figure in ``claim`` (only a close rounding)."""
    for match in _NUMBER.finditer(claim):
        figure = parse_number(match.group(0))
        if not figure:
            continue
        if hedge == "above" and figure > value >= figure * 0.85:
            return True
        if hedge == "below" and value > figure >= value * 0.9:
            return True
        if hedge == "near" and abs(figure - value) <= figure * 0.05:
            return True
    return False


def _topic_evidence(text: str, intent: dict[str, Any]) -> bool:
    topic = _words(str(intent.get("topic") or "") + " " + _question(intent))
    return bool(topic & _words(text)) or not topic


# Structural sentence completeness (grammar only, any topic).  A spoken hook
# is the first thing heard: it must stand alone.
_BACK_REFERENCE = re.compile(
    r"(?i)^\s*(?:aber|und|doch|denn|jedoch|trotzdem|dennoch|deshalb|deswegen|daher|darum|dadurch|dabei|au(?:ß|ss)erdem|zudem|"
    r"also|but|and|however|therefore|thus|hence|besides|moreover)\b"
)
# A pronoun subject whose referent was in the omitted text ("Es ist der ...").
_OPENING_PRONOUN = re.compile(
    r"(?i)^\s*(?:(?:er|sie|es(?!\s+gibt\b)|it(?:'s|’s)?|they|he|she)\b(?!\s*[?!])|"
    r"(?:dies|diese[rs]?|this|these|those|that)\s+(?:ist|sind|war|waren|is|are|was|were|means|bedeutet|zeigt|shows)\b)"
)
# Adverbs that answer an earlier sentence, unless the sentence carries the
# clause they answer ("Obwohl X, ist Y trotzdem Z" stands alone).
_REFERRING_ADVERB = {
    re.compile(r"(?i)\b(?:trotzdem|dennoch|nevertheless|nonetheless)\b"): re.compile(r"(?i)\b(?:obwohl|obgleich|wenn auch|although|though|even if|despite|trotz)\b"),
    re.compile(r"(?i)\b(?:deshalb|deswegen|daher|darum|dadurch|therefore|hence|thus)\b"): re.compile(r"(?i)\b(?:weil|da|denn|because|since|as)\b"),
}


def standalone_issue(text: str) -> str | None:
    """Why a sentence cannot be the first thing a viewer hears (None when it stands alone)."""
    value = clean_narration_text(str(text or "")).strip().lstrip("\"'„“»«(")
    if not value:
        return "clause_fragment"
    if value[:1].isalpha() and value[:1].islower():
        return "clause_fragment"  # cut out of a longer sentence ("ist es der größte ...")
    if not re.search(r"[.!?…\"'”’)]$", value):
        return "clause_fragment"
    if _BACK_REFERENCE.match(value):
        return "context_dependent_opener"
    if _OPENING_PRONOUN.match(value):
        return "unresolved_reference"
    # Only the first clause can point outside the sentence; a later one
    # ("... – und hat trotzdem ...") refers to the clause before it.
    first_clause = re.split(r"[,;:–—]|\s(?:und|and|but|aber|doch)\s", value, maxsplit=1)[0]
    for adverb, own_clause in _REFERRING_ADVERB.items():
        if adverb.search(first_clause) and not own_clause.search(value):
            return "unresolved_reference"
    return None


def _grounded_insight(claim: str, intent: dict[str, Any]) -> str:
    value = clean_narration_text(claim).strip()
    if len(value.split()) <= 24:
        return value.rstrip(".!?") + "."
    parts = [part.strip() for part in re.split(r"(?<=[,;—])\s+|\s+(?=(?:but|because|although|while|sondern|weil|obwohl|während)\s)", value, flags=re.IGNORECASE)]
    for part in parts:
        if standalone_issue(part.rstrip(",;—") + ".") is None and 5 <= len(part.split()) <= 18 and re.search(r"\b(?:is|are|was|were|scatters|causes|can|does|wird|werden|ist|sind|kann|führt|verursacht|gestreut)\b", part, re.IGNORECASE) and not re.search(r"(?:\b(?:is|are|was|were|will|wird|werden|can|kann|and|but|because|although|sondern|weil|obwohl|dass|und|in|als|than)\s*)$", part, re.IGNORECASE):
            return part.rstrip(".!?,;:") + "."
    return value.rstrip(".!?") + "."


def hook_issues(
    text: str, facts: list[dict[str, Any]], *, body: str = "", intent: dict[str, Any] | None = None, research_scoped: bool = False,
) -> list[str]:
    raw = str(text or "").strip()
    value = clean_narration_text(raw).strip()
    if not value:
        return ["meta_language"] if _META.search(raw) else ["empty"]
    issues: list[str] = []
    if _CLICHE.search(raw) or _CLICHE.search(value): issues.append("generic_clickbait")
    if _ATTACK.search(value): issues.append("personal_attack")
    if _META.search(raw) or _META.search(value): issues.append("meta_language")
    if _META_FILLER.search(value): issues.append("generic_meta_filler")
    if _OPENING_SPECIALIST_TERM.search(value): issues.append("jargon_first")
    if intent and (facts or body) and _is_question_echo(value, intent):
        issues.append("question_echo")
    if _PREVALENCE.search(value) and not _PREVALENCE.search(" ".join(_evidence_facts(facts))): issues.append("unsupported_prevalence")
    if _NUMBER.search(value) and not _number_supported(value, facts, intent, research_scoped=research_scoped): issues.append("unsupported_statistic")
    if _TREND_WORDS.search(value):
        evidence = " ".join(_evidence_facts(facts))
        if not _TREND_WORDS.search(evidence) or (intent and not _topic_evidence(value, intent)): issues.append("unsupported_trend")
    if len(value.split()) > 24: issues.append("too_long")
    if re.search(r"(?i)^\s*(?:hook|answer|detail|support|context)\s*[:—-]", value): issues.append("structural_label")
    if body:
        left, right = _words(value), _words(body)
        if left and right and len(left) / len(right) >= 0.8 and len(left & right) / len(right) >= 0.8: issues.append("repeats_body")
    return issues


def _is_question_echo(text: str, intent: dict[str, Any]) -> bool:
    """Reject empty question restatements while retaining genuinely new questions."""
    question = _question(intent).strip().casefold()
    value = text.strip().rstrip("?!.").casefold()
    if value == question:
        return True
    question_words = set(re.findall(r"[a-zäöüß]{2,}", question))
    text_words = set(re.findall(r"[a-zäöüß]{2,}", value))
    if len(question_words) < 2 or not text_words:
        return False
    overlap = len(question_words & text_words)
    if "?" not in text and overlap != len(question_words):
        return False
    return overlap / len(question_words) >= 0.75 and overlap / len(text_words) >= 0.55


def _plain_evidence_explanation(text: str, facts: list[dict[str, Any]], body: str) -> bool:
    """Identify a near-verbatim causal proposition that belongs in the body.

    This is deliberately structural, not a topic-specific vocabulary list. A
    fresh wording of a surprising relationship can still be an evidence insight;
    simply lifting the explanation from a fact or the body cannot.
    """
    candidate_words = _words(text)
    if len(candidate_words) < 2:
        return False
    references = [str(fact.get("claim") or "") for fact in facts]
    if body:
        references.append(body)
    for reference in references:
        reference_words = _words(reference)
        if reference_words and len(candidate_words & reference_words) / len(candidate_words) >= 0.8:
            return True
    return False


def strategy_matches(strategy: str, text: str) -> bool:
    """The rhetoric a canonical strategy requires is actually present in the text.

    Language grammar only (negation, contrast, address, numbers); a label the
    wording does not carry is never accepted.
    """
    value = text.casefold()
    canonical = canonical_strategy(strategy)
    if canonical == "counterintuitive_insight":
        return bool(re.search(
            r"(?:\w+n[’']t\b)|\b(?:not|no|never|but|although|despite|opposite|rather than|instead of|actually|still|"
            r"nicht|kein\w*|sondern|obwohl|statt|trotzdem|doch|eigentlich|gar nicht)\b", value
        ))
    if canonical == "direct_reframe":
        return bool(re.search(r"\b(?:not|nicht|kein\w*|no|isn't|aren't)\b[^.!?]{0,80}(?:\b(?:but|sondern|rather|instead)\b|\s[—–;:]\s*\w|[—–;:]\s*(?:it|es|sie|they|er)\b)", value)) or bool(
            re.search(r"\b(?:less about|more about|statt|instead of|rather than)\b", value)
        )
    if canonical == "ego_challenge":
        return bool(re.search(
            r"(?:\?|\b(?:can you|do you|could you|would you|guess|kannst du|erkennst du|schaffst du|weißt du|"
            r"errätst du|würdest du|rate mal|tippst du)\b)", value
        ))
    if canonical == "common_mistake":
        return bool(re.search(
            r"\b(?:mistake|error|wrong|myth|fail(?:s|ed|ure)?|misconception|people think|you think|"
            r"fehler|irrtum|falsch\w*|mythos|scheitert|scheitern|denken viele|glauben viele|du denkst|die meisten denken)\b", value
        ))
    if canonical == "high_stakes_consequence":
        return bool(re.search(r"\b(?:risk|cost|lose|prevents?|leads?|consequence|impact|damage|risiko|kostet|verhindert|führt|folge|schaden)\b", value))
    if canonical == "verified_statistic":
        return bool(_NUMBER.search(text))
    if canonical == "social_proof_or_trend":
        return bool(_TREND_WORDS.search(text))
    if canonical == "curiosity_gap":
        return "?" in text or bool(re.search(r"\b(?:why|how|what|which|who|warum|wieso|weshalb|wie|was|welche\w*|wer)\b", value))
    return canonical == "evidence_insight"


# Backwards-compatible private name.
_strategy_matches = strategy_matches
