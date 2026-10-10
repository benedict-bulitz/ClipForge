"""Script & Story Quality V2: critic -> holistic creative rewrite -> verifier.

The first draft is an input, not something to preserve.  When an AI provider
is available it works like a strong human editor:

1. **Critic** - judges the whole draft (hook, depth, mechanism, redundancy,
   progression, payoff, unsupported statements, missed research facts).
2. **Holistic rewrite** - rewrites the *entire* script from the full research
   dossier, the story contract and the critic report.  It may change every
   sentence, the hook wording, the beat count and order, the answer structure
   and the payoff, and may use any supported research fact.
3. **Verifier** - independently checks hard requirements (grounding, numbers,
   reveal contract, answered question, payoff) and objective quality.  At most
   one contract repair and one fresh regeneration follow a failed verification.

Only *intent* is protected: the hook keeps its curiosity promise without
spending the protected answer, the reveal never moves earlier than its
dependencies, and the payoff answers the question.  Exact wording is never
protected.

Hard rules stay deterministic where they can be (cited fact IDs exist and are
usable, every number is in the research, protected reveal order by fact
identity, duration budget, language) and are re-checked by the AI verifier
for meaning (no invented or contradicting claims).

Without a provider, or when it is unavailable, the deterministic Script &
Story Quality V1 pass is the fallback and its gate stays authoritative.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .contract_diagnostics import sanitized
from .language import detect_text_language
from .novelty import (
    VERIFIED_AUDIT_SOURCE,
    assess_information_gain,
    fact_is_supported,
    verified_script_key,
)
from .payoff import reveals_protected_payoff
from .script_grounding import (
    ClaimGrounding,
    check_claim_grounding,
    contradicts_citation,
    evidence_key,
    grounding_request,
    hook_has_assertion,
    hook_semantically_supported,
)
from .script_review import ScriptReviewSentence, ScriptReviewSufficiency
from .script_story_quality import (
    _premature_reveal,
    assess_script_story_quality,
    run_script_story_quality_v1,
    script_quality_signature,
)
from .story_arc import arc_units, editorial_payoff_plan, story_brief
from .verbal_hook import _numbers, _rounded_from, information_gain, proposition_words

REWRITE_VERSION = 2
# One holistic rewrite plus one bounded repair; never an open-ended loop.
MAX_REWRITE_ATTEMPTS = 2
MAX_CONTRACT_ATTEMPTS = 3
# A payoff that adds less than this share of new propositions beyond the
# answer beat says the same thing twice (language independent: it compares
# meaning tokens, not wording).
ANSWER_PAYOFF_MIN_NEW_SHARE = 0.35
# Deterministic V1 findings that stay hard even when the AI verifier passes a
# rewrite: they are objective grounding/reveal failures, not style.
DETERMINISTIC_HARD_V1 = {"unsupported_claim", "premature_reveal"}

BeatRole = Literal["hook", "answer", "explanation", "support", "detail", "payoff"]
Severity = Literal["hard", "major", "minor"]


# ---------------------------------------------------------------------------
# Structured provider contracts
# ---------------------------------------------------------------------------


class EditorFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=48)
    severity: Severity
    beat_index: int | None = Field(default=None, ge=1, le=40)
    message: str = Field(min_length=2, max_length=320)


class CriticResponse(BaseModel):
    """The critic's judgement of the first draft."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["strong", "rewrite", "needs_research"]
    findings: list[EditorFinding] = Field(default_factory=list, max_length=16)
    missed_fact_ids: list[str] = Field(default_factory=list, max_length=12)
    research_need: str = Field(default="", max_length=320)


class RewriteBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: BeatRole
    text: str = Field(min_length=1, max_length=600)
    fact_ids: list[str] = Field(default_factory=list, max_length=8)


class RewriteResponse(BaseModel):
    """A complete new script; beat count and order are free."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["rewritten", "needs_research"]
    beats: list[RewriteBeat] = Field(default_factory=list, max_length=16)
    hook_intent_preserved: bool = True
    hook_intent_note: str = Field(default="", max_length=240)
    reveal_beat_index: int | None = Field(default=None, ge=1, le=40)
    payoff_beat_index: int | None = Field(default=None, ge=1, le=40)
    rationale: str = Field(default="", max_length=600)
    research_insufficiency: str = Field(default="", max_length=320)


class ContractObligationEvaluation(BaseModel):
    id: str
    status: Literal["satisfied", "partially_satisfied", "missing", "circular", "unsupported", "insufficient_depth"]
    is_primary: bool
    is_required: bool
    reasoning: str

class VerifierResponse(BaseModel):
    """Independent verification of one rewrite."""

    model_config = ConfigDict(extra="forbid")

    contract_evaluations: list[ContractObligationEvaluation]
    contract_sufficient: bool = Field(description="True if all REQUIRED obligations are satisfied and deep enough")

    grounded: bool
    answers_question: bool
    payoff_fulfilled: bool
    premature_reveal: bool
    hook_promise_kept: bool
    answer_payoff_duplicate: bool
    better_than_draft: bool
    findings: list[EditorFinding] = Field(default_factory=list, max_length=16)
    answer_sufficiency: ScriptReviewSufficiency
    explanation_audit: list[ScriptReviewSentence] = Field(default_factory=list, max_length=24)
    claim_grounding: list[ClaimGrounding] = Field(default_factory=list, max_length=64)


class ScriptStoryProviderError(RuntimeError):
    """The AI editor could not be reached or returned nothing usable."""


class ScriptStoryProvider(Protocol):
    """Critic, holistic rewriter and verifier.  Each call may raise."""

    name: str

    def critique(self, brief: dict[str, Any]) -> CriticResponse: ...

    def rewrite(self, brief: dict[str, Any]) -> RewriteResponse: ...

    def verify(self, brief: dict[str, Any]) -> VerifierResponse: ...


SHARED_RULES = (
    "Facts: research.facts is the only evidence. A fact with usable=false must not be used. Every factual "
    "statement must be supported by usable facts; cite their exact ids. Never invent facts, mechanisms, "
    "numbers, statistics, dates, names or sources, never cite an id that is not listed, never contradict the "
    "research. "
    "Positive evidence for one purpose does not establish the absence of every other purpose or an exclusive "
    "negative contrast. Do not infer an actor, necessity, or a causal link between separate facts without "
    "support for that relationship. Optional context may be omitted instead of inventing a bridge. "
    "Do not generalize evidence about one instance into a universal claim. "
    "With QuestionAnswerContract, legacy order_after_if_included dependencies constrain the order of "
    "facts you choose to include, not which background facts are mandatory. Only QAC required obligations "
    "define essential answer content. Optional context absent from the script is not missing required context. "
    "Reveal contract: when reveal_contract.withhold_answer is true the protected answer may be said "
    "only after the facts it depends on; later is fine, earlier never, and the hook never states or implies "
    "it. Write for the ear in the target language (language field); never mix languages. "
)

CRITIC_INSTRUCTIONS = (
    "You are the critic of ClipForge's short-form script desk. Judge the complete first draft (draft.beats) "
    "against the original question, the provided QuestionAnswerContract, the research dossier, and the story contract the way a demanding senior "
    "editor would. You MUST evaluate the draft against the QuestionAnswerContract. Look for: missing primary answer, missing mechanism, missing motive, shallow causal depth, circular answer, too much secondary context, mandatory research facts ignored, weak or generic hook, shallow explanation, missing mechanism (a why/how answer "
    "that only names the result, e.g. 'X counters Y', instead of explaining what actually happens), "
    "redundancy, weak information progression, low specificity, repeated analogy, tautological answer, an "
    "answer and payoff that say the same thing, weak payoff, a concept mentioned but never used, filler, "
    "unnatural spoken language, unsupported statements, and strong research facts the draft failed to use. "
    + SHARED_RULES
    + "Severity: hard only for unsupported or contradicting statements, a premature protected reveal, or a "
    "question that is not answered; major for objectively weak writing a viewer would notice; minor for "
    "taste. Required depth comes from QuestionAnswerContract; optional background is not a missing mechanism. "
    "beat_index is the 1-based draft beat. missed_fact_ids lists usable fact ids that would "
    "materially improve the explanation. verdict: strong only if you would ship the draft unchanged; "
    "needs_research only if the research cannot support a REQUIRED obligation from the contract (say what is missing in "
    "research_need); otherwise rewrite. deterministic_findings are lexical hints and may be wrong. Return only "
    "the structured output."
)

REWRITE_INSTRUCTIONS = (
    "You are the senior editor of ClipForge's short-form script desk. Write the BEST possible spoken script "
    "for a vertical short video that answers the original question. You have full creative freedom but MUST satisfy every required answer obligation from the QuestionAnswerContract using supported research: rewrite "
    "every sentence, write new hook wording, reorder, merge, split, add or drop beats, change the answer "
    "structure, rewrite the payoff completely, choose a better analogy or none, compress or expand. Use ANY "
    "usable research fact, including facts the draft ignored, and choose the strongest subset. Quality beats "
    "preserving the draft; optimise the whole script except when grounding_repair explicitly limits changes. "
    + SHARED_RULES
    + "Story contract: the first beat has role hook and keeps hook_intent (its curiosity promise and intended "
    "viewer reaction) in new or old words, without spending the protected answer. The payoff must answer the "
    "original question, deliver payoff_intent and leave the viewer with an 'ah, that is why' insight. For "
    "why/how questions explain the mechanism step by step - what physically or causally happens and why it "
    "produces the result - not only its name. An answer beat orients; the payoff closes the causal loop with "
    "new information and must not restate the answer. The contract sets necessary depth: preserve every required "
    "step, but do not invent deeper motives or negative contrasts for a stronger ending. When one closing "
    "beat can contain the complete answer, orient with supported observations before it rather than giving "
    "the complete answer twice. Mention a concept (a list of factors, a technical "
    "term) only if the script uses it. Every beat must give the viewer something new; no filler, no "
    "throat-clearing, no generic outro, no repeated analogy; end right after the payoff. Short, concrete, "
    "natural spoken sentences, one idea each. Stay within style.word_budget words in total. Every beat after "
    "the hook cites the fact ids it relies on. Factual claims or premises in the hook ALSO cite their "
    "actual supporting research fact IDs, even when phrased as a question. A purely nonfactual rhetorical "
    "hook may have no citations. Never invent IDs or attach a related fact that does not support the claim. "
    "required_answer_evidence supplies each mandatory cause or motive and its supported claims. Preserve "
    "the actual explanatory relationship through compression and in the payoff; a related precursor "
    "cannot substitute for the direct cause. "
    "If the research cannot support a complete answer, return status needs_research with "
    "research_insufficiency instead of guessing. reveal_beat_index and payoff_beat_index are 1-based "
    "positions in your beats; hook_intent_preserved says whether your hook keeps hook_intent; rationale names "
    "your main editorial decisions in 1-3 sentences. On a repair attempt (attempt 2) fix every hard and major "
    "finding in verifier_findings and keep what worked in previous_rewrite. "
    "For mode contract_repair, resolve every failed_obligations entry holistically using its exact "
    "supporting_fact_ids and supporting_facts; do not append a patch sentence. "
    "When grounding_repair is supplied, return the same number of beats and change only its repair_beat_indices; "
    "keep all other text, roles and citations unchanged. Fix rejected assertions or presuppositions using "
    "entailed wording, not new factual claims. The result will receive full independent verification. "
    "For mode fresh_regeneration, create a completely new script from the contract, required_supported_fact_ids, "
    "full research, hook intent, reveal constraints, language and word budget. No failed draft wording is supplied. "
    "recovery_constraints names prior failure classes without quoting failed wording; avoid recreating them. "
    "Return only the structured output."
)

VERIFIER_INSTRUCTIONS = (
    "You are the independent verifier of ClipForge's short-form script desk. Check candidate.beats against "
    "the research dossier, the story contract, and the QuestionAnswerContract. Evaluate every obligation "
    "(satisfied, partially_satisfied, missing, circular, unsupported, insufficient_depth) and copy its "
    "is_primary and is_required flags. Only required obligations block when not fully satisfied. "
    "Structured evaluations override a generic answers_question=true. You did not write it; be strict on hard requirements and "
    "tolerant of style. "
    + SHARED_RULES
    + "Hard requirements: grounded (every factual statement is supported by usable research facts, nothing "
    "invented, nothing contradicting, cited ids fit their sentences). Audit factual hook assertions and "
    "presuppositions inside questions claim by claim: actor, agency, action, intent and causal relation. "
    "Political or other context does not establish who acted. Use hook_grounding to inspect the candidate's "
    "actual cited facts; uncited evidence elsewhere in the dossier cannot authorize a claim. Reject unsupported "
    "agency even when the motive is answered. Pure rhetorical questions need no invented citations. "
    "Historical motives require a supported immediate purpose, not a physical process or repetition of the "
    "action verb; context and consequences alone do not answer WHY an actor acted. premature_reveal (the protected answer "
    "appears before its dependencies or in the hook); answers_question (the original question is actually "
    "answered); payoff_fulfilled (the payoff delivers the promised resolution); hook_promise_kept (the hook "
    "keeps the hook intent and its promise is paid off). Quality: information progression, sufficient "
    "explanatory depth (a why/how answer must explain the mechanism, not just name the result), no severe "
    "redundancy, answer_payoff_duplicate (answer and payoff say the same thing), strong relevant research "
    "facts actually used, natural spoken language, no filler. Report findings: hard for violated hard "
    "requirements, major only for objectively bad writing a viewer would notice, minor for taste. Never "
    "fail a script for differing from the draft or for stylistic choices, and never demand perfection by "
    "arbitrary numbers. better_than_draft compares candidate and draft as a whole. beat_index is 1-based in "
    "candidate.beats. A condition in the question ('although they are heavy', 'je älter man wird') must be "
    "explained causally, otherwise answers_question is false. Fill explanation_audit with one entry per "
    "candidate sentence after the hook (quote it "
    "exactly) and answer_sufficiency for the ORIGINAL question. deterministic_findings are lexical hints and "
    "may be wrong. Fill claim_grounding with one entry for EVERY exact sentence in claim_grounding_request, "
    "including the hook. Judge all assertions and presuppositions in that sentence (including actor, agency, "
    "intent, causal direction and specificity), not just its topic. supporting_fact_ids must be a subset of "
    "that beat's actual citations, never uncited facts elsewhere. covers_all_claims is true only when every "
    "claim in the sentence is accounted for. Mark supported semantic paraphrases supported, factual gaps "
    "unsupported, ambiguity uncertain and genuinely rhetorical text nonfactual with no supporting IDs. "
    "Every cited ID must support some actual claim in the beat; reject unrelated citations. "
    "The QuestionAnswerContract defines necessary answer depth; legacy story context cannot add mandatory "
    "requirements. Optional detail may enrich the script but is not required. Return only the structured output."
)


class OpenAIScriptStoryProvider:
    """Critic and verifier on the worker model, the rewrite on the director model."""

    name = "openai"

    def __init__(self, settings: Settings):
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._worker = settings.openai_worker_model
        self._writer = settings.openai_director_model

    def _parse(self, *, model: str, instructions: str, brief: dict[str, Any], schema: type[BaseModel], tokens: int):
        try:
            response = self._client.responses.parse(
                model=model,
                instructions=instructions,
                input=json.dumps(brief, ensure_ascii=False, default=str),
                text_format=schema,
                max_output_tokens=tokens,
                store=False,
            )
        except (OpenAIError, ValueError, TypeError) as exc:
            raise ScriptStoryProviderError(sanitized(str(exc), (getattr(self._client, "api_key", None),))[:240]) from exc
        parsed = response.output_parsed
        if not isinstance(parsed, schema):
            raise ScriptStoryProviderError(f"No parsed {schema.__name__}")
        return parsed

    def critique(self, brief: dict[str, Any]) -> CriticResponse:
        return self._parse(model=self._worker, instructions=CRITIC_INSTRUCTIONS, brief=brief, schema=CriticResponse, tokens=2_000)

    def rewrite(self, brief: dict[str, Any]) -> RewriteResponse:
        return self._parse(model=self._writer, instructions=REWRITE_INSTRUCTIONS, brief=brief, schema=RewriteResponse, tokens=4_000)

    def verify(self, brief: dict[str, Any]) -> VerifierResponse:
        return self._parse(model=self._worker, instructions=VERIFIER_INSTRUCTIONS, brief=brief, schema=VerifierResponse, tokens=3_000)


# ---------------------------------------------------------------------------
# The brief: everything an editor needs, not isolated sentences
# ---------------------------------------------------------------------------


def _role(block: dict[str, Any]) -> str:
    return str(block.get("role") or "").casefold()


def _ids(block: dict[str, Any]) -> list[str]:
    return [str(item) for item in block.get("fact_ids") or [] if str(item)]


def _beats(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"index": index, "role": _role(block), "text": str(block.get("text") or ""), "fact_ids": _ids(block)}
        for index, block in enumerate(blocks, 1)
    ]


def _arc(context: dict[str, Any]) -> dict[str, Any]:
    return context.get("story_arc") if isinstance(context.get("story_arc"), dict) else {}


def _payoff_plan(context: dict[str, Any]) -> dict[str, Any]:
    return context.get("payoff_plan") if isinstance(context.get("payoff_plan"), dict) else {}


def _triple_hook(context: dict[str, Any]) -> dict[str, Any]:
    script = context.get("script") if isinstance(context.get("script"), dict) else {}
    plan = script.get("triple_hook")
    return plan if isinstance(plan, dict) else {}


def _language(context: dict[str, Any]) -> str:
    return str((context.get("intent") or {}).get("language") or "en")


def _withhold(context: dict[str, Any]) -> bool:
    return bool((_arc(context).get("curiosity_gap") or {}).get("withhold_answer")) or bool(
        _payoff_plan(context).get("hook_must_not_reveal")
    )


def _protected_ids(context: dict[str, Any]) -> set[str]:
    arc = _arc(context)
    return {str(item) for item in (arc.get("hook") or {}).get("protected_ids") or []}


def _usable_facts(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(fact.get("id")): fact
        for fact in context.get("facts") or []
        if isinstance(fact, dict) and fact.get("id") and fact_is_supported(fact)
    }


def _small(value: Any, limit: int = 400) -> Any:
    """JSON-safe, bounded copy for provider input."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {str(key): _small(item, limit) for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_small(item, limit) for item in list(value)[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


def _story(arc: dict[str, Any], contract: dict[str, Any] | None = None) -> Any:
    try:
        return _small(story_brief(arc, question_answer_contract=contract))
    except (KeyError, TypeError):  # a partial (legacy) arc is still useful as-is
        return _small(arc)


def build_brief(blocks: list[dict[str, Any]], context: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    """The complete editorial context shared by critic, rewriter and verifier."""
    arc = _arc(context)
    payoff = _payoff_plan(context)
    hook = _triple_hook(context)
    intent = context.get("intent") if isinstance(context.get("intent"), dict) else {}
    units = arc_units(arc)
    primary = str(arc.get("primary_answer_id") or "")
    usable = _usable_facts(context)
    script = context.get("script") if isinstance(context.get("script"), dict) else {}
    gain = assessment.get("information_gain") if isinstance(assessment.get("information_gain"), dict) else {}
    contract = context.get("question_answer_contract")
    editorial_payoff = editorial_payoff_plan(payoff, contract) or {}
    return {
        "question_answer_contract": context.get("question_answer_contract"),
        "research_coverage": context.get("research_coverage"),
        "required_answer_evidence": [item for item in _supported_obligations(context) if item["is_required"]],
        "grounding_policy": {
            "factual_hook_requires_supporting_fact_ids": True,
            "nonfactual_rhetorical_hook_may_be_uncited": True,
            "citations_must_support_the_actual_claim": True,
            "factual_question_presuppositions_require_grounding": True,
            "context_does_not_establish_actor_agency_or_intent": True,
            "positive_purpose_does_not_establish_an_exclusive_negative_contrast": True,
            "separate_facts_do_not_establish_an_unstated_causal_link": True,
        },
        "version": REWRITE_VERSION,
        "question": {
            "original": str(context.get("prompt") or ""),
            "intended": str(
                (contract or {}).get("core_question") or ((intent.get("question_intent") or {}) if isinstance(intent.get("question_intent"), dict) else {}).get(
                    "intended_question"
                )
                or intent.get("question")
                or arc.get("primary_question")
                or ""
            ),
            "topic": str(intent.get("topic") or ""),
        },
        "language": _language(context),
        "research": {
            "usable_dossier": [copy.deepcopy(fact) for fact in usable.values()],
            "facts": [
                {
                    "id": str(fact.get("id")),
                    "claim": str(fact.get("claim") or ""),
                    "usable": str(fact.get("id")) in usable,
                    "research_role": fact.get("research_role") or units.get(str(fact.get("id")), {}).get("role"),
                    "priority": fact.get("priority"),
                    "independent_sources": fact.get("independent_sources"),
                    "sources": [
                        str(source.get("label") or source.get("url") or "")[:80]
                        for source in fact.get("sources") or []
                        if isinstance(source, dict)
                    ][:3],
                }
                for fact in context.get("facts") or []
                if isinstance(fact, dict) and fact.get("id")
            ],
        },
        "story_arc": _story(arc, contract),
        "hook_intent": {
            "selected_hook": str(script.get("selected_hook") or hook.get("verbal_hook") or ""),
            "strategy": hook.get("selected_strategy"),
            "curiosity_target": (contract or {}).get("core_question") or _small(hook.get("curiosity_target")),
            "promised_payoff": (contract or {}).get("primary_answer_obligation", {}).get("description") or _small(hook.get("promised_payoff")),
            "on_screen_text_hook": _small(hook.get("on_screen_text_hook")),
            "intended_reaction": hook.get("intended_reaction"),
            "hook_promise": (contract or {}).get("core_question") or _small((arc.get("question_contract") or {}).get("hook_promise")),
        },
        "reveal_contract": {
            "withhold_answer": _withhold(context),
            "protected_fact_ids": sorted(_protected_ids(context)),
            "primary_answer_id": primary or None,
            "must_follow_fact_ids": [] if contract else sorted(str(item) for item in units.get(primary, {}).get("depends_on") or []),
            **({"order_after_if_included_fact_ids": sorted(str(item) for item in units.get(primary, {}).get("depends_on") or [])}
               if contract else {}),
            "must_not_reveal_early": _small(payoff.get("hook_must_not_reveal")),
            "reveal_policy": payoff.get("reveal_policy"),
        },
        "payoff_intent": {
            "final_payoff_id": None if contract else arc.get("final_payoff_id"),
            "payoff": _small(editorial_payoff.get("payoff")),
            "payoff_type": payoff.get("payoff_type"),
            "desired_viewer_reaction": editorial_payoff.get("desired_viewer_reaction"),
        },
        "viewer_reaction": _small((context.get("reaction_plan") or {}).get("planned_arc")),
        "draft": {"beats": _beats(blocks)},
        "deterministic_findings": [
            {
                "code": item.get("issue_type"),
                "severity": item.get("severity"),
                "beat_index": next(
                    (index for index, block in enumerate(blocks, 1) if str(block.get("id") or "") == str(item.get("segment_id") or "")),
                    None,
                ),
                "reason": str(item.get("reason") or "")[:240],
            }
            for item in assessment.get("issues") or []
        ][:20],
        "information_gain": {
            "status": gain.get("status"),
            "summary": _small(gain.get("summary")),
            "issues": [
                {"code": item.get("code"), "severity": item.get("severity"), "message": str(item.get("message") or "")[:200]}
                for item in gain.get("issues") or []
            ][:12],
        },
        "style": {
            "format": "vertical short-form video narration, spoken aloud",
            "word_budget": context.get("word_budget"),
            "beat_roles": ["hook", "answer", "explanation", "support", "detail", "payoff"],
        },
    }


# ---------------------------------------------------------------------------
# Deterministic verification (hard rules and objective signals)
# ---------------------------------------------------------------------------


def _finding(code: str, severity: str, message: str, beat_index: int | None = None, source: str = "deterministic") -> dict[str, Any]:
    return {"code": code, "severity": severity, "beat_index": beat_index, "message": message[:320], "source": source}


def _hook_findings(candidate: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    """The rewritten hook keeps the hook *intent's* safety rules, not its wording."""
    from .triple_hook import _INVALIDATING, REVEAL_CODES, state_context, verbal_still_valid
    from .verbal_hook import assess_verbal, canonical_strategy

    hook = candidate[0] if candidate and _role(candidate[0]) == "hook" else None
    if hook is None:
        return []
    text = str(hook.get("text") or "")
    found: list[dict[str, Any]] = []
    if _withhold(context) and hook_has_assertion(text) and set(_ids(hook)) & _protected_ids(context):
        found.append(_finding("hook_reveals_answer", "hard", "The hook cites a protected payoff fact.", 1))
    if _withhold(context) and reveals_protected_payoff(text, _payoff_plan(context)):
        found.append(_finding("hook_reveals_answer", "hard", "The hook states the protected answer.", 1))
    plan = _triple_hook(context)
    state = {
        **{key: context.get(key) for key in ("intent", "facts", "story_arc", "payoff_plan", "format_plan", "novelty_plan", "explanation_audit")},
        "script": {"blocks": candidate, "triple_hook": plan},
    }
    try:
        valid = verbal_still_valid(state, text, plan.get("selected_strategy"))
    except Exception:  # noqa: BLE001 - an unassessable hook is judged by the other checks
        valid = True
    if not valid:
        hard = assess_verbal(text, canonical_strategy(plan.get("selected_strategy")) or "evidence_insight", state_context(state))["hard_fail"]
        invalidating = {code for code in hard if code in _INVALIDATING or code.endswith(REVEAL_CODES)}
        code = "hook_lexical_grounding" if invalidating == {"unsupported_cause"} else "hook_safety"
        found.append(_finding(
            code, "hard",
            "The hook fails the Triple Hook safety rules (spoiler, unsupported claim, clickbait, question echo or body duplicate).",
            1,
        ))
    return found


def _restore_hook(
    candidate: list[dict[str, Any]], context: dict[str, Any], draft_hook: dict[str, Any],
    rejected_hooks: set[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Replace an unsafe or missing rewritten hook by the draft's selected hook when that one is safe."""
    original = str(draft_hook.get("text") or "").strip()
    hooks = [block for block in candidate if _role(block) == "hook"]
    if not original or original in (rejected_hooks or set()) or len(hooks) > 1:
        return candidate, False
    body = [block for block in candidate if _role(block) != "hook"]
    restored = [{"id": "voice_block_01", "role": "hook", "text": original, "fact_ids": _ids(draft_hook)}, *body]
    if _hook_findings(restored, context) or any(
        finding["severity"] == "hard" and finding.get("beat_index") == 1
        for finding in deterministic_findings(restored, context)
    ):
        return candidate, False
    return restored, True


def answer_payoff_duplicate(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The answer beat and the payoff state the same proposition."""
    answer = next((block for block in blocks if _role(block) == "answer"), None)
    payoff = next((block for block in reversed(blocks) if _role(block) == "payoff"), None)
    if answer is None or payoff is None or answer is payoff:
        return None
    said = proposition_words(payoff.get("text"))
    if not said:
        return None
    new = [item for item in information_gain(str(answer.get("text") or ""), str(payoff.get("text") or "")) if item != "negation"]
    share = len(new) / len(said)
    if share >= ANSWER_PAYOFF_MIN_NEW_SHARE:
        return None
    return {"new_share": round(share, 3), "new_terms": new[:8]}


def deterministic_findings(
    candidate: list[dict[str, Any]], context: dict[str, Any], *, draft_has_hook: bool = True
) -> list[dict[str, Any]]:
    """Objective, language-independent checks of a rewritten script."""
    found: list[dict[str, Any]] = []
    roles = [_role(block) for block in candidate]
    body = [block for block in candidate if _role(block) != "hook"]
    if not body:
        return [_finding("structure", "hard", "The rewrite has no body after the hook.")]
    if roles.count("hook") > 1:
        found.append(_finding("structure", "hard", "The rewrite has more than one hook beat."))
    if draft_has_hook and roles[0] != "hook":
        found.append(_finding("structure", "hard", "The rewrite must open with exactly one hook beat.", 1))
    if not draft_has_hook and "hook" in roles:
        found.append(_finding("structure", "hard", "No hook was selected for this script; the rewrite added one."))
    if "payoff" not in roles:
        found.append(_finding("no_payoff", "hard", "The rewrite has no payoff beat that resolves the question."))

    facts = {str(fact.get("id")): fact for fact in context.get("facts") or [] if isinstance(fact, dict) and fact.get("id")}
    usable = _usable_facts(context)
    for index, block in enumerate(candidate, 1):
        ids = _ids(block)
        unknown = sorted(set(ids) - set(facts))
        if unknown:
            found.append(_finding("fabricated_fact_id", "hard", "Cites fact IDs that do not exist: " + ", ".join(unknown) + ".", index))
        unusable = sorted(set(ids) & set(facts) - set(usable))
        if unusable:
            found.append(_finding("unusable_fact_id", "hard", "Cites research facts that are not supported: " + ", ".join(unusable) + ".", index))
        if _role(block) != "hook" and not ids:
            found.append(_finding("uncited_beat", "hard", "A factual beat cites no research fact.", index))
        text = str(block.get("text") or "")
        if _role(block) == "hook" and not ids and hook_has_assertion(text):
            found.append(_finding("uncited_hook", "hard", "A factual hook assertion cites no supporting research fact.", index))
        cited_claims = [str(usable[identifier].get("claim") or "") for identifier in ids if identifier in usable]
        if contradicts_citation(text, cited_claims):
            found.append(_finding("citation_contradiction", "hard", "The claim reverses its cited evidence.", index))
        allowed_numbers = set().union(*(_numbers(claim) for claim in cited_claims)) if cited_claims else set()
        numbers = sorted(
            number for number in _numbers(text)
            if number not in allowed_numbers and not any(_rounded_from(text, claim) for claim in cited_claims)
        )
        if numbers:
            found.append(_finding("unsupported_number", "hard", "Numbers not in the research: " + ", ".join(numbers) + ".", index))

    for reveal in _premature_reveal(candidate, context):
        found.append(_finding("premature_reveal", "hard", str(reveal["reason"]), int(reveal["index"]) + 1))
    found.extend(_hook_findings(candidate, context))

    budget = context.get("word_budget")
    words = sum(len(str(block.get("text") or "").split()) for block in candidate)
    if isinstance(budget, int) and budget > 0 and words > budget:
        found.append(_finding("over_duration", "hard", f"The rewrite has {words} words; the duration budget allows {budget}."))
    detected = detect_text_language(" ".join(str(block.get("text") or "") for block in candidate))
    if detected not in {"unknown", _language(context)}:
        found.append(_finding("language_mismatch", "hard", f"The rewrite is written in {detected}, not {_language(context)}."))

    duplicate = answer_payoff_duplicate(candidate)
    if duplicate:
        found.append(_finding(
            "answer_payoff_duplicate", "major",
            f"The payoff adds only {round(100 * duplicate['new_share'])}% new meaning beyond the answer beat.",
            max(index for index, role in enumerate(roles, 1) if role == "payoff"),
        ))
    payoff_index = max((index for index, role in enumerate(roles) if role == "payoff"), default=-1)
    if 0 <= payoff_index < len(candidate) - 1:
        found.append(_finding("post_payoff_tail", "minor", "Beats continue after the payoff.", payoff_index + 2))
    return found


def _content_blockers(candidate: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Production content blockers (answer sufficiency, narrated failure) and V1 grounding errors."""
    from .readiness import CONTENT_BLOCKERS

    found: list[dict[str, Any]] = []
    state = {**copy.deepcopy(context), "script": {**(context.get("script") or {}), "blocks": copy.deepcopy(candidate)}}
    gain = assess_information_gain(state)
    for issue in gain.get("issues") or []:
        if issue.get("severity") == "error" and issue.get("code") in CONTENT_BLOCKERS:
            found.append(_finding(f"content_{issue['code']}", "hard", str(issue.get("message") or issue["code"])))
    report = assess_script_story_quality(candidate, context)
    ids = {str(block.get("id") or ""): index for index, block in enumerate(candidate, 1)}
    for issue in report.get("issues") or []:
        if issue.get("severity") == "error" and issue.get("issue_type") in DETERMINISTIC_HARD_V1:
            found.append(_finding(
                str(issue["issue_type"]), "hard", str(issue.get("reason") or ""), ids.get(str(issue.get("segment_id") or "")),
            ))
    return found


def _verification_audit(verdict: VerifierResponse, blocks: list[dict[str, Any]], context: dict[str, Any],
                        findings: list[dict[str, Any]]) -> dict[str, Any]:
    findings.extend(check_claim_grounding(verdict.claim_grounding, blocks, _usable_facts(context)))
    audit = {
        "sentences": [item.model_dump(mode="json") for item in verdict.explanation_audit],
        "answer_sufficiency": verdict.answer_sufficiency.model_dump(mode="json"),
        "source": VERIFIED_AUDIT_SOURCE,
        "verified_script": verified_script_key([str(block.get("text") or "") for block in blocks]),
        "claim_grounding": {
            "approved": verdict.grounded and not _severity([item for item in findings if item["code"] != "hook_lexical_grounding"], "hard"),
            "evidence_key": evidence_key(blocks, context.get("facts") or []),
            "evaluations": [item.model_dump(mode="json") for item in verdict.claim_grounding],
        },
    }

    if hook_semantically_supported(blocks, context.get("facts") or [], audit):
        findings[:] = [item for item in findings if item["code"] != "hook_lexical_grounding"]
    audit["claim_grounding"]["approved"] = verdict.grounded and not _severity(findings, "hard")
    return audit


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _candidate_blocks(response: RewriteResponse) -> list[dict[str, Any]]:
    return [
        {
            "id": f"voice_block_{index:02d}",
            "role": beat.role,
            "text": " ".join(beat.text.split()),
            "fact_ids": list(dict.fromkeys(str(item) for item in beat.fact_ids if str(item).strip())),
        }
        for index, beat in enumerate(response.beats, 1)
        if beat.text.strip()
    ]


def _contract_obligations(context: dict[str, Any]) -> list[dict[str, Any]]:
    contract = context.get("question_answer_contract") or {}
    primary = contract.get("primary_answer_obligation")
    obligations = ([{**primary, "is_primary": True}] if primary else []) + (contract.get("required_supporting_obligations") or [])
    return [{**item, "is_required": bool(item.get("is_primary") or item.get("is_required"))} for item in obligations]


def _checked_verdict(verdict: VerifierResponse, context: dict[str, Any]) -> VerifierResponse:
    """Contract flags and completeness cannot be overridden by the verifier."""
    obligations = _contract_obligations(context)
    if not obligations:
        return verdict
    by_id = {item.id: item for item in verdict.contract_evaluations}
    evaluations = []
    for obligation in obligations:
        evaluation = by_id.get(obligation["id"]) or ContractObligationEvaluation(
            id=obligation["id"], status="missing", is_primary=obligation["is_primary"],
            is_required=obligation["is_required"], reasoning="Verifier omitted this obligation.",
        )
        evaluations.append(evaluation.model_copy(update={
            "is_primary": obligation["is_primary"], "is_required": obligation["is_required"],
        }))
    return verdict.model_copy(update={"contract_evaluations": evaluations})


def _supported_obligations(context: dict[str, Any]) -> list[dict[str, Any]]:
    usable = _usable_facts(context)
    coverage = {
        item["obligation_id"]: item for item in (context.get("research_coverage") or {}).get("coverage", [])
    }
    supported = []
    for obligation in _contract_obligations(context):
        item = coverage.get(obligation["id"], {})
        ids = [identifier for identifier in item.get("supporting_fact_ids", []) if identifier in usable]
        if item.get("status") == "satisfied" and ids:
            supported.append({
                "id": obligation["id"], "description": obligation["description"],
                "is_primary": obligation["is_primary"], "is_required": obligation["is_required"],
                "supporting_fact_ids": ids,
                "supporting_facts": [{"id": identifier, "claim": usable[identifier]["claim"]} for identifier in ids],
            })
    return supported


def _verifier_findings(verdict: VerifierResponse) -> list[dict[str, Any]]:
    found = [
        _finding(item.code, "hard" if item.code.casefold() == "answer_payoff_duplicate" and item.severity != "minor" else item.severity,
                 item.message, item.beat_index, source="verifier")
        for item in verdict.findings
    ]

    evaluations = verdict.contract_evaluations
    required_failures = [item for item in evaluations if (item.is_primary or item.is_required) and item.status != "satisfied"]
    # Structured evaluations are authoritative, including optional omissions.
    if required_failures or not verdict.contract_sufficient:
        found.append(_finding("contract_insufficient", "hard", "Required answer contract not satisfied.", source="verifier"))
    for evaluation in required_failures:
        if evaluation.status in {"missing", "partially_satisfied"}:
            code = "primary_answer_missing" if evaluation.is_primary else "required_obligation_missing"
        else:
            code = {
                "circular": "answer_circular",
                "unsupported": "unsupported_required_answer",
                "insufficient_depth": "insufficient_causal_depth",
            }[evaluation.status]
        found.append(_finding(
            code, "hard", f"{evaluation.id}: {evaluation.reasoning}", source="verifier",
        ))

    checks = (
        (not verdict.grounded, "ungrounded", "hard", "The verifier found statements the research does not support."),
        (verdict.premature_reveal, "premature_reveal", "hard", "The verifier found the protected answer revealed too early."),
        (not verdict.answers_question, "question_unanswered", "hard", "The verifier found the original question unanswered."),
        (not verdict.payoff_fulfilled, "payoff_unfulfilled", "hard", "The verifier found the payoff unfulfilled."),
        (verdict.answer_sufficiency.verdict == "unanswered", "question_unanswered", "hard", "Answer sufficiency: unanswered."),
        (not verdict.hook_promise_kept, "hook_promise_broken", "major", "The hook promise is not kept."),
        (verdict.answer_payoff_duplicate, "answer_payoff_duplicate", "hard", "Answer and payoff say the same thing."),
    )
    known = {(item["code"], item["severity"]) for item in found}
    for failed, code, severity, message in checks:
        if failed and (code, severity) not in known:
            known.add((code, severity))
            found.append(_finding(code, severity, message, source="verifier"))
    return found


def _severity(findings: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    return [item for item in findings if item["severity"] == level]


def _grounding_repair(attempt: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    """Scope a grounding-only repair; this preserves content, never approval."""
    blocks = attempt.get("blocks") or []
    review = attempt.get("claim_grounding") or {}
    flags = attempt.get("verification_flags") or {}
    # Normalization can expand a 16-beat rewrite. Do not demand that a repair
    # return more beats than RewriteResponse's structured schema permits.
    if (not blocks or len(blocks) > 16 or attempt.get("major") or flags.get("premature_reveal")
            or not all(flags.get(key) for key in ("answers_question", "payoff_fulfilled", "hook_promise_kept"))
            or attempt.get("failed_obligations")
            or review.get("evidence_key") != evidence_key(blocks, context.get("facts") or [])):
        return None
    entries = [ClaimGrounding.model_validate(entry) for entry in review.get("evaluations") or []]
    repair = {entry.beat_index for entry in entries if entry.status in {"unsupported", "uncertain"}}
    if not repair or any(item["code"] != "ungrounded" and item.get("beat_index") not in repair
                         for item in attempt.get("hard") or []):
        return None
    # Incomplete/mismatched reviews cannot establish which remaining beats
    # were supported. A global grounded flag alone is insufficient.
    if any(item["code"] not in {"claim_unsupported", "claim_unverifiable"}
           for item in check_claim_grounding(entries, blocks, _usable_facts(context))):
        return None
    return {
        "repair_beat_indices": sorted(repair),
        "preserve_beat_indices": [index for index in range(1, len(blocks) + 1) if index not in repair],
        "claim_evaluations": [entry.model_dump(mode="json") for entry in entries if entry.beat_index in repair],
    }


def _recovery_constraints(attempts: list[dict[str, Any]]) -> list[str]:
    """Fixed labels only: fresh generation must not receive rejected prose."""
    constraints: set[str] = set()
    for attempt in attempts:
        for item in attempt.get("hard") or []:
            if item.get("beat_index") == 1:
                constraints.add("hook_grounding")
            if item["code"] in {"claim_unsupported", "claim_unverifiable", "ungrounded"}:
                constraints.add("claim_grounding")
            if item["code"] == "answer_payoff_duplicate":
                constraints.add("answer_payoff_repetition")
    return sorted(constraints)


def _guard(call: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001 - every provider failure has a deterministic fallback
        return None, sanitized(f"{type(exc).__name__}: {str(exc)[:200]}")


def _with_holistic(report: dict[str, Any], holistic: dict[str, Any], mode: str) -> dict[str, Any]:
    report["mode"] = mode
    report["holistic"] = holistic
    report["provider"] = {
        "status": holistic["status"],
        "provider": holistic.get("provider"),
        "error": holistic.get("error"),
        "issues": [],
    }
    return report


def _block_report(report: dict[str, Any], code: str, reason: str, *, research: bool) -> dict[str, Any]:
    report = copy.deepcopy(report)
    report["issues"].append({
        "issue_type": code, "severity": "error", "segment_id": None, "segment": None,
        "reason": reason[:320], "suggested_action": "Generate again or extend the research.",
    })
    blocking = sorted({*report["gate"].get("blocking", []), code})
    report["gate"] = {"ready": False, "status": "blocked", "blocking": blocking}
    report["research_insufficient"] = bool(report.get("research_insufficient") or research)
    return report


def _accepted_report(
    original: list[dict[str, Any]], final: list[dict[str, Any]], context: dict[str, Any],
    attempt: dict[str, Any], fallback_report: dict[str, Any],
) -> dict[str, Any]:
    audit = attempt.get("explanation_audit")
    audited = {**context, "explanation_audit": audit} if audit else context
    report = assess_script_story_quality(final, audited)
    issues = []
    for issue in report["issues"]:
        if issue["severity"] == "error" and issue["issue_type"] not in DETERMINISTIC_HARD_V1:
            # Lexical heuristics are critic signals; the verifier owns the verdict.
            issue = {**issue, "severity": "warning", "advisory": True}
        issues.append(issue)
    for item in [*attempt["major"], *attempt["minor"]]:
        issues.append({
            "issue_type": item["code"], "severity": "warning" if item["severity"] == "major" else "info",
            "segment_id": None, "segment": None, "reason": item["message"], "suggested_action": "Editorial note.",
            "source": item.get("source"),
        })
    report["issues"] = issues
    report["gate"] = {"ready": True, "status": "passed_with_warnings" if issues else "passed", "blocking": []}
    report["research_insufficient"] = False
    report.update({
        "original_signature": script_quality_signature(original),
        "final_signature": script_quality_signature(final),
        "original_scores": fallback_report.get("original_scores") or {},
        "final_scores": report["dimensions"],
        "original_issues": fallback_report.get("original_issues") or [],
        "actions": [{
            "action": "holistic_rewrite",
            "attempt": attempt["attempt"],
            "segment_ids": [str(block.get("id") or "") for block in final],
            "reason": attempt.get("rationale") or "Holistic creative rewrite.",
        }],
        "changes": {
            "sentences_removed": [],
            "segments_merged": [],
            "segments_reordered": [],
            "segments_rewritten": [[str(block.get("id") or "") for block in final]],
        },
    })
    return report


def verify_current_script(
    blocks: list[dict[str, Any]], context: dict[str, Any],
    provider: ScriptStoryProvider | None,
) -> dict[str, Any]:
    """One independent verification of the exact current script; never rewrite it."""
    report = assess_script_story_quality(blocks, context)
    signature = script_quality_signature(blocks)
    report.update(final_signature=signature, rewrite={
        "verified_by": None, "verified_script_signature": None,
        "status": "CONTRACT_VERIFICATION_STALE",
    })
    if provider is None:
        report["verification_diagnostics"] = {"status": "CONTRACT_VERIFICATION_STALE", "error": "AI verifier unavailable.", "attempts": 0}
        return report
    findings = deterministic_findings(blocks, context, draft_has_hook=any(_role(block) == "hook" for block in blocks))
    brief = build_brief(blocks, context, report)
    verdict, error = _guard(lambda: provider.verify({
        **brief, "mode": "verify_current_script", "candidate": {"beats": _beats(blocks)}, "hook_grounding": _hook_grounding(blocks, context),
        "claim_grounding_request": grounding_request(blocks, _usable_facts(context)),
        "deterministic_findings": findings,
    }))
    audit = None
    if verdict is not None:
        verdict = _checked_verdict(verdict, context)
        findings.extend(_verifier_findings(verdict))
        audit = _verification_audit(verdict, blocks, context, findings)
        findings.extend(_content_blockers(blocks, {**context, "explanation_audit": audit}))
    failures = _severity(findings, "hard")
    # A re-verification must also keep the current hook's promise.
    if verdict is not None and not verdict.hook_promise_kept:
        failures.append(_finding("hook_promise_broken", "hard", "The current hook promise is not kept."))
    if verdict is None or failures:
        report = _block_report(report, "rewrite_hard_failure", "Current script contract verification failed.", research=False)
        report["rewrite"]["status"] = "CONTRACT_REVERIFY_FAILED"
    else:
        report = _accepted_report(blocks, blocks, context, {
            "attempt": 1, "major": _severity(findings, "major"), "minor": _severity(findings, "minor"),
            "explanation_audit": audit, "rationale": "Verify current script without changing wording.",
        }, report)
        report["actions"] = []
        report["changes"] = {key: [] for key in report["changes"]}
        report["rewrite"] = {
            "verified_by": "ai_verifier", "verified_script_signature": signature, "status": "verified",
            "verified_evidence_key": evidence_key(blocks, context.get("facts") or []),
        }
    report["rewrite"]["contract_evaluations"] = [item.model_dump() for item in verdict.contract_evaluations] if verdict else []
    report["holistic"] = {"explanation_audit": audit}
    report["verification_diagnostics"] = {
        "status": report["rewrite"]["status"], "error": error,
        "findings": findings, "attempts": 1,
    }
    return report


def _hook_grounding(blocks: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    usable = _usable_facts(context)
    return [{
        "text": str(block.get("text") or ""),
        "fact_ids": list(block.get("fact_ids") or []),
        "cited_facts": [copy.deepcopy(usable[identifier]) for identifier in block.get("fact_ids") or [] if identifier in usable],
        "audit_claims": ["actor", "agency", "action", "intent", "causal_relation", "question_presuppositions"],
    } for block in blocks if _role(block) == "hook"]


def run_script_story_quality(
    blocks: list[dict[str, Any]],
    context: dict[str, Any],
    provider: ScriptStoryProvider | None = None,
    *, finalize_candidate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Critic -> holistic rewrite -> verifier, with a bounded repair and a deterministic fallback.

    Returns production blocks and the report persisted as
    ``script.script_story_quality_v1``.  ``report["rewrite"]`` is present
    only when an AI rewrite was accepted; its hook may differ from the
    selected Triple Hook wording (the caller adopts it).
    """
    original = copy.deepcopy(blocks)
    fallback_blocks, fallback_report = run_script_story_quality_v1(original, context)
    holistic: dict[str, Any] = {
        "version": REWRITE_VERSION,
        "status": "not_requested",
        "provider": getattr(provider, "name", None),
        "error": None,
        "critic": None,
        "attempts": [],
        "draft_blocks": copy.deepcopy(original),
    }
    if _contract_obligations(context) and not context.get("research_coverage"):
        holistic.update(status="coverage_unavailable", failure_type="COVERAGE_UNAVAILABLE")
        blocked = _block_report(fallback_report, "rewrite_hard_failure", "Independent research coverage unavailable.", research=False)
        return fallback_blocks, _with_holistic(blocked, holistic, "deterministic")
    if provider is None:
        if _contract_obligations(context):
            fallback_report = _block_report(fallback_report, "rewrite_hard_failure", "Contract editor unavailable.", research=False)
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")
    if any(_role(block) == "status" for block in original) or not _usable_facts(context):
        holistic["status"] = "not_applicable"
        holistic["reason"] = "status_script" if any(_role(block) == "status" for block in original) else "no_supported_research"
        if _contract_obligations(context):
            fallback_report = _block_report(fallback_report, "rewrite_hard_failure", holistic["reason"], research=False)
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")

    draft_assessment = assess_script_story_quality(original, context)
    brief = build_brief(original, context, draft_assessment)
    critic, error = _guard(lambda: provider.critique(brief))
    if error is not None:
        holistic.update(status="provider_unavailable", error=error)
        if _contract_obligations(context):
            fallback_report = _block_report(fallback_report, "rewrite_hard_failure", error, research=False)
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")
    holistic["critic"] = critic.model_dump(mode="json")
    critic_hard = [item for item in critic.findings if item.severity == "hard"]
    critic_major = [item for item in critic.findings if item.severity == "major"]
    draft_clean = bool(fallback_report["gate"]["ready"]) and not critic_hard
    if critic.verdict == "strong" and draft_clean and not critic_major and not _contract_obligations(context):
        holistic["status"] = "skipped_already_strong"
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")

    draft_hook = next((block for block in original if _role(block) == "hook"), None)
    rejected_hooks = {str(draft_hook.get("text") or "").strip()} if draft_hook and any(
        finding.severity == "hard" and finding.beat_index == 1 for finding in critic.findings
    ) else set()
    draft_has_hook = draft_hook is not None
    critic_payload = {
        "verdict": critic.verdict,
        "findings": [item.model_dump(mode="json") for item in critic.findings],
        "missed_fact_ids": list(critic.missed_fact_ids),
        "research_need": critic.research_need,
    }
    attempts: list[dict[str, Any]] = []
    research_need = ""
    previous: list[dict[str, Any]] | None = None
    repair_findings: list[dict[str, Any]] = []
    supported = _supported_obligations(context)
    failed_obligations: list[dict[str, Any]] = []
    contract_failed = False
    limit = MAX_CONTRACT_ATTEMPTS if _contract_obligations(context) else MAX_REWRITE_ATTEMPTS
    for number in range(1, limit + 1):
        mode = "normal_rewrite" if number == 1 else "contract_repair" if failed_obligations else "quality_repair"
        if number == 3:
            mode = "fresh_regeneration"

        request = {
            **brief,
            "critic": critic_payload,
            "mode": mode,
            "failed_obligations": failed_obligations,
            "attempt": number,
            "previous_rewrite": _beats(previous) if previous else None,
            "verifier_findings": repair_findings or None,
        }
        grounding_repair = _grounding_repair(attempts[-1], context) if number == 2 and attempts else None
        if grounding_repair:
            request["grounding_repair"] = grounding_repair
        if number == 3:
            # Remove all failed prose and draft critiques; keep only generation constraints.
            request = {key: value for key, value in brief.items() if key not in {
                "draft", "deterministic_findings", "information_gain",
            }}
            request.update(
                mode=mode, attempt=number, failed_obligations=[
                    {key: copy.deepcopy(value) for key, value in item.items() if key in {
                        "id", "description", "is_primary", "is_required", "status",
                        "supporting_fact_ids", "supporting_facts",
                    }} for item in failed_obligations
                ],
                required_supported_fact_ids=sorted({
                    identifier for item in supported if item["is_required"]
                    for identifier in item["supporting_fact_ids"]
                }),
                recovery_constraints=_recovery_constraints(attempts),
            )
        response, error = _guard(lambda request=request: provider.rewrite(request))
        if error is not None:
            attempts.append({"attempt": number, "status": "provider_error", "error": error})
            break
        if response.status == "needs_research":
            reason = response.research_insufficiency or critic.research_need or "The research cannot support a complete answer."
            required = [item for item in _contract_obligations(context) if item["is_required"]]
            supported_ids = {item["id"] for item in supported}
            if required and all(item["id"] in supported_ids for item in required):
                failed_obligations = [
                    {**item, "verifier_reason": reason, "status": "missing"}
                    for item in supported if item["is_required"]
                ]
                contract_failed = True
                holistic["failure_type"] = "SUPPORTED_BUT_OMITTED"
                attempts.append({"attempt": number, "mode": mode, "status": "supported_answer_omitted", "reason": reason})
                continue
            research_need = reason
            holistic["failure_type"] = "RESEARCH_MISSING"
            attempts.append({"attempt": number, "status": "needs_research", "research_need": research_need})
            break
        candidate = _candidate_blocks(response)
        proposed = copy.deepcopy(candidate) if grounding_repair else None
        scope_mismatch = bool(grounding_repair and len(candidate) != len(previous))
        if grounding_repair and not scope_mismatch:
            candidate = [block if index in grounding_repair["repair_beat_indices"] else copy.deepcopy(previous[index - 1])
                         for index, block in enumerate(candidate, 1)]
        hook_restored = False
        if draft_has_hook and (
            not candidate or _role(candidate[0]) != "hook" or any(item["code"] != "hook_lexical_grounding" for item in _hook_findings(candidate, context))
        ):
            candidate, hook_restored = _restore_hook(candidate, context, draft_hook, rejected_hooks)
        # Verify the exact production text and citation mapping, including deterministic transitions.
        if finalize_candidate is not None:
            candidate = finalize_candidate(candidate)
        findings = deterministic_findings(candidate, context, draft_has_hook=draft_has_hook)
        if scope_mismatch:
            findings.append(_finding("grounding_repair_structure", "hard", "Grounding repair changed the beat count."))
        if hook_restored:
            findings.append(_finding("hook_restored", "minor", "The rewritten hook broke the hook intent; the selected Triple Hook was kept.", 1))
        verdict: VerifierResponse | None = None
        verifier_error: str | None = None
        if not _severity([item for item in findings if item["code"] not in {"uncited_hook", "hook_lexical_grounding"}], "hard"):
            verify_brief = {
                **(request if number == 3 else brief),
                "candidate": {"beats": _beats(candidate)}, "hook_grounding": _hook_grounding(candidate, context),
                "claim_grounding_request": grounding_request(candidate, _usable_facts(context)),
                "deterministic_findings": [item for item in findings if item["severity"] != "minor"],
            }
            verdict, verifier_error = _guard(lambda verify_brief=verify_brief: provider.verify(verify_brief))
        audit = None
        if verdict is not None:
            verdict = _checked_verdict(verdict, context)
            findings.extend(_verifier_findings(verdict))
            failures = [item for item in verdict.contract_evaluations if item.is_required and item.status != "satisfied"]
            contract_failed = contract_failed or bool(failures)
            by_id = {item["id"]: item for item in supported}
            failed_obligations = [
                {**by_id[item.id], "verifier_reason": item.reasoning, "status": item.status}
                for item in failures if item.id in by_id
            ]
            coverage_known = bool(context.get("research_coverage"))
            missing_research = [item for item in failures if item.id not in by_id] if coverage_known else []
            if missing_research:
                research_need = " | ".join(
                    f"Missing required answer: {item['description']}"
                    for item in _contract_obligations(context)
                    if item["id"] in {failure.id for failure in missing_research}
                )
            if failures:
                holistic["failure_type"] = ("COVERAGE_UNAVAILABLE" if not coverage_known else "RESEARCH_MISSING" if missing_research else "SUPPORTED_BUT_OMITTED")

            audit = _verification_audit(verdict, candidate, context, findings)
        if not _severity(findings, "hard"):
            findings.extend(_content_blockers(candidate, {**context, "explanation_audit": audit} if audit else context))
        if verdict is None and _contract_obligations(context):
            findings.append(_finding("unverified_contract", "hard", "Independent contract verification unavailable."))
            contract_failed = True
        if verdict is None and verifier_error is not None and not _severity(findings, "hard"):
            # Without an independent AI verification the deterministic V1 gate governs.
            gate = assess_script_story_quality(candidate, context)["gate"]
            if not gate["ready"]:
                findings.append(_finding(
                    "unverified_rewrite", "hard",
                    "The verifier was unavailable and the deterministic gate blocks: " + ", ".join(gate["blocking"]) + ".",
                ))
        attempt = {
            "attempt": number,
            "mode": mode,
            "failed_obligations": copy.deepcopy(failed_obligations),
            "contract_evaluations": [item.model_dump() for item in verdict.contract_evaluations] if verdict else [],
            "status": "verified" if verdict is not None else "deterministic_only",
            "blocks": candidate,
            "hook_changed": bool(
                draft_hook and candidate and str(candidate[0].get("text") or "") != str(draft_hook.get("text") or "")
            ),
            "hook_restored": hook_restored,
            "hook_intent_preserved": response.hook_intent_preserved,
            "hook_intent_note": response.hook_intent_note,
            "reveal_beat_index": response.reveal_beat_index,
            "payoff_beat_index": response.payoff_beat_index,
            "rationale": response.rationale,
            "research_insufficiency": response.research_insufficiency,
            "better_than_draft": verdict.better_than_draft if verdict is not None else None,
            "verifier_error": verifier_error,
            "verification_passed": verdict is not None and not _severity(findings, "hard"),
            "verification_flags": {name: getattr(verdict, name) for name in (
                "grounded", "answers_question", "payoff_fulfilled", "hook_promise_kept", "premature_reveal",
            )} if verdict else None,
            "explanation_audit": audit,
            "claim_grounding": audit.get("claim_grounding") if audit else None,
            "grounding_repair": grounding_repair,
            "proposed_blocks": proposed,
            "hard": _severity(findings, "hard"),
            "major": _severity(findings, "major"),
            "minor": _severity(findings, "minor"),
        }
        attempts.append(attempt)
        if candidate and any(item.get("beat_index") == 1 for item in attempt["hard"]):
            rejected_hooks.add(str(candidate[0].get("text") or "").strip())
        if research_need or (not attempt["hard"] and not attempt["major"]):
            break
        previous = candidate
        repair_findings = [*attempt["hard"], *attempt["major"]]

    holistic["attempts"] = [
        {key: value for key, value in item.items() if key not in {"explanation_audit"}} for item in attempts
    ]
    holistic["rejected_hooks"] = sorted(rejected_hooks)
    passing = [item for item in attempts if "blocks" in item and not item["hard"]]
    if passing:
        best = min(passing, key=lambda item: (len(item["major"]), -item["attempt"]))
        if best["better_than_draft"] is False and draft_clean and not contract_failed and not _contract_obligations(context):
            holistic.update(status="kept_draft_rewrite_not_better", selected_attempt=None)
            return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")
        final = copy.deepcopy(best["blocks"])
        holistic.update(
            status="rewritten" if not best["major"] else "rewritten_with_warnings",
            selected_attempt=best["attempt"],
            explanation_audit=best.get("explanation_audit"),
        )
        report = _accepted_report(original, final, context, best, fallback_report)
        report["rewrite"] = {
            "attempt": best["attempt"],
            "hook_text": str(final[0].get("text") or "") if final and _role(final[0]) == "hook" else None,
            "hook_changed": best["hook_changed"],
            "hook_restored": best["hook_restored"],
            "hook_intent_preserved": best["hook_intent_preserved"],
            "hook_intent_note": best["hook_intent_note"],
            "reveal_beat_index": best["reveal_beat_index"],
            "payoff_beat_index": best["payoff_beat_index"],
            "rationale": best["rationale"],
            "research_insufficiency": best["research_insufficiency"],
            "beats": _beats(final),
            "verified_by": "ai_verifier" if best["status"] == "verified" else "deterministic_gate",
            "verified_script_signature": script_quality_signature(final) if best["status"] == "verified" else None,
            "verified_evidence_key": evidence_key(final, context.get("facts") or []) if best["status"] == "verified" else None,
            "contract_evaluations": best["contract_evaluations"],
        }
        return final, _with_holistic(report, holistic, "holistic_ai")

    if research_need:
        holistic.update(status="needs_research", research_need=research_need)
        report = _block_report(fallback_report, "needs_research", research_need, research=True)
        return fallback_blocks, _with_holistic(report, holistic, "deterministic")
    if not any("blocks" in item for item in attempts) and not contract_failed and not _contract_obligations(context):
        # The rewriter itself was unreachable: the deterministic pass stands.
        holistic.update(status="provider_unavailable", error=next((item.get("error") for item in attempts), None))
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")
    if draft_clean and not contract_failed and not _contract_obligations(context):
        holistic["status"] = "rewrite_rejected_kept_draft"
        return fallback_blocks, _with_holistic(fallback_report, holistic, "deterministic")
    last = next((item for item in reversed(attempts) if "blocks" in item), {})
    reason = "Bounded script recovery exhausted: " + "; ".join(item["message"] for item in last.get("hard", [])[:3])
    if not last:
        reason += str(attempts[-1].get("reason") or attempts[-1].get("error") or "No supported answer generated.")
    holistic["status"] = "needs_fix"
    # Keep an answer marked satisfied by the verifier for editing when only the hook failed.
    # This is a rejected candidate, never an approval or a verification signature.
    required_ids = {item["id"] for item in _contract_obligations(context) if item["is_required"]}
    for item in reversed(attempts):
        satisfied = {evaluation["id"] for evaluation in item.get("contract_evaluations", []) if evaluation["status"] == "satisfied"}
        hard = item.get("hard", [])
        if (required_ids and required_ids <= satisfied and item.get("blocks") and hard
                and any(finding.get("beat_index") == 1 for finding in hard)
                and all(finding.get("beat_index") == 1 or finding["code"] == "ungrounded" for finding in hard)):
            fallback_blocks = item["blocks"]
            fallback_report = assess_script_story_quality(fallback_blocks, context)
            holistic["rejected_candidate_preserved"] = {"attempt": item["attempt"], "reason": "Required answer retained; hook remains rejected."}
            break
    report = _block_report(fallback_report, "rewrite_hard_failure", reason, research=False)
    return fallback_blocks, _with_holistic(report, holistic, "deterministic")
