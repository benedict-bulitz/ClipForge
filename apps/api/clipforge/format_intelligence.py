"""Small, factuality-first format planning for short-form videos.

Format is guidance for the existing payoff, hook, reaction, pacing, and scene
paths.  It is deliberately deterministic so a failed optional planner cannot
block generation or add another provider call.
"""
from __future__ import annotations

import re
from typing import Any

_RANKING = re.compile(
    r"(?i)\b(?:top\s*\d+|rank(?:ed|ing)?|ranking|fastest|largest|smallest|best\s+\d+|worst\s+\d+)\b"
)
_COMPARISON = re.compile(
    r"(?i)\b(?:which|who|what|wer|welche[rsn]?|was)\b.*\b(?:more|less|most|least|bigger|smaller|better|higher|lower|mehr|weniger|meisten|größer|kleiner|besser|höher|tiefer)\b"
)
_COMPARISON_MARKER = re.compile(r"(?i)\b(?:vs\.?|versus|compared\s+with|compare|oder|or)\b")
_QUIZ = re.compile(r"(?i)\b(?:quiz|trivia|guess|can\s+you\s+guess|rate\s+your\s+knowledge|rätsel|quizfrage)\b")
_CORRECTION = re.compile(r"(?i)\b(?:myth|misconception|false|wrong|actually|assumption|irrtum|annahme|stimmt\s+nicht)\b")
_BEFORE_AFTER = re.compile(r"(?i)\b(?:before\s+and\s+after|before\s*/\s*after|transformation|vorher\s+und\s+nachher|verwandelt)\b")
_STORY = re.compile(r"(?i)\b(?:story|tale|journey|timeline|geschichte|reise|verlauf)\b")


def _text(intent: dict[str, Any], facts: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> str:
    values = [intent.get("question"), intent.get("topic"), intent.get("content_type")]
    values.extend(fact.get("claim") for fact in facts[:8])
    values.extend(block.get("text") for block in blocks[:8])
    return " ".join(str(value or "") for value in values).casefold()


def _confidence(selected: str, text: str) -> float:
    if selected == "comparison" and (_COMPARISON.search(text) or _COMPARISON_MARKER.search(text)):
        return 0.92
    if selected == "ranking" and _RANKING.search(text):
        return 0.94
    if selected == "quiz" and _QUIZ.search(text):
        return 0.9
    if selected in {"misconception_correction", "before_after", "story"}:
        return 0.86
    return 0.7


def _details(selected: str) -> dict[str, Any]:
    details = {
        "comparison": {
            "requirements": ["establish comparable alternatives", "preserve the result until earned", "land a clear comparison payoff"],
            "payoff_structure": "contrast_then_result",
            "pacing_guidance": "Contrast alternatives efficiently; allow only the context needed to make the comparison meaningful.",
            "visual_structure": "A vs B visuals with the protected winner withheld when required.",
            "unsuitable_formats": ["quiz_without_a_real_question", "ranking_without_ordering_evidence"],
        },
        "quiz": {
            "requirements": ["state a clear challenge", "use options only when useful", "reveal a supported answer"],
            "payoff_structure": "question_then_reveal",
            "pacing_guidance": "A short anticipation window is acceptable; do not delay basic context.",
            "visual_structure": "question state, factual setup, then clear reveal",
            "unsuitable_formats": ["comparison_without_comparable_options"],
        },
        "ranking": {
            "requirements": ["use a real ordering basis", "show ordinal progression", "explain why the order matters"],
            "payoff_structure": "ordered_progression",
            "pacing_guidance": "Each item must earn its place; do not add list items to fill time.",
            "visual_structure": "clear ordinal progression with one item per meaningful beat",
            "unsuitable_formats": ["quiz_without_a_challenge"],
        },
        "explanation": {
            "requirements": ["answer the question clearly", "show cause and effect", "prioritize understanding"],
            "payoff_structure": "context_to_understanding",
            "pacing_guidance": "Allow enough comprehension time for the causal chain; no artificial reveal delay.",
            "visual_structure": "cause/effect, process, or simple explanatory visuals",
            "unsuitable_formats": ["quiz_without_explicit_challenge", "ranking_without_ordering_basis"],
        },
        "misconception_correction": {
            "requirements": ["state the common assumption accurately", "correct it with evidence", "end with clarity"],
            "payoff_structure": "assumption_then_correction",
            "pacing_guidance": "Make the correction easy to follow; avoid manufactured controversy.",
            "visual_structure": "assumption contrasted with the supported explanation",
            "unsuitable_formats": [],
        },
        "before_after": {
            "requirements": ["show a meaningful state change", "explain what caused it", "avoid cosmetic filler"],
            "payoff_structure": "state_change",
            "pacing_guidance": "Let the transformation remain readable; do not cut faster than the change can be understood.",
            "visual_structure": "before state, process, after state",
            "unsuitable_formats": [],
        },
        "story": {
            "requirements": ["establish a progression", "keep events coherent", "resolve the final beat"],
            "payoff_structure": "progression_to_resolution",
            "pacing_guidance": "Use meaningful turns, not extra scenes or artificial suspense.",
            "visual_structure": "chronological progression with a resolved final image",
            "unsuitable_formats": [],
        },
    }
    return details.get(selected, details["explanation"])


def select_format(
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    novelty_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select one compact format from the actual prompt and available evidence."""
    facts = facts or []
    blocks = blocks or []
    question = str(intent.get("question") or "")
    content_type = str(intent.get("content_type") or "").casefold()
    text = _text(intent, facts, blocks)
    prompt_text = " ".join(
        str(intent.get(key) or "") for key in ("question", "topic", "content_type")
    ).casefold()
    # The user's request decides the format; research only informs it.  A
    # rank the evidence happens to mention ("Top 5", "ranking") never turns
    # a question into a list video.
    binary = bool(_COMPARISON_MARKER.search(question)) and bool(_COMPARISON.search(question) or re.search(r"(?i)\b(?:vs\.?|versus)\b", question))
    if content_type == "fictional_story" or _STORY.search(question):
        selected, reason = "story", "question_is_narrative"
    elif binary:
        selected, reason = "comparison", "question_names_two_alternatives"
    elif _RANKING.search(question):
        selected, reason = "ranking", "question_asks_for_an_ordering"
    elif _COMPARISON.search(question) or _COMPARISON_MARKER.search(question):
        selected, reason = "comparison", "question_compares"
    elif _QUIZ.search(question):
        selected, reason = "quiz", "question_is_a_challenge"
    elif _CORRECTION.search(prompt_text):
        selected, reason = "misconception_correction", "prompt_corrects_an_assumption"
    elif _BEFORE_AFTER.search(prompt_text):
        selected, reason = "before_after", "prompt_describes_a_change"
    else:
        selected, reason = "explanation", "default"
    details = _details(selected)
    novelty_plan = novelty_plan if isinstance(novelty_plan, dict) else {}
    return {
        "status": "planned",
        "selected_format": selected,
        "selection_reason": reason,
        "research_mentions_ranking": bool(_RANKING.search(text)),
        "confidence": _confidence(selected, text),
        "rationale": {
            "comparison": "The question asks viewers to distinguish comparable alternatives.",
            "ranking": "The topic supplies an explicit ordered list or ranking basis.",
            "quiz": "The prompt explicitly frames a challenge or trivia question.",
            "misconception_correction": "The topic centers on correcting a stated assumption.",
            "before_after": "The topic contains a meaningful before/after transformation.",
            "story": "The topic is explicitly narrative or fictional.",
            "explanation": "No stronger evidence-supported format fits, so clarity-first explanation is safest.",
        }[selected],
        "format_requirements": details["requirements"],
        "payoff_structure": details["payoff_structure"],
        "pacing_guidance": details["pacing_guidance"],
        "visual_structure": details["visual_structure"],
        "unsuitable_formats": details["unsuitable_formats"],
        "novelty_guidance": {
            "recommended_angle": str(novelty_plan.get("recommended_angle") or ""),
            "distinctive_fact_ids": list(novelty_plan.get("distinctive_facts") or []),
            "explanatory_gain_ids": list(novelty_plan.get("explanatory_gain") or []),
            "comparison_gain_ids": list(novelty_plan.get("comparison_gain") or []),
        },
    }


def plan_format(
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    novelty_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return select_format(intent, facts, blocks, novelty_plan)
    except Exception as exc:  # noqa: BLE001 - optional enrichment must not block generation
        return {
            "status": "fallback",
            "selected_format": "explanation",
            "confidence": 0.0,
            "rationale": "Format planning failed; use the existing explanation path.",
            "format_requirements": ["answer clearly", "preserve factual context"],
            "payoff_structure": "context_to_understanding",
            "pacing_guidance": "Allow enough comprehension time; do not add fixed timing rules.",
            "visual_structure": "relevant explanatory visuals",
            "unsuitable_formats": [],
            "novelty_guidance": {},
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def format_guidance(plan: dict[str, Any] | None) -> str:
    plan = plan if isinstance(plan, dict) else {}
    return " ".join(
        str(plan.get(key) or "").strip()
        for key in ("selected_format", "payoff_structure", "pacing_guidance", "visual_structure")
        if str(plan.get(key) or "").strip()
    )


def format_quality_issues(state: dict[str, Any]) -> list[str]:
    plan = state.get("format_plan") if isinstance(state.get("format_plan"), dict) else {}
    if not plan:
        return []
    selected = str(plan.get("selected_format") or "explanation")
    prompt = str(state.get("prompt") or state.get("intent", {}).get("question") or "")
    facts = state.get("facts") if isinstance(state.get("facts"), list) else []
    text = _text(state.get("intent", {}), facts, state.get("script", {}).get("blocks", []))
    issues: list[str] = []
    if selected == "ranking" and not _RANKING.search(text):
        issues.append("ranking_without_ordering_basis")
    if selected == "quiz" and not _QUIZ.search(text):
        issues.append("quiz_without_explicit_challenge")
    if selected == "comparison":
        alternatives = re.findall(r"(?i)\b(?:vs\.?|versus|or|oder|compared\s+with)\b", prompt)
        if not (_COMPARISON.search(prompt) or len(alternatives) >= 1):
            issues.append("comparison_without_comparable_alternatives")
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    payoff_type = str(payoff.get("payoff_type") or "").casefold()
    if selected == "explanation" and payoff_type in {"comparison_winner", "ranking_result"}:
        issues.append("format_conflicts_with_payoff")
    reaction = state.get("reaction_plan") if isinstance(state.get("reaction_plan"), dict) else {}
    if selected == "explanation" and reaction.get("payoff_reaction") == "surprise" and payoff_type == "explanation":
        issues.append("format_conflicts_with_reaction")
    return issues
