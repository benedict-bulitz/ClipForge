from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .format_intelligence import format_quality_issues
from .hooks import hook_issues
from .language import detect_text_language
from .narration import begins_with_preamble, contamination_issues
from .novelty import novelty_quality_issues
from .payoff import payoff_quality_issues
from .pipeline import _normalise_blocks, _refresh_script_derivatives
from .reactions import reaction_quality_issues


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: Literal[
        "language",
        "prompt_fidelity",
        "script",
        "directness",
        "brevity",
        "accessibility",
        "relevance",
        "contamination",
        "repetition",
        "research",
        "scenes",
        "timing",
        "captions",
        "output",
        "hook",
        "payoff",
    ]
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=2, max_length=320)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "passed_with_warnings", "needs_fix", "failed"]
    items: list[ReviewFinding] = Field(default_factory=list, max_length=12)
    corrected_script_blocks: list[dict[str, str]] | None = None


class ReviewProvider(Protocol):
    name: str

    def review(self, context: dict[str, Any]) -> ReviewDecision: ...


class OpenAIReviewProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_worker_model

    def review(self, context: dict[str, Any]) -> ReviewDecision:
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=(
                    "Review this short-video plan and return concise findings only. It should begin "
                    "with one short audience-facing curiosity hook that makes sense without seeing the "
                    "original prompt. Respect the supplied payoff_plan: when a payoff is protected, do not "
                    "reveal it in the hook, visual hook, or text hook, and let useful context earn it; when "
                    "immediate context is necessary, do not delay it. Never require a fixed reveal time. "
                    "Reject hook/body repetition, visual or text-hook spoilers, weak generic post-payoff "
                    "outros, and CTAs after a natural ending. Check "
                    "brevity, plain-language accessibility for a viewer with zero prior knowledge, "
                    "relevance, repetition, and contamination by HTML, Markdown, structural labels, "
                    "source snippets, attribution boilerplate, editorial publication directions, scraper "
                    "artifacts, or model commentary. "
                    "The maximum duration is a ceiling, never a target. Check language, prompt fidelity, "
                    "research consistency, selected format versus actual content, scenes, timing, captions, and render inputs. "
                    "Flag ranking without an evidence-based ordering, quiz answers revealed immediately, and comparison alternatives that are not comparable. Never claim facts "
                    "are verified without provided sources. If safe, return rewritten clean script blocks "
                    "in the project language without adding new factual claims. Do not reveal reasoning."
                ),
                input=json.dumps(context, ensure_ascii=False),
                text_format=ReviewDecision,
                max_output_tokens=1_500,
                store=False,
            )
        except (OpenAIError, ValueError, TypeError) as exc:
            raise RuntimeError("AI review provider unavailable") from exc
        if not isinstance(response.output_parsed, ReviewDecision):
            raise TypeError("AI review returned no structured result")
        return response.output_parsed


def review_input_hash(state: dict[str, Any]) -> str:
    payload = {
        "prompt": state.get("prompt"),
        "language": state.get("intent", {}).get("language"),
        "research": state.get("research"),
        "facts": state.get("facts"),
        "script": state.get("script"),
        "payoff_plan": state.get("payoff_plan"),
        "format_plan": state.get("format_plan"),
        "reaction_plan": state.get("reaction_plan"),
        "novelty_plan": state.get("novelty_plan"),
        "scenes": state.get("scenes"),
        "voice": {
            key: value
            for key, value in state.get("voice", {}).items()
            if key in {"voice_id", "tone", "gender_presentation", "speed"}
        },
        "captions": {
            key: value
            for key, value in state.get("captions", {}).items()
            if key
            in {
                "enabled",
                "style",
                "position",
                "font_size",
                "text_color",
                "highlight_color",
                "words_per_group",
            }
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def review_context(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": state.get("prompt"),
        "language": state.get("intent", {}).get("language"),
        "research": {
            "status": state.get("research", {}).get("status"),
            "source_count": len(state.get("research", {}).get("sources", [])),
        },
        "facts": [fact.get("claim") for fact in state.get("facts", [])[:6]],
        "script_blocks": [
            {"role": block.get("role"), "text": block.get("text")}
            for block in state.get("script", {}).get("blocks", [])
        ],
        "scenes": [
            {
                "start": scene.get("start"),
                "end": scene.get("end"),
                "narration": scene.get("narration"),
                "visual_goal": scene.get("visual_goal"),
                "media_kind": (scene.get("media") or {}).get("kind"),
                "asset_status": scene.get("asset_status"),
            }
            for scene in state.get("scenes", [])
        ],
        "voice": {
            key: value
            for key, value in state.get("voice", {}).items()
            if key in {"voice_id", "tone", "gender_presentation", "speed"}
        },
        "captions": {
            key: value
            for key, value in state.get("captions", {}).items()
            if key in {"enabled", "style", "position", "timing", "words_per_group"}
        },
        "duration": state.get("duration"),
        "format_plan": state.get("format_plan"),
        "payoff_plan": state.get("payoff_plan"),
        "triple_hook": state.get("script", {}).get("triple_hook"),
        "reaction_plan": state.get("reaction_plan"),
        "novelty_plan": state.get("novelty_plan"),
    }


def local_review_items(state: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    expected = state.get("intent", {}).get("language", "en")
    detected = detect_text_language(state.get("script", {}).get("text", ""))
    if detected not in {"unknown", expected}:
        items.append(
            {
                "check": "language",
                "severity": "error",
                "message": f"The script appears to be {detected}, but the project language is {expected}.",
            }
        )
    else:
        items.append(
            {
                "check": "language",
                "severity": "info",
                "message": f"Script and project language are consistent ({expected}).",
            }
        )
    research = state.get("research", {})
    if research.get("required") and not research.get("sources"):
        items.append(
            {
                "check": "research",
                "severity": "warning",
                "message": "Research was unavailable; factual verification is not claimed.",
            }
        )
    prompt_words = _meaningful_words(str(state.get("prompt") or ""))
    script_text = str(state.get("script", {}).get("text") or "")
    script_words = _meaningful_words(script_text)
    if prompt_words and not prompt_words.intersection(script_words):
        items.append(
            {
                "check": "prompt_fidelity",
                "severity": "warning",
                "message": "The script has little clear vocabulary overlap with the original request.",
            }
        )
    blocks = state.get("script", {}).get("blocks", [])
    normalised_blocks = [
        " ".join(str(block.get("text") or "").lower().split()) for block in blocks
    ]
    if len(set(normalised_blocks)) < len(normalised_blocks):
        items.append(
            {
                "check": "script",
                "severity": "error",
                "message": "The script contains a duplicated narration block.",
            }
        )
    if any(
        marker in script_text.lower()
        for marker in ("as an ai", "language model", "here is the script", "als ki")
    ):
        items.append(
            {
                "check": "script",
                "severity": "error",
                "message": "The script contains accidental assistant or meta text.",
            }
        )
    first_block = next(iter(state.get("script", {}).get("blocks", [])), {})
    if begins_with_preamble(first_block.get("text")):
        items.append(
            {
                "check": "directness",
                "severity": "error",
                "message": "The opening talks about answering instead of beginning with the answer.",
            }
        )
    first_sentence = next(
        (
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", script_text)
            if sentence.strip()
        ),
        "",
    )
    first_block = next(iter(blocks), {})
    if str(first_block.get("role") or "").casefold() == "hook":
        hook_problems = hook_issues(
            str(first_block.get("text") or ""),
            state.get("facts", []),
            body=str(blocks[1].get("text") or "") if len(blocks) > 1 else "",
        )
        for problem in hook_problems:
            severity = "error" if problem in {"unsupported_statistic", "unsupported_trend", "personal_attack", "generic_clickbait", "generic_meta_filler", "meta_language", "structural_label"} else "warning"
            items.append({"check": "hook", "severity": severity, "message": f"Hook quality issue: {problem.replace('_', ' ')}."})
    for problem in payoff_quality_issues(state):
        severity = "error" if "revealed" in problem or "duplicates" in problem else "warning"
        items.append(
            {
                "check": "payoff" if "payoff" in problem or "outro" in problem else "hook",
                "severity": severity,
                "message": f"Payoff/hook quality issue: {problem.replace('_', ' ')}.",
            }
        )
    for problem in reaction_quality_issues(state):
        severity = "error" if problem in {"unsupported_emotional_framing", "fake_clickbait_reaction"} else "warning"
        items.append(
            {
                "check": "hook" if "clickbait" in problem or "framing" in problem else "scenes",
                "severity": severity,
                "message": f"Reaction-plan quality issue: {problem.replace('_', ' ')}.",
            }
        )
    for problem in novelty_quality_issues(state):
        items.append(
            {
                "check": "research",
                "severity": "warning",
                "message": f"Novelty-plan quality issue: {problem.replace('_', ' ')}.",
            }
        )
    if len(first_sentence.split()) > 18:
        items.append(
            {
                "check": "directness",
                "severity": "warning",
                "message": "The opening hook is too long; reach the answer sooner.",
            }
        )
    contamination = contamination_issues(script_text)
    if contamination:
        items.append(
            {
                "check": "contamination",
                "severity": "error",
                "message": "Narration contains " + ", ".join(contamination) + ".",
            }
        )
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", script_text)
        if sentence.strip()
    ]
    sentence_words = [_meaningful_words(sentence) for sentence in sentences]
    repeated = any(
        left
        and right
        and len(left & right) / max(1, min(len(left), len(right))) >= 0.8
        for index, left in enumerate(sentence_words)
        for right in sentence_words[index + 1 :]
    )
    if repeated:
        items.append(
            {
                "check": "repetition",
                "severity": "warning",
                "message": "Two narration sentences make substantially the same point.",
            }
        )
    if any(len(sentence.split()) > 30 for sentence in sentences):
        items.append(
            {
                "check": "accessibility",
                "severity": "warning",
                "message": "At least one sentence is too dense for an accessible short-form explanation.",
            }
        )
    if len(script_text.split()) > 140:
        items.append(
            {
                "check": "brevity",
                "severity": "warning",
                "message": "The narration may contain more detail than a concise short-form answer needs.",
            }
        )
    maximum = float(state.get("duration", {}).get("max_seconds") or 0)
    speaking_rate = float(state.get("duration", {}).get("speaking_rate_wpm") or 165)
    if maximum and len(script_text.split()) > int(maximum * speaking_rate / 60):
        items.append(
            {
                "check": "brevity",
                "severity": "error",
                "message": "The narration cannot fit the maximum duration without cutting speech.",
            }
        )
    if re.search(
        r"(?i)\b(?:according to|covers? \d|acres?|square miles|annual visitors?|"
        r"was founded in|headquarters)\b",
        script_text,
    ):
        items.append(
            {
                "check": "relevance",
                "severity": "warning",
                "message": "Narration may include source attribution or low-value factual detail.",
            }
        )
    scenes = state.get("scenes", [])
    if any(
        float(scene.get("end") or 0) <= float(scene.get("start") or 0)
        or not str(scene.get("narration") or "").strip()
        for scene in scenes
    ):
        items.append(
            {
                "check": "timing",
                "severity": "error",
                "message": "At least one scene has invalid timing or no narration.",
            }
        )
    expected_duration = float(state.get("duration", {}).get("estimated_seconds") or 0)
    scene_duration = max((float(scene.get("end") or 0) for scene in scenes), default=0)
    if expected_duration and abs(scene_duration - expected_duration) > max(2.0, expected_duration * 0.2):
        items.append(
            {
                "check": "timing",
                "severity": "warning",
                "message": "Storyboard timing differs materially from the planned narration duration.",
            }
        )
    fallback_count = sum(
        not scene.get("media") for scene in scenes
    )
    if fallback_count:
        items.append(
            {
                "check": "scenes",
                "severity": "warning",
                "message": f"{fallback_count} scene(s) use generated fallback visuals.",
            }
        )
    captions = state.get("captions", {})
    if captions.get("enabled") and not captions.get("items"):
        items.append(
            {"check": "captions", "severity": "error", "message": "Captions are enabled but empty."}
        )
    elif captions.get("enabled"):
        caption_text = " ".join(
            str(item.get("text") or "") for item in captions.get("items", [])
        )
        caption_words = _meaningful_words(caption_text)
        if script_words and len(script_words.intersection(caption_words)) < max(1, len(script_words) // 3):
            items.append(
                {
                    "check": "captions",
                    "severity": "warning",
                    "message": "Caption text does not sufficiently correspond to the narration.",
                }
            )
    if captions.get("style") not in {
        "clean", "bold", "minimal", "pop", "boxed", "outline", "karaoke",
    }:
        items.append(
            {"check": "captions", "severity": "error", "message": "The caption style is unsupported."}
        )
    if not state.get("script", {}).get("text") or not state.get("scenes"):
        items.append(
            {"check": "output", "severity": "error", "message": "Required render inputs are missing."}
        )
    pacing = state.get("pacing_analysis") if isinstance(state.get("pacing_analysis"), dict) else {}
    for recommendation in pacing.get("recommendations", [])[:8]:
        action = str(recommendation.get("action") or "")
        reasons = recommendation.get("reasons") or []
        if action and reasons:
            items.append(
                {
                    "check": "timing" if action in {"MERGE_WITH_NEXT", "TRIM", "SHORTEN_POST_PAYOFF"} else "scenes",
                    "severity": "warning",
                    "message": f"Scene pacing recommends {action.lower().replace('_', ' ')}: {reasons[0]}",
                }
            )
    return items


def pre_render_quality_gate(state: dict[str, Any]) -> dict[str, Any]:
    """Perform final structural consistency checks without another model call."""
    issues: list[dict[str, str]] = []
    severe: list[str] = []
    script = state.get("script", {}) if isinstance(state.get("script"), dict) else {}
    triple = script.get("triple_hook") if isinstance(script.get("triple_hook"), dict) else {}
    selected_hook = str(script.get("selected_hook") or "").strip()
    verbal_hook = str(triple.get("verbal_hook") or "").strip()
    if selected_hook and verbal_hook and selected_hook != verbal_hook:
        issues.append({"code": "hook_channel_drift", "severity": "warning", "message": "Verbal hook metadata differs from the narration opening."})
        triple["verbal_hook"] = selected_hook
    scenes = list(state.get("scenes") or [])
    empty_scene_ids = [str(scene.get("id") or "") for scene in scenes if not str(scene.get("narration") or "").strip()]
    if empty_scene_ids:
        issues.append({"code": "empty_scene", "severity": "error", "message": "Empty scene artifacts remain before rendering."})
        severe.append("empty_scene")
    visual_hook = triple.get("visual_hook") if isinstance(triple.get("visual_hook"), dict) else {}
    first_visual = scenes[0].get("visual_intent") if scenes and isinstance(scenes[0].get("visual_intent"), dict) else {}
    visual_goal = str(visual_hook.get("visual_goal") or "").strip()
    first_goal = str(first_visual.get("visual_goal") or "").strip()
    if visual_goal and first_goal and visual_goal != first_goal:
        issues.append({"code": "first_visual_drift", "severity": "warning", "message": "The first scene visual intent differs from the planned visual hook."})
    payoff_issues = payoff_quality_issues(state)
    for problem in payoff_issues:
        severity = "error" if problem in {"protected_payoff_revealed_in_hook", "visual_hook_reveals_protected_payoff"} else "warning"
        issues.append({"code": problem, "severity": severity, "message": problem.replace("_", " ") + "."})
        if severity == "error":
            severe.append(problem)
    for problem in format_quality_issues(state):
        issues.append({"code": problem, "severity": "warning", "message": problem.replace("_", " ") + "."})
    for problem in reaction_quality_issues(state):
        severity = "error" if problem in {"unsupported_emotional_framing", "fake_clickbait_reaction"} else "warning"
        issues.append({"code": problem, "severity": severity, "message": problem.replace("_", " ") + "."})
        if severity == "error":
            severe.append(problem)
    for problem in novelty_quality_issues(state):
        issues.append({"code": problem, "severity": "warning", "message": problem.replace("_", " ") + "."})
    pacing = state.get("pacing_analysis") if isinstance(state.get("pacing_analysis"), dict) else {}
    if pacing.get("status") == "fallback":
        issues.append({"code": "pacing_fallback", "severity": "warning", "message": "Pacing analysis fell back; existing scene timing is preserved."})
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    roles = {
        str(block.get("id") or ""): str(block.get("role") or "").casefold()
        for block in script.get("blocks", [])
    }
    has_payoff_scene = any(roles.get(str(scene.get("block_id") or "")) == "payoff" for scene in scenes)
    if payoff.get("payoff") and not has_payoff_scene:
        issues.append({"code": "missing_payoff_scene", "severity": "warning", "message": "The persisted payoff has no explicit payoff scene; no filler was added."})
    state["quality_gate"] = {
        "status": "fallback" if severe else ("passed_with_warnings" if issues else "passed"),
        "issues": issues,
        "severe_issues": sorted(set(severe)),
        "ai_calls": 0,
    }
    return state["quality_gate"]


def _meaningful_words(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "is", "are", "to", "of", "in", "on", "our",
        "der", "die", "das", "ein", "eine", "und", "oder", "ist", "sind", "zu", "von", "im", "auf", "unser",
    }
    return {word for word in re.findall(r"[a-zäöüß]{3,}", text.lower()) if word not in stop}


def run_ai_review(
    state: dict[str, Any],
    settings: Settings,
    *,
    provider: ReviewProvider | None = None,
    max_rounds: int = 2,
) -> dict[str, Any]:
    current_hash = review_input_hash(state)
    existing = state.get("ai_review", {})
    if existing.get("input_hash") == current_hash and existing.get("status") not in {"pending", "needs_fix"}:
        return state
    selected = provider
    if selected is None and settings.openai_api_key:
        selected = OpenAIReviewProvider(settings)
    local_items = local_review_items(state)
    if selected is None:
        state["ai_review"] = {
            "status": "unavailable",
            "provider": None,
            "rounds": 0,
            "input_hash": current_hash,
            "items": local_items,
            "automatic_corrections": [],
            "message": "AI review is unavailable because no suitable model provider is configured.",
        }
        return state

    corrections: list[str] = []
    decision: ReviewDecision | None = None
    rounds = 0
    for rounds in range(1, max(1, min(max_rounds, 2)) + 1):
        try:
            decision = selected.review(review_context(state))
        except (RuntimeError, TypeError):
            state["ai_review"] = {
                "status": "unavailable",
                "provider": selected.name,
                "rounds": rounds - 1,
                "input_hash": current_hash,
                "items": local_items,
                "automatic_corrections": corrections,
                "message": "The AI review provider could not be reached.",
            }
            return state
        if (
            decision.corrected_script_blocks
            and decision.status == "needs_fix"
            and rounds == 1
            and not state.get("script", {}).get("narration_owned_by_v2")
        ):
            old_scenes = copy.deepcopy(state.get("scenes", []))
            state["script"]["blocks"] = _normalise_blocks(
                decision.corrected_script_blocks, int(state["duration"]["max_seconds"])
            )
            hook_block = next(
                (
                    block
                    for block in state["script"]["blocks"]
                    if str(block.get("role") or "").casefold() == "hook"
                ),
                None,
            )
            if hook_block and str(hook_block.get("text") or "").strip():
                state["script"]["selected_hook"] = str(hook_block["text"]).strip()
            _refresh_script_derivatives(state, old_scenes=old_scenes)
            corrections.append(
                "Rewrote the narration for language, directness, brevity, and clean spoken text."
            )
            continue
        if (
            decision.corrected_script_blocks
            and decision.status == "needs_fix"
            and state.get("script", {}).get("narration_owned_by_v2")
        ):
            corrections.append("Ignored narration correction because Script Writer V2 owns the body.")
            decision = decision.model_copy(update={"corrected_script_blocks": None})
        break
    assert decision is not None
    merged_items = local_review_items(state) + [item.model_dump() for item in decision.items]
    has_error = any(item["severity"] == "error" for item in merged_items)
    has_warning = any(item["severity"] == "warning" for item in merged_items)
    status = "needs_fix" if has_error else decision.status
    if status == "passed" and has_warning:
        status = "passed_with_warnings"
    state["ai_review"] = {
        "status": status,
        "provider": selected.name,
        "rounds": rounds,
        "input_hash": review_input_hash(state),
        "items": merged_items,
        "automatic_corrections": corrections,
        "message": "Review completed.",
    }
    return state
