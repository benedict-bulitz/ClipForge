"""Script & Story Quality V1.

This is the last content-only pass before narration becomes production input.
It composes the existing information-gain, Story Arc and payoff authorities;
it does not invent a second hook, reveal plan, fact model or duration target.

The deterministic editor can only remove weak material, restore Story Arc
ordering, or replace a vague line with the exact text of a cited supported
fact.  An optional provider may propose a broader wording edit, but that edit
is accepted only when every new content word and number is traceable to the
original script or the cited research facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .novelty import (
    assess_information_gain,
    fact_is_supported,
    prune_redundant_information,
)
from .payoff import reveals_protected_payoff, trim_post_payoff_fluff
from .story_arc import arc_units, is_explanatory_question, order_blocks_for_reveal
from .verbal_hook import information_gain, proposition_words

QUALITY_VERSION = 1
QUALITY_DIMENSIONS = (
    "information_density",
    "information_gain",
    "relevance_to_core_question",
    "redundancy",
    "specificity",
    "clarity",
    "logical_progression",
    "curiosity_progression",
    "payoff_strength",
    "post_payoff_efficiency",
    "factual_support",
    "spoken_naturalness",
)

_GENERIC = re.compile(
    r"(?i)^\s*(?:this|that|it|dies|das|es)\s+(?:is|can be|war|ist|kann)\s+"
    r"(?:important|interesting|significant|remarkable|complex|fascinating|"
    r"wichtig|interessant|bedeutend|bemerkenswert|komplex|faszinierend)\W*$"
)
_TRANSITION_ONLY = re.compile(
    r"(?i)^\s*(?:and|but|so|therefore|meanwhile|und|aber|also|deshalb|währenddessen)\W*$"
)
_POST_PAYOFF_FLUFF = re.compile(
    r"(?i)^\s*(?:thanks? for watching|follow for more|like and subscribe|see you next time|"
    r"danke fürs (?:zuschauen|ansehen)|folge für mehr|bis zum nächsten mal|pretty (?:wild|cool)|"
    r"ziemlich (?:wild|cool|verrückt))\b"
)
_COPULAR_LABEL = re.compile(
    r"(?i)^\W*(?:that|this|it|those|these|the|das|dies|dieses|diese|dieser)\b"
    r"[^.!?]{0,45}\b(?:is|are|was|were|means?|refers? to|ist|sind|war|bedeutet|nennt man)\b"
)
_VAGUE_CAUSE = re.compile(
    r"(?i)\b(?:caused by|happens? because of|comes? from|verursacht durch|entsteht durch)\s+"
    r"(?:something|things?|a process|some process|different factors?|various factors?|"
    r"etwas|einen prozess|verschiedene faktoren)\b"
)
_WORDS = re.compile(r"[a-zäöüß0-9]+", re.IGNORECASE)
_NUMBERS = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*%)?\b")
_SPECIFICITY_FLOOR = 0.45
_COMMON_EDITOR_WORDS = {
    "a", "an", "and", "are", "as", "at", "because", "but", "by", "can", "does", "for",
    "from", "has", "have", "in", "is", "it", "its", "not", "of", "on", "or", "so", "that",
    "the", "their", "then", "there", "therefore", "this", "to", "until", "was", "when", "which",
    "while", "with", "without", "you", "your", "ein", "eine", "einer", "eines", "als", "aber",
    "auch", "auf", "aus", "bei", "bis", "das", "dass", "darum", "der", "des", "deshalb", "die",
    "dies", "durch", "er", "es", "für", "hat", "im", "ist", "mit", "nicht",
    "oder", "sich", "sie", "und", "von", "wenn", "wird", "zu", "zum", "zur",
}


class QualityEditBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["hook", "answer", "explanation", "support", "payoff", "detail", "status"]
    text: str = Field(min_length=1, max_length=1_200)
    fact_ids: list[str] = Field(default_factory=list, max_length=8)


class QualityProviderIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: str = Field(min_length=2, max_length=64)
    severity: Literal["info", "warning", "error"]
    segment_id: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=2, max_length=280)
    suggested_action: str = Field(min_length=2, max_length=160)


class QualityProviderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["remove", "merge", "reorder", "tighten", "strengthen_payoff", "replace_with_supported_specific"]
    segment_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=2, max_length=240)


class QualityProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["approve", "revise", "needs_research"]
    issues: list[QualityProviderIssue] = Field(default_factory=list, max_length=20)
    actions: list[QualityProviderAction] = Field(default_factory=list, max_length=20)
    blocks: list[QualityEditBlock] | None = Field(default=None, max_length=12)
    research_need: str = Field(default="", max_length=320)


@dataclass(frozen=True)
class QualityProviderResult:
    response: QualityProviderResponse | None
    status: Literal["approved", "revised", "needs_research", "provider_error", "validation_error"]
    error: str | None = None


@dataclass(frozen=True)
class QualityRequest:
    prompt: str
    language: str
    blocks: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    story_arc: dict[str, Any]
    payoff_plan: dict[str, Any]
    triple_hook: dict[str, Any]
    reaction_plan: dict[str, Any]
    detected_issues: list[dict[str, Any]]

    def model_input(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "language": self.language,
            "blocks": self.blocks,
            "facts": [
                {
                    "id": fact.get("id"),
                    "claim": fact.get("claim"),
                    "verification": fact.get("verification"),
                    "supported": fact_is_supported(fact),
                }
                for fact in self.facts
            ],
            "story_arc": self.story_arc,
            "payoff_plan": self.payoff_plan,
            "triple_hook": self.triple_hook,
            "reaction_plan": self.reaction_plan,
            "detected_issues": self.detected_issues,
        }


class ScriptStoryQualityProvider(Protocol):
    name: str

    def edit(self, request: QualityRequest) -> QualityProviderResult: ...


SCRIPT_STORY_QUALITY_V1_INSTRUCTIONS = (
    "You are ClipForge's Script & Story Quality V1 editor. Tighten the complete spoken script without "
    "changing its answer, selected hook, protected reveal intent, fact IDs, or research meaning. Remove "
    "filler, redundancy, paraphrase, weak transitions, unnecessary setup and post-payoff fluff; reorder "
    "only when Story Arc dependencies allow it; replace vague wording only with specifics stated in the "
    "supplied facts; and strengthen payoff wording only from those facts. Every beat must add useful new "
    "information toward the original question. Never invent a fact, number, scene, hook or duration target. "
    "Never move a protected answer earlier. Keep the first hook block byte-for-byte unchanged. Preserve all "
    "non-optional research facts and stop immediately after concise closure. If the evidence cannot support a "
    "stronger complete answer, return needs_research instead of guessing. Return only the structured output."
)


class OpenAIScriptStoryQualityProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_worker_model

    def edit(self, request: QualityRequest) -> QualityProviderResult:
        try:
            response = self._client.responses.parse(
                model=self._model,
                instructions=SCRIPT_STORY_QUALITY_V1_INSTRUCTIONS,
                input=json.dumps(request.model_input(), ensure_ascii=False),
                text_format=QualityProviderResponse,
                max_output_tokens=3_000,
                store=False,
            )
            parsed = response.output_parsed
            if not isinstance(parsed, QualityProviderResponse):
                return QualityProviderResult(None, "provider_error", "No parsed Script & Story Quality V1 result")
            if parsed.status == "approve":
                if parsed.blocks is not None:
                    return QualityProviderResult(None, "validation_error", "Approved response must not replace blocks")
                return QualityProviderResult(parsed, "approved")
            if parsed.status == "needs_research":
                return QualityProviderResult(parsed, "needs_research")
            if not parsed.blocks:
                return QualityProviderResult(None, "validation_error", "Revised response must include blocks")
            return QualityProviderResult(parsed, "revised")
        except (OpenAIError, ValueError, TypeError) as exc:
            return QualityProviderResult(None, "provider_error", str(exc)[:240])


def script_quality_signature(blocks: list[dict[str, Any]]) -> str:
    payload = [
        [
            str(block.get("id") or ""),
            str(block.get("role") or ""),
            " ".join(str(block.get("text") or "").split()),
            [str(item) for item in block.get("fact_ids") or []],
        ]
        for block in blocks
        if isinstance(block, dict)
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:20]


def _state(blocks: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    state = copy.deepcopy(context)
    state["script"] = {**(state.get("script") or {}), "blocks": copy.deepcopy(blocks)}
    state["script"]["text"] = " ".join(str(block.get("text") or "") for block in blocks)
    return state


def _body_units(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [unit for unit in report.get("units") or [] if unit.get("category") != "hook"]


def _supported_facts(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(fact.get("id")): fact
        for fact in context.get("facts") or []
        if isinstance(fact, dict) and fact.get("id") and fact_is_supported(fact)
    }


def _mechanism_fact_ids(context: dict[str, Any]) -> set[str]:
    arc = context.get("story_arc") if isinstance(context.get("story_arc"), dict) else {}
    contract = arc.get("question_contract") if isinstance(arc.get("question_contract"), dict) else {}
    spine = contract.get("explanation_spine") if isinstance(contract.get("explanation_spine"), dict) else {}
    explicit = {str(item) for item in spine.get("mechanism") or []}
    if explicit:
        return explicit
    return {
        fact_id
        for fact_id, unit in arc_units(arc).items()
        if str(unit.get("role") or "").casefold() in {"cause", "explanation", "mechanism"}
    }


def _claim_coverage(text: str, claims: list[str]) -> tuple[float, list[str]]:
    """How much supported claim meaning is present in ``text``.

    ``information_gain`` supplies the repository's synonym/inflection-aware
    comparison.  Here the direction is intentionally reversed: facts are the
    candidate sentence, so their remaining gain terms are the specifics the
    script failed to say.
    """
    claim_text = " ".join(claim for claim in claims if claim)
    terms = proposition_words(claim_text)
    if not terms:
        return 1.0, []
    missing = [
        item for item in information_gain(text, claim_text)
        if item != "negation" and not _NUMBERS.fullmatch(item)
    ]
    return max(0.0, 1.0 - len(set(missing)) / len(terms)), list(dict.fromkeys(missing))


def _semantic_quality_signals(
    report: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Meaning-level signals that lexical novelty and role labels cannot prove."""
    facts = _supported_facts(context)
    mechanism_ids = _mechanism_fact_ids(context) & set(facts)
    arc = context.get("story_arc") if isinstance(context.get("story_arc"), dict) else {}
    question = str(
        (context.get("intent") or {}).get("question")
        or arc.get("primary_question")
        or context.get("prompt")
        or ""
    )
    explanatory = is_explanatory_question(question)
    tautological_answers: dict[str, dict[str, Any]] = {}
    specificity_gaps: dict[str, dict[str, Any]] = {}
    analogy_repetitions: set[str] = set()
    coverage_by_id: dict[str, float] = {}
    units = _body_units(report)
    carrier_texts: dict[str, list[str]] = {}
    for unit in units:
        for fact_id in (unit.get("evidence") or {}).get("fact_ids") or []:
            carrier_texts.setdefault(str(fact_id), []).append(str(unit.get("text") or ""))

    for unit in units:
        block_id = str(unit.get("block_id") or "")
        fact_ids = [str(item) for item in (unit.get("evidence") or {}).get("fact_ids") or []]
        cited = [facts[fact_id] for fact_id in fact_ids if fact_id in facts]
        claims = [str(fact.get("claim") or "") for fact in cited]
        coverage, _missing = _claim_coverage(str(unit.get("text") or ""), claims)
        coverage_by_id[block_id] = coverage
        cited_mechanisms = [facts[fact_id] for fact_id in fact_ids if fact_id in mechanism_ids]
        aggregate_mechanism_text = " ".join(
            text
            for fact_id in fact_ids if fact_id in mechanism_ids
            for text in carrier_texts.get(fact_id, [])
        )
        mechanism_coverage, mechanism_missing = _claim_coverage(
            aggregate_mechanism_text,
            [str(fact.get("claim") or "") for fact in cited_mechanisms],
        )

        if unit.get("hook_spent"):
            analogy_repetitions.add(block_id)

        if (
            cited_mechanisms
            and mechanism_coverage < _SPECIFICITY_FLOOR
            and len(mechanism_missing) >= 3
            and not (unit.get("is_payoff") and (report.get("payoff") or {}).get("status") == "pass")
        ):
            specificity_gaps[block_id] = {
                "coverage": round(mechanism_coverage, 3),
                "missing_terms": mechanism_missing[:8],
                "fact_ids": [str(fact.get("id")) for fact in cited_mechanisms],
            }

        if explanatory and str(unit.get("role") or "").casefold() == "answer" and mechanism_ids:
            all_mechanism_coverage = max(
                (
                    _claim_coverage(
                        str(unit.get("text") or ""),
                        [str(facts[fact_id].get("claim") or "")],
                    )[0]
                    for fact_id in mechanism_ids
                ),
                default=0.0,
            )
            label_only = bool(_COPULAR_LABEL.search(str(unit.get("text") or "")))
            vague_cause = bool(_VAGUE_CAUSE.search(str(unit.get("text") or "")))
            # A why/how answer must state a real result or mechanism.  A
            # copular label is accepted only when it carries a substantial
            # portion of a supported mechanism (for example "a cloud of ice
            # crystals"), not merely a new name for the subject.
            if vague_cause or (label_only and all_mechanism_coverage < _SPECIFICITY_FLOOR):
                tautological_answers[block_id] = {
                    "coverage": round(all_mechanism_coverage, 3),
                    "reason": (
                        "The answer uses a vague causal placeholder instead of the supported mechanism."
                        if vague_cause
                        else "The answer only labels or redescribes the subject instead of answering the explanatory question."
                    ),
                }

    payoff = report.get("payoff") if isinstance(report.get("payoff"), dict) else {}
    failed_payoff_id = str(payoff.get("block_id") or "") if payoff.get("status") == "fail" else ""
    disqualified = set(tautological_answers) | set(specificity_gaps) | analogy_repetitions
    if failed_payoff_id:
        disqualified.add(failed_payoff_id)
    meaningful = [
        unit for unit in units
        if str(unit.get("block_id") or "") not in disqualified
        and unit.get("counts_as_gain")
        and unit.get("beat_class") == "useful_gain"
        and unit.get("explanatory_delta") not in {"context_only", "restatement", "tangent", "weak_value"}
    ]
    meaningful_mechanisms = [
        unit for unit in meaningful
        if {str(item) for item in (unit.get("evidence") or {}).get("fact_ids") or []} & mechanism_ids
    ]
    ratio = len(meaningful) / len(units) if units else 0.0
    low_information_gain = bool(
        units
        and (
            (len(units) >= 3 and (len(meaningful) < 2 or ratio < 0.5))
            or (explanatory and mechanism_ids and not meaningful_mechanisms)
        )
    )
    return {
        "tautological_answers": tautological_answers,
        "specificity_gaps": specificity_gaps,
        "analogy_repetitions": analogy_repetitions,
        "failed_payoff_id": failed_payoff_id,
        "meaningful_ids": {str(unit.get("block_id") or "") for unit in meaningful},
        "meaningful_ratio": ratio,
        "low_information_gain": low_information_gain,
        "coverage_by_id": coverage_by_id,
    }


def _issue(
    issue_type: str,
    severity: str,
    reason: str,
    suggested_action: str,
    unit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "segment_id": unit.get("block_id") if unit else None,
        "segment": unit.get("text") if unit else None,
        "reason": reason[:320],
        "suggested_action": suggested_action[:180],
    }


def _dependency_violations(blocks: list[dict[str, Any]], arc: dict[str, Any]) -> list[dict[str, Any]]:
    units = arc_units(arc)
    seen: set[str] = set()
    violations: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if str(block.get("role") or "").casefold() == "hook":
            continue
        own = {str(item) for item in block.get("fact_ids") or []}
        missing = {
            str(dependency)
            for fact_id in own
            for dependency in units.get(fact_id, {}).get("depends_on") or []
            if str(dependency) not in seen and str(dependency) not in own
        }
        if missing:
            violations.append({"index": index, "block": block, "missing": sorted(missing)})
        seen |= own
    return violations


def _quality_scores(
    report: dict[str, Any], blocks: list[dict[str, Any]], context: dict[str, Any]
) -> dict[str, int]:
    body = _body_units(report)
    total = max(1, len(body))
    semantic = _semantic_quality_signals(report, context)
    gain = [unit for unit in body if str(unit.get("block_id") or "") in semantic["meaningful_ids"]]
    relevant = [unit for unit in body if unit.get("beat_class") != "off_chain"]
    supported = [
        unit for unit in body
        if (unit.get("evidence") or {}).get("status") in {"supported", "derived", "not_applicable"}
    ]
    clear = [unit for unit in body if (unit.get("language") or {}).get("status") != "complex"]
    specific = [
        unit for unit in body
        if str(unit.get("block_id") or "") in semantic["meaningful_ids"]
        and (
            unit.get("category") in {"mechanism", "quantitative", "contrast", "resolution"}
            or semantic["coverage_by_id"].get(str(unit.get("block_id") or ""), 0.0) >= _SPECIFICITY_FLOOR
        )
    ]
    redundant_ids = {
        str(unit.get("block_id") or "") for unit in body if unit.get("redundancy") != "none"
    } | set(semantic["tautological_answers"]) | semantic["analogy_repetitions"]
    order_violations = _dependency_violations(blocks, context.get("story_arc") or {})
    payoff = report.get("payoff") or {}
    payoff_score = 100 if payoff.get("result") == "strong" else 75 if payoff.get("status") == "pass" else 25
    payoff_indexes = [index for index, block in enumerate(blocks) if str(block.get("role") or "").casefold() == "payoff"]
    tail = blocks[max(payoff_indexes) + 1 :] if payoff_indexes else []
    weak_tail = sum(1 for unit in body if unit.get("weak_tail"))
    protected = bool((context.get("story_arc") or {}).get("curiosity_gap", {}).get("withhold_answer"))
    premature = _premature_reveal(blocks, context)
    avg_gain = sum(float(unit.get("information_gain_score") or 0.0) for unit in gain) / total
    return {
        "information_density": round(100 * len(gain) / total),
        "information_gain": round(100 * avg_gain),
        "relevance_to_core_question": round(100 * len(relevant) / total),
        "redundancy": round(100 * (total - len(redundant_ids)) / total),
        "specificity": round(100 * len(specific) / total),
        "clarity": round(100 * len(clear) / total),
        "logical_progression": max(0, 100 - 30 * len(order_violations)),
        "curiosity_progression": 100 if not protected or not premature else 20,
        "payoff_strength": payoff_score,
        "post_payoff_efficiency": max(0, 100 - 40 * (len(tail) + weak_tail)),
        "factual_support": round(100 * len(supported) / total),
        "spoken_naturalness": round(100 * len(clear) / total),
    }


def _premature_reveal(blocks: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    arc = context.get("story_arc") if isinstance(context.get("story_arc"), dict) else {}
    payoff = context.get("payoff_plan") if isinstance(context.get("payoff_plan"), dict) else {}
    if not (arc.get("curiosity_gap") or {}).get("withhold_answer") and not payoff.get("hook_must_not_reveal"):
        return []
    primary = str(arc.get("primary_answer_id") or "")
    units = arc_units(arc)
    dependencies = {str(item) for item in units.get(primary, {}).get("depends_on") or []}
    present = {str(item) for block in blocks for item in block.get("fact_ids") or []}
    required_here = dependencies & present
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        role = str(block.get("role") or "").casefold()
        fact_ids = {str(item) for item in block.get("fact_ids") or []}
        # Fact identity is authoritative. A provider's broad ``answer`` role
        # can contain setup, while Triple Hook/payoff validators already own
        # lexical hook-spoiler detection.
        states_answer = primary in fact_ids if primary else role == "answer"
        if role == "hook" and fact_ids & {str(item) for item in (arc.get("hook") or {}).get("protected_ids") or []}:
            found.append({"index": index, "block": block, "reason": "The hook carries a protected payoff fact."})
        elif states_answer and required_here - seen - fact_ids:
            found.append({
                "index": index,
                "block": block,
                "reason": "The protected answer appears before dependencies "
                + ", ".join(sorted(required_here - seen - fact_ids))
                + ".",
            })
        seen |= fact_ids
    return found


def assess_script_story_quality(
    blocks: list[dict[str, Any]], context: dict[str, Any]
) -> dict[str, Any]:
    """Return structured, deterministic Script & Story Quality V1 diagnostics."""
    state = _state(blocks, context)
    gain_report = assess_information_gain(state)
    body = _body_units(gain_report)
    by_id = {str(unit.get("block_id") or ""): unit for unit in gain_report.get("units") or []}
    issues: list[dict[str, Any]] = []
    mapped: set[tuple[str, str | None]] = set()

    mapping = {
        "filler_segment": ("filler", "error", "Remove the filler segment."),
        "redundant_segment": ("redundancy", "warning", "Remove or merge the repeated idea."),
        "unsupported_information": ("unsupported_claim", "error", "Remove it or replace it with existing supported research."),
        "off_question_segment": ("irrelevant_segment", "warning", "Remove it unless it directly helps answer the core question."),
        "complex_language": ("clarity", "warning", "Tighten into natural spoken language without changing the fact."),
        "information_plateau": ("artificial_lengthening", "error", "Cut the run of beats that adds no information."),
        "weak_tail": ("post_payoff_fluff", "warning", "End when the explanation or payoff is complete."),
        "low_explanatory_value": ("low_information_line", "warning", "Remove it unless it is necessary to understand the answer."),
        "answer_insufficient": ("too_thin", "error", "Request the missing research; do not pad the script."),
        "no_supported_information_gain": ("too_thin", "error", "Request supported substance; do not lengthen artificially."),
        "no_question_relevant_information": ("too_thin", "error", "Research the actual question before production."),
        "supported_gain_unused": ("low_information_line", "info", "Use an existing stronger fact if it improves the answer."),
        "hook_body_no_information_gain": ("weak_transition", "warning", "Make the first body beat advance beyond the hook."),
        "hook_analogy_reuse": ("analogy_repetition", "error", "Remove the repeated analogy unless it adds a new supported explanation."),
    }
    for item in gain_report.get("issues") or []:
        code = str(item.get("code") or "")
        unit = by_id.get(str(item.get("block_id") or ""))
        if code == "payoff_unsupported":
            target = ("unsupported_claim", "error", "Remove it or replace it with existing supported research.")
        elif code in {"payoff_missing", "payoff_weak_resolution"} or code.startswith("payoff_"):
            target = ("weak_payoff", "error", "End on the strongest supported resolution instead of a restatement.")
        elif code in mapping:
            target = mapping[code]
        else:
            continue
        issue_type, severity, action = target
        if code == "answer_insufficient":
            severity = str(item.get("severity") or severity)
        if code == "redundant_segment" and unit and unit.get("category") == "paraphrase":
            issue_type = "repeated_paraphrase"
        key = (issue_type, unit.get("block_id") if unit else None)
        if key in mapped:
            continue
        mapped.add(key)
        issues.append(_issue(issue_type, severity, str(item.get("message") or code), action, unit))

    semantic = _semantic_quality_signals(gain_report, context)
    for block_id, finding in semantic["tautological_answers"].items():
        unit = by_id.get(block_id)
        key = ("tautological_answer", block_id)
        if key not in mapped:
            mapped.add(key)
            issues.append(_issue(
                "tautological_answer",
                "error",
                str(finding["reason"]),
                "State the supported result or mechanism that actually answers the question.",
                unit,
            ))
    for block_id, finding in semantic["specificity_gaps"].items():
        unit = by_id.get(block_id)
        key = ("vague_mechanism", block_id)
        if key not in mapped:
            mapped.add(key)
            issues.append(_issue(
                "vague_mechanism",
                "warning",
                "The line cites mechanism research but omits its concrete explanatory content: "
                + ", ".join(finding["missing_terms"])
                + ".",
                "Replace the vague wording with the exact cited mechanism claim or request a grounded rewrite.",
                unit,
            ))
    if semantic["low_information_gain"]:
        issues.append(_issue(
            "low_information_gain",
            "error",
            f"Only {len(semantic['meaningful_ids'])} of {len(body)} body beats add supported explanatory meaning.",
            "Remove labels, repeated analogies and circular closure; use the supported mechanism instead.",
        ))

    facts_by_id = {
        str(fact.get("id")): fact
        for fact in context.get("facts") or []
        if isinstance(fact, dict) and fact.get("id")
    }
    for unit in body:
        text = str(unit.get("text") or "")
        if _GENERIC.match(text) or _TRANSITION_ONLY.match(text):
            fact_ids = [item for item in (unit.get("evidence") or {}).get("fact_ids") or [] if item in facts_by_id]
            supported = any(fact_is_supported(facts_by_id[item]) for item in fact_ids)
            issues.append(_issue(
                "generic_statement",
                "warning" if supported else "error",
                "The line is generic and contributes no concrete, question-relevant detail.",
                "Replace it with the cited supported fact." if supported else "Return a needs-research issue; do not invent specificity.",
                unit,
            ))
        elif unit.get("beat_class") == "weak_value" and unit.get("redundancy") == "none":
            issues.append(_issue(
                "low_information_line",
                "warning",
                "The line changes wording or tone without materially advancing the explanation.",
                "Remove it unless another beat depends on it.",
                unit,
            ))

    for violation in _dependency_violations(blocks, context.get("story_arc") or {}):
        unit = by_id.get(str(violation["block"].get("id") or ""))
        issues.append(_issue(
            "poor_fact_ordering",
            "warning",
            "This beat appears before required context: " + ", ".join(violation["missing"]) + ".",
            "Move it after its Story Arc dependencies without moving the protected reveal earlier.",
            unit,
        ))
    for reveal in _premature_reveal(blocks, context):
        unit = by_id.get(str(reveal["block"].get("id") or ""))
        issues.append(_issue(
            "premature_reveal",
            "error",
            reveal["reason"],
            "Restore the protected reveal order and keep the selected hook intent intact.",
            unit,
        ))

    payoff_indexes = [index for index, block in enumerate(blocks) if str(block.get("role") or "").casefold() == "payoff"]
    if payoff_indexes:
        for block in blocks[max(payoff_indexes) + 1 :]:
            unit = by_id.get(str(block.get("id") or ""))
            text = str(block.get("text") or "")
            if unit and (
                unit.get("redundancy") != "none"
                or unit.get("weak_tail")
                or _GENERIC.match(text)
                or _POST_PAYOFF_FLUFF.match(text)
            ):
                issues.append(_issue(
                    "post_payoff_fluff",
                    "error" if _GENERIC.match(text) or _POST_PAYOFF_FLUFF.match(text) else "warning",
                    "This segment continues after the payoff without adding useful closure.",
                    "Remove it and end on the payoff.",
                    unit,
                ))

    scores = _quality_scores(gain_report, blocks, context)
    if any(item["issue_type"] == "artificial_lengthening" for item in issues):
        length_assessment = "unnecessary_length"
    elif any(item["issue_type"] == "too_thin" for item in issues) or gain_report.get("status") in {"empty", "thin"}:
        length_assessment = "too_thin"
    else:
        length_assessment = "appropriately_concise"
    has_supported_fact = any(
        isinstance(fact, dict) and fact_is_supported(fact) for fact in context.get("facts") or []
    )
    research_insufficient = any(
        item["issue_type"] in {"unsupported_claim", "too_thin"} and item["severity"] == "error"
        for item in issues
    ) or (not has_supported_fact and any(item["issue_type"] == "generic_statement" for item in issues))
    blockers = sorted({item["issue_type"] for item in issues if item["severity"] == "error"})
    return {
        "version": QUALITY_VERSION,
        "signature": script_quality_signature(blocks),
        "score_semantics": "0-100; higher is better",
        "dimensions": {name: int(scores[name]) for name in QUALITY_DIMENSIONS},
        "issues": issues,
        "length_assessment": length_assessment,
        "research_insufficient": research_insufficient,
        "protected_payoff_constrained_edit": bool((context.get("story_arc") or {}).get("curiosity_gap", {}).get("withhold_answer")),
        "gate": {
            "ready": not blockers,
            "status": "passed" if not issues else ("blocked" if blockers else "passed_with_warnings"),
            "blocking": blockers,
        },
        "information_gain": gain_report,
    }


def _supported_fact_replacement(
    blocks: list[dict[str, Any]], context: dict[str, Any], assessment: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts = {
        str(fact.get("id")): fact
        for fact in context.get("facts") or []
        if isinstance(fact, dict) and fact.get("id") and fact_is_supported(fact)
    }
    generic_ids = {
        str(item.get("segment_id") or "")
        for item in assessment.get("issues") or []
        if item.get("issue_type") == "generic_statement"
    }
    vague_mechanism_ids = {
        str(item.get("segment_id") or "")
        for item in assessment.get("issues") or []
        if item.get("issue_type") == "vague_mechanism"
    }
    edited = copy.deepcopy(blocks)
    actions: list[dict[str, Any]] = []
    protected_reveal = bool(
        ((context.get("story_arc") or {}).get("curiosity_gap") or {}).get("withhold_answer")
    )
    for block in edited:
        block_id = str(block.get("id") or "")
        if block_id not in generic_ids | vague_mechanism_ids or str(block.get("role") or "").casefold() in {"hook", "payoff"}:
            continue
        fact_ids = [str(item) for item in block.get("fact_ids") or []]
        selected_ids = [fact_id for fact_id in fact_ids if fact_id in facts]
        if block_id in generic_ids and len(selected_ids) != 1:
            continue
        if block_id in vague_mechanism_ids:
            mechanism_ids = _mechanism_fact_ids(context)
            selected_ids = [fact_id for fact_id in selected_ids if fact_id in mechanism_ids][:3]
        claims = list(dict.fromkeys(
            " ".join(str(facts[fact_id].get("claim") or "").split())
            for fact_id in selected_ids
            if str(facts[fact_id].get("claim") or "").strip()
        ))
        replacement = " ".join(claims)[:1_200].strip()
        if not replacement or replacement == str(block.get("text") or ""):
            continue
        protected_ids = {
            str(item)
            for item in ((context.get("story_arc") or {}).get("hook") or {}).get("protected_ids") or []
        }
        if protected_reveal and (
            set(selected_ids) & protected_ids
            or reveals_protected_payoff(replacement, context.get("payoff_plan") or {})
        ):
            continue
        before = str(block.get("text") or "")
        block["text"] = replacement
        actions.append({
            "action": "replace_with_supported_specific",
            "segment_ids": [block.get("id")],
            "before": before,
            "after": replacement,
            "fact_ids": selected_ids,
            "reason": "Replaced vague wording with exact cited research claim text.",
        })
    return edited, actions


def _deterministic_edit(
    blocks: list[dict[str, Any]], context: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    before_order = [str(block.get("id") or "") for block in blocks]
    ordered = order_blocks_for_reveal(copy.deepcopy(blocks), context.get("story_arc") or {})
    actions: list[dict[str, Any]] = []
    if [str(block.get("id") or "") for block in ordered] != before_order:
        actions.append({
            "action": "reorder",
            "segment_ids": [str(block.get("id") or "") for block in ordered],
            "before_order": before_order,
            "after_order": [str(block.get("id") or "") for block in ordered],
            "reason": "Restored Story Arc dependency and protected-reveal order.",
        })
    pruned, repairs = prune_redundant_information(ordered, _state(ordered, context))
    for repair in repairs:
        action = str(repair.get("action") or "")
        actions.append({
            "action": "merge" if "merge" in action or action == "hand_over_payoff" else "tighten" if action == "trim_lead_in" else "remove",
            "segment_ids": [str(repair.get("block_id") or "")],
            "reason": str(repair.get("category") or action).replace("_", " "),
            "details": repair,
        })
    trimmed, did_trim = trim_post_payoff_fluff(pruned)
    if did_trim:
        removed = [str(block.get("id") or "") for block in pruned[len(trimmed) :]]
        actions.append({
            "action": "remove",
            "segment_ids": removed,
            "reason": "Removed generic post-payoff outro.",
        })
    # Unsupported provider prose with no cited fact is safe to delete. When it
    # was only a closing flourish, the preceding supported beat becomes the
    # natural ending; no new wording or fact is introduced.
    gain = assess_information_gain(_state(trimmed, context))
    unsupported_ids = {
        str(unit.get("block_id") or "")
        for unit in gain.get("units") or []
        if unit.get("category") != "hook"
        and (unit.get("evidence") or {}).get("status") == "unsupported"
        and not (unit.get("evidence") or {}).get("fact_ids")
    }
    if unsupported_ids:
        supported_facts = {
            str(fact.get("id")): fact
            for fact in context.get("facts") or []
            if isinstance(fact, dict) and fact.get("id") and fact_is_supported(fact)
        }
        kept: list[dict[str, Any]] = []
        for block in trimmed:
            if str(block.get("id") or "") not in unsupported_ids:
                kept.append(block)
                continue
            if str(block.get("role") or "").casefold() == "payoff":
                carrier = next(
                    (
                        prior
                        for prior in reversed(kept)
                        if any(str(fact_id) in supported_facts for fact_id in prior.get("fact_ids") or [])
                    ),
                    None,
                )
                if carrier is not None:
                    before = str(block.get("text") or "")
                    previous_role = str(carrier.get("role") or "")
                    carrier["role"] = "payoff"
                    carrier["fact_ids"] = list(dict.fromkeys([
                        *[str(item) for item in carrier.get("fact_ids") or []],
                        *[str(item) for item in block.get("fact_ids") or []],
                    ]))
                    actions.append({
                        "action": "strengthen_payoff",
                        "segment_ids": [str(block.get("id") or "")],
                        "before": before,
                        "after": str(carrier.get("text") or ""),
                        "fact_ids": list(carrier.get("fact_ids") or []),
                        "reason": "Removed an unsupported closing flourish and ended on the preceding supported answer.",
                        "details": {
                            "action": "hand_over_unsupported_payoff",
                            "block_id": block.get("id"),
                            "text": before,
                            "payoff_block_id": carrier.get("id"),
                            "payoff_previous_role": previous_role,
                        },
                    })
                    continue
            actions.append({
                "action": "remove",
                "segment_ids": [str(block.get("id") or "")],
                "reason": "Removed an unsupported line with no research fact reference.",
                "details": {
                    "action": "remove_unsupported",
                    "block_id": block.get("id"),
                    "text": block.get("text"),
                    "category": "unsupported",
                },
            })
        if kept and any(str(block.get("role") or "").casefold() == "payoff" for block in trimmed) and not any(
            str(block.get("role") or "").casefold() == "payoff" for block in kept
        ):
            last = next((block for block in reversed(kept) if str(block.get("role") or "").casefold() != "hook"), None)
            if last is not None and str(last.get("role") or "").casefold() != "answer":
                last["role"] = "payoff"
        trimmed = kept
    assessment = assess_script_story_quality(trimmed, context)
    specific, replacements = _supported_fact_replacement(trimmed, context, assessment)
    return specific, [*actions, *replacements]


def _meaningful_words(value: object) -> set[str]:
    return {
        word.casefold()
        for word in _WORDS.findall(str(value or ""))
        if len(word) > 1 and not word.isdigit()
    }


def _essential_fact_ids(context: dict[str, Any], original: list[dict[str, Any]]) -> set[str]:
    arc = context.get("story_arc") if isinstance(context.get("story_arc"), dict) else {}
    units = arc_units(arc)
    essential = {
        fact_id for fact_id, unit in units.items() if not unit.get("may_be_omitted")
    }
    essential |= {str(arc.get(key)) for key in ("primary_answer_id", "final_payoff_id") if arc.get(key)}
    original_ids = {str(item) for block in original for item in block.get("fact_ids") or []}
    return essential & original_ids


def validate_provider_blocks(
    original: list[dict[str, Any]], candidate: list[dict[str, Any]], context: dict[str, Any]
) -> list[str]:
    """Reject any provider edit that cannot be traced to current evidence."""
    errors: list[str] = []
    hooks = [block for block in candidate if str(block.get("role") or "").casefold() == "hook"]
    original_hook = next((block for block in original if str(block.get("role") or "").casefold() == "hook"), None)
    if original_hook:
        if len(hooks) != 1 or candidate[0] is not hooks[0] or str(hooks[0].get("text") or "") != str(original_hook.get("text") or ""):
            errors.append("selected hook changed")
    elif hooks:
        errors.append("provider added a hook")

    facts = {
        str(fact.get("id")): fact
        for fact in context.get("facts") or []
        if isinstance(fact, dict) and fact.get("id")
    }
    known = set(facts)
    candidate_ids = {str(item) for block in candidate for item in block.get("fact_ids") or []}
    missing = _essential_fact_ids(context, original) - candidate_ids
    if missing:
        errors.append("important research facts were discarded: " + ", ".join(sorted(missing)))
    original_words = _meaningful_words(" ".join(str(block.get("text") or "") for block in original))
    original_numbers = set(_NUMBERS.findall(" ".join(str(block.get("text") or "") for block in original)))
    for index, block in enumerate(candidate):
        fact_ids = [str(item) for item in block.get("fact_ids") or []]
        unknown = sorted(set(fact_ids) - known)
        if unknown:
            errors.append(f"block {index + 1} references unknown fact IDs: {', '.join(unknown)}")
            continue
        unsupported = [fact_id for fact_id in fact_ids if not fact_is_supported(facts[fact_id])]
        if unsupported:
            errors.append(f"block {index + 1} references unsupported fact IDs: {', '.join(unsupported)}")
        evidence = " ".join(str(facts[fact_id].get("claim") or "") for fact_id in fact_ids if fact_id in facts)
        allowed_words = original_words | _meaningful_words(evidence) | _COMMON_EDITOR_WORDS
        new_words = sorted(_meaningful_words(block.get("text")) - allowed_words)
        if new_words:
            errors.append(f"block {index + 1} adds untraceable words: {', '.join(new_words[:8])}")
        allowed_numbers = original_numbers | set(_NUMBERS.findall(evidence))
        new_numbers = sorted(set(_NUMBERS.findall(str(block.get("text") or ""))) - allowed_numbers)
        if new_numbers:
            errors.append(f"block {index + 1} adds unsupported numbers: {', '.join(new_numbers)}")
    ordered = order_blocks_for_reveal(copy.deepcopy(candidate), context.get("story_arc") or {})
    if script_quality_signature(ordered) != script_quality_signature(candidate):
        errors.append("provider moved the protected reveal before its dependencies")
    if _premature_reveal(candidate, context):
        errors.append("provider exposed the protected payoff early")
    return list(dict.fromkeys(errors))


def run_script_story_quality_v1(
    blocks: list[dict[str, Any]],
    context: dict[str, Any],
    provider: ScriptStoryQualityProvider | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Edit once, validate, and return production blocks plus diagnostics."""
    original = copy.deepcopy(blocks)
    original_report = assess_script_story_quality(original, context)
    edited, actions = _deterministic_edit(original, context)
    provider_diagnostics: dict[str, Any] = {
        "status": "not_requested" if provider is None else "not_run",
        "provider": getattr(provider, "name", None),
        "error": None,
        "issues": [],
    }
    intermediate = assess_script_story_quality(edited, context)
    actionable = [item for item in intermediate["issues"] if item["severity"] in {"warning", "error"}]
    if provider is not None and actionable:
        request = QualityRequest(
            prompt=str(context.get("prompt") or ""),
            language=str((context.get("intent") or {}).get("language") or "en"),
            blocks=copy.deepcopy(edited),
            facts=copy.deepcopy(context.get("facts") or []),
            story_arc=copy.deepcopy(context.get("story_arc") or {}),
            payoff_plan=copy.deepcopy(context.get("payoff_plan") or {}),
            triple_hook=copy.deepcopy((context.get("script") or {}).get("triple_hook") or {}),
            reaction_plan=copy.deepcopy(context.get("reaction_plan") or {}),
            detected_issues=copy.deepcopy(actionable),
        )
        try:
            result = provider.edit(request)
        except Exception as exc:  # noqa: BLE001 - a valid deterministic result always survives
            result = QualityProviderResult(None, "provider_error", f"{type(exc).__name__}: {str(exc)[:180]}")
        provider_diagnostics.update(status=result.status, error=result.error)
        if result.response:
            provider_diagnostics["issues"] = [item.model_dump(mode="json") for item in result.response.issues]
            if result.response.status == "needs_research":
                provider_diagnostics["research_need"] = result.response.research_need
            elif result.response.status == "revise" and result.response.blocks:
                candidate = [block.model_dump(mode="json") for block in result.response.blocks]
                for index, block in enumerate(candidate, 1):
                    block["id"] = str(edited[index - 1].get("id") or f"voice_block_{index:02d}") if index <= len(edited) else f"voice_block_{index:02d}"
                validation_errors = validate_provider_blocks(edited, candidate, context)
                if validation_errors:
                    provider_diagnostics.update(
                        status="validation_error",
                        error="; ".join(validation_errors)[:600],
                        rejected_revision=True,
                    )
                else:
                    edited = candidate
                    actions.extend(item.model_dump(mode="json") for item in result.response.actions)
                    provider_diagnostics["accepted_revision"] = True
    elif provider is not None:
        provider_diagnostics["status"] = "skipped_already_strong"

    final_report = assess_script_story_quality(edited, context)
    if provider_diagnostics.get("status") == "needs_research":
        final_report["research_insufficient"] = True
        if "needs_research" not in final_report["gate"]["blocking"]:
            final_report["gate"]["blocking"].append("needs_research")
        final_report["gate"].update(ready=False, status="blocked")
    final_report.update({
        "original_signature": script_quality_signature(original),
        "final_signature": script_quality_signature(edited),
        "original_scores": original_report["dimensions"],
        "final_scores": final_report["dimensions"],
        "original_issues": original_report["issues"],
        "actions": actions,
        "provider": provider_diagnostics,
        "changes": {
            "sentences_removed": [
                action.get("details", {}).get("text")
                for action in actions
                if action.get("action") == "remove" and action.get("details", {}).get("text")
            ],
            "segments_merged": [action.get("segment_ids") for action in actions if action.get("action") == "merge"],
            "segments_reordered": [action.get("segment_ids") for action in actions if action.get("action") == "reorder"],
            "segments_rewritten": [action.get("segment_ids") for action in actions if action.get("action") in {"tighten", "replace_with_supported_specific", "strengthen_payoff"}],
        },
    })
    return edited, final_report


def current_script_story_quality(state: dict[str, Any]) -> dict[str, Any]:
    """Return a fresh report unless the persisted report still matches the script."""
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    blocks = [block for block in script.get("blocks") or [] if isinstance(block, dict)]
    stored = script.get("script_story_quality_v1") if isinstance(script.get("script_story_quality_v1"), dict) else {}
    if stored.get("final_signature") == script_quality_signature(blocks):
        return stored
    try:
        return assess_script_story_quality(blocks, state)
    except Exception as exc:  # noqa: BLE001 - old projects remain renderable through existing gates
        return {
            "version": QUALITY_VERSION,
            "signature": script_quality_signature(blocks),
            "issues": [],
            "gate": {"ready": True, "status": "fallback", "blocking": []},
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
