"""Compact payoff and coordinated-hook planning for short-form scripts.

The plan is deliberately descriptive rather than a second script writer.  The
body remains the source of truth; this module records the question it is
building toward and gives the existing hook/media paths enough constraints to
avoid spoiling that answer in the opening.
"""
from __future__ import annotations

import re
from typing import Any

from .narration import clean_narration_text

_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "who",
    "why", "how", "are", "is", "was", "were", "has", "have", "more", "than",
    "der", "die", "das", "und", "für", "mit", "von", "welche", "welcher",
    "welches", "wer", "warum", "wie", "ist", "sind", "hat", "haben", "mehr",
}
_OUTRO = re.compile(
    r"(?i)^\s*(?:thanks? for watching|thank you|follow for more|like and subscribe|"
    r"see you next time|danke fürs zuschauen|danke fürs ansehen|folge für mehr|"
    r"like und abonniere|bis zum nächsten mal)\b"
)
_COMPARISON = re.compile(
    r"(?i)\b(?:which|who|what|welche[rsn]?|wer|was)\b.*\b(?:more|less|most|least|"
    r"bigger|smaller|better|higher|lower|mehr|weniger|meisten|größer|kleiner|besser|höher|tiefer)\b"
)
_RESULT_LANGUAGE = re.compile(
    r"(?i)\b(?:more|less|most|least|wins?|winner|has|have|is|are|"
    r"mehr|weniger|meisten|gewinnt|sieger|hat|haben|ist|sind)\b"
)


def _words(value: object) -> set[str]:
    return {
        word for word in re.findall(r"[a-zäöüß]{3,}", str(value or "").casefold())
        if word not in _STOP
    }


def _clean(value: object, limit: int = 320) -> str:
    return " ".join(clean_narration_text(str(value or "")).split())[:limit].strip()


def _label(value: object, limit: int = 140) -> str:
    return _clean(value, limit).rstrip(".!?")


def _is_protected_question(intent: dict[str, Any]) -> bool:
    question = str(intent.get("question") or "")
    return bool(_COMPARISON.search(question) or re.search(r"(?i)\b(?:really|actually|wirklich|tatsächlich)\b", question))


def _reaction(intent: dict[str, Any], protected: bool) -> str:
    text = " ".join(str(intent.get(key) or "") for key in ("question", "tone", "content_type")).casefold()
    if any(term in text for term in ("story", "fiction", "suspense", "spannung")):
        return "tension"
    if any(term in text for term in ("why", "how", "warum", "wieso", "explain", "erklär")):
        return "insight"
    return "surprise" if protected else "curiosity"


def _payoff_type(intent: dict[str, Any], protected: bool, payoff: str) -> str:
    question = str(intent.get("question") or "")
    if _COMPARISON.search(question):
        return "comparison_winner"
    if re.search(r"(?i)\b(?:myth|misconception|assumption|irrtum|annahme)\b", question + " " + payoff):
        return "correction"
    if protected:
        return "reveal"
    if re.search(r"(?i)\b(?:why|how|warum|wieso|wie)\b", question):
        return "explanation"
    return "answer"


def _protected_answer(payoff: str, question: str) -> str:
    """Keep a comparison result hidden without banning its whole visual subject."""
    match = re.match(
        r"^\s*([A-ZÄÖÜ][\wÄÖÜäöüß-]*(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]*){0,2})\s+"
        r"(?:has|have|is|are|wins?|won|hat|haben|ist|sind|gewinnt)\b",
        payoff,
    )
    if match and match.group(1).casefold() in question.casefold():
        return match.group(1)
    return payoff


def build_payoff_plan(
    intent: dict[str, Any],
    blocks: list[dict[str, Any]],
    *,
    supplied: dict[str, Any] | None = None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a safe, persisted plan without changing the narration body."""
    supplied = supplied if isinstance(supplied, dict) else {}
    body = [block for block in blocks if str(block.get("role") or "").casefold() != "hook"]
    explicit = next(
        (_clean(block.get("text")) for block in reversed(body) if str(block.get("role") or "").casefold() == "payoff"),
        "",
    )
    payoff = _clean(supplied.get("payoff")) or explicit or _clean(body[-1].get("text") if body else "")
    protected = bool(supplied.get("hook_must_not_reveal")) or _is_protected_question(intent)
    question = _clean(supplied.get("curiosity_question")) or _clean(intent.get("question"))
    support = [
        _clean(block.get("text"))
        for block in body
        if str(block.get("role") or "").casefold() != "payoff" and _clean(block.get("text"))
    ][:4]
    dependencies = [item for item in support if item != payoff][:3]
    hidden = _clean(supplied.get("hook_must_not_reveal")) or (
        _protected_answer(payoff, question) if protected else ""
    )
    format_name = str((format_plan or {}).get("selected_format") or "").casefold()
    novelty = novelty_plan if isinstance(novelty_plan, dict) else {}
    format_payoff_type = {
        "comparison": "comparison_winner",
        "ranking": "ranking_result",
        "quiz": "answer",
        "misconception_correction": "correction",
        "before_after": "reveal",
        "story": "reveal",
    }.get(format_name)
    supplied_type = _clean(supplied.get("payoff_type"), 80)
    return {
        "curiosity_question": question,
        "payoff": payoff,
        "payoff_type": supplied_type or format_payoff_type or _payoff_type(intent, protected, payoff),
        "payoff_dependencies": dependencies,
        "reveal_policy": "after_supporting_information" if protected else "immediate_context_allowed",
        "hook_must_not_reveal": hidden,
        "desired_viewer_reaction": _clean(supplied.get("desired_viewer_reaction"), 80) or _reaction(intent, protected),
        "supporting_information": support,
        "format": format_name or None,
        "novelty_guidance": {
            "recommended_angle": str(novelty.get("recommended_angle") or ""),
            "distinctive_fact_ids": list(novelty.get("distinctive_facts") or []),
            "explanatory_gain_ids": list(novelty.get("explanatory_gain") or []),
            "comparison_gain_ids": list(novelty.get("comparison_gain") or []),
        },
        "status": "planned",
    }


def hidden_payoff_words(plan: dict[str, Any]) -> set[str]:
    return _words(plan.get("hook_must_not_reveal"))


def reveals_protected_payoff(value: object, plan: dict[str, Any]) -> bool:
    hidden = hidden_payoff_words(plan)
    candidate = _words(value)
    if not hidden or not candidate or not (hidden & candidate):
        return False
    if len(hidden) == 1:
        return bool(_RESULT_LANGUAGE.search(str(value or "")) or candidate == hidden)
    return len(hidden & candidate) >= 2


def _hook_text(intent: dict[str, Any], plan: dict[str, Any]) -> str:
    if intent.get("language") == "de":
        return "WER LIEGT VORN?" if plan.get("reveal_policy") == "after_supporting_information" else "WAS STECKT DAHINTER?"
    return "WHO COMES OUT AHEAD?" if plan.get("reveal_policy") == "after_supporting_information" else "WHAT'S REALLY HAPPENING?"


def fallback_triple_hook(
    intent: dict[str, Any], plan: dict[str, Any], verbal_hook: str | None, strategy: str | None,
    reaction: str | None = None, format_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create complementary hook channels when the provider is unavailable."""
    topic = _clean(intent.get("topic"), 140) or _clean(intent.get("question"), 140)
    hidden = _label(plan.get("hook_must_not_reveal"), 140)
    structure = str((format_plan or {}).get("visual_structure") or "").strip()
    visual_goal = structure or (f"clear visual contrast for {topic}" if plan.get("reveal_policy") == "after_supporting_information" else f"immediate visual context for {topic}")
    return {
        "verbal_hook": _clean(verbal_hook),
        "visual_hook": {
            "visual_goal": visual_goal,
            "subjects_to_show": [topic],
            "contrast": "comparison before the result" if hidden else "show the real-world mechanism",
            "motion_or_change": "quick visual contrast" if hidden else "show the relevant action",
            "visual_priority": "make the subject recognizable immediately",
            "must_not_show": [hidden] if hidden else [],
            "media_queries": [topic],
        },
        "on_screen_text_hook": _hook_text(intent, plan),
        "selected_strategy": strategy or "existing_hook_library",
        "intended_reaction": reaction or plan.get("desired_viewer_reaction") or "curiosity",
        "status": "fallback",
        "format": (format_plan or {}).get("selected_format"),
    }


def normalise_triple_hook(
    candidate: dict[str, Any] | None,
    fallback: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Accept provider guidance only when it cannot spoil the protected payoff."""
    candidate = candidate if isinstance(candidate, dict) else {}
    visual = candidate.get("visual_hook") if isinstance(candidate.get("visual_hook"), dict) else {}
    text = _clean(candidate.get("on_screen_text_hook"), 80)
    verbal = fallback["verbal_hook"]
    if (
        not text
        or reveals_protected_payoff(text, plan)
        or text.casefold() == _clean(verbal, 80).casefold()
        or (_words(text) and _words(text) == _words(verbal))
    ):
        text = fallback["on_screen_text_hook"]
    proposed_visual = {
        "visual_goal": _clean(visual.get("visual_goal"), 220),
        "subjects_to_show": [_clean(item, 100) for item in visual.get("subjects_to_show", []) if _clean(item, 100)][:4],
        "contrast": _clean(visual.get("contrast"), 140),
        "motion_or_change": _clean(visual.get("motion_or_change"), 140),
        "visual_priority": _clean(visual.get("visual_priority"), 140),
        "must_not_show": [_clean(item, 100) for item in visual.get("must_not_show", []) if _clean(item, 100)][:4],
        "media_queries": [_clean(item, 120) for item in visual.get("media_queries", []) if _clean(item, 120)][:4],
    }
    visual_text = " ".join(
        [proposed_visual["visual_goal"], *proposed_visual["subjects_to_show"], *proposed_visual["media_queries"]]
    )
    if not proposed_visual["visual_goal"] or reveals_protected_payoff(visual_text, plan):
        proposed_visual = fallback["visual_hook"]
    hidden = _label(plan.get("hook_must_not_reveal"), 100)
    if hidden and hidden not in proposed_visual["must_not_show"]:
        proposed_visual["must_not_show"] = [*proposed_visual["must_not_show"], hidden]
    return {
        **fallback,
        "visual_hook": proposed_visual,
        "on_screen_text_hook": text,
        "status": "connected" if candidate else "fallback",
    }


def trim_post_payoff_fluff(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Drop only generic CTA/outro blocks after an actual payoff; never rewrite facts."""
    payoff_index = max(
        (index for index, block in enumerate(blocks) if str(block.get("role") or "").casefold() == "payoff"),
        default=-1,
    )
    if payoff_index < 0:
        return blocks, False
    kept = blocks[: payoff_index + 1]
    removed = any(_OUTRO.search(_clean(block.get("text"))) for block in blocks[payoff_index + 1 :])
    return (kept if removed else blocks), removed


def payoff_quality_issues(state: dict[str, Any]) -> list[str]:
    plan = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    triple = state.get("script", {}).get("triple_hook") if isinstance(state.get("script", {}).get("triple_hook"), dict) else {}
    blocks = state.get("script", {}).get("blocks", [])
    if not plan or not triple or not blocks:
        return []
    issues: list[str] = []
    verbal = triple.get("verbal_hook") or blocks[0].get("text")
    if reveals_protected_payoff(verbal, plan):
        issues.append("protected_payoff_revealed_in_hook")
    body = next((block.get("text") for block in blocks[1:] if block.get("text")), "")
    if _words(verbal) and _words(body) and len(_words(verbal) & _words(body)) / max(1, min(len(_words(verbal)), len(_words(body)))) >= 0.8:
        issues.append("hook_repeats_first_body_sentence")
    text_hook = triple.get("on_screen_text_hook")
    if (
        _clean(text_hook, 80).casefold() == _clean(verbal, 80).casefold()
        or (_words(text_hook) and _words(text_hook) == _words(verbal))
    ):
        issues.append("text_hook_duplicates_verbal_hook")
    visual = triple.get("visual_hook") if isinstance(triple.get("visual_hook"), dict) else {}
    visual_text = " ".join(str(visual.get(key) or "") for key in ("visual_goal", "subjects_to_show", "media_queries"))
    if reveals_protected_payoff(visual_text, plan):
        issues.append("visual_hook_reveals_protected_payoff")
    payoff_seen = False
    for block in blocks:
        if str(block.get("role") or "").casefold() == "payoff":
            payoff_seen = True
            continue
        if payoff_seen and _OUTRO.search(_clean(block.get("text"))):
            issues.append("generic_post_payoff_outro")
            break
    return issues
