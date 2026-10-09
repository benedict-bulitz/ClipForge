"""Script readiness: may this script go on to TTS and render?

One success contract, consumed by generation (the ``script`` pipeline stage
and the project status) and by the pre-render gate (which refuses to start
TTS/render).  A script is ready only if the original question is actually
answered, no internal diagnostic is narrated and the hook asserts nothing
the research does not support.  Everything else stays a warning.
"""
from __future__ import annotations

from typing import Any

from .novelty import current_information_gain
from .renderer import RenderUnavailable
from .script_story_quality import current_script_story_quality, script_quality_signature
from .triple_hook import state_context
from .verbal_hook import narrates_failure, ungrounded_cause

# Information-gain errors that mean "this is not an answer to the question".
# (A body without new information after a hook that already answers stays a
# severe gate issue, not a block: the question is answered.)
CONTENT_BLOCKERS = {
    "answer_insufficient",
    "narrated_failure",
    "no_question_relevant_information",
}

# Only objective Script & Story Quality failures join the production
# contract. Advisory payoff/style findings remain visible without blocking a
# render that still answers the question.  When the AI verifier accepted a
# holistic rewrite, the lexical V1 findings are advisory and only hard
# grounding/reveal failures can appear in its gate.
QUALITY_CONTENT_BLOCKERS = {
    "primary_answer_missing",
    "required_obligation_missing",
    "answer_circular",
    "insufficient_causal_depth",
    "unsupported_required_answer",
    "contract_insufficient",
    "analogy_repetition",
    "artificial_lengthening",
    "filler",
    "low_information_gain",
    "needs_research",
    "post_payoff_fluff",
    "premature_reveal",
    "rewrite_hard_failure",
    "tautological_answer",
    "too_thin",
    "unsupported_claim",
    "weak_payoff",
}


class ScriptNotReady(RenderUnavailable):
    """TTS/render refused: the script does not answer the question (retryable)."""

    def __init__(self, message: str, *, category: str = "script_not_ready"):
        super().__init__(message)
        self.category = category


def _hook_text(state: dict[str, Any]) -> str:
    blocks = (state.get("script") or {}).get("blocks") or []
    hook = next((block for block in blocks if isinstance(block, dict) and str(block.get("role") or "").casefold() == "hook"), None)
    return str((hook or {}).get("text") or "").strip()


def content_readiness(state: dict[str, Any]) -> dict[str, Any]:
    """The success contract for ``state``'s current script (pure; never raises)."""
    blocking: list[dict[str, str]] = []
    report = current_information_gain(state)
    for issue in report.get("issues") or []:
        if issue.get("severity") == "error" and issue.get("code") in CONTENT_BLOCKERS:
            blocking.append({"code": f"information_gain_{issue['code']}", "message": str(issue.get("message") or "")[:320]})
    quality = current_script_story_quality(state)
    for issue_type in (quality.get("gate") or {}).get("blocking") or []:
        if issue_type not in QUALITY_CONTENT_BLOCKERS:
            continue
        issue = next(
            (
                item
                for item in quality.get("issues") or []
                if item.get("issue_type") == issue_type and item.get("severity") == "error"
            ),
            {},
        )
        code = f"script_story_quality_{issue_type}"
        if not any(item["code"] == code for item in blocking):
            blocking.append({
                "code": code,
                "message": str(issue.get("reason") or f"Script & Story Quality V1: {issue_type}.")[:320],
            })
    # Research Pipeline V2: no direct answer was found in any retrieved source.
    research = state.get("research") if isinstance(state.get("research"), dict) else {}
    package = research.get("package") if isinstance(research.get("package"), dict) else None
    if research.get("required") and package is not None and package.get("status") in {"insufficient", "missing_mechanism"}:
        blocking.append({
            "code": "research_insufficient",
            "message": (
                "The retrieved sources do not explain why or how this happens."
                if package.get("status") == "missing_mechanism"
                else "The retrieved sources do not contain a direct answer to the question."
            ),
        })
    if research.get("contract_diagnostic") == "error":
        blocking.append({"code": "contract_unavailable", "message": "Question answer contract generation unavailable."})
    contract_coverage = state.get("contract_coverage") or {}
    if state.get("contract") and not contract_coverage:
        blocking.append({"code": "coverage_unavailable", "message": "Independent research coverage unavailable."})
    if state.get("contract"):
        script = state.get("script") or {}
        rewrite = (script.get("script_story_quality_v1") or {}).get("rewrite") or {}
        signature = script_quality_signature(script.get("blocks") or [])
        if rewrite.get("verified_by") != "ai_verifier" or rewrite.get("verified_script_signature") != signature:
            code = "contract_reverify_failed" if rewrite.get("status") == "CONTRACT_REVERIFY_FAILED" else "contract_verification_stale"
            blocking.append({"code": code, "message": "No independent contract verification for the current script."})
    if state.get("contract") and contract_coverage.get("is_sufficient") is False:
        blocking.append({
            "code": "research_contract_insufficient",
            "message": "Required research obligations unsupported: " + ", ".join(contract_coverage.get("missing_obligations") or []),
        })
    hook = _hook_text(state)
    if hook:
        if narrates_failure(hook):
            blocking.append({"code": "hook_narrates_failure", "message": "The hook reports missing evidence instead of opening the question."})
        try:
            cause = ungrounded_cause(hook, state_context(state))
        except Exception:  # noqa: BLE001 - a broken hook context must not hide the other checks
            cause = []
        if cause:
            blocking.append({
                "code": "hook_unsupported_claim",
                "message": "The hook asserts a cause no researched fact supports: " + ", ".join(cause[:4]) + ".",
            })
    sufficiency = report.get("answer_sufficiency") if isinstance(report.get("answer_sufficiency"), dict) else {}
    research_required = (bool(sufficiency.get("research_required")) and any(
        item["code"] == "information_gain_answer_insufficient" for item in blocking
    )) or any(item["code"] in {"research_insufficient", "research_contract_insufficient"} for item in blocking) or bool(
        quality.get("research_insufficient") and blocking
    )
    if not blocking:
        status = "ready"
    elif research_required:
        status = "research_required"
    else:
        status = "blocked"
    return {
        "language": (state.get("intent") or {}).get("language", "en"),
        "ready": not blocking,
        "status": status,
        "research_required": research_required,
        "blocking": blocking,
        "answer_sufficiency": sufficiency,
    }


def not_ready_message(readiness: dict[str, Any], language: str | None = None) -> str:
    """Localized user failure; technical reasons stay in readiness.blocking."""
    if str(language or readiness.get("language") or "en").startswith("de"):
        return "ClipForge konnte für diese Frage noch keine ausreichend belegte Antwort erstellen. Bitte versuche es erneut."
    return "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."
