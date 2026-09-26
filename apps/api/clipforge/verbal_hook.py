"""Verbal hook authority: the documented Hook Strategy framework, applied to research.

The spoken hook is derived only from the documented strategy families (DAS
HOOK-MANIFEST, ``config/hook_library.json``) in their canonical form
(``hooks.CANONICAL_STRATEGIES``).  A model may write topic-specific wording;
it may not invent a strategy system:

    document strategies + rules
        -> which strategies the researched content actually supports
           (``strategy_signals``: facts, numbers, contrasts, misconceptions ...)
        -> topic-specific wording for those strategies
           (the provider, or ``deterministic_candidates`` without one)
        -> the document's quality rubric (``assess_verbal``)
        -> one selected verbal hook (Triple Hook V2 carries it)

A strategy never earns points for its name: it must be supported by research
(``strategy_fit``) and visible in the wording; the candidate's own quality
decides.  Repeating the user's question is an emergency fallback only.

No topic vocabulary lives here: only grammar (stop words, negation,
contrast, hedges, address) and Story Arc structure.
"""
from __future__ import annotations

import re
from typing import Any

from .hooks import (
    CANONICAL_STRATEGIES,
    _grounded_insight,
    _is_question_echo,
    _plain_evidence_explanation,
    canonical_strategy,
    hook_issues,
    parse_number,
    rounded_number_supported,
    standalone_issue,
    strategy_function,
    strategy_matches,
)
from .media import _mentions, _visual_query_tokens, visual_target_key
from .narration import clean_narration_text
from .payoff import _protected_answer, reveals_protected_payoff
from .story_arc import arc_units, comparison_sides, hook_safe_facts

_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "who", "why", "how", "are", "is", "was",
    "were", "has", "have", "than", "into", "its", "their", "there", "about", "your", "you", "they", "them", "does",
    "did", "not", "but", "just", "only", "even", "still", "really", "actually", "can", "will", "would", "when",
    "der", "die", "das", "den", "dem", "des", "und", "für", "mit", "von", "welche", "welcher", "welches", "wer",
    "warum", "wieso", "wie", "ist", "sind", "hat", "haben", "als", "auf", "aus", "bei", "ein", "eine", "einer",
    "eines", "einem", "einen", "im", "in", "zu", "sich", "auch", "noch", "nur", "oder", "dass", "dein", "deine",
    "deinen", "deiner", "nicht", "aber", "doch", "wird", "werden", "kann", "können", "eigentlich", "wirklich",
    "gar", "mal", "wenn", "dann", "denn", "dir", "dich", "uns", "wir", "ihr", "sie", "man", "es",
    # Quantity/comparison grammar: never the identity of an answer on its own.
    "gibt", "also", "etwa", "rund", "fast", "über", "mehr", "weniger", "more", "less", "most", "meisten",
    "jedes", "jeder", "jede", "andere", "anderes", "anderen", "other", "every", "any", "around", "nearly",
    "schon", "sehr", "very", "much", "viel", "viele", "many", "some", "einige", "platz", "rang", "rank",
    "weil", "because", "ihre", "ihren", "sein", "seine", "unsere", "unser", "our", "these", "diese", "dieser",
    "dieses", "gegen", "versus", "zahl", "number", "kein", "keine", "keinen", "keiner", "keinem",
}
# Grammar of empty openers (any topic): they announce a fact instead of creating a gap.
_GENERIC_OPENER = re.compile(
    r"(?i)^\s*(?:wusstest du(?:,| schon)?|hast du dich (?:schon )?(?:jemals|mal|je) gefragt|"
    r"hier (?:ist|kommt) ein (?:interessanter|spannender|verrückter) fakt|"
    r"did you know|have you ever wondered|here(?:'|’)s an? (?:interesting|fun|crazy) fact|fun fact)\b"
)
_CHEAP_BAIT = re.compile(
    r"(?i)(?:das wirst du nicht glauben|du wirst (?:es )?nicht glauben|you won(?:'|’)t believe|"
    r"this will blow your mind|mind[- ]blowing|unglaublich,? aber wahr|schockierend)"
)
# Manufactured conflict without substance (the document forbids fake controversy).
_FAKE_CONTROVERSY = re.compile(
    r"(?i)(?:\b(?:controversial|nobody wants to hear|everyone is wrong|you are wrong|the truth about|"
    r"umstritten|keiner will das hören|alle liegen falsch|du liegst falsch|die wahrheit über)\b)"
)
_ABSOLUTE = re.compile(r"(?i)\b(?:always|never|nobody|everyone|everybody|immer|nie|niemals|niemand|jeder|alle)\b")
_CONTRAST = re.compile(
    r"(?i)(?:\b(?:not|no|never|but|although|despite|instead|rather|yet|actually|still|nicht|kein\w*|aber|doch|sondern|"
    r"obwohl|statt|trotzdem|eigentlich|gar nicht)\b|\w+n[’']t\b)"
)
_SELF_TEST = re.compile(r"(?i)(?:\?|\b(?:du|dein\w*|dich|dir|you|your|guess|rate mal)\b)")
_NEGATED_RESULT = re.compile(
    r"(?i)\b(?:nicht|kein\w*|not|no|never|nie|niemals)\b[^.!?]{0,40}\b(?:mehr|meisten|gewinn\w*|vorn\w*|sieger\w*|"
    r"win\w*|most|more|first|erste\w*|platz|top)\b|\b(?:verliert|verlieren|loses?|losing|second|zweite\w*)\b"
)
_OPTION_MARKERS = re.compile(r"(?i)(?:\?|\b(?:oder|or|vs\.?|versus|gegen)\b)")
# A stated comparative result ("A has more ... than B"), not a quantity ("more than 17,000").
_COMPARATIVE_RESULT = re.compile(
    r"(?i)\b(?:mehr|weniger|more|fewer|less|größer|kleiner|bigger|larger|smaller|höher|higher|länger|longer|"
    r"gewinnt|wins?|won|vorn\w*|ahead|die meisten|the most)\b(?![^.!?]{0,6}\d)"
)
# Research signals per documented strategy (grammar only).
_FACT_CONTRAST = re.compile(
    r"(?i)\b(?:but|although|despite|instead|rather than|yet|even though|nevertheless|nonetheless|however|actually|still|"
    r"aber|obwohl|trotzdem|dennoch|jedoch|statt|anstatt|sondern|entgegen|eigentlich|nicht|kein\w*|not|no)\b"
)
_FACT_REFRAME = re.compile(
    r"(?i)\b(?:not|nicht|kein\w*|no)\b[^.!?]{0,80}\b(?:but|sondern|rather|instead)\b|"
    r"\b(?:is not|isn't|are not|aren't|ist kein\w*|ist nicht|sind kein\w*|sind nicht)\b"
)
_FACT_MISTAKE = re.compile(
    r"(?i)\b(?:mistake|misconception|myth|wrong(?:ly)?|error|false|misunderstand\w*|commonly (?:believed|thought|assumed)|"
    r"often (?:assumed|thought|believed)|people think|irrtum|irrig\w*|fehler|falsch\w*|mythos|missverständnis|"
    r"oft (?:geglaubt|gedacht|angenommen)|viele (?:glauben|denken|meinen)|nicht beschädigt|kein schaden|kein defekt)\b"
)
_FACT_CAUSE = re.compile(
    r"(?i)\b(?:because|since|due to|caused by|causes?|so that|therefore|helps?|serves?|ensures?|allows?|"
    r"weil|da|denn|dadurch|deshalb|daher|durch|sodass|verursacht|entsteh\w*|hilft|helfen|dient|dienen|sorgt|"
    r"sorgen|ermöglicht)\b"
)
_FACT_RESTRICTION = re.compile(r"(?i)\b(?:nur|lediglich|bloß|only|merely|just)\b")
# The correcting side of a spoken reframe ("not X, but Y" -> Y).
_REFRAME_SIDE = re.compile(r"(?i)(?:\b(?:but|sondern|rather|instead|stattdessen)\b|[—–;:])(.+)$")
_FACT_CONSEQUENCE = re.compile(
    r"(?i)\b(?:risk|danger\w*|cost\w*|lose|loss|damage\w*|prevent\w*|leads? to|harm\w*|"
    r"risiko|gefahr\w*|kostet|kosten|verlust\w*|schäd\w*|verhindert|führt zu|verursacht)\b"
)
_FACT_TREND = re.compile(
    r"(?i)\b(?:currently|lately|increasingly|trending|adopt(?:ed|ion)|more and more|aktuell|zunehmend|immer mehr|im trend)\b"
)
_NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:[.,  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(\s*(?:%|prozent\b|percent\b))?", re.IGNORECASE)
_HEDGE = re.compile(r"(?i)\b(etwa|rund|circa|knapp|fast|über|about|around|roughly|nearly|almost|over|approximately)\s*$")
_MALFORMED = re.compile(
    r"(?i)(?:\b(\w+)\s+\1\b|\b(?:und|oder|aber|weil|dass|and|or|but|because|that|the|der|die|eine|a)\s*[.!?]?\s*$|"
    r"[,;:–—-]\s*[.!?]?\s*$)"
)
# Spoken fluency (grammar only, any topic): sentences a listener can parse
# but nobody would say aloud.
# A main clause whose fronted phrase is followed by an auxiliary/modal verb
# and then no subject at all ("Bei X kann [?] kurz besser sein").
_FRONTED_AUXILIARY = re.compile(
    r"(?i)^(?:bei|beim|in|im|nach|mit|für|vor|ohne|am|zum|zur|durch|wegen|während|unter|über|auf|aus)\s[^,–—:;?!.]{1,60}?\s"
    r"(?:kann|können|könnte|muss|müssen|soll|sollte|darf|ist|sind|war|wird|werden|wäre)\s(?P<rest>[^.!?]*)"
    r"|^(?:at|in|on|with|for|after|during|before|without|by)\s[^,–—:;?!.]{1,60}?\s"
    r"(?:can|could|may|might|will|would|should|must|is|are|was|were)\s(?P<rest_en>[^.!?]*)"
)
_SUBJECT = re.compile(
    r"(?:\b(?:ich|du|er|sie|es|man|wir|ihr|das|dies\w*|jede\w*|viele|alle|nichts|etwas|der|die|den|dem|des|ein\w*|kein\w*|"
    r"dein\w*|mein\w*|seine\w*|ihre?\w*|unser\w*|i|you|he|she|it|we|they|this|that|these|those|the|a|an|your|my|his|her|"
    r"our|their|everyone|nothing|something)\b|\b[A-ZÄÖÜ]\w+|\d)"
)
# A comparison with nothing to compare to ("kann besser sein", "can be better").
_VAGUE_COMPARISON = re.compile(
    r"(?i)\b(?:kann|können|könnte|kann auch|can|could|may|might)\b[^.!?]{0,30}?\b(?:besser|schlechter|mehr|weniger|größer|"
    r"kleiner|länger|kürzer|schneller|langsamer|gesünder|sinnvoller|wichtiger|leichter|schwerer|stärker|höher|wacher|müder|"
    r"better|worse|more|less|bigger|smaller|longer|shorter|faster|slower|healthier)\s+(?:sein|be)\b"
    r"|\b(?:can|could|may|might)\s+(?:\w+\s+)?be\s+(?:better|worse|more|less|bigger|smaller|longer|shorter|faster|slower|healthier)\b"
)
_COMPARISON_TARGET = re.compile(r"(?i)\b(?:als|than|wie|as)\b")


def fluency_issues(text: str) -> list[str]:
    """Structural signs of unnatural spoken wording (grammar, not a phrase list)."""
    issues: list[str] = []
    if "?" not in text:
        match = _FRONTED_AUXILIARY.search(text.strip())
        rest = (match.group("rest") if match and match.group("rest") is not None else match.group("rest_en")) if match else None
        if rest is not None and not _SUBJECT.search(rest):
            issues.append("missing_subject")
    if _VAGUE_COMPARISON.search(text) and not _COMPARISON_TARGET.search(text):
        issues.append("vague_comparison")
    return issues


_EVIDENCE_STRATEGIES = {
    "verified_statistic", "counterintuitive_insight", "direct_reframe", "common_mistake",
    "social_proof_or_trend", "high_stakes_consequence", "evidence_insight",
}
# Strategies whose support must be their own signal fact (a number, a
# reframe, a misconception, a trend); the others may use any hook-safe fact.
_LOOSE_EVIDENCE = {"counterintuitive_insight", "evidence_insight", "high_stakes_consequence"}
VERBAL_DIMENSIONS = (
    "spoken_simplicity", "useful_information", "topic_relevance", "attention_value", "factual_defensibility", "natural_language",
    "brevity", "body_transition", "curiosity", "insight", "non_repetition", "strategy_fit",
)
# The document's priority order: useful specific information first.
VERBAL_WEIGHTS = {
    # A 14-year-old must understand the hook on first listen.
    "spoken_simplicity": 1.6,
    "useful_information": 1.6, "topic_relevance": 1.5, "attention_value": 1.4, "factual_defensibility": 1.4,
    # Said aloud as a person would say it: as important as being easy.
    "natural_language": 1.6, "brevity": 1.1, "body_transition": 1.0, "curiosity": 0.9, "insight": 0.9,
    "non_repetition": 0.8, "strategy_fit": 1.0,
}
REVEAL_CODES = ("names_protected_answer", "implies_protected_answer", "states_comparison_result", "queries_protected_target", "states_primary_answer")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean(value: object, limit: int = 240) -> str:
    return " ".join(clean_narration_text(str(value or "")).split())[:limit].strip()


def _plain(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _words(value: object) -> set[str]:
    return {
        word for word in re.findall(r"[a-zäöüß0-9]{3,}", str(value or "").casefold())
        if word not in _STOP
    }


def _overlap(first: set[str], second: set[str]) -> float:
    """Shared share of the smaller set (0 when either is empty)."""
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def _related(first: set[str], second: set[str]) -> set[str]:
    """Words of ``first`` related to any word of ``second`` (inflection tolerant)."""
    return {word for word in first if _mentions([word], second)}


def _numbers(value: object) -> set[str]:
    return {re.sub(r"[.,\s]", "", match) for match in re.findall(r"\d[\d.,\s]*\d|\d", str(value or ""))}


def _sourced(fact: dict[str, Any]) -> bool:
    return bool(fact.get("sources")) and fact.get("verification") in {"source_attributed", "source_snippet"}


def _salient_numbers(claim: str) -> list[dict[str, str]]:
    """Numbers worth saying (not years, not single digits), with hedge and unit label."""
    found: list[dict[str, str]] = []
    for match in _NUMBER.finditer(claim):
        digits = re.sub(r"\D", "", match.group(1))
        percent = bool(match.group(2))
        year = not percent and re.fullmatch(r"(1[5-9]|20)\d\d", match.group(1)) is not None
        if year or (len(digits) < 3 and not percent):
            continue
        hedge = _HEDGE.search(claim[: match.start()])
        following = [word for word in re.findall(r"[\wÄÖÜäöüß-]+", claim[match.end():])[:3] if word.casefold() not in _STOP]
        found.append({
            "value": " ".join(match.group(1).split()) + (" %" if percent else ""),
            "hedge": hedge.group(1) if hedge else "",
            "label": following[0] if following and not percent else "",
            "weight": str(len(digits) + (3 if percent else 0)),
        })
    return found


# ---------------------------------------------------------------------------
# Story context (the Story Arc is authoritative)
# ---------------------------------------------------------------------------

def hook_context(
    intent: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    story_arc: dict[str, Any] | None,
    payoff_plan: dict[str, Any] | None,
    format_plan: dict[str, Any] | None,
    novelty_plan: dict[str, Any] | None,
    body_blocks: list[dict[str, Any]],
    protected_target: str | None = None,
) -> dict[str, Any]:
    """Everything a hook may use or must protect, derived from the persisted plans.

    ``protected_target`` is the structural visual key of the primary answer
    (fact identity -> planner intent), which wins over the planner's own
    payoff guess exactly as ``story_arc.bind_story_visual_protection`` does.
    """
    arc = story_arc if isinstance(story_arc, dict) else {}
    plan = payoff_plan if isinstance(payoff_plan, dict) else {}
    units = arc_units(arc)
    facts = [fact for fact in facts or [] if isinstance(fact, dict)]
    question = _plain(arc.get("primary_question") or intent.get("question") or intent.get("topic"))
    topic = _plain(intent.get("topic") or question)
    withhold = bool((arc.get("curiosity_gap") or {}).get("withhold_answer")) or plan.get("reveal_policy") == "after_supporting_information"
    primary_id = str(arc.get("primary_answer_id") or plan.get("primary_answer_id") or "")
    final_id = str(arc.get("final_payoff_id") or plan.get("final_payoff_id") or "")
    primary = _plain(units.get(primary_id, {}).get("claim") or plan.get("primary_answer") or plan.get("payoff"))
    final = _plain(units.get(final_id, {}).get("claim") or plan.get("final_payoff") or plan.get("payoff"))
    protected_ids = [str(item) for item in (arc.get("hook") or {}).get("protected_ids") or []]
    format_name = str((format_plan or {}).get("selected_format") or arc.get("format") or "explanation").casefold()
    label = _plain(plan.get("hook_must_not_reveal")).rstrip(".!?")
    if withhold and primary and not label:
        label = _protected_answer(primary, question)
    question_words = _words(question) | _words(topic)
    allowed = [fact for fact in hook_safe_facts(facts, arc) if _plain(fact.get("claim"))]
    shared = question_words | set().union(*(_words(fact.get("claim")) for fact in allowed)) if allowed else question_words
    forbidden: set[str] = set()
    if withhold:
        # Only the answer's own identity is secret: never the question's words,
        # nor words that hook-safe facts share with it.
        forbidden = _words(label) if len(_words(label)) == 1 else _words(label) - _related(_words(label), shared)
    body = [block for block in body_blocks if str(block.get("role") or "").casefold() != "hook" and _plain(block.get("text"))]
    first_body = re.split(r"(?<=[.!?])\s+", _plain(body[0].get("text")), maxsplit=1)[0] if body else ""
    novelty = novelty_plan if isinstance(novelty_plan, dict) else {}
    distinctive = {str(item) for key in ("distinctive_facts", "explanatory_gain", "comparison_gain") for item in novelty.get(key) or []}
    claims = {str(fact.get("id") or ""): _plain(fact.get("claim")) for fact in facts if _plain(fact.get("claim"))}
    claims.update({fact_id: _plain(unit.get("claim")) for fact_id, unit in units.items() if _plain(unit.get("claim"))})
    context = {
        "language": str(intent.get("language") or "en"),
        "format": format_name,
        "structure": str(arc.get("structure") or ""),
        "withhold": withhold,
        "question": question,
        "topic": topic,
        "intent": intent,
        "primary_answer_id": primary_id or None,
        "primary_answer": primary,
        "final_payoff_id": final_id or None,
        "final_payoff": final,
        "secondary": [_plain(unit.get("claim")) for unit in units.values() if unit.get("role") == "secondary_insight"],
        "protected_ids": protected_ids,
        "protected_label": label,
        "forbidden": forbidden,
        "sides": [side for side in comparison_sides(question) if side],
        "facts": facts,
        "allowed_facts": allowed,
        # Numbers and trends may be supported by any sourced fact; whether
        # the wording attributes a protected fact is the reveal check's job.
        "sourced_facts": [fact for fact in facts if _sourced(fact)],
        "claims": claims,
        "shared_words": shared,
        "question_words": question_words,
        "arc_words": set().union(*(_words(claim) for claim in claims.values())) | question_words if claims else question_words,
        "body": " ".join(_plain(block.get("text")) for block in body),
        "first_body": first_body,
        "body_sentences": [
            sentence for block in body
            for sentence in re.split(r"(?<=[.!?])\s+", _plain(block.get("text"))) if sentence.strip()
        ],
        "distinctive_words": set().union(*(_words(claims.get(fact_id, "")) for fact_id in distinctive)) if distinctive else set(),
        "payoff_plan": plan,
        "protected_target": (visual_target_key(protected_target) or visual_target_key(plan.get("protected_visual_target"))) if withhold else "",
    }
    context["signals"] = strategy_signals(context)
    return context


def leaks(text: object, context: dict[str, Any], *, strict: bool = False) -> str | None:
    """Why ``text`` would reveal the protected answer before the Story Arc allows it.

    ``strict`` (on-screen text and visuals, like the Final Video Critic's own
    reveal check): the protected subject may not appear at all.  Spoken
    hooks may name it as one open option of the question.
    """
    value = _plain(text)
    if not value or not context.get("withhold"):
        return None
    forbidden = set(context.get("forbidden") or ())
    tokens = _visual_query_tokens(value)
    hits = {term for term in forbidden if _mentions(tokens, {term})}
    needed = 1 if len(forbidden) <= 2 else 2
    sides = context.get("sides") or []
    open_options = len(sides) >= 2 and all(_mentions(tokens, side) for side in sides) and bool(_OPTION_MARKERS.search(value))
    if hits and len(hits) >= needed:
        # Naming the protected subject as one open option of the question is
        # not a reveal: every side is named and it is framed as open.
        return None if open_options and not strict else "names_protected_answer"
    if len(sides) == 2 and any(_mentions(tokens, side) for side in sides) and not open_options:
        if _NEGATED_RESULT.search(value):
            # "X does not win" names the other side by elimination.
            return "implies_protected_answer"
        if _COMPARATIVE_RESULT.search(value) and "?" not in value:
            # "A has more than B" states the result whichever side it names.
            return "states_comparison_result"
    if not forbidden and not sides and reveals_protected_payoff(value, context.get("payoff_plan") or {}):
        return "names_protected_answer"
    return None


# ---------------------------------------------------------------------------
# 1. Strategy fit comes before wording: what does the research support?
# ---------------------------------------------------------------------------

def _side_name(question: str, side: set[str]) -> str:
    return next((word for word in re.findall(r"[\wÄÖÜäöüß-]+", question) if _mentions(_visual_query_tokens(word), side)), "")


def comparison_numbers(context: dict[str, Any]) -> dict[str, Any] | None:
    """One sourced, comparable number per side of an A-or-B question.

    Numbers of a protected fact may only be spoken detached from their side
    (the reveal check enforces that); they make a measurable-contrast hook
    possible without saying who wins.
    """
    sides = context.get("sides") or []
    if len(sides) != 2:
        return None
    found: list[dict[str, Any]] = []
    for index, side in enumerate(sides):
        other = sides[1 - index]
        match = None
        for fact in context["sourced_facts"]:
            claim = _plain(fact.get("claim"))
            tokens = _visual_query_tokens(claim)
            if not _mentions(tokens, side) or _mentions(tokens, other):
                continue
            numbers = _salient_numbers(claim)
            if numbers:
                match = {**max(numbers, key=lambda item: int(item["weight"])), "fact_id": str(fact.get("id") or ""), "side": _side_name(context["question"], side)}
                break
        if match is None or not match["side"]:
            return None
        found.append(match)
    first, second = found
    if first["value"] == second["value"]:
        return None
    # Both numbers must count the same thing: the unit word after one number
    # that both claims name ("267.570 zählt man hier" in a claim about islands).
    claims = [_visual_query_tokens(context["claims"].get(item["fact_id"], "")) for item in found]
    unit = next((
        item["label"] for item in found
        if item["label"] and all(_mentions(tokens, {item["label"].casefold()}) for tokens in claims)
    ), "")
    if not unit:
        return None
    return {"sides": [{**item, "label": unit} for item in found]}


def strategy_signals(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Which documented strategies the researched content supports, with provenance."""
    signals = {strategy: {"viable": False, "fact_ids": [], "signals": []} for strategy in CANONICAL_STRATEGIES}

    def add(strategy: str, fact_id: str | None, code: str) -> None:
        entry = signals[strategy]
        entry["viable"] = True
        if fact_id and fact_id not in entry["fact_ids"]:
            entry["fact_ids"].append(fact_id)
        if code not in entry["signals"]:
            entry["signals"].append(code)

    allowed_ids = {str(fact.get("id") or "") for fact in context["allowed_facts"]}
    # Before a withheld reveal, protected facts may inspire a hook only
    # detached from their subject (the reveal checks forbid naming it).
    detached = [
        fact for fact in context["facts"]
        if context["withhold"] and str(fact.get("id") or "") in set(context["protected_ids"]) - allowed_ids and _plain(fact.get("claim"))
    ]
    for fact in [*context["allowed_facts"], *detached]:
        claim = _plain(fact.get("claim"))
        fact_id = str(fact.get("id") or "") or None
        if fact in detached:
            if _FACT_CONTRAST.search(claim) or _FACT_RESTRICTION.search(claim):
                add("counterintuitive_insight", fact_id, "researched_contrast_detached")
            if _sourced(fact) and _salient_numbers(claim):
                add("verified_statistic", fact_id, "strong_sourced_number_detached")
            continue
        if _sourced(fact) and _salient_numbers(claim):
            add("verified_statistic", fact_id, "strong_sourced_number")
        if _FACT_REFRAME.search(claim):
            add("direct_reframe", fact_id, "researched_reframe")
        elif _FACT_CAUSE.search(claim):
            # A researched mechanism is a defensible "Y" for "not X, but Y".
            add("direct_reframe", fact_id, "researched_mechanism")
        if _FACT_RESTRICTION.search(claim):
            # "only Y" corrects a larger assumption: a defensible reframe/contrast.
            add("direct_reframe", fact_id, "researched_restriction")
            add("counterintuitive_insight", fact_id, "researched_restriction")
        if _FACT_CONTRAST.search(claim):
            add("counterintuitive_insight", fact_id, "researched_contrast")
        elif _FACT_CAUSE.search(claim):
            # A researched cause is what corrects the intuitive explanation.
            add("counterintuitive_insight", fact_id, "researched_mechanism")
        if _FACT_MISTAKE.search(claim):
            add("common_mistake", fact_id, "researched_misconception")
            add("ego_challenge", fact_id, "self_check_on_misconception")
        if _FACT_CONSEQUENCE.search(claim):
            add("high_stakes_consequence", fact_id, "researched_consequence")
        if _sourced(fact) and _FACT_TREND.search(claim):
            add("social_proof_or_trend", fact_id, "researched_trend")
        if len(claim.split()) >= 4:
            add("evidence_insight", fact_id, "concrete_fact")
    pair = comparison_numbers(context) if context["withhold"] else None
    if pair:
        for side in pair["sides"]:
            add("verified_statistic", side["fact_id"], "comparable_sourced_numbers")
        add("ego_challenge", None, "numeric_self_test")
    if context["withhold"]:
        add("curiosity_gap", context.get("primary_answer_id"), "withheld_answer")
    elif context["allowed_facts"] and re.search(
        r"(?i)\b(?:why|how|warum|wieso|weshalb|wie|what (?:causes|makes|happens)|was (?:löst|bewirkt|passiert|verursacht))\b",
        context["question"],
    ):
        add("curiosity_gap", None, "causal_question")
    if context["format"] == "quiz":
        add("ego_challenge", None, "quiz_self_test")
    return signals


# ---------------------------------------------------------------------------
# 2. Deterministic, document-based candidates (no provider)
# ---------------------------------------------------------------------------

def _speakable(item: dict[str, str], language: str) -> str:
    """A figure as a 14-year-old hears it: long numbers as a true hedged rounding."""
    value = parse_number(item["value"])
    digits = re.sub(r"\D", "", item["value"]).rstrip("0")
    if value is None or value < 10_000 or len(digits) <= 2 or "%" in item["value"]:
        return f"{item['hedge']} {item['value']}".strip()
    magnitude = 10 ** (len(str(int(value))) - 2)
    rounded = int(round(value / magnitude) * magnitude)
    text = f"{rounded:,}".replace(",", "." if language == "de" else ",")
    return f"{'rund' if language == 'de' else 'about'} {text}"


def _numeric_contrast(context: dict[str, Any]) -> dict[str, Any] | None:
    """verified_statistic + self-test: two sourced numbers, sides named as open options.

    Numbers are said largest first; the sides are named in the opposite
    pairing so word order never hands out the answer.
    """
    pair = comparison_numbers(context)
    if not pair:
        return None
    first, second = sorted(pair["sides"], key=lambda item: -float(re.sub(r"[^\d]", "", item["value"]) or 0))

    def said(item: dict[str, Any]) -> str:
        return _speakable(item, context["language"])

    label = first["label"]
    if context["language"] == "de":
        text = f"{said(first)} gegen {said(second)} {label} – welche Zahl gehört zu {second['side']}, welche zu {first['side']}?"
    else:
        text = f"{said(first)} versus {said(second)} {label} – which number belongs to {second['side']}, and which to {first['side']}?"
    return {
        "strategy": "verified_statistic",
        "text": text[:1].upper() + text[1:],
        "supported_by_fact_ids": [first["fact_id"], second["fact_id"]],
        "reason_codes": ["comparable_sourced_numbers", "open_self_test"],
    }


def _clause(claim: str, pattern: re.Pattern[str]) -> str:
    """The shortest complete sentence/clause of a claim that carries the signal."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?;])\s+", claim) if part.strip()]
    for sentence in sentences:
        if pattern.search(sentence) and 4 <= len(sentence.split()) <= 20:
            return sentence.rstrip(".!?;,:") + "."
    return ""


def deterministic_candidates(context: dict[str, Any], *, planner_hook: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Topic wording from research, per supported documented strategy (3-5 when possible)."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(strategy: str, text: str, fact_ids: list[str], codes: list[str], origin: str = "deterministic") -> None:
        text = _clean(text)
        key = " ".join(text.casefold().split()).rstrip(".!?")
        if not text or key in seen or canonical_strategy(strategy) is None:
            return
        if origin != "emergency" and not strategy_matches(strategy, text):
            return
        seen.add(key)
        candidates.append({"strategy": canonical_strategy(strategy), "text": text, "supported_by_fact_ids": fact_ids, "reason_codes": codes, "origin": origin})

    if planner_hook and canonical_strategy(planner_hook.get("strategy")):
        origin = "emergency" if planner_hook.get("origin") == "emergency" else "planner"
        push(str(planner_hook["strategy"]), str(planner_hook.get("text") or ""), list(planner_hook.get("fact_ids") or []), ["planner_hook"], origin)
    contrast = _numeric_contrast(context) if context["withhold"] else None
    if contrast:
        push(contrast["strategy"], contrast["text"], contrast["supported_by_fact_ids"], contrast["reason_codes"])
    signals = context["signals"]
    by_id = {str(fact.get("id") or ""): fact for fact in context["allowed_facts"]}
    patterns = {
        "direct_reframe": _FACT_REFRAME,
        "counterintuitive_insight": _FACT_CONTRAST,
        "common_mistake": _FACT_MISTAKE,
        "high_stakes_consequence": _FACT_CONSEQUENCE,
        "social_proof_or_trend": _FACT_TREND,
    }
    for strategy, pattern in patterns.items():
        for fact_id in signals[strategy]["fact_ids"]:
            clause = _clause(_plain(by_id.get(fact_id, {}).get("claim")), pattern)
            if clause:
                push(strategy, clause, [fact_id], list(signals[strategy]["signals"]))
                break
    for fact_id in signals["verified_statistic"]["fact_ids"]:
        if fact_id in by_id:
            clause = _clause(_plain(by_id[fact_id].get("claim")), _NUMBER)
            if clause:
                push("verified_statistic", clause, [fact_id], ["strong_sourced_number"])
                break
    for fact_id in signals["evidence_insight"]["fact_ids"]:
        text = _grounded_insight(_plain(by_id.get(fact_id, {}).get("claim")), context["intent"])
        if text and len(text.split()) <= 20:
            push("evidence_insight", text, [fact_id], ["concrete_fact"])
        if sum(1 for item in candidates if item["strategy"] == "evidence_insight") >= 2:
            break
    return candidates[:5]


def emergency_candidate(context: dict[str, Any]) -> dict[str, Any] | None:
    """The question itself — only when no researched candidate is usable.

    Only a real question qualifies; an instruction ("tell a story about ...")
    or a story prompt never becomes a spoken hook.
    """
    question = _clean(context["question"]).rstrip(".!?")
    if not question or str(context["intent"].get("content_type") or "") == "fictional_story":
        return None
    if not (_clean(context["question"]).endswith("?") or re.match(r"(?i)^(?:why|how|what|which|who|when|where|is|are|do|does|can|warum|wieso|weshalb|wie|was|welche\w*|wer|wann|wo|ist|sind|kann)\b", question)):
        return None
    return {"strategy": "curiosity_gap", "text": question + "?", "supported_by_fact_ids": [], "reason_codes": ["question_fallback"], "origin": "emergency"}


# ---------------------------------------------------------------------------
# 3. The document's selection rubric
# ---------------------------------------------------------------------------

def assess_verbal(
    text: str,
    strategy: object,
    context: dict[str, Any],
    *,
    fact_ids: list[str] | None = None,
    emergency: bool = False,
) -> dict[str, Any]:
    """Hard failures, penalty codes, positive reason codes and 0..1 rubric scores."""
    hard: list[str] = []
    codes: list[str] = []
    positive: list[str] = []
    verbal = _clean(text)
    canonical = canonical_strategy(strategy)
    if canonical is None:
        hard.append("non_document_strategy")
    spoken = _words(verbal)
    question_words = context["question_words"]
    claim_words = context["arc_words"] - question_words
    specific = _related(spoken - _related(spoken, question_words), claim_words)
    numbers = _numbers(verbal)

    for issue in hook_issues(verbal, context["sourced_facts"], body=context["body"], intent=context["intent"], research_scoped=True):
        if issue == "generic_clickbait":
            hard.append("cheap_clickbait")
        elif issue == "personal_attack":
            hard.append("unnecessary_provocation")
        elif issue in {"meta_language", "generic_meta_filler", "structural_label", "empty"}:
            hard.append("meta_language")
        elif issue == "unsupported_statistic":
            hard.append("unsupported_statistic")
        elif issue in {"unsupported_prevalence", "unsupported_trend"}:
            hard.append("unsupported_trend")
        elif issue == "question_echo":
            (codes if emergency else hard).append("question_echo")
        elif issue != "repeats_body":
            codes.append(f"verbal_{issue}")
    if _CHEAP_BAIT.search(verbal):
        hard.append("cheap_clickbait")
    if _FAKE_CONTROVERSY.search(verbal) and not any(_FAKE_CONTROVERSY.search(_plain(fact.get("claim"))) for fact in context["sourced_facts"]):
        hard.append("fake_controversy")
    generic_opener = bool(_GENERIC_OPENER.search(verbal))
    if generic_opener:
        codes.append("generic_opener")
    reason = leaks(verbal, context)
    if reason:
        hard.append(f"verbal_{reason}")
    answer_words = _words(context["primary_answer"])
    # A question offering both alternatives as open options states nothing.
    open_options = "?" in verbal and len(context["sides"]) == 2 and all(_mentions(_visual_query_tokens(verbal), side) for side in context["sides"])
    states_answer = not open_options and bool(spoken and answer_words and _overlap(spoken, answer_words) >= 0.75 and len(spoken & answer_words) >= 3)
    if states_answer:
        (hard if context["withhold"] else codes).append("states_primary_answer")
    final_words = _words(context["final_payoff"])
    spends_payoff = bool(
        context.get("final_payoff_id") and context.get("final_payoff_id") != context.get("primary_answer_id")
        and spoken and final_words and _overlap(spoken, final_words) >= 0.6
    )
    if spends_payoff:
        # The last meaningful beat of the arc would be spent in the first second.
        codes.append("spends_final_payoff")
    first = _words(context["first_body"])
    same_as_first = " ".join(verbal.casefold().split()).rstrip(".!?") == " ".join(context["first_body"].casefold().split()).rstrip(".!?")
    # Duplication: a statement that covers most of the first body sentence.
    # A question that names the observation the body then explains is a
    # transition, not a repetition.
    repeats_first = same_as_first or bool(
        spoken and first and "?" not in verbal
        and len(spoken & first) / len(spoken) >= 0.75 and len(spoken & first) / len(first) >= 0.6
    )
    if repeats_first:
        # Identical: the hook delivers that block and the pipeline folds it
        # (said once).  A paraphrase would be heard twice: body duplication.
        (codes if same_as_first else hard).append("repeats_first_body" if same_as_first else "body_duplication")
    elif any(_same_sentence(verbal, sentence) or _covers(spoken, sentence, verbal) for sentence in context["body_sentences"][1:]):
        # A later body sentence said as the opening would be heard twice.
        hard.append("body_duplication")
    malformed = bool(_MALFORMED.search(verbal)) or (verbal[:1].isalpha() and verbal[:1].islower())
    if malformed:
        codes.append("malformed_grammar")
    fluency = fluency_issues(verbal)
    codes.extend(fluency)
    # The first thing heard must stand alone: no clause cut out of a longer
    # sentence, no back-reference to text the viewer never heard.
    incomplete = standalone_issue(verbal)
    if incomplete:
        hard.append(incomplete)
    # Off the story's axis: a statement importing research content (beyond
    # the question's own words) that neither the story the viewer then hears
    # nor the arc's answer and payoff carry - a promise the video never pays.
    # A question only opens the gap.
    own = {word for word in spoken - _related(spoken, question_words) if len(word) >= 4 and not word.isdigit()}
    imported = _related(own, claim_words)
    body_words = _words(context["body"])
    axis_words = body_words | _words(context["primary_answer"]) | _words(context["final_payoff"])
    if "?" not in verbal and imported and body_words and not _related(imported, axis_words) and not numbers & _numbers(context["body"]):
        hard.append("off_story_axis")
    empty_curiosity = not specific and not numbers and not emergency
    if empty_curiosity:
        codes.append("empty_curiosity")

    # Strategy fit: supported by research, and performed by the sentence (its
    # rhetorical function, not its vocabulary).
    fit = 0.0
    if canonical:
        entry = context["signals"].get(canonical) or {}
        text_fit = strategy_function(canonical, verbal)
        evidence_ids = set(entry.get("fact_ids") or [])
        if canonical in _LOOSE_EVIDENCE:
            # Insight-type strategies may draw on any hook-safe researched fact.
            evidence_ids |= {str(fact.get("id") or "") for fact in context["allowed_facts"]}
        # A reframe is supported only through its correcting side (Y).
        side = _REFRAME_SIDE.search(verbal) if canonical == "direct_reframe" else None
        evidence_words = _words(side.group(1)) if side else spoken
        used = [
            fact_id for fact_id in sorted(evidence_ids)
            if _related(evidence_words, _words(context["claims"].get(fact_id, "")) - question_words)
            or (numbers & _numbers(context["claims"].get(fact_id, "")))
            or _rounded_from(verbal, context["claims"].get(fact_id, ""))
        ]
        if emergency and canonical == "curiosity_gap":
            fit = 0.5
        elif not entry.get("viable"):
            hard.append("strategy_not_supported_by_research")
        elif not text_fit:
            # The sentence genuinely does not do what the strategy does.
            hard.append("strategy_not_in_wording")
        elif canonical in _EVIDENCE_STRATEGIES and not used:
            hard.append("strategy_evidence_not_used")
        else:
            fit = text_fit
            if text_fit < 1:
                codes.append("strategy_implicit")
            positive.extend(entry.get("signals") or [])
        claimed = [str(value) for value in fact_ids or [] if str(value) in context["claims"]]
        fact_ids = list(dict.fromkeys([*claimed, *used]))
    else:
        fact_ids = []

    contrast = bool(_CONTRAST.search(verbal))
    supported_number = bool(numbers) and "unsupported_statistic" not in hard
    self_test = bool(_SELF_TEST.search(verbal))
    word_count = len(verbal.split())
    useful = 0.1 if empty_curiosity else min(1.0, 0.25 + 0.2 * len(specific) + (0.35 if supported_number else 0.0))
    relevance = 1.0 if (specific or supported_number) and _related(spoken, question_words | claim_words) else 0.6 if _related(spoken, question_words) else 0.2
    attention = 0.35 + (0.2 if contrast else 0) + (0.25 if supported_number else 0) + (0.15 if self_test else 0) - (0.35 if generic_opener else 0)
    defensibility = 1.0 - (0.3 if _ABSOLUTE.search(verbal) and not any(_ABSOLUTE.search(_plain(fact.get("claim"))) for fact in context["sourced_facts"]) else 0)
    natural = (
        1.0 - (0.4 if malformed else 0) - (0.3 if "verbal_jargon_first" in codes else 0) - (0.2 if "verbal_too_long" in codes else 0)
        - (0.5 if "missing_subject" in fluency else 0) - (0.3 if "vague_comparison" in fluency else 0)
    )
    brevity = 1.0 if word_count <= 14 else max(0.2, 1.0 - (word_count - 14) * 0.07)
    body_words = _words(context["body"])
    transition = 0.7 if same_as_first else 1.0 if (not body_words or _related(spoken, body_words)) else 0.75
    curiosity = 0.2 + (0.3 if "?" in verbal or context["withhold"] else 0) + (0.25 if contrast else 0) + (0.2 if supported_number else 0) - (0.3 if empty_curiosity else 0)
    research_contrast = contrast and any(_FACT_CONTRAST.search(context["claims"].get(fact_id, "")) for fact_id in fact_ids)
    insight = 0.3 + (0.3 if research_contrast else 0) + (0.2 if _related(spoken, context["distinctive_words"]) else 0) + (0.2 if supported_number else 0) + (0.1 if specific else 0) - (0.3 if generic_opener else 0)
    question_overlap = len(_related(spoken, question_words)) / max(1, len(spoken))
    non_repetition = 1.0 - (0.5 if "question_echo" in codes + hard else 0) - (0.3 if same_as_first else 0) - (0.2 if question_overlap > 0.8 else 0)
    non_repetition -= 0.4 if spends_payoff else 0
    insight -= 0.3 if states_answer else 0
    transition -= 0.3 if spends_payoff else 0
    if not (contrast or supported_number or self_test or "?" in verbal):
        # The document: neutral information provokes little; a hook needs a
        # contrast, a number, a question or a self-check.
        codes.append("neutral_information")
        attention = min(attention, 0.2)
        curiosity = min(curiosity, 0.1)
        insight -= 0.1
    if same_as_first and (states_answer or _plain_explanation(verbal, context)):
        # The hook is just the first body sentence: no hook at all, only the
        # explanation said early (the document: neutral information is weak).
        codes.append("hook_is_plain_explanation")
        curiosity -= 0.3
        attention -= 0.25
        insight -= 0.2
        transition = min(transition, 0.5)
    simplicity, simplicity_codes = spoken_simplicity(verbal, context)
    codes.extend(simplicity_codes)
    dimensions = {
        "spoken_simplicity": simplicity,
        "useful_information": useful, "topic_relevance": relevance, "attention_value": attention,
        "factual_defensibility": defensibility, "natural_language": natural, "brevity": brevity,
        "body_transition": transition, "curiosity": curiosity, "insight": insight,
        "non_repetition": non_repetition, "strategy_fit": fit,
    }
    dimensions = {key: round(max(0.0, min(1.0, value)), 3) for key, value in dimensions.items()}
    if supported_number:
        positive.append("strong_sourced_number")
    if context["withhold"] and not reason:
        positive.append("protected_answer_safe")
    if useful >= 0.65:
        positive.append("specific")
    if curiosity >= 0.7:
        positive.append("high_curiosity")
    if transition == 1.0 and not repeats_first:
        positive.append("clean_transition")
    if simplicity >= 0.9:
        positive.append("easy_to_follow")
    return {
        "strategy": canonical,
        "hard_fail": list(dict.fromkeys(hard)),
        "reason_codes": list(dict.fromkeys(codes)),
        "positive_codes": list(dict.fromkeys(positive)),
        "dimensions": dimensions,
        "supported_by_fact_ids": fact_ids,
    }


# Spoken-clarity grammar (no topic vocabulary): abstract noun endings, office
# language and clause connectors that make a sentence hard to follow by ear.
_ABSTRACT_NOUN = re.compile(r"(?i)^\w{4,}(?:ung|heit|keit|tion|ität|ismus|ierung|schaft|ance|ence|ment|ity|ness)(?:en|s)?$")
_BUREAUCRATIC = re.compile(
    r"(?i)\b(?:bezüglich|hinsichtlich|diesbezüglich|seitens|infolgedessen|zwecks|gemäß|im rahmen|im hinblick|"
    r"aufgrund dessen|demzufolge|regarding|pertaining|thereby|whereby|aforementioned|with respect to|in terms of|"
    r"notwithstanding|henceforth)\b"
)
# Subordinating/relative connectors (interrogatives like "welche ...?" are not clauses).
_CONNECTOR = re.compile(r"(?i)\b(?:dass|weil|obwohl|wobei|deren|dessen|sodass|whereas|whom|whose|although)\b")
LONG_WORD = 13
RARE_WORD = 11
UNFAMILIAR_TERM = 10
SPOKEN_UNIT_WORDS = 12


def spoken_simplicity(text: str, context: dict[str, Any]) -> tuple[float, list[str]]:
    """How easily an average 14-year-old follows the hook on first listen (0..1).

    Spoken clarity, not a school-grade formula: short spoken units, common
    words, one idea at a time.  Long, rare or research terms, abstract noun
    chains, office language, long spoken numbers and stacked clauses cost
    points — also when the user's own question used them: how technically
    the user asked says nothing about what a 14-year-old understands.  A
    necessary term may stay; it is simply counted.  Only the names of the
    compared subjects (the question's sides) are exempt: they are the topic,
    not a wording choice.
    """
    words = re.findall(r"[\wÄÖÜäöüß'-]+", text)
    if not words:
        return 0.0, ["empty"]
    subjects = set().union(*context.get("sides") or [set()])
    known = {word.casefold() for word in words if _mentions(_visual_query_tokens(word), subjects)} if subjects else set()
    research = {word.casefold() for claim in context["claims"].values() for word in re.findall(r"[\wÄÖÜäöüß'-]+", claim)}
    units = [unit for unit in re.split(r"[.!?]+|\s[–—-]\s|;", text) if unit.strip()]
    longest = max(len(unit.split()) for unit in units)
    unfamiliar = [word for word in words if word.casefold() not in known and not word[:1].isdigit()]
    long_words = [word for word in unfamiliar if len(word) >= LONG_WORD]
    rare_words = [word for word in unfamiliar if RARE_WORD <= len(word) < LONG_WORD and word.casefold() not in research]
    research_terms = [word for word in unfamiliar if UNFAMILIAR_TERM <= len(word) < LONG_WORD and word.casefold() in research]
    abstract = [word for word in unfamiliar if _ABSTRACT_NOUN.match(word)]
    long_numbers = [raw for raw in re.findall(r"\d[\d.,\u202f]*\d|\d", text) if len(re.sub(r"\D", "", raw).rstrip("0")) > 3]
    clauses = text.count(",") + text.count("(") + len(_CONNECTOR.findall(text))
    score, codes = 1.0, []
    if longest > SPOKEN_UNIT_WORDS:
        score -= min(0.35, 0.05 * (longest - SPOKEN_UNIT_WORDS))
        codes.append("long_sentence")
    if len(words) > 18:
        score -= min(0.2, 0.03 * (len(words) - 18))
    if long_words or rare_words:
        score -= 0.15 * len(long_words) + 0.05 * len(rare_words)
        codes.append("long_words")
    if research_terms:
        score -= 0.1 * len(research_terms)
        codes.append("unfamiliar_term")
    if len(abstract) >= 2:
        score -= 0.15 * (len(abstract) - 1)
        codes.append("abstract_nouns")
    if _BUREAUCRATIC.search(text):
        score -= 0.3
        codes.append("bureaucratic_wording")
    if long_numbers:
        score -= 0.1 * len(long_numbers)
        codes.append("long_number")
    if clauses > 2:
        score -= 0.1 * (clauses - 2)
        codes.append("nested_clauses")
    return round(max(0.0, min(1.0, score)), 3), codes


def _rounded_from(text: str, claim: str) -> bool:
    """A hedged rounding in ``text`` stands for a figure of ``claim``."""
    from .hooks import _NUMBER, hedge_kind

    for match in _NUMBER.finditer(text):
        value, hedge = parse_number(match.group(0)), hedge_kind(text[: match.start()])
        if value is not None and hedge and rounded_number_supported(value, hedge, claim):
            return True
    return False


def _plain_explanation(text: str, context: dict[str, Any]) -> bool:
    return _plain_evidence_explanation(text, context["sourced_facts"], context["body"])


def _same_sentence(first: str, second: str) -> bool:
    return " ".join(first.casefold().split()).rstrip(".!?") == " ".join(second.casefold().split()).rstrip(".!?")


def _covers(spoken: set[str], sentence: str, verbal: str) -> bool:
    words = _words(sentence)
    return bool(spoken and words and "?" not in verbal and len(spoken & words) / len(spoken) >= 0.75 and len(spoken & words) / len(words) >= 0.6)


def verbal_score(dimensions: dict[str, float]) -> float:
    total = sum(VERBAL_WEIGHTS.values())
    return 100 * sum(VERBAL_WEIGHTS[key] * float(dimensions.get(key, 0.0)) for key in VERBAL_DIMENSIONS) / total


def rank_verbal(context: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assessed candidates, best first (ineligible ones last, score 0)."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        result = assess_verbal(
            candidate["text"], candidate.get("strategy"), context,
            fact_ids=candidate.get("supported_by_fact_ids"), emergency=candidate.get("origin") == "emergency",
        )
        score = 0.0 if result["hard_fail"] else verbal_score(result["dimensions"])
        # Provenance codes of the candidate (e.g. question_fallback) are kept.
        codes = list(dict.fromkeys([*(candidate.get("reason_codes") or []), *result["reason_codes"]]))
        ranked.append({**candidate, **result, "reason_codes": codes, "score": round(score, 1), "eligible": not result["hard_fail"]})
    return sorted(ranked, key=lambda item: (not item["eligible"], -item["score"]))


# Never relaxed in any tier: truth, safety and reveal rules.
_NEVER_RELAXED = {
    "non_document_strategy", "cheap_clickbait", "unnecessary_provocation", "meta_language",
    "unsupported_statistic", "unsupported_trend", "fake_controversy",
}
# Only these say "the strategy fits imperfectly" (not "the sentence is untrue").
_FIT_ONLY = {"strategy_not_supported_by_research", "strategy_not_in_wording", "strategy_evidence_not_used"}
# When evidence is thin, the safest documented strategies claim the least.
_SAFEST = ("evidence_insight", "curiosity_gap")


def closest_strategy(text: str, context: dict[str, Any]) -> str:
    """Rank ALL documented strategies by approximate fit for a truthful sentence.

    The wording must carry the strategy's rhetoric; research support counts
    next; with thin evidence the safest strategies win ties.  Always returns
    one of ``CANONICAL_STRATEGIES``.
    """
    def fit(name: str) -> tuple[float, int]:
        score = (2.0 if strategy_matches(name, text) else 0.0) + (1.0 if context["signals"][name]["viable"] else 0.0)
        score += 0.5 if name in _SAFEST else 0.0
        return score, -CANONICAL_STRATEGIES.index(name)

    return max(CANONICAL_STRATEGIES, key=fit)


def _relaxable(item: dict[str, Any], allowed: set[str]) -> bool:
    hard = set(item["hard_fail"])
    return bool(hard) and not (hard & _NEVER_RELAXED) and not any(code.endswith(REVEAL_CODES) for code in hard) and hard <= allowed


def _topic_curiosity(context: dict[str, Any]) -> dict[str, Any] | None:
    """Last resort: an honest open question about the topic (claims nothing)."""
    topic = _clean(context["topic"] or context["question"]).rstrip(".!?:")
    # "Tell a story about X" / "Erzähle eine Geschichte über X" -> X.
    topic = re.sub(
        r"(?i)^(?:please\s+|bitte\s+)?(?:erzähle?|erzaehle?|schreibe?|write|tell|explain|erkläre?|show|zeige?)\b.*?\b(?:über|ueber|about|von|on)\s+",
        "", topic,
    ).strip() or topic
    if not topic:
        return None
    text = f"{topic} – was steckt dahinter?" if context["language"] == "de" else f"{topic} – what is behind it?"
    return {"strategy": "curiosity_gap", "text": text[:1].upper() + text[1:], "supported_by_fact_ids": [], "reason_codes": ["topic_curiosity_fallback"], "origin": "emergency"}


def select_verbal(
    context: dict[str, Any],
    *,
    extra: list[dict[str, Any]] | None = None,
    exclude: set[str] | None = None,
    planner_hook: dict[str, Any] | None = None,
    max_words: int | None = None,
    allow_fallback: bool = True,
) -> dict[str, Any] | None:
    """The best documented verbal hook — always one, never a non-document strategy.

    1. Strong fit: candidates that pass every rule.
    2. Approximate fit: a truthful candidate whose only problem is an
       imperfect strategy fit gets the closest documented strategy.
    3. Guaranteed truthful fallback: the first body sentence (said once), the
       user's real question, or an open question about the topic.
    Truth, clickbait and reveal rules are never relaxed.  ``None`` only when
    ``max_words`` leaves no candidate, or there is no text at all.
    """
    exclude = {" ".join(text.casefold().split()) for text in exclude or set()}

    def allowed(item: dict[str, Any]) -> bool:
        text = str(item.get("text") or "")
        return bool(text.strip()) and " ".join(text.casefold().split()) not in exclude and (max_words is None or len(text.split()) <= max_words)

    pool = [item for item in [*(extra or []), *deterministic_candidates(context, planner_hook=planner_hook)] if allowed(item)]
    ranked = rank_verbal(context, pool)
    strong = [item for item in ranked if item["eligible"]]
    if strong:
        return strong[0]
    approximate = []
    for item in ranked:
        if _relaxable(item, _FIT_ONLY):
            closest = closest_strategy(item["text"], context)
            retry = rank_verbal(context, [{**item, "strategy": closest, "reason_codes": [*(item.get("reason_codes") or []), "approximate_strategy_fit"]}])[0]
            if retry["eligible"]:
                approximate.append(retry)
    if approximate:
        return max(approximate, key=lambda item: item["score"])
    if not allow_fallback:
        # An explicit request for a *better* opening never gets the last resort.
        return None
    fallbacks: list[dict[str, Any]] = []
    first = context["body_sentences"][0] if context.get("body_sentences") else ""
    if first:
        fallbacks.append({"strategy": closest_strategy(first, context), "text": first, "supported_by_fact_ids": [], "reason_codes": ["first_body_fallback"], "origin": "fallback"})
    fallbacks.extend(item for item in (emergency_candidate(context), _topic_curiosity(context)) if item)
    for item in fallbacks:
        if not allowed(item):
            continue
        result = rank_verbal(context, [item])[0]
        # The fallback is true by construction; only the fit may be imperfect.
        if result["eligible"] or _relaxable(result, _FIT_ONLY | {"question_echo", "body_duplication"}):
            result["eligible"] = True
            result["score"] = result["score"] or round(verbal_score(result["dimensions"]), 1)
            return result
    return None


def question_is_echo(text: str, context: dict[str, Any]) -> bool:
    return _is_question_echo(_clean(text), context["intent"]) if context.get("intent") else False
