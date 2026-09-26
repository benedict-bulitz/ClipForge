"""Triple Hook V2: the opening as one coordinated verbal / visual / on-screen unit.

Extends the existing hook path (``script.triple_hook`` is the one persisted
plan) instead of adding a parallel hook system:

1. ONE bounded provider call proposes complete candidates.  Their spoken hook
   must use a documented strategy (``hooks.CANONICAL_STRATEGIES``) that the
   research supports (``verbal_hook.strategy_signals``); without a provider,
   document-based candidates come from ``verbal_hook`` and are paired with
   the planner's own visual intents.
2. Every candidate is checked deterministically: the verbal hook against the
   document's rubric (``verbal_hook.assess_verbal``), plus reveal safety of
   every channel, payoff alignment, visual concreteness, on-screen text
   validity and cross-modal redundancy.  These checks are authoritative for
   safety: a leaking, redundant or unpayable candidate cannot win.
3. ONE bounded judge call (``ai.judge_triple_hooks_with_openai``) scores the
   remaining complete triples on the same rubric; its scores are blended
   with the deterministic ones.  Only short reason codes are persisted.

The selected triple drives the real opening: its verbal hook becomes the hook
block (TTS remains the timing authority), its visual becomes the opening
scene's visual intent (queries, verification, crop, generated-image prompt)
and its on-screen hook becomes a ``label`` overlay drawn by the renderer.

No topic vocabulary lives here: only language grammar and structural rules
per selected format.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .hooks import CANONICAL_STRATEGIES, HookCandidate, canonical_strategy
from .media import (
    _VISUAL_QUERY_STOP,
    _mentions,
    _query_is_concrete,
    _semantic_query,
    _visual_query_tokens,
    visual_target_key,
)
from .overlay_copy import MAX_ELEMENT_CHARS, MAX_ELEMENT_WORDS, assess_elements
from .payoff import fallback_triple_hook
from .verbal_hook import (
    REVEAL_CODES,
    VERBAL_DIMENSIONS,
    VERBAL_WEIGHTS,
    _clean,
    _numbers,
    _overlap,
    _plain,
    _related,
    _words,
    assess_verbal,
    hook_context,
    leaks,
    select_verbal,
)
from .verbal_hook import deterministic_candidates as verbal_candidates

VERSION = 2
SOURCE = "triple_hook_v2"
CANDIDATE_TARGET = 4
MAX_CANDIDATES = 5
MAX_POOL = 6

# The spoken hook's strategy is always a documented canonical strategy.
STRATEGIES = CANONICAL_STRATEGIES
DIMENSIONS = (
    *VERBAL_DIMENSIONS,
    "visual_intrigue", "visual_feasibility", "on_screen_quality", "complementarity", "payoff_alignment",
    "reveal_safety", "format_fit", "clickbait_free", "production_feasibility",
)
_WEIGHTS = {
    **VERBAL_WEIGHTS,
    "visual_intrigue": 0.8, "visual_feasibility": 1.0, "on_screen_quality": 0.6, "complementarity": 1.3,
    "payoff_alignment": 1.2, "reveal_safety": 1.0, "format_fit": 0.9, "clickbait_free": 0.8,
    "production_feasibility": 0.8,
}
# Safety dimensions: the judge may lower them, never raise them above the checks.
_SAFETY = {"reveal_safety", "factual_defensibility", "clickbait_free"}
_JUDGE_VETOES = {"leaks_answer", "payoff_mismatch", "impossible_visual", "redundant_channels", "cheap_clickbait", "contradicts_story"}
_REVEAL_CODES = REVEAL_CODES
_FEASIBILITY = {"real_media_likely": 1.0, "generated_image_ok": 0.8, "hard_to_source": 0.4, "impossible": 0.0}

_ABSTRACT_VISUAL = re.compile(
    r"(?i)\b(?:concept|idea|science|scientific|interesting|information|topic|theme|explanation|knowledge|abstract|"
    r"phenomenon|mystery|konzept|idee|wissenschaft\w*|interessant\w*|thema|erklärung|wissen|fakt|phänomen|rätsel)\b"
)
_NEEDS_TEXT = re.compile(
    r"(?i)\b(?:text|caption|headline|title card|label(?:ed|led|s)?|words?|letters|schrift|beschriftung|"
    r"überschrift|infographic|diagram|chart|logo)\b"
)
_CLOSE_FRAMING = re.compile(r"(?i)\b(?:close[- ]?up|macro|detail|nahaufnahme|makro|extreme)\b")
_FRAGMENT = re.compile(r"(?:\.\.\.|…|\w-\s*$|^\s*[-–—,;:])")


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
        # A planner visual borrowed for the hook keeps the facts it depicts.
        "source_fact_ids": [str(value) for value in visual.get("source_fact_ids") or []],
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
    strategy = canonical_strategy(raw.get("strategy"))
    if strategy is None:
        # Only documented strategies exist; an invented label is never guessed.
        return None
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
        "supported_by_fact_ids": [_plain(item, 16) for item in raw.get("supported_by_fact_ids") or [] if _plain(item, 16)][:4],
        "provider_reason_codes": [re.sub(r"[^a-z0-9_]", "", str(code).casefold())[:40] for code in raw.get("reason_codes") or [] if str(code).strip()][:5],
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
    fmt = context["format"]
    spoken = _words(verbal)

    # -- verbal: the document's strategy and rubric (one authority) -----------
    verbal_result = assess_verbal(
        verbal, candidate["strategy"], context, fact_ids=candidate.get("supported_by_fact_ids"),
        emergency=candidate.get("origin") == "emergency",
    )
    hard.extend(verbal_result["hard_fail"])
    codes.extend(verbal_result["reason_codes"])
    candidate["supported_by_fact_ids"] = verbal_result["supported_by_fact_ids"]
    candidate["positive_codes"] = verbal_result["positive_codes"]
    generic_opener = "generic_opener" in codes

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

    # -- format (structural rules only; no strategy is preferred by name) ----
    format_fit = 1.0
    if fmt == "quiz" and "?" not in verbal and not re.search(r"(?i)\b(?:du|you|rate|guess)\b", verbal):
        format_fit = 0.4
        codes.append("quiz_without_challenge")
    if context["withhold"] and len(context.get("sides") or []) == 2 and not _related(spoken, context["question_words"] | context["arc_words"]):
        format_fit = min(format_fit, 0.6)
        codes.append("comparison_not_framed")
    if fmt in {"explanation", "misconception_correction"} and generic_opener:
        format_fit = min(format_fit, 0.2)

    # -- dimension scores (0..1) ---------------------------------------------
    filled = sum(1 for key in ("subject", "action_state", "framing", "key_detail", "motion") if visual.get(key)) + (1 if visual.get("contrast") or visual.get("tension") else 0)
    intrigue = 0.3 + 0.1 * filled + (0.1 if _CLOSE_FRAMING.search(visual.get("framing") or "") else 0)
    on_screen_quality = 1.0 if text else (0.5 if not raw_text or dropped == "no_supplementary_text" else 0.2)
    scores = {
        **verbal_result["dimensions"],
        "visual_intrigue": intrigue,
        "visual_feasibility": feasibility if queries else 0.0,
        "on_screen_quality": on_screen_quality,
        "complementarity": complementarity,
        "payoff_alignment": payoff_score if anchored else 0.0,
        "reveal_safety": 0.0 if any(code.endswith(_REVEAL_CODES) for code in hard) else 1.0,
        "format_fit": format_fit,
        "clickbait_free": 0.0 if {"cheap_clickbait", "fake_controversy", "unnecessary_provocation"} & set(hard) else (0.6 if generic_opener else 1.0),
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
        "source_fact_ids": [str(value) for value in intent.get("source_fact_ids") or []],
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
    visuals: list[dict[str, Any]],
    planner_hook: dict[str, Any] | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Complete triples: document-based verbal hooks with the planner's own visuals."""
    texts = _on_screen_options(context)
    candidates: list[dict[str, Any]] = []
    for verbal in verbal_candidates(context, planner_hook=planner_hook):
        spoken = _words(verbal["text"])
        ranked = sorted(visuals, key=lambda item: -_overlap(spoken, _words(_visual_text(item))))
        visual = next((item for item in ranked if not visual_leaks(item, context)), None)
        if visual is None:
            continue
        on_screen = next((option for option in texts if validate_on_screen(option, verbal["text"], context, visual)[0]), "")
        raw = {
            "strategy": verbal["strategy"],
            "verbal_hook": verbal["text"],
            "visual": visual,
            "on_screen_hook": on_screen,
            "supported_by_fact_ids": verbal["supported_by_fact_ids"],
            "reason_codes": verbal["reason_codes"],
            "rationale": "Deterministic: documented strategy applied to the research, with the planner's visual intent.",
        }
        candidate = normalise_candidate(raw, offset + len(candidates), context, origin=verbal.get("origin") or "deterministic")
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= MAX_CANDIDATES:
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
        "supported_strategies": {name: entry["signals"] for name, entry in context["signals"].items() if entry["viable"]},
    }
    items = [
        {
            "candidate_id": candidate["id"],
            "strategy": candidate["strategy"],
            "verbal_hook": candidate["verbal_hook"],
            "supported_by_fact_ids": candidate.get("supported_by_fact_ids") or [],
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
        "supported_by_fact_ids": candidate.get("supported_by_fact_ids") or [],
        "positive_codes": candidate.get("positive_codes") or [],
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
    generation: Any | None,
    judge: Callable[..., Any] | None,
    settings: Any | None,
    reaction: str | None = None,
    planner_visuals: list[dict[str, Any]] | None = None,
    protected_target: str | None = None,
    planner_hook: dict[str, Any] | None = None,
    baseline: HookCandidate | None = None,
) -> dict[str, Any]:
    """Select the authoritative triple hook (the persisted ``script.triple_hook``).

    ``planner_hook`` (or ``baseline``) is a hook the planner already wrote with
    a documented strategy; it only competes as one more candidate.
    """
    if planner_hook is None and baseline is not None:
        planner_hook = {"strategy": baseline.strategy, "text": baseline.text}
    context = hook_context(
        intent, facts, story_arc=story_arc, payoff_plan=payoff_plan, format_plan=format_plan,
        novelty_plan=novelty_plan, body_blocks=body_blocks, protected_target=protected_target,
    )
    generation_status = str(getattr(generation, "status", "") or "not_called")
    raw_ai = list(getattr(generation, "triple_candidates", None) or [])
    legacy = getattr(generation, "triple_hook", None)
    visuals = [visual for visual in (planner_visual(item) for item in planner_visuals or []) if visual]
    legacy_verbal = [item for item in getattr(generation, "candidates", None) or [] if isinstance(item, dict) and str(item.get("text") or "").strip()]
    if not raw_ai and legacy_verbal:
        # A V1-shaped provider answer: verbal candidates plus at most one
        # visual/text; the planner's visual stands in when none was given.
        legacy = legacy if isinstance(legacy, dict) else {}
        hook_visual = legacy.get("visual_hook") if isinstance(legacy.get("visual_hook"), dict) else None
        legacy_visual = (
            {**hook_visual, "subject": ", ".join(hook_visual.get("subjects_to_show") or []) or hook_visual.get("visual_goal")}
            if hook_visual else (visuals[0] if visuals else None)
        )
        if legacy_visual is not None:
            raw_ai = [
                {"strategy": item.get("strategy"), "verbal_hook": item.get("text"), "visual": legacy_visual, "on_screen_hook": legacy.get("on_screen_text_hook") or ""}
                for item in legacy_verbal
            ]
    normalised = [normalise_candidate(raw, index, context, origin="ai") for index, raw in enumerate(raw_ai[:MAX_CANDIDATES])]
    rejected_strategies = sorted({str(raw.get("strategy")) for raw, item in zip(raw_ai, normalised, strict=False) if item is None and isinstance(raw, dict)})
    candidates = _distinct([item for item in normalised if item is not None])[:MAX_CANDIDATES]
    assessed = {candidate["id"]: assess_candidate(candidate, context) for candidate in candidates}
    eligible_ai = sum(1 for result in assessed.values() if not result["hard_fail"])
    if eligible_ai < CANDIDATE_TARGET and visuals:
        # Top up with deterministic complete triples so a failing provider
        # candidate never leaves the opening without a real choice.
        extra = deterministic_candidates(context, visuals=visuals, planner_hook=planner_hook, offset=len(candidates))
        extra = [item for item in _distinct(candidates + extra) if item["origin"] != "ai"]
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
        "generation": {
            "status": generation_status, "error": getattr(generation, "error", None), "candidate_count": len(raw_ai),
            "rejected_non_document_strategies": rejected_strategies,
        },
        "supported_strategies": {name: {"fact_ids": entry["fact_ids"], "signals": entry["signals"]} for name, entry in context["signals"].items() if entry["viable"]},
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
            "legacy_strategy": winner["strategy"],
            "supported_by_fact_ids": winner.get("supported_by_fact_ids") or [],
            "curiosity_target": winner["curiosity_target"],
            "promised_payoff": winner["promised_payoff"],
            "payoff_fact_id": winner.get("payoff_fact_id"),
            "protected_information": winner["protected_information"],
            "intended_reaction": reaction or (payoff_plan or {}).get("desired_viewer_reaction") or "curiosity",
            "format": context["format"],
            "score": result["score"],
            "reason_codes": list(dict.fromkeys([*(winner.get("positive_codes") or []), *result["reason_codes"]]))[:10],
            "rationale": winner["rationale"],
            "reveal_contract": story_brief,
            "selection": {**selection, "selected_id": winner["id"]},
        }
    # No complete triple passed: the best document-based verbal hook still
    # leads (a provider hook whose visual failed competes with the research).
    extra = [
        {"strategy": item["strategy"], "text": item["verbal_hook"], "supported_by_fact_ids": item.get("supported_by_fact_ids") or [], "reason_codes": [], "origin": item["origin"]}
        for item in candidates
    ] + [
        {"strategy": item.get("strategy"), "text": item.get("text"), "supported_by_fact_ids": [], "reason_codes": ["provider_verbal"], "origin": "ai"}
        for item in legacy_verbal if canonical_strategy(item.get("strategy"))
    ]
    verbal = select_verbal(context, extra=extra, planner_hook=planner_hook)
    return fallback_plan(context, verbal, reaction=reaction, format_plan=format_plan, payoff_plan=payoff_plan, selection=selection, visuals=visuals)


def fallback_plan(
    context: dict[str, Any],
    verbal: dict[str, Any] | None,
    *,
    reaction: str | None,
    format_plan: dict[str, Any] | None,
    payoff_plan: dict[str, Any] | None,
    selection: dict[str, Any],
    visuals: list[dict[str, Any]],
) -> dict[str, Any]:
    """No complete triple passed: a document-based verbal hook, no guessed text."""
    text_verbal = _clean((verbal or {}).get("text"))
    strategy = (verbal or {}).get("strategy")
    legacy = fallback_triple_hook(context["intent"], payoff_plan or {}, text_verbal, strategy, reaction, format_plan)
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
        text = validate_on_screen(option, text_verbal, context, visual)[0]
        if text:
            reason = None
            break
    return {
        "version": VERSION,
        "status": "fallback",
        "hook_id": "hook_fallback",
        "source": "fallback",
        "verbal_hook": text_verbal,
        "visual_hook": visual,
        "on_screen_text_hook": text,
        "on_screen_hook_status": "shown" if text else "omitted",
        "on_screen_omitted_reason": reason if not text else None,
        "selected_strategy": strategy,
        "legacy_strategy": strategy,
        "supported_by_fact_ids": list((verbal or {}).get("supported_by_fact_ids") or []),
        "curiosity_target": context["question"],
        "promised_payoff": context["primary_answer"] or context["final_payoff"],
        "payoff_fact_id": context["primary_answer_id"],
        "protected_information": [context["protected_label"]] if context["withhold"] and context["protected_label"] else [],
        "intended_reaction": reaction or (payoff_plan or {}).get("desired_viewer_reaction") or "curiosity",
        "format": context["format"],
        "score": (verbal or {}).get("score"),
        "reason_codes": list(dict.fromkeys([*((verbal or {}).get("positive_codes") or []), *((verbal or {}).get("reason_codes") or []), "fallback_no_complete_candidate"]))[:10],
        "verbal_origin": (verbal or {}).get("origin"),
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
    return HookCandidate(str(plan.get("selected_strategy") or ""), text, float(plan.get("score") or 0.0), f"Triple Hook V2 {plan.get('hook_id')}.")


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


def state_context(state: dict[str, Any]) -> dict[str, Any]:
    """The hook context of a persisted project (for re-checks after content changes)."""
    return hook_context(
        state.get("intent") or {}, list(state.get("facts") or []), story_arc=state.get("story_arc"),
        payoff_plan=state.get("payoff_plan"), format_plan=state.get("format_plan"), novelty_plan=state.get("novelty_plan"),
        body_blocks=[block for block in (state.get("script") or {}).get("blocks") or [] if isinstance(block, dict)],
    )


# Failures that make an existing opening unusable after a content change (a
# user's own rewording may change the strategy, never the safety rules).
_INVALIDATING = {
    "cheap_clickbait", "unnecessary_provocation", "meta_language", "unsupported_statistic", "unsupported_trend",
    "fake_controversy", "question_echo", "body_duplication", "states_primary_answer",
}


def verbal_still_valid(state: dict[str, Any], text: str, strategy: object) -> bool:
    """A hook survives a content change only if it still passes the document's safety rules."""
    if not _clean(text):
        return False
    hard = assess_verbal(text, canonical_strategy(strategy) or "evidence_insight", state_context(state))["hard_fail"]
    return not any(code in _INVALIDATING or code.endswith(REVEAL_CODES) for code in hard)


def reselect_verbal(
    state: dict[str, Any], *, exclude: set[str] | None = None, max_words: int | None = None, allow_fallback: bool = True,
) -> dict[str, Any] | None:
    """The next valid document-based verbal hook: earlier eligible candidates first."""
    plan = state_plan(state) or {}
    previous = [
        {"strategy": item.get("strategy"), "text": item.get("verbal_hook"), "supported_by_fact_ids": item.get("supported_by_fact_ids") or [], "reason_codes": [], "origin": item.get("origin") or "ai"}
        for item in (plan.get("selection") or {}).get("candidates") or []
        if isinstance(item, dict) and item.get("eligible") and canonical_strategy(item.get("strategy"))
    ]
    return select_verbal(state_context(state), extra=previous, exclude=exclude, max_words=max_words, allow_fallback=allow_fallback)
