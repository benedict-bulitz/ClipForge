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
    research_required = bool(sufficiency.get("research_required")) and any(
        item["code"] == "information_gain_answer_insufficient" for item in blocking
    )
    if not blocking:
        status = "ready"
    elif research_required:
        status = "research_required"
    else:
        status = "blocked"
    return {
        "ready": not blocking,
        "status": status,
        "research_required": research_required,
        "blocking": blocking,
        "answer_sufficiency": sufficiency,
    }


def not_ready_message(readiness: dict[str, Any]) -> str:
    """User-facing reason (no narration of it ever reaches the video)."""
    if readiness.get("research_required"):
        return "The research does not explain the question yet, so no video was produced. Retry to research again."
    first = (readiness.get("blocking") or [{}])[0]
    return f"The script is not ready to produce: {first.get('message') or first.get('code') or 'quality gate failed'}"[:480]
