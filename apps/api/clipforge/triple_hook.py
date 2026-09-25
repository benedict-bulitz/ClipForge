"""Triple Hook V2: the opening as one coordinated verbal / visual / on-screen unit.

Extends the existing hook path (``hooks`` for verbal truthfulness,
``payoff`` for reveal protection, ``script.triple_hook`` as the one persisted
plan) instead of adding a parallel hook system:

1. ONE bounded provider call proposes four complete candidates
   (``ai.generate_hook_candidates_with_openai``); without a provider,
   deterministic candidates are assembled from the existing verbal hook
   candidates and the planner's own visual intents.
2. Every candidate is checked deterministically (reveal safety against the
   Story Arc, payoff alignment, clickbait, visual concreteness, on-screen
   text validity, cross-modal redundancy).  These checks are authoritative
   for safety: a leaking, redundant or unpayable candidate cannot win.
3. ONE bounded judge call (``ai.judge_triple_hooks_with_openai``) scores the
   remaining complete triples on a structured rubric; its scores are blended
   with the deterministic ones.  Only short reason codes are persisted.

The selected triple drives the real opening: its verbal hook becomes the hook
block (TTS remains the timing authority), its visual becomes the opening
scene's visual intent (queries, verification, crop, generated-image prompt)
and its on-screen hook becomes a ``label`` overlay drawn by the renderer.

No topic vocabulary lives here: only language grammar (stop words, filler
openers, negation) and structural rules per selected format.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .hooks import HookCandidate, generate_hook_candidates, hook_issues
from .media import (
    _VISUAL_QUERY_STOP,
    _mentions,
    _query_is_concrete,
    _semantic_query,
    _visual_query_tokens,
    visual_target_key,
)
from .narration import clean_narration_text
from .overlay_copy import MAX_ELEMENT_CHARS, MAX_ELEMENT_WORDS, assess_elements
from .payoff import _protected_answer, fallback_triple_hook, reveals_protected_payoff
from .story_arc import arc_units, comparison_sides, hook_safe_facts

VERSION = 2
SOURCE = "triple_hook_v2"
CANDIDATE_TARGET = 4
MAX_POOL = 6

STRATEGIES = (
    "contradiction", "unexpected_consequence", "concrete_anomaly", "challenge_question", "visual_mystery",
    "comparison_tension", "misconception_gap", "cause_effect_mystery", "surprising_scale", "immediate_scenario",
)
# The existing verbal hook library's strategy -> the closest opening strategy.
_FROM_V1 = {
    "direct_reframe": "contradiction", "counterintuitive_insight": "contradiction", "hot_take": "contradiction",
    "direct_confrontation": "contradiction", "common_mistake": "misconception_gap",
    "high_stakes_consequence": "unexpected_consequence", "verified_statistic": "surprising_scale",
    "social_proof_or_trend": "unexpected_consequence", "curiosity_gap": "challenge_question",
    "ego_challenge": "challenge_question", "evidence_insight": "concrete_anomaly",
}
_TO_V1 = {
    "contradiction": "counterintuitive_insight", "unexpected_consequence": "high_stakes_consequence",
    "concrete_anomaly": "evidence_insight", "challenge_question": "curiosity_gap", "visual_mystery": "curiosity_gap",
    "comparison_tension": "curiosity_gap", "misconception_gap": "common_mistake",
    "cause_effect_mystery": "curiosity_gap", "surprising_scale": "verified_statistic",
    "immediate_scenario": "evidence_insight",
}
# Which opening mechanisms suit each selected format (structural, not topical).
FORMAT_STRATEGIES = {
    "explanation": {"concrete_anomaly", "cause_effect_mystery", "misconception_gap", "contradiction", "unexpected_consequence", "visual_mystery"},
    "comparison": {"comparison_tension", "challenge_question", "surprising_scale", "contradiction", "misconception_gap"},
    "quiz": {"challenge_question", "comparison_tension", "visual_mystery", "surprising_scale"},
    "ranking": {"surprising_scale", "challenge_question", "comparison_tension", "unexpected_consequence"},
    "list": {"surprising_scale", "challenge_question", "unexpected_consequence", "concrete_anomaly"},
    "misconception_correction": {"misconception_gap", "contradiction", "challenge_question"},
    "before_after": {"unexpected_consequence", "visual_mystery", "immediate_scenario", "cause_effect_mystery"},
    "story": {"immediate_scenario", "visual_mystery", "unexpected_consequence", "contradiction"},
}
DIMENSIONS = (
    "curiosity", "specificity", "comprehension", "visual_intrigue", "visual_feasibility", "verbal_quality",
    "on_screen_quality", "complementarity", "story_alignment", "payoff_alignment", "reveal_safety", "format_fit",
    "novelty", "credibility", "clickbait_free", "production_feasibility",
)
_WEIGHTS = {
    "curiosity": 1.4, "specificity": 1.0, "comprehension": 1.0, "visual_intrigue": 0.8, "visual_feasibility": 1.0,
    "verbal_quality": 1.2, "on_screen_quality": 0.6, "complementarity": 1.3, "story_alignment": 1.0,
    "payoff_alignment": 1.2, "reveal_safety": 1.0, "format_fit": 0.9, "novelty": 0.6, "credibility": 1.0,
    "clickbait_free": 0.8, "production_feasibility": 0.8,
}
# Safety dimensions: the judge may lower them, never raise them above the checks.
_SAFETY = {"reveal_safety", "credibility", "clickbait_free"}
_JUDGE_VETOES = {"leaks_answer", "payoff_mismatch", "impossible_visual", "redundant_channels", "cheap_clickbait", "contradicts_story"}
_REVEAL_CODES = ("names_protected_answer", "implies_protected_answer", "states_comparison_result", "queries_protected_target", "states_primary_answer")
_FEASIBILITY = {"real_media_likely": 1.0, "generated_image_ok": 0.8, "hard_to_source": 0.4, "impossible": 0.0}

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
    "weil", "because", "ihre", "ihren", "sein", "seine", "unsere", "unser", "our", "these",
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
_CONTRAST = re.compile(
    r"(?i)(?:\?|\b(?:not|but|although|despite|instead|rather|yet|actually|nicht|kein\w*|aber|doch|sondern|"
    r"obwohl|statt|trotzdem|eigentlich|gar nicht)\b)"
)
_ABSTRACT_VISUAL = re.compile(
    r"(?i)\b(?:concept|idea|science|scientific|interesting|information|topic|theme|explanation|knowledge|abstract|"
    r"phenomenon|mystery|konzept|idee|wissenschaft\w*|interessant\w*|thema|erklärung|wissen|fakt|phänomen|rätsel)\b"
)
_NEEDS_TEXT = re.compile(
    r"(?i)\b(?:text|caption|headline|title card|label(?:ed|led|s)?|words?|letters|schrift|beschriftung|"
    r"überschrift|infographic|diagram|chart|logo)\b"
)
_CLOSE_FRAMING = re.compile(r"(?i)\b(?:close[- ]?up|macro|detail|nahaufnahme|makro|extreme)\b")
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
_FRAGMENT = re.compile(r"(?:\.\.\.|…|\w-\s*$|^\s*[-–—,;:])")


# ---------------------------------------------------------------------------
# Small text helpers
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
    forbidden: set[str] = set()
    if withhold:
        # Only the answer's own identity is secret: never the question's words,
        # nor words that hook-safe facts share with it.
        shared = question_words | set().union(*(_words(fact.get("claim")) for fact in allowed)) if allowed else question_words
        forbidden = _words(label) if len(_words(label)) == 1 else _words(label) - _related(_words(label), shared)
    body = [block for block in body_blocks if str(block.get("role") or "").casefold() != "hook" and _plain(block.get("text"))]
    first_body = re.split(r"(?<=[.!?])\s+", _plain(body[0].get("text")), maxsplit=1)[0] if body else ""
    novelty = novelty_plan if isinstance(novelty_plan, dict) else {}
    distinctive = {str(item) for key in ("distinctive_facts", "explanatory_gain", "comparison_gain") for item in novelty.get(key) or []}
    claims = {str(fact.get("id") or ""): _plain(fact.get("claim")) for fact in facts if _plain(fact.get("claim"))}
    claims.update({fact_id: _plain(unit.get("claim")) for fact_id, unit in units.items() if _plain(unit.get("claim"))})
    return {
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
        "claims": claims,
        "shared_words": question_words | set().union(*(_words(fact.get("claim")) for fact in allowed)) if allowed else question_words,
        "arc_words": set().union(*(_words(claim) for claim in claims.values())) | question_words if claims else question_words,
        "body": " ".join(_plain(block.get("text")) for block in body),
        "first_body": first_body,
        "distinctive_words": set().union(*(_words(claims.get(fact_id, "")) for fact_id in distinctive)) if distinctive else set(),
        "payoff_plan": plan,
        "protected_target": (visual_target_key(protected_target) or visual_target_key(plan.get("protected_visual_target"))) if withhold else "",
    }


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


def visual_leaks(visual: dict[str, Any], context: dict[str, Any]) -> str | None:
    """Text leak of the visual semantics, or a query keyed to the protected target."""
    target = context.get("protected_target")
    if target and target in (visual.get("media_query_targets") or []):
        return "queries_protected_target"
    return leaks(_visual_text(visual), context, strict=True)


# ---------------------------------------------------------------------------
# Candidate normalisation
# ---------------------------------------------------------------------------

def visual_intent(visual: dict[str, Any], *, must_not_show: list[str], hook_id: str, source: str = SOURCE) -> dict[str, Any]:
    """Structured hook visual in the Visual Director's intent shape (plus V1 keys)."""
    subject = _plain(visual.get("subject") or visual.get("visual_goal"), 120)
    action = _plain(visual.get("action_state"), 120)
    framing = _plain(visual.get("framing"), 80)
    detail = _plain(visual.get("key_detail"), 120)
    contrast = _plain(visual.get("contrast"), 120)
    motion = _plain(visual.get("motion") or visual.get("motion_or_change"), 100)
    tension = _plain(visual.get("tension"), 120)
    raw_targets = visual.get("media_query_targets") if isinstance(visual.get("media_query_targets"), list) else []
    pairs = [
        (_plain(query, 100), visual_target_key(raw_targets[index]) if index < len(raw_targets) else "")
        for index, query in enumerate(visual.get("media_queries") or [])
        if _plain(query, 100)
    ][:4]
    goal = _plain(visual.get("visual_goal")) or ", ".join(part for part in (
        f"{framing} of {subject}" if framing else subject, action, detail,
    ) if part)
    blocked = list(dict.fromkeys([*(_plain(item, 100) for item in visual.get("must_not_show") or [] if _plain(item, 100)), *must_not_show]))[:6]

    def planned(key: str, fallback: list[str]) -> list[str]:
        # A planner intent keeps its own structured lists.
        values = [_plain(item, 100) for item in visual.get(key) or [] if _plain(item, 100)] if isinstance(visual.get(key), list) else []
        return values[:6] or fallback

    return {
        # Visual Director / verifier / crop / generation-prompt fields.
        "visual_goal": goal[:220],
        "objects": planned("objects", [subject] if subject else []),
        "actions": planned("actions", [action] if action else []),
        "context": planned("context", [part for part in (detail, contrast) if part][:2]),
        "visual_strategy": "literal",
        "media_queries": [query for query, _target in pairs],
        "media_query_targets": [target for _query, target in pairs] if any(target for _query, target in pairs) else [],
        "must_not_show": blocked,
        # Structured hook semantics.
        "subject": subject,
        "action_state": action,
        "framing": framing,
        "key_detail": detail,
        "contrast": contrast,
        "motion": motion,
        "tension": tension,
        # V1-compatible keys (thumbnails, reactions, canonical query plan).
        "subjects_to_show": [subject] if subject else [],
        "motion_or_change": motion,
        "visual_priority": detail or tension,
        "source": source,
        "hook_id": hook_id,
    }


def _payoff_fact(candidate: dict[str, Any], context: dict[str, Any]) -> str | None:
    fact_id = _plain(candidate.get("payoff_fact_id"), 16)
    if fact_id in context["claims"]:
        return fact_id
    promise = _words(candidate.get("promised_payoff"))
    best = max(context["claims"].items(), key=lambda item: _overlap(promise, _words(item[1])), default=None) if promise else None
    return best[0] if best and _overlap(promise, _words(best[1])) >= 0.34 else None


def normalise_candidate(raw: dict[str, Any], index: int, context: dict[str, Any], *, origin: str) -> dict[str, Any] | None:
    """A complete, bounded candidate; ``None`` when a channel is structurally missing."""
    if not isinstance(raw, dict):
        return None
    verbal = _clean(raw.get("verbal_hook") or raw.get("text"), 240)
    visual = raw.get("visual") if isinstance(raw.get("visual"), dict) else raw.get("visual_hook") if isinstance(raw.get("visual_hook"), dict) else {}
    if not verbal or not visual:
        return None
    strategy = str(raw.get("strategy") or "")
    strategy = strategy if strategy in STRATEGIES else _FROM_V1.get(strategy, "concrete_anomaly")
    hook_id = f"hook_{chr(ord('a') + index)}"
    protected = [context["protected_label"]] if context.get("withhold") and context.get("protected_label") else []
    candidate = {
        "id": hook_id,
        "origin": origin,
        "strategy": strategy,
        "verbal_hook": verbal,
        "visual_hook": visual_intent(visual, must_not_show=protected, hook_id=hook_id),
        "on_screen_hook": _plain(raw.get("on_screen_hook") if "on_screen_hook" in raw else raw.get("on_screen_text_hook"), 80),
        "curiosity_target": _plain(raw.get("curiosity_target"), 160) or context["question"],
        "promised_payoff": _plain(raw.get("promised_payoff"), 200),
        "protected_information": [_plain(item, 100) for item in raw.get("protected_information") or [] if _plain(item, 100)][:4] or protected,
        "production_feasibility": raw.get("production_feasibility") if raw.get("production_feasibility") in _FEASIBILITY else "real_media_likely",
        "rationale": _plain(raw.get("rationale"), 180),
    }
    candidate["payoff_fact_id"] = _payoff_fact({**raw, **candidate}, context)
    if not candidate["promised_payoff"] and candidate["payoff_fact_id"] is None:
        candidate["payoff_fact_id"] = context.get("primary_answer_id") or context.get("final_payoff_id")
    if not candidate["promised_payoff"]:
        candidate["promised_payoff"] = context["claims"].get(str(candidate["payoff_fact_id"] or ""), "")
    return candidate


# ---------------------------------------------------------------------------
# Deterministic checks (authoritative for safety)
# ---------------------------------------------------------------------------

def validate_on_screen(text: object, verbal: str, context: dict[str, Any], visual: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """The on-screen hook if it is short, complete, new and safe; else ``("", reason)``.

    Text is never truncated: an over-long text is omitted, because a cut
    phrase is worse than no text.  Existing overlay semantics stay the
    authority for completeness.
    """
    value = _plain(text, 120)
    if not value:
        return "", "no_supplementary_text"
    if _FRAGMENT.search(value) or value.count('"') % 2 or value.count("„") != value.count("“"):
        return "", "on_screen_fragment"
    words = re.findall(r"[\wÀ-ÖØ-öø-ÿ'-]+", value)
    if len(words) > MAX_ELEMENT_WORDS or len(value.rstrip("?!.")) > MAX_ELEMENT_CHARS:
        return "", "on_screen_too_long"
    if leaks(value, context, strict=True):
        return "", "on_screen_leak"
    own, spoken = _words(value), _words(verbal)
    new_numbers = _numbers(value) - _numbers(verbal)
    if (own and len(_related(own, spoken)) / len(own) >= 0.8 and not new_numbers) or value.casefold().rstrip("?!.") == verbal.casefold().rstrip("?!."):
        return "", "on_screen_duplicates_verbal"
    if assess_elements([value]):
        return "", "on_screen_fragment"
    anchors = context["arc_words"] | _words(context["topic"]) | _words(" ".join(str(v) for v in (visual or {}).values() if isinstance(v, str)))
    if not _related(own, anchors) and not _numbers(value):
        return "", "on_screen_generic"
    return value, None


def _visual_text(visual: dict[str, Any]) -> str:
    return " ".join(
        str(visual.get(key) or "")
        for key in ("subject", "action_state", "key_detail", "contrast", "tension", "visual_goal")
    ) + " " + " ".join(str(item) for item in visual.get("media_queries") or [])


def assess_candidate(candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Deterministic rubric: hard failures, penalties and 0..1 dimension scores."""
    hard: list[str] = []
    codes: list[str] = []
    verbal = candidate["verbal_hook"]
    visual = candidate["visual_hook"]
    strategy = candidate["strategy"]
    fmt = context["format"]
    facts = context["allowed_facts"]
    spoken = _words(verbal)

    # -- verbal ---------------------------------------------------------------
    issues = hook_issues(verbal, facts, body=context["body"], intent=context["intent"])
    for issue in issues:
        if issue in {"generic_clickbait", "personal_attack"}:
            hard.append("cheap_clickbait")
        elif issue in {"meta_language", "generic_meta_filler", "structural_label", "empty"}:
            hard.append("meta_language")
        elif issue in {"unsupported_statistic", "unsupported_prevalence", "unsupported_trend"}:
            hard.append("unsupported_claim")
        elif issue == "question_echo":
            hard.append("question_echo")
        else:
            codes.append(f"verbal_{issue}")
    if _CHEAP_BAIT.search(verbal):
        hard.append("cheap_clickbait")
    generic_opener = bool(_GENERIC_OPENER.search(verbal))
    if generic_opener:
        codes.append("generic_opener")
    reason = leaks(verbal, context)
    if reason:
        hard.append(f"verbal_{reason}")
    answer_words = _words(context["primary_answer"])
    states_answer = bool(spoken and answer_words and _overlap(spoken, answer_words) >= 0.75 and len(spoken & answer_words) >= 3)
    if states_answer:
        (hard if context["withhold"] else codes).append("states_primary_answer")
    first = _words(context["first_body"])
    repeats_first = bool(spoken and first and len(spoken & first) / len(spoken) >= 0.75)
    if repeats_first:
        # Identical to the first body sentence: the hook delivers that block and
        # the pipeline folds it (said once).  A paraphrase would be heard twice.
        same = " ".join(verbal.casefold().split()).rstrip(".!?") == " ".join(context["first_body"].casefold().split()).rstrip(".!?")
        (codes if same else hard).append("repeats_first_body")

    # -- visual ---------------------------------------------------------------
    subject = visual.get("subject") or visual.get("visual_goal") or ""
    queries = [query for query in visual.get("media_queries") or [] if _query_is_concrete(query, _semantic_query(query, _VISUAL_QUERY_STOP, limit=6))]
    abstract = bool(_ABSTRACT_VISUAL.search(subject)) or len(_words(subject)) < 1
    if abstract or not queries:
        hard.append("visual_vague")
    feasibility = _FEASIBILITY.get(candidate.get("production_feasibility") or "real_media_likely", 1.0)
    if feasibility == 0.0:
        hard.append("visual_infeasible")
    elif feasibility < 0.5:
        codes.append("visual_hard_to_source")
    if _NEEDS_TEXT.search(" ".join((subject, visual.get("action_state") or "", visual.get("key_detail") or ""))):
        codes.append("visual_needs_text")
        feasibility = min(feasibility, 0.4)
    reason = visual_leaks(visual, context)
    if reason:
        hard.append(f"visual_{reason}")
    if context["withhold"] and not reason:
        # Self-consistency across languages: the candidate's own protected
        # names (e.g. the English name of the answer) never appear in what it
        # searches for or shows.
        own = _words(" ".join(candidate.get("protected_information") or []))
        own -= _related(own, context["shared_words"])
        shown_tokens = _visual_query_tokens(_visual_text(visual) + " " + str(candidate.get("on_screen_hook") or ""))
        if own and any(_mentions(shown_tokens, {word}) for word in own):
            hard.append("visual_names_protected_answer")

    # -- on-screen ------------------------------------------------------------
    raw_text = candidate.get("on_screen_hook") or ""
    text, dropped = validate_on_screen(raw_text, verbal, context, visual)
    candidate["on_screen_hook"] = text
    candidate["on_screen_omitted_reason"] = dropped
    if dropped == "on_screen_leak":
        hard.append("on_screen_names_protected_answer")
    elif dropped and raw_text:
        codes.append(dropped)

    # -- payoff / story -------------------------------------------------------
    # The promise must be paid by what the story actually says: words the
    # question already contains prove nothing, the claims' own words do.
    arc_words = context["arc_words"]
    question_words = _words(context["question"]) | _words(context["topic"])
    claim_words = arc_words - question_words
    promise = _words(candidate.get("promised_payoff")) | _words(candidate.get("curiosity_target"))
    anchored = _related(spoken | promise, arc_words)
    promise_specific = _words(candidate.get("promised_payoff")) - _related(_words(candidate.get("promised_payoff")), question_words)
    unpaid_promise = bool(promise_specific) and not _related(promise_specific, claim_words)
    if not anchored or unpaid_promise:
        hard.append("payoff_mismatch")
    payoff_ids = {str(context.get("primary_answer_id") or ""), str(context.get("final_payoff_id") or "")}
    payoff_score = 1.0 if candidate.get("payoff_fact_id") in payoff_ids else 0.75 if candidate.get("payoff_fact_id") else 0.5

    # -- cross-modal complementarity -----------------------------------------
    seen = _words(" ".join(str(visual.get(key) or "") for key in ("subject", "action_state", "key_detail", "contrast", "tension")))
    adds_structure = bool(visual.get("key_detail") or visual.get("contrast") or visual.get("tension"))
    shown = _words(text)
    visual_new = seen - _related(seen, spoken)
    visual_restates = len(visual_new) < 2 and not adds_structure
    text_verbal = _overlap(shown, spoken) if shown else 0.0
    text_visual = _overlap(shown, seen) if shown else 0.0
    complementarity = 1.0
    if visual_restates:
        complementarity -= 0.4
        codes.append("visual_restates_verbal")
    if shown and text_verbal >= 0.5:
        complementarity -= 0.3
        codes.append("on_screen_overlaps_verbal")
    if shown and text_visual >= 0.8:
        complementarity -= 0.2
        codes.append("on_screen_restates_visual")
    if raw_text and dropped == "on_screen_duplicates_verbal":
        complementarity -= 0.3
        if visual_restates or not adds_structure:
            # The text repeats the voice and the image adds no detail,
            # contrast or tension of its own: one statement three times.
            hard.append("redundant_channels")
    elif shown and visual_restates and text_verbal >= 0.5:
        hard.append("redundant_channels")
    if shown and (_numbers(text) - _numbers(verbal) or ("?" in text) != ("?" in verbal) or shown - _related(shown, spoken)):
        complementarity += 0.1

    # -- format ---------------------------------------------------------------
    preferred = FORMAT_STRATEGIES.get(fmt, FORMAT_STRATEGIES["explanation"])
    format_fit = 1.0 if strategy in preferred else 0.6
    if fmt == "quiz" and "?" not in verbal and strategy != "challenge_question":
        format_fit = min(format_fit, 0.4)
        codes.append("quiz_without_challenge")
    if fmt in {"explanation", "misconception_correction"} and generic_opener:
        format_fit = min(format_fit, 0.2)

    # -- dimension scores (0..1) ---------------------------------------------
    concrete = _related(spoken, set().union(*(_words(fact.get("claim")) for fact in facts)) | _words(context["topic"])) if spoken else set()
    word_count = len(verbal.split())
    sentences = len([part for part in re.split(r"[.!?]+", verbal) if part.strip()])
    curiosity = 0.5 + (0.2 if _CONTRAST.search(verbal) else 0.0) + (0.15 if concrete else 0.0) + (0.1 if strategy in preferred else 0.0)
    curiosity -= 0.3 if generic_opener else 0.0
    verbal_quality = 1.0 - (0.4 if generic_opener else 0) - (0.4 if repeats_first else 0) - (0.5 if states_answer else 0)
    verbal_quality -= 0.3 if "verbal_too_long" in codes else 0
    verbal_quality -= 0.2 if "verbal_jargon_first" in codes else 0
    comprehension = 1.0 - max(0, word_count - 16) * 0.08 - (0.2 if sentences > 2 else 0) - (0.3 if "verbal_jargon_first" in codes else 0)
    filled = sum(1 for key in ("subject", "action_state", "framing", "key_detail", "motion") if visual.get(key)) + (1 if visual.get("contrast") or visual.get("tension") else 0)
    intrigue = 0.3 + 0.1 * filled + (0.1 if _CLOSE_FRAMING.search(visual.get("framing") or "") else 0)
    novelty = 0.6 + (0.3 if _related(spoken, context["distinctive_words"]) else 0) - (0.3 if generic_opener else 0)
    on_screen_quality = 1.0 if text else (0.5 if not raw_text or dropped == "no_supplementary_text" else 0.2)
    scores = {
        "curiosity": curiosity,
        "specificity": min(1.0, 0.25 + len(concrete) / 4 + (0.15 if _numbers(verbal) else 0)),
        "comprehension": comprehension,
        "visual_intrigue": intrigue,
        "visual_feasibility": feasibility if queries else 0.0,
        "verbal_quality": verbal_quality,
        "on_screen_quality": on_screen_quality,
        "complementarity": complementarity,
        "story_alignment": 1.0 if anchored and "states_primary_answer" not in hard else 0.3,
        "payoff_alignment": payoff_score if anchored else 0.0,
        "reveal_safety": 0.0 if any(code.endswith(_REVEAL_CODES) for code in hard) else 1.0,
        "format_fit": format_fit,
        "novelty": novelty,
        "credibility": 0.0 if "unsupported_claim" in hard else 1.0,
        "clickbait_free": 0.0 if "cheap_clickbait" in hard else (0.6 if generic_opener else 1.0),
        "production_feasibility": (feasibility + (1.0 if text or not raw_text else 0.6)) / 2 if queries else 0.2,
    }
    scores = {key: round(max(0.0, min(1.0, value)), 3) for key, value in scores.items()}
    return {"hard_fail": list(dict.fromkeys(hard)), "reason_codes": list(dict.fromkeys(codes)), "dimensions": scores}


def _weighted(dimensions: dict[str, float]) -> float:
    total = sum(_WEIGHTS.values())
    return 100 * sum(_WEIGHTS[key] * float(dimensions.get(key, 0.0)) for key in DIMENSIONS) / total


def blend_judgement(assessment: dict[str, Any], judgement: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic scores + the judge's rubric (the judge can veto, never un-veto)."""
    dimensions = dict(assessment["dimensions"])
    hard = list(assessment["hard_fail"])
    codes = list(assessment["reason_codes"])
    if isinstance(judgement, dict):
        for key in DIMENSIONS:
            value = judgement.get(key)
            if not isinstance(value, (int, float)):
                continue
            judged = max(0.0, min(1.0, float(value) / 10))
            dimensions[key] = round(min(dimensions[key], judged) if key in _SAFETY else 0.4 * dimensions[key] + 0.6 * judged, 3)
        veto = str(judgement.get("veto") or "none")
        if veto in _JUDGE_VETOES:
            hard.append(f"judge_{veto}")
        codes.extend(re.sub(r"[^a-z0-9_]", "", str(code).casefold())[:40] for code in judgement.get("reason_codes") or [] if str(code).strip())
    score = 0.0 if hard else _weighted(dimensions)
    return {
        "hard_fail": list(dict.fromkeys(hard)),
        "reason_codes": list(dict.fromkeys(code for code in codes if code))[:8],
        "dimensions": dimensions,
        "score": round(score, 1),
        "eligible": not hard,
    }


# ---------------------------------------------------------------------------
# Deterministic candidates (no provider)
# ---------------------------------------------------------------------------

def planner_visual(intent: dict[str, Any] | None) -> dict[str, Any] | None:
    """A planner visual intent as structured hook visual semantics."""
    if not isinstance(intent, dict) or intent.get("source") == "narration_fallback":
        return None
    objects = [_plain(item, 100) for item in intent.get("objects") or [] if _plain(item, 100)]
    actions = [_plain(item, 100) for item in intent.get("actions") or [] if _plain(item, 100)]
    context = [_plain(item, 100) for item in intent.get("context") or [] if _plain(item, 100)]
    subject = _plain(intent.get("visual_goal"), 120) or ", ".join(objects[:2])
    if not subject or not intent.get("media_queries"):
        return None
    return {
        "objects": objects,
        "actions": actions,
        "context": context,
        "subject": subject,
        "action_state": ", ".join(actions[:2]),
        "key_detail": ", ".join(context[:1]),
        "framing": "",
        "visual_goal": _plain(intent.get("visual_goal"), 220),
        "must_not_show": list(intent.get("must_not_show") or []),
        "media_queries": list(intent.get("media_queries") or [])[:4],
        "media_query_targets": list(intent.get("media_query_targets") or [])[:4],
    }


def _on_screen_options(context: dict[str, Any]) -> list[str]:
    """Supplementary text only from structure: a stated scale or an open A-vs-B."""
    from .visual_director import salient_number

    options: list[str] = []
    for fact in context["allowed_facts"]:
        number = salient_number(_plain(fact.get("claim")))
        if number and number.get("label"):
            options.append(f"{number['value']} {number['label']}")
    sides = context.get("sides") or []
    if context["format"] in {"comparison", "quiz"} and len(sides) == 2:
        question = context["question"]
        names = []
        for side in sides:
            match = next((word for word in re.findall(r"[\wÄÖÜäöüß-]+", question) if _mentions(_visual_query_tokens(word), side)), None)
            if match:
                names.append(match)
        if len(names) == 2:
            options.append(f"{names[0]} vs. {names[1]}?")
    return list(dict.fromkeys(options))


def deterministic_candidates(
    context: dict[str, Any],
    *,
    baseline: HookCandidate | None,
    visuals: list[dict[str, Any]],
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Complete triples from the existing verbal library and the planner's visuals."""
    verbal: list[tuple[str, str]] = []
    if baseline is not None:
        verbal.append((baseline.strategy, baseline.text))
    try:
        library = generate_hook_candidates(context["intent"], context["allowed_facts"], body=context["body"])
    except Exception:  # noqa: BLE001 - the library is enrichment here
        library = []
    for item in sorted(library, key=lambda item: -item.score):
        verbal.append((item.strategy, item.text))
    seen: set[str] = set()
    texts = _on_screen_options(context)
    candidates: list[dict[str, Any]] = []
    for strategy, text in verbal:
        key = " ".join(text.casefold().split())
        if not text or key in seen or leaks(text, context):
            continue
        seen.add(key)
        spoken = _words(text)
        ranked = sorted(visuals, key=lambda item: -_overlap(spoken, _words(_visual_text(item))))
        visual = next((item for item in ranked if not visual_leaks(item, context)), None)
        if visual is None:
            continue
        on_screen = next((option for option in texts if validate_on_screen(option, text, context, visual)[0]), "")
        raw = {
            "strategy": _FROM_V1.get(strategy, "concrete_anomaly"),
            "verbal_hook": text,
            "visual": visual,
            "on_screen_hook": on_screen,
            "rationale": "Deterministic: existing verbal hook library with the planner's visual intent.",
        }
        candidate = normalise_candidate(raw, offset + len(candidates), context, origin="deterministic")
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= CANDIDATE_TARGET:
            break
    return candidates


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _distinct(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop near-paraphrases: distinct candidates must be different approaches."""
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        words = _words(candidate["verbal_hook"])
        if any(_overlap(words, _words(other["verbal_hook"])) >= 0.8 and other["strategy"] == candidate["strategy"] for other in kept):
            continue
        if any(" ".join(candidate["verbal_hook"].casefold().split()) == " ".join(other["verbal_hook"].casefold().split()) for other in kept):
            continue
        kept.append(candidate)
    return kept


def _judge_payload(context: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    story = {
        "language": context["language"],
        "format": context["format"],
        "primary_question": context["question"],
        "primary_answer": context["primary_answer"],
        "final_payoff": context["final_payoff"],
        "secondary_insights": context["secondary"][:3],
        "withhold_answer": context["withhold"],
        "hook_must_not_reveal": context["protected_label"] if context["withhold"] else "",
        "first_body_sentence": context["first_body"],
    }
    items = [
        {
            "candidate_id": candidate["id"],
            "strategy": candidate["strategy"],
            "verbal_hook": candidate["verbal_hook"],
            "visual": {key: candidate["visual_hook"].get(key) for key in ("subject", "action_state", "framing", "key_detail", "contrast", "motion", "tension", "media_queries")},
            "on_screen_hook": candidate["on_screen_hook"],
            "promised_payoff": candidate["promised_payoff"],
            "production_feasibility": candidate["production_feasibility"],
        }
        for candidate in candidates
    ]
    return story, items


def _compact(candidate: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    visual = candidate["visual_hook"]
    return {
        "id": candidate["id"],
        "origin": candidate["origin"],
        "strategy": candidate["strategy"],
        "verbal_hook": candidate["verbal_hook"],
        "on_screen_hook": candidate["on_screen_hook"],
        "on_screen_omitted_reason": candidate.get("on_screen_omitted_reason"),
        "visual_summary": visual_summary(visual),
        "promised_payoff": candidate["promised_payoff"],
        "payoff_fact_id": candidate.get("payoff_fact_id"),
        "score": result["score"],
        "eligible": result["eligible"],
        "hard_fail": result["hard_fail"],
        "reason_codes": result["reason_codes"],
        "dimensions": result["dimensions"],
    }


def visual_summary(visual: dict[str, Any] | None) -> str:
    visual = visual if isinstance(visual, dict) else {}
    parts = [visual.get("framing"), visual.get("subject") or visual.get("visual_goal"), visual.get("action_state"), visual.get("key_detail")]
    return _plain(", ".join(str(part) for part in parts if part), 200)


def plan_triple_hook(
    *,
    intent: dict[str, Any],
    facts: list[dict[str, Any]],
    story_arc: dict[str, Any] | None,
    payoff_plan: dict[str, Any] | None,
    format_plan: dict[str, Any] | None,
    novelty_plan: dict[str, Any] | None,
    body_blocks: list[dict[str, Any]],
    baseline: HookCandidate | None,
    generation: Any | None,
    judge: Callable[..., Any] | None,
    settings: Any | None,
    reaction: str | None = None,
    planner_visuals: list[dict[str, Any]] | None = None,
    protected_target: str | None = None,
) -> dict[str, Any]:
    """Select the authoritative triple hook (the persisted ``script.triple_hook``)."""
    context = hook_context(
        intent, facts, story_arc=story_arc, payoff_plan=payoff_plan, format_plan=format_plan,
        novelty_plan=novelty_plan, body_blocks=body_blocks, protected_target=protected_target,
    )
    generation_status = str(getattr(generation, "status", "") or "not_called")
    raw_ai = list(getattr(generation, "triple_candidates", None) or [])
    legacy = getattr(generation, "triple_hook", None)
    if not raw_ai and isinstance(legacy, dict) and isinstance(legacy.get("visual_hook"), dict) and baseline is not None:
        # A V1-shaped provider answer: one visual/text for the chosen verbal hook.
        raw_ai = [{
            "strategy": baseline.strategy, "verbal_hook": baseline.text,
            "visual": {**legacy["visual_hook"], "subject": ", ".join(legacy["visual_hook"].get("subjects_to_show") or []) or legacy["visual_hook"].get("visual_goal")},
            "on_screen_hook": legacy.get("on_screen_text_hook") or "",
        }]
    candidates = [
        candidate for index, raw in enumerate(raw_ai[:5])
        if (candidate := normalise_candidate(raw, index, context, origin="ai")) is not None
    ]
    candidates = _distinct(candidates)[:CANDIDATE_TARGET]
    visuals = [visual for visual in (planner_visual(item) for item in planner_visuals or []) if visual]
    assessed = {candidate["id"]: assess_candidate(candidate, context) for candidate in candidates}
    eligible_ai = sum(1 for result in assessed.values() if not result["hard_fail"])
    if eligible_ai < CANDIDATE_TARGET and visuals:
        # Top up with deterministic complete triples so a failing provider
        # candidate never leaves the opening without a real choice.
        extra = deterministic_candidates(context, baseline=baseline, visuals=visuals, offset=len(candidates))
        extra = [item for item in _distinct(candidates + extra) if item["origin"] == "deterministic"]
        for candidate in extra[: MAX_POOL - len(candidates)]:
            candidates.append(candidate)
            assessed[candidate["id"]] = assess_candidate(candidate, context)
    judge_status, judge_error, judgements = "not_called", None, {}
    pending = [candidate for candidate in candidates if not assessed[candidate["id"]]["hard_fail"]]
    if judge is not None and settings is not None and generation_status == "connected" and len(pending) >= 2:
        story, items = _judge_payload(context, pending)
        try:
            verdict = judge(story, items, settings)
            judge_status = str(getattr(verdict, "status", "") or "provider_error")
            judge_error = getattr(verdict, "error", None)
            judgements = {str(item.get("candidate_id")): item for item in getattr(verdict, "judgements", None) or [] if isinstance(item, dict)}
        except Exception as exc:  # noqa: BLE001 - the deterministic rubric still decides
            judge_status, judge_error = "provider_error", str(exc)[:240]
    results = {candidate["id"]: blend_judgement(assessed[candidate["id"]], judgements.get(candidate["id"])) for candidate in candidates}
    ranked = sorted(
        (candidate for candidate in candidates if results[candidate["id"]]["eligible"]),
        key=lambda candidate: (-results[candidate["id"]]["score"], candidate["origin"] != "ai", candidate["id"]),
    )
    selection = {
        "version": VERSION,
        "generation": {"status": generation_status, "error": getattr(generation, "error", None), "candidate_count": len(raw_ai)},
        "judge": {"status": judge_status, "error": judge_error},
        "candidate_count": len(candidates),
        "eligible_count": len(ranked),
        "candidates": [_compact(candidate, results[candidate["id"]]) for candidate in candidates],
    }
    story_brief = {
        "primary_question": context["question"],
        "primary_answer_id": context["primary_answer_id"],
        "final_payoff_id": context["final_payoff_id"],
        "withhold_answer": context["withhold"],
    }
    if ranked:
        winner = ranked[0]
        result = results[winner["id"]]
        return {
            "version": VERSION,
            "status": "connected" if winner["origin"] == "ai" else "deterministic",
            "hook_id": winner["id"],
            "source": winner["origin"],
            "verbal_hook": winner["verbal_hook"],
            "visual_hook": winner["visual_hook"],
            "on_screen_text_hook": winner["on_screen_hook"],
            "on_screen_hook_status": "shown" if winner["on_screen_hook"] else "omitted",
            "on_screen_omitted_reason": winner.get("on_screen_omitted_reason") if not winner["on_screen_hook"] else None,
            "selected_strategy": winner["strategy"],
            "legacy_strategy": _TO_V1.get(winner["strategy"], "evidence_insight"),
            "curiosity_target": winner["curiosity_target"],
            "promised_payoff": winner["promised_payoff"],
            "payoff_fact_id": winner.get("payoff_fact_id"),
            "protected_information": winner["protected_information"],
            "intended_reaction": reaction or (payoff_plan or {}).get("desired_viewer_reaction") or "curiosity",
            "format": context["format"],
            "score": result["score"],
            "reason_codes": result["reason_codes"],
            "rationale": winner["rationale"],
            "reveal_contract": story_brief,
            "selection": {**selection, "selected_id": winner["id"]},
        }
    return fallback_plan(context, baseline, reaction=reaction, format_plan=format_plan, payoff_plan=payoff_plan, selection=selection, visuals=visuals)


def fallback_plan(
    context: dict[str, Any],
    baseline: HookCandidate | None,
    *,
    reaction: str | None,
    format_plan: dict[str, Any] | None,
    payoff_plan: dict[str, Any] | None,
    selection: dict[str, Any],
    visuals: list[dict[str, Any]],
) -> dict[str, Any]:
    """No complete candidate passed: keep the deterministic verbal hook, no guessed text."""
    verbal = baseline.text if baseline is not None else ""
    legacy = fallback_triple_hook(context["intent"], payoff_plan or {}, verbal, baseline.strategy if baseline else None, reaction, format_plan)
    visual_source = next((item for item in visuals if not visual_leaks(item, context)), None)
    visual = visual_intent(
        visual_source or {**legacy["visual_hook"], "subject": ", ".join(legacy["visual_hook"].get("subjects_to_show") or [])},
        must_not_show=[context["protected_label"]] if context["withhold"] and context["protected_label"] else [],
        hook_id="hook_fallback",
        source=SOURCE if visual_source else f"{SOURCE}_fallback",
    )
    text = ""
    reason = "no_candidate_passed"
    for option in _on_screen_options(context):
        text = validate_on_screen(option, verbal, context, visual)[0]
        if text:
            reason = None
            break
    return {
        "version": VERSION,
        "status": "fallback",
        "hook_id": "hook_fallback",
        "source": "fallback",
        "verbal_hook": _clean(verbal),
        "visual_hook": visual,
        "on_screen_text_hook": text,
        "on_screen_hook_status": "shown" if text else "omitted",
        "on_screen_omitted_reason": reason if not text else None,
        "selected_strategy": _FROM_V1.get(baseline.strategy, "concrete_anomaly") if baseline else "concrete_anomaly",
        "legacy_strategy": baseline.strategy if baseline else None,
        "curiosity_target": context["question"],
        "promised_payoff": context["primary_answer"] or context["final_payoff"],
        "payoff_fact_id": context["primary_answer_id"],
        "protected_information": [context["protected_label"]] if context["withhold"] and context["protected_label"] else [],
        "intended_reaction": reaction or (payoff_plan or {}).get("desired_viewer_reaction") or "curiosity",
        "format": context["format"],
        "score": None,
        "reason_codes": ["fallback_no_complete_candidate"],
        "rationale": "",
        "reveal_contract": {
            "primary_question": context["question"],
            "primary_answer_id": context["primary_answer_id"],
            "final_payoff_id": context["final_payoff_id"],
            "withhold_answer": context["withhold"],
        },
        "selection": {**selection, "selected_id": None},
    }


def selected_hook_candidate(plan: dict[str, Any]) -> HookCandidate | None:
    text = _clean(plan.get("verbal_hook"))
    if not text:
        return None
    return HookCandidate(str(plan.get("legacy_strategy") or "evidence_insight"), text, float(plan.get("score") or 0.0), f"Triple Hook V2 {plan.get('hook_id')}.")


# ---------------------------------------------------------------------------
# Consumers: Visual Director, attention planning, Final Video Critic
# ---------------------------------------------------------------------------

def is_v2(plan: object) -> bool:
    return isinstance(plan, dict) and int(plan.get("version") or 0) >= VERSION


def state_plan(state: dict[str, Any]) -> dict[str, Any] | None:
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    plan = script.get("triple_hook")
    return plan if is_v2(plan) else None


def hook_block_id(state: dict[str, Any]) -> str | None:
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    block = next((item for item in script.get("blocks") or [] if isinstance(item, dict) and str(item.get("role") or "").casefold() == "hook"), None)
    return str(block.get("id") or "") or None if block else None


def is_hook_scene(scene: dict[str, Any], state: dict[str, Any]) -> bool:
    block = hook_block_id(state)
    return bool(block) and str(scene.get("block_id") or "") == block


def hook_overlay_spec(state: dict[str, Any]) -> dict[str, Any] | None:
    """The on-screen hook as a small label overlay for the opening scene(s)."""
    plan = state_plan(state)
    if plan is None:
        return None
    text = _plain(plan.get("on_screen_text_hook"), 60)
    if not text or len(text.rstrip("?!.")) > MAX_ELEMENT_CHARS:
        return None
    return {"kind": "label", "text": text, "source": SOURCE, "hook_id": plan.get("hook_id")}


def hook_text_leaks(state: dict[str, Any], text: object) -> str | None:
    """Reveal check for rendered/narrated opening text against the persisted plans."""
    context = hook_context(
        state.get("intent") or {}, list(state.get("facts") or []), story_arc=state.get("story_arc"),
        payoff_plan=state.get("payoff_plan"), format_plan=state.get("format_plan"), novelty_plan=None,
        body_blocks=[block for block in (state.get("script") or {}).get("blocks") or [] if isinstance(block, dict)],
    )
    return leaks(text, context)


def validate_hook_text(text: object, spoken: str, state: dict[str, Any]) -> str | None:
    """Why a drawn on-screen hook fails against what is heard (``None`` = fine)."""
    context = hook_context(
        state.get("intent") or {}, list(state.get("facts") or []), story_arc=state.get("story_arc"),
        payoff_plan=state.get("payoff_plan"), format_plan=state.get("format_plan"), novelty_plan=None,
        body_blocks=[block for block in (state.get("script") or {}).get("blocks") or [] if isinstance(block, dict)],
    )
    return validate_on_screen(text, spoken, context)[1]
