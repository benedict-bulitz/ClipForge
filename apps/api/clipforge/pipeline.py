import copy
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .ai import (
    ai_plan_to_dict,
    generate_hook_candidates_with_openai,
    interpret_edit,
    plan_with_openai,
)
from .alignment import phrase_fallback_items
from .attention import replan_attention, resolve_attention_preferences
from .config import Settings
from .dependencies import resolve_edit_scope
from .format_intelligence import plan_format
from .hashing import attach_hashes
from .hooks import STRATEGIES, select_hook, select_hook_candidate
from .language import detect_text_language, resolve_language
from .music import automatic_music_layer
from .narration import (
    clean_narration_text,
    clean_research_claim,
    clean_script_blocks,
    contamination_issues,
)
from .novelty import safe_novelty_plan
from .pacing import analyze_pacing
from .payoff import (
    _is_protected_question,
    _protected_answer,
    build_payoff_plan,
    fallback_triple_hook,
    hidden_payoff_words,
    normalise_triple_hook,
    trim_post_payoff_fluff,
)
from .progress import ProgressCallback, report_progress
from .reactions import plan_viewer_reactions, reaction_arc
from .research import research_topic
from .schemas import AdvancedOptions
from .script_review import (
    OpenAIScriptReviewProvider,
    ScriptReviewProvider,
    ScriptReviewRequest,
    compact_script_draft,
    review_script_v2,
)
from .script_writer import (
    OpenAIScriptWriterProvider,
    ScriptBlockV2,
    ScriptWriterFact,
    ScriptWriterProvider,
    ScriptWriterRequest,
    ScriptWriterResult,
    generate_script_v2,
)
from .story_arc import (
    annotate_story_roles,
    arc_units,
    essential_fact_ids,
    hook_safe_facts,
    omittable_fact_ids,
    safe_story_arc,
    story_brief,
)
from .voice import apply_voice_preferences, initial_voice

STAGE_LABELS = [
    ("intent", "Understanding your idea"),
    ("research", "Researching"),
    ("script", "Writing story"),
    ("assets", "Creating visuals"),
    ("voice", "Generating voice"),
    ("captions", "Building captions"),
    ("render", "Editing video"),
    ("qc", "Reviewing video"),
]

SPEAKING_RATE_WPM = 165
AUTO_MIN_DURATION = 10


class UnsupportedEdit(ValueError):
    pass


def _language(prompt: str, override: str | None) -> str:
    return resolve_language(prompt, override)


def _intent(prompt: str, options: AdvancedOptions) -> dict[str, Any]:
    text = prompt.casefold()
    words = set(re.findall(r"[a-zäöüß]+", text))
    explicit_story = bool(words & {"story", "geschichte", "fiction"}) or any(
        term in text for term in ("erzähl mir", "erzaehl mir", "write a story", "fictional")
    )
    narrative_scene = any(term in text for term in ("wakes alone", "wacht allein", "wacht auf"))
    fictional = explicit_story or narrative_scene
    hypothetical = any(term in text for term in ("what if", "was wäre wenn", "was waere wenn"))
    current = any(term in words for term in ("heute", "aktuell", "latest", "today", "news", "tomorrow"))
    language = _language(prompt, options.language)
    topic = re.sub(r"[?!.,]", "", prompt).strip()
    if len(topic) > 76:
        topic = topic[:73].rstrip() + "…"
    content_type = (
        "fictional_story"
        if fictional
        else ("hypothetical_explainer" if hypothetical else ("current_explainer" if current else "factual_explainer"))
    )
    return {
        "topic": topic,
        "intent": "tell_story" if fictional else "explain",
        "question": prompt,
        "language": language,
        "content_type": content_type,
        "tone": options.style or ("cinematic" if fictional else "fast_documentary"),
        "research_required": options.research == "on" or (
            options.research == "auto" and not fictional
        ),
        "visual_style": "cinematic_story" if fictional else "documentary_graphics",
        "shortform": True,
    }


def _fiction_plan(prompt: str, intent: dict[str, Any]) -> dict[str, Any]:
    de = intent["language"] == "de"
    subject = intent["topic"]
    if de:
        blocks = [
            ("hook", f"Als {subject}, stimmt etwas an der Welt plötzlich nicht mehr."),
            ("setup", "Ein einziges Signal widerspricht allem, worauf die Hauptfigur vertraut hat."),
            ("turn", "Die Spur führt nicht nach draußen, sondern zu einer Entscheidung aus der eigenen Vergangenheit."),
            ("payoff", "Jetzt bleiben nur Sekunden, um zu entscheiden, welche Wahrheit überleben darf."),
        ]
    else:
        blocks = [
            ("hook", f"When {subject}, one detail makes the whole world feel wrong."),
            ("setup", "A single signal contradicts everything the main character trusted."),
            ("turn", "The trail leads inward, toward a choice made in their own past."),
            ("payoff", "Only seconds remain to decide which truth gets to survive."),
        ]
    return {
        "intent": intent,
        "research_questions": [],
        "facts": [],
        "answer_skeleton": [role.upper() for role, _ in blocks],
        "script_blocks": [{"role": role, "text": text} for role, text in blocks],
        "music_mood": "cinematic_suspense",
    }


def _arc_forbidden_terms(story_arc: dict[str, Any] | None, intent: dict[str, Any]) -> set[str]:
    """Words of a withheld primary answer that an opening hook must not use."""
    if not isinstance(story_arc, dict) or not (story_arc.get("curiosity_gap") or {}).get("withhold_answer"):
        return set()
    claim = str(arc_units(story_arc).get(str(story_arc.get("primary_answer_id") or ""), {}).get("claim") or "")
    if not claim:
        return set()
    return hidden_payoff_words({"hook_must_not_reveal": _protected_answer(claim, str(intent.get("question") or ""))})


def _story_blocks(
    intent: dict[str, Any], facts: list[dict[str, Any]], story_arc: dict[str, Any]
) -> list[dict[str, Any]]:
    """Deterministic body in the arc's information order, carrying fact IDs.

    The shortest complete version: every required unit, plus optional units
    only when novelty says they add real value.
    """
    units = arc_units(story_arc)
    by_id = {str(fact.get("id") or ""): fact for fact in facts}
    required = essential_fact_ids(story_arc)
    primary, final = story_arc.get("primary_answer_id"), story_arc.get("final_payoff_id")
    blocks: list[dict[str, Any]] = []
    for fact_id in story_arc.get("order") or []:
        unit, fact = units.get(fact_id), by_id.get(fact_id)
        if unit is None or fact is None:
            continue
        valuable = unit.get("novelty") in {"distinctive", "explanatory", "comparison", "core"}
        if fact_id not in required and not valuable:
            continue
        claim = clean_research_claim(fact.get("claim"))
        if not claim:
            continue
        if fact_id == final and final != primary:
            role = "payoff"
        elif fact_id == primary:
            role = "payoff" if story_arc.get("structure") in {"reveal", "ranked_progression"} and fact_id == final else "answer"
        else:
            role = {"explanation": "explanation"}.get(str(unit.get("role")), "support")
        blocks.append({"role": role, "text": claim, "fact_ids": [fact_id]})
    return blocks


def _factual_blocks(
    intent: dict[str, Any], facts: list[dict[str, Any]], story_arc: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    de = intent["language"] == "de"
    if facts and isinstance(story_arc, dict) and story_arc.get("units") and story_arc.get("status") != "fallback":
        body = _story_blocks(intent, facts, story_arc)
        if body:
            hook = select_hook(
                intent,
                hook_safe_facts(facts, story_arc),
                body=body[0]["text"],
                forbidden_terms=_arc_forbidden_terms(story_arc, intent),
            )
            return ([{"role": "hook", "text": hook}] if hook else []) + body
    if not facts:
        message = (
            "Ohne verlässliche Recherche kann ich diese Frage noch nicht gut beantworten."
            if de
            else "I could not verify enough reliable information for a good answer yet."
        )
        return [{"role": "status", "text": message}]
    useful: list[str] = []
    low_value = re.compile(
        r"(?i)\b(?:covers?|area|acres?|square (?:miles|kilomet(?:er|re)s)|"
        r"visitors?|founded|established|headquarters)\b"
    )
    for fact in facts:
        claim = clean_research_claim(fact.get("claim"))
        if not claim or claim in useful or low_value.search(claim):
            continue
        useful.append(claim)
        if len(useful) == 3:
            break
    if not useful:
        useful = [
            claim
            for fact in facts
            if (claim := clean_research_claim(fact.get("claim")))
        ][:2]
    roles = ["answer", "support", "context"]
    blocks = [{"role": roles[index], "text": claim} for index, claim in enumerate(useful)]
    hook = select_hook(intent, facts, body=useful[1] if len(useful) > 1 else (useful[0] if useful else None))
    return ([{"role": "hook", "text": hook}] if hook else []) + blocks


def _attach_story_fact_ids(blocks: list[dict[str, Any]], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort fact identity for planner blocks that carry none.

    Writer V2 and the story fallback already attach fact IDs; only legacy
    planner blocks are matched to the fact whose claim they clearly restate.
    """
    claims = [
        (str(fact.get("id") or ""), {word.casefold() for word in re.findall(r"[\wäöüß]{4,}", str(fact.get("claim") or ""))})
        for fact in facts
        if fact.get("id")
    ]
    for block in blocks:
        if block.get("fact_ids") or str(block.get("role") or "").casefold() in {"hook", "status"}:
            continue
        words = {word.casefold() for word in re.findall(r"[\wäöüß]{4,}", str(block.get("text") or ""))}
        if not words:
            continue
        best = max(claims, key=lambda item: len(words & item[1]) / max(1, len(item[1])), default=None)
        if best and len(words & best[1]) / max(1, len(best[1])) >= 0.5:
            block["fact_ids"] = [best[0]]
    return blocks


def _aligned_visual_intents(
    blocks: list[dict[str, Any]],
    visual_hook: dict[str, Any],
    planner_blocks: list[dict[str, Any]],
    planner_intents: list[dict[str, Any]],
    *,
    hook_is_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Pair each final block with the planner visual intent for the same facts.

    The planner emits one visual intent per planner block.  Final blocks may
    be rewritten, split into sentences or merged into the hook, so position is
    unreliable; fact identity is not.  Blocks without a matching fact keep the
    previous positional pairing (or get the generic fallback intent).
    """
    positional = [visual_hook, *planner_intents]
    by_fact: dict[str, dict[str, Any]] = {}
    for block, intent in zip(planner_blocks, planner_intents):
        for fact_id in block.get("fact_ids") or []:
            by_fact.setdefault(str(fact_id), intent)
    if not by_fact:
        return positional
    aligned: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if _is_hook_block(block):
            # A fallback visual hook is generic; when the hook states a planned
            # fact, that fact's planner visual is the better opening image.
            planned = next((by_fact[str(fact_id)] for fact_id in block.get("fact_ids") or [] if str(fact_id) in by_fact), None)
            if hook_is_fallback and planned is not None:
                merged = {**copy.deepcopy(planned), "must_not_show": list(visual_hook.get("must_not_show") or [])}
                visual_hook.clear()
                visual_hook.update(merged)
            aligned.append(visual_hook)
            continue
        matched = next((by_fact[str(fact_id)] for fact_id in block.get("fact_ids") or [] if str(fact_id) in by_fact), None)
        aligned.append(matched if matched is not None else (positional[index] if index < len(positional) else {}))
    return aligned


def _safe_payoff_plan(
    intent: dict[str, Any], blocks: list[dict[str, Any]], supplied: dict[str, Any] | None = None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    story_arc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Planning enrichment must never make an otherwise valid generation fail."""
    try:
        return build_payoff_plan(
            intent,
            blocks,
            supplied=supplied,
            format_plan=format_plan,
            novelty_plan=novelty_plan,
            story_arc=story_arc,
        )
    except Exception:  # noqa: BLE001 - keep the established hook/body fallback usable
        body = [block for block in blocks if not _is_hook_block(block)]
        return {
            "curiosity_question": str(intent.get("question") or ""),
            "payoff": str(body[-1].get("text") or "") if body else "",
            "payoff_type": "answer",
            "payoff_dependencies": [],
            "reveal_policy": "immediate_context_allowed",
            "hook_must_not_reveal": "",
            "desired_viewer_reaction": "insight",
            "supporting_information": [],
            "novelty_guidance": {
                "recommended_angle": str((novelty_plan or {}).get("recommended_angle") or ""),
                "distinctive_fact_ids": list((novelty_plan or {}).get("distinctive_facts") or []),
                "explanatory_gain_ids": list((novelty_plan or {}).get("explanatory_gain") or []),
                "comparison_gain_ids": list((novelty_plan or {}).get("comparison_gain") or []),
            },
            "status": "fallback",
        }


def _script_writer_fact(fact: dict[str, Any], index: int) -> ScriptWriterFact | None:
    claim = clean_research_claim(fact.get("claim"))
    if not claim or contamination_issues(claim):
        return None
    verification = str(fact.get("verification") or "unverified_model_synthesis")
    if verification == "source_snippet":
        verification = "source_attributed"
    if verification not in {
        "supported",
        "source_attributed",
        "uncertain",
        "conflicting",
        "unsupported",
        "unverified_model_synthesis",
    }:
        verification = "unverified_model_synthesis"
    return ScriptWriterFact(
        id=str(fact.get("id") or f"fact_{index:02d}"),
        claim=claim,
        verification=verification,
        confidence=fact.get("confidence"),
        priority=fact.get("priority"),
    )


def _bounded_script_writer_summary(facts: list[ScriptWriterFact], limit: int = 2_000) -> str:
    """Provide optional context without making it compete with structured facts."""
    summary = ""
    for fact in facts:
        candidate = f"{summary} {fact.claim}".strip()
        if len(candidate) > limit:
            break
        summary = candidate
    return summary


def _v2_body_blocks(draft_blocks: list[ScriptBlockV2]) -> list[dict[str, Any]]:
    return [
        {
            "role": block.role,
            "text": block.text,
            "fact_ids": list(block.fact_ids),
        }
        for block in draft_blocks
    ]


def _generate_body_with_v2_or_fallback(
    prompt: str,
    intent: dict[str, Any],
    options: AdvancedOptions,
    settings: Settings,
    facts: list[dict[str, Any]],
    legacy_blocks: list[dict[str, Any]],
    payoff_plan: dict[str, Any] | None = None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    *,
    provider: ScriptWriterProvider | None = None,
    review_provider: ScriptReviewProvider | None = None,
    story_arc: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "attempted": False,
        "status": "legacy_fallback",
        "provider": None,
        "error": None,
        "fact_ids": [],
        "writer_draft": None,
        "reviewed_draft": None,
        "review": {"status": "not_run", "issues": [], "error": None},
    }
    if not settings.openai_api_key or intent.get("content_type") == "fictional_story":
        diagnostics["reason"] = "unsupported_generation_mode"
        return legacy_blocks, diagnostics

    normalized_facts = [
        normalized
        for index, fact in enumerate(facts, 1)
        if (normalized := _script_writer_fact(fact, index)) is not None
    ]
    if not normalized_facts:
        diagnostics["reason"] = "no_normalized_facts"
        return legacy_blocks, diagnostics

    diagnostics["attempted"] = True
    try:
        request = ScriptWriterRequest(
            prompt=prompt,
            language=str(intent["language"]),
            tone=str(intent.get("tone") or "fast_documentary"),
            audience="general",
            content_type=str(intent.get("content_type") or "factual_explainer"),
            facts=normalized_facts,
            research_summary=_bounded_script_writer_summary(normalized_facts),
            target_duration={
                "max_seconds": min(options.max_duration, settings.shortform_max_duration)
            },
            writing_requirements=[
                "Write a body-only explanation; do not create a hook.",
                "Stop when the explanation is complete.",
            ],
            payoff_plan=payoff_plan,
            format_plan=format_plan,
            novelty_plan=novelty_plan,
            story_arc=story_brief(story_arc) or None,
        )
    except ValidationError as exc:
        diagnostics["reason"] = "request_validation_failed"
        diagnostics["error"] = "; ".join(
            str(error.get("msg", "invalid request"))[:160] for error in exc.errors()
        )[:240]
        return legacy_blocks, diagnostics

    selected_provider = provider or OpenAIScriptWriterProvider(settings)
    result: ScriptWriterResult = generate_script_v2(request, selected_provider)
    diagnostics["provider"] = getattr(selected_provider, "name", "custom")
    diagnostics["status"] = "v2_success" if result.draft else "legacy_fallback"
    diagnostics["error"] = result.error
    if result.draft is None:
        diagnostics["reason"] = (
            "draft_validation_failed"
            if result.status == "validation_error"
            else "provider_failed"
        )
        return legacy_blocks, diagnostics
    diagnostics["fact_ids"] = sorted(
        {fact_id for block in result.draft.blocks for fact_id in block.fact_ids}
    )
    diagnostics["writer_draft"] = compact_script_draft(result.draft)
    if review_provider is None and provider is not None:
        diagnostics["review"] = {
            "status": "not_run",
            "issues": [],
            "error": "custom writer provider has no review provider",
        }
        diagnostics["reviewed_draft"] = compact_script_draft(result.draft)
        return _v2_body_blocks(result.draft.blocks), diagnostics

    selected_review_provider = review_provider or OpenAIScriptReviewProvider(settings)
    review_request = ScriptReviewRequest(
        prompt=prompt,
        language=str(intent["language"]),
        tone=str(intent.get("tone") or "fast_documentary"),
        audience="general",
        content_type=str(intent.get("content_type") or "factual_explainer"),
        draft=result.draft,
        facts=normalized_facts,
        target_duration={
            "max_seconds": min(options.max_duration, settings.shortform_max_duration)
        },
        writing_requirements=[
            "Preserve complete causal context needed to understand the answer.",
            "Keep the body concise without optimizing for the shortest possible version.",
            "Respect the payoff plan; do not add a generic post-payoff outro.",
        ],
    )
    try:
        review_result = review_script_v2(review_request, selected_review_provider)
    except Exception as exc:  # noqa: BLE001 - V2 review must not discard a valid writer draft
        diagnostics["reviewed_draft"] = compact_script_draft(result.draft)
        diagnostics["review"] = {
            "status": "review_failed_kept_writer",
            "issues": [],
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "provider": getattr(selected_review_provider, "name", "custom"),
        }
        return _v2_body_blocks(result.draft.blocks), diagnostics
    diagnostics["review"] = {
        "status": review_result.status,
        "issues": (
            [issue.model_dump(mode="json") for issue in review_result.response.issues]
            if review_result.response
            else []
        ),
        "error": review_result.error,
        "provider": getattr(selected_review_provider, "name", "custom"),
    }
    if review_result.response and review_result.response.status == "revise":
        reviewed = review_result.response.draft
        if reviewed is not None:
            diagnostics["reviewed_draft"] = compact_script_draft(reviewed)
            return _v2_body_blocks(reviewed.blocks), diagnostics
    diagnostics["reviewed_draft"] = compact_script_draft(result.draft)
    if review_result.status not in {"approved", "revised"}:
        diagnostics["review"]["status"] = "review_failed_kept_writer"
    return _v2_body_blocks(result.draft.blocks), diagnostics


def _audience_hook(intent: dict[str, Any]) -> str | None:
    question = clean_narration_text(intent.get("question") or "").strip()
    if not question:
        return None
    question = re.sub(
        r"(?i)^(?:please\s+)?(?:explain|tell me|show me|erkläre|erklaere|erzähl mir|erzaehl mir)\s+",
        "",
        question,
    ).strip()
    words = question.rstrip(".!?").split()
    if len(words) <= 14:
        return " ".join(words).rstrip(".!?") + "?"
    topic = str(intent.get("topic") or "").strip(" .!?")
    topic_words = topic.split()
    if len(topic_words) > 7:
        topic = " ".join(topic_words[:7])
    if intent.get("language") == "de":
        return f"Was ist das Überraschende an {topic}?" if topic else None
    return f"What is surprising about {topic}?" if topic else None


def _ensure_audience_hook(
    blocks: list[dict[str, Any]], intent: dict[str, Any], facts: list[dict[str, Any]] | None = None, model_candidates: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    if (
        not blocks
        or intent.get("content_type") == "fictional_story"
        or str(blocks[0].get("role") or "").casefold() == "status"
    ):
        return blocks
    first_role = str(blocks[0].get("role") or "").casefold()
    evidence = facts or []
    if first_role == "hook":
        body = next((str(block.get("text") or "") for block in blocks[1:] if block.get("text")), "")
        safe = select_hook(intent, evidence, body=body, existing=str(blocks[0].get("text") or ""), model_candidates=model_candidates)
        if safe:
            blocks[0]["text"] = safe
        return blocks
    body = str(blocks[0].get("text") or "")
    hook = select_hook(intent, evidence, body=body, model_candidates=model_candidates)
    return ([{"role": "hook", "text": hook}] if hook else []) + blocks


def _authoritative_hook_blocks(
    blocks: list[dict[str, Any]],
    intent: dict[str, Any],
    facts: list[dict[str, Any]] | None = None,
    model_candidates: list[dict[str, Any]] | None = None,
    payoff_plan: dict[str, Any] | None = None,
    story_arc: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Any | None]:
    """Select one hook independently of whether the model supplied a hook block.

    Hook selection is performed before duration normalization so an inserted hook
    participates in the normal word budget and can never be lost from the
    canonical narration path.
    """
    if not blocks and not str(intent.get("question") or "").strip():
        return blocks, None
    existing = next(
        (str(block.get("text") or "").strip() for block in blocks
         if str(block.get("role") or "").casefold() == "hook" and str(block.get("text") or "").strip()),
        None,
    )
    body = " ".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if str(block.get("role") or "").casefold() != "hook" and str(block.get("text") or "").strip()
    )
    candidate = select_hook_candidate(
        intent,
        hook_safe_facts(facts or [], story_arc),
        body=body,
        existing=existing,
        model_candidates=model_candidates or [],
        forbidden_terms=hidden_payoff_words(payoff_plan or {}) | _arc_forbidden_terms(story_arc, intent),
    )
    if not candidate:
        return blocks, None
    remaining = [
        block for block in blocks
        if str(block.get("role") or "").casefold() != "hook"
    ]
    hook_block: dict[str, Any] = {"role": "hook", "text": candidate.text}
    if story_arc and remaining:
        # When the hook states the arc's opening fact verbatim, it delivers that
        # fact: drop the duplicate body block instead of saying it twice.
        def _norm(value: object) -> str:
            return " ".join(str(value or "").casefold().split()).rstrip(".!?")

        if _norm(candidate.text) == _norm(remaining[0].get("text")):
            hook_block["fact_ids"] = list(remaining[0].get("fact_ids") or [])
            remaining = remaining[1:]
    return ([hook_block, *remaining], candidate)


def _generate_authoritative_hook_blocks(
    blocks: list[dict[str, Any]],
    prompt: str,
    intent: dict[str, Any],
    facts: list[dict[str, Any]],
    settings: Settings,
    payoff_plan: dict[str, Any] | None = None,
    reaction_arc: dict[str, Any] | None = None,
    format_plan: dict[str, Any] | None = None,
    novelty_plan: dict[str, Any] | None = None,
    story_arc: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Any | None, Any]:
    """Use the one post-body hook path shared by production and validation."""
    body_blocks = [
        block for block in blocks
        if str(block.get("role") or "").casefold() != "hook"
    ]
    final_body = " ".join(
        str(block.get("text") or "").strip() for block in body_blocks
    )
    planning = {
        "payoff_plan": payoff_plan, "reaction_arc": reaction_arc,
        "format_plan": format_plan, "novelty_plan": novelty_plan,
    }
    try:
        hook_generation = generate_hook_candidates_with_openai(
            prompt, intent, facts, final_body, settings, **planning, story_arc=story_brief(story_arc) or None,
        )
    except TypeError:  # Compatibility with hook-provider test doubles of older signatures.
        try:
            hook_generation = generate_hook_candidates_with_openai(
                prompt, intent, facts, final_body, settings, **planning
            )
        except TypeError:
            hook_generation = generate_hook_candidates_with_openai(
                prompt, intent, facts, final_body, settings
            )
    hooked_blocks, candidate = _authoritative_hook_blocks(
        body_blocks, intent, facts, hook_generation.candidates, payoff_plan, story_arc
    )
    return hooked_blocks, candidate, hook_generation


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


_VISUAL_FILLER = re.compile(r"(?i)\b(?:if i remember correctly|the answer is|provided material|according to the source|wenn ich mich recht erinnere|die antwort ist|bereitgestellten material)\b")


def _visual_goal_valid(value: object) -> bool:
    text = " ".join(str(value or "").split()).strip(" .!?—")
    if len(text.split()) < 2 or _VISUAL_FILLER.search(text):
        return False
    return not re.match(r"(?i)^(?:liegt|also|nun|hier|ich|wir)\b", text)


def _fallback_visual_intent(narration: str, language: str) -> dict[str, Any]:
    """Topic-independent emergency intent: bounded concrete words from the scene text.

    Used only when the planner supplied no valid visual intent for a block.
    """
    text = " ".join(narration.split())
    words = [word.strip(".,!?;:") for word in _words(text) if len(word.strip(".,!?;:")) > 3]
    goal = " ".join(words[:6]) or ("visual explanation" if language != "de" else "visuelle Erklärung")
    # Marked so visual direction never mistakes narration words for a planned
    # visual subject (they are only a search fallback).
    return {"visual_goal": goal, "objects": words[:3], "actions": [], "context": [], "visual_strategy": "literal", "media_queries": [goal], "source": "narration_fallback"}


def _is_hook_block(block: dict[str, Any]) -> bool:
    return str(block.get("role") or "").casefold() == "hook"


def _apply_selected_hook(
    blocks: list[dict[str, Any]], selected_hook: str | None
) -> list[dict[str, Any]]:
    """Keep exactly one hook block whose text is the authoritative selected hook."""
    hook_text = clean_narration_text(selected_hook or "").strip()
    if not hook_text:
        return blocks
    existing = next((block for block in blocks if _is_hook_block(block)), None)
    remaining = [block for block in blocks if not _is_hook_block(block)]
    hook: dict[str, Any] = {"role": "hook", "text": hook_text}
    if existing is not None and existing.get("fact_ids"):
        hook["fact_ids"] = list(existing["fact_ids"])  # keep the hook's story identity
    return [hook, *remaining]


def _fit_blocks(
    blocks: list[dict[str, str]], max_duration: int, wpm: int = SPEAKING_RATE_WPM,
    story_arc: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    max_words = max(12, int(max_duration * wpm / 60))
    fitted = copy.deepcopy(blocks)
    omittable = omittable_fact_ids(story_arc)
    required = essential_fact_ids(story_arc)
    anchors = {
        str(story_arc.get(key)) for key in ("primary_answer_id", "final_payoff_id")
        if isinstance(story_arc, dict) and story_arc.get(key)
    }

    def drop_priority(index: int) -> tuple[int, int]:
        # Drop optional information first, required information last; within
        # a tier the latest block goes first (previous behaviour).
        fact_ids = set(fitted[index].get("fact_ids") or [])
        if fact_ids and fact_ids <= omittable:
            tier = 0
        elif fact_ids & anchors:
            tier = 3  # the primary answer and final payoff go last
        elif fact_ids & required:
            tier = 2
        else:
            tier = 1
        return (tier, -index)

    def total_words(items: list[dict[str, str]]) -> int:
        return sum(len(_words(item["text"])) for item in items)

    while len(fitted) > 1 and total_words(fitted) > max_words:
        # Never drop the authoritative hook; trim trailing body blocks first.
        drop_index = min(
            (index for index in range(len(fitted)) if not _is_hook_block(fitted[index])),
            key=drop_priority,
            default=None,
        )
        if drop_index is None:
            break
        fitted.pop(drop_index)
    if total_words(fitted) <= max_words:
        return fitted
    # Still over budget: shorten non-hook body copy only. Truncating the hook
    # produces broken grammar and breaks the canonical narration opening.
    for index, block in enumerate(fitted):
        if _is_hook_block(block):
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", block["text"])
            if sentence.strip()
        ]
        kept: list[str] = []
        for sentence in sentences:
            trial_text = " ".join([*kept, sentence]).strip()
            trial_words = total_words(fitted) - len(_words(block["text"])) + len(
                _words(trial_text)
            )
            if kept and trial_words > max_words:
                break
            kept.append(sentence)
        if kept:
            fitted[index]["text"] = " ".join(kept).strip()
        if total_words(fitted) <= max_words:
            return fitted
    return fitted


def _dimensions(aspect_ratio: str) -> tuple[int, int]:
    return {"1:1": (1080, 1080), "16:9": (1920, 1080)}.get(aspect_ratio, (1080, 1920))


def _build_scenes(
    blocks: list[dict[str, str]],
    total_duration: float,
    old_scenes: list[dict] | None = None,
    cut_pace: str = "fast",
    visual_intents: list[dict[str, Any]] | None = None,
    format_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    total_words = max(1, sum(len(_words(block["text"])) for block in blocks))
    old_by_block: dict[str, list[dict[str, Any]]] = {}
    for scene in old_scenes or []:
        old_by_block.setdefault(str(scene.get("block_id") or ""), []).append(scene)
    cursor = 0.0
    scenes: list[dict[str, Any]] = []
    target_cut_seconds = {"fast": 3.0, "balanced": 5.5, "slow": 9.0}.get(cut_pace, 3.0)
    chunks: list[tuple[dict[str, str], str, int, int]] = []
    for block_index, block in enumerate(blocks):
        words = _words(block["text"])
        block_duration = total_duration * len(words) / total_words
        max_readable_parts = max(1, (len(words) + 2) // 3)
        part_count = max(
            1,
            min(max_readable_parts, round(block_duration / target_cut_seconds)),
        )
        base_size, remainder = divmod(len(words), part_count)
        offset = 0
        for part_index in range(part_count):
            size = base_size + (1 if part_index < remainder else 0)
            chunks.append((block, " ".join(words[offset : offset + size]), part_index, block_index))
            offset += size

    for index, (block, narration, part_index, block_index) in enumerate(chunks):
        share = len(_words(narration)) / total_words
        end = total_duration if index == len(chunks) - 1 else round(cursor + total_duration * share, 2)
        existing_options = old_by_block.get(block["id"], [])
        existing = existing_options[part_index] if part_index < len(existing_options) else {}
        suffix = block["id"].removeprefix("voice_block_")
        existing_intent = existing.get("visual_intent") if isinstance(existing.get("visual_intent"), dict) else None
        intent = (
            (visual_intents or [])[block_index]
            if block_index < len(visual_intents or [])
            else (copy.deepcopy(existing_intent) if existing_intent else _fallback_visual_intent(narration, "en"))
        )
        if isinstance(format_plan, dict):
            intent = copy.deepcopy(intent)
            intent.setdefault("format_guidance", format_plan.get("visual_structure"))
            intent.setdefault("format", format_plan.get("selected_format"))
        visual_goal = str(intent.get("visual_goal") or "")
        if not _visual_goal_valid(visual_goal):
            intent = _fallback_visual_intent(narration, "en")
            if isinstance(format_plan, dict):
                intent["format_guidance"] = format_plan.get("visual_structure")
                intent["format"] = format_plan.get("selected_format")
            visual_goal = intent["visual_goal"]
        if existing.get("visual_goal") and not _visual_goal_valid(existing.get("visual_goal")):
            intent = _fallback_visual_intent(narration, "en")
            if isinstance(format_plan, dict):
                intent["format_guidance"] = format_plan.get("visual_structure")
                intent["format"] = format_plan.get("selected_format")
            visual_goal = intent["visual_goal"]
        scene = {
            "id": existing.get("id", f"scene_{suffix}_{part_index + 1:02d}"),
            "block_id": block["id"],
            "start": round(cursor, 2),
            "end": end,
            "narration": narration,
            "visual_goal": existing.get("visual_goal", visual_goal),
            "visual_intent": intent,
            "preferred_media": existing.get("preferred_media", "video"),
            "fallback_media": "real_stock",
            "search_queries": existing.get("search_queries", intent.get("media_queries", [])),
            "motion": (
                "fast_cut"
                if cut_pace == "fast"
                else ("slow_push" if cut_pace == "slow" else "subtle_pan")
            ),
            "asset_status": existing.get("asset_status", "search_required"),
        }
        if existing.get("media"):
            scene["media"] = copy.deepcopy(existing["media"])
            if isinstance(existing.get("visual_director"), dict):
                scene["visual_director"] = copy.deepcopy(existing["visual_director"])
        if existing.get("edit_instruction"):
            scene["edit_instruction"] = existing["edit_instruction"]
        scenes.append(scene)
        cursor = end
    return scenes


def _build_captions(
    script: str, duration: float, words_per_group: int = 4
) -> list[dict[str, Any]]:
    return phrase_fallback_items(script, duration, words_per_group)


def _normalise_blocks(
    blocks: list[dict[str, Any]],
    max_duration: int,
    wpm: int = SPEAKING_RATE_WPM,
    story_arc: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    clean = clean_script_blocks(blocks)
    sentence_blocks: list[dict[str, str]] = []
    for block in clean:
        # Keep the authoritative hook as one block so duration fitting cannot
        # split it into hook+detail fragments that later duplicate on restore.
        if _is_hook_block(block):
            sentence_blocks.append(
                {
                    "role": "hook",
                    "text": block["text"],
                    "fact_ids": list(block.get("fact_ids") or []),
                }
            )
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", block["text"])
            if sentence.strip()
        ]
        for sentence_index, sentence in enumerate(sentences):
            sentence_blocks.append(
                {
                    "role": block["role"] if sentence_index == 0 else "detail",
                    "text": sentence,
                    "fact_ids": list(block.get("fact_ids") or []),
                }
            )
    fitted = _fit_blocks(sentence_blocks, max_duration, wpm, story_arc)
    for index, block in enumerate(fitted, 1):
        block["id"] = f"voice_block_{index:02d}"
    return fitted


def _refresh_script_derivatives(
    state: dict[str, Any], *, old_scenes: list[dict] | None = None
) -> None:
    max_duration = int(state["duration"]["max_seconds"])
    voice_speed = max(0.7, min(1.4, float(state.get("voice", {}).get("speed") or 1.0)))
    wpm = max(1, round(SPEAKING_RATE_WPM * voice_speed))
    story_arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else None
    blocks = _normalise_blocks(state["script"]["blocks"], max_duration, wpm, story_arc)
    state["script"]["blocks"] = blocks
    script_text = " ".join(block["text"] for block in blocks)
    word_count = len(_words(script_text))
    minimum = state["duration"].get("minimum_seconds")
    natural_duration = round(max(4, word_count / wpm * 60), 2)
    effective_minimum = int(minimum or AUTO_MIN_DURATION)
    duration = round(min(max_duration, max(effective_minimum, natural_duration)), 2)
    state["script"]["text"] = script_text
    state["script"]["word_count"] = word_count
    state["duration"]["estimated_seconds"] = duration
    state["duration"]["natural_seconds"] = natural_duration
    state["duration"]["effective_minimum_seconds"] = effective_minimum
    state["duration"]["actual_seconds"] = None
    state["duration"]["speaking_rate_wpm"] = wpm
    state["scenes"] = _build_scenes(
        blocks,
        duration,
        old_scenes,
        str(state.get("timeline", {}).get("cut_pace") or "fast"),
        format_plan=state.get("format_plan"),
    )
    state["storyboard"] = {"status": "ready", "scene_count": len(state["scenes"])}
    state["voice"]["blocks"] = [{"id": block["id"], "status": "awaiting_tts"} for block in blocks]
    state["voice"]["status"] = "regeneration_required"
    state["captions"]["items"] = _build_captions(
        script_text, duration, int(state["captions"].get("words_per_group", 4))
    )
    state["captions"]["timing"] = "phrase_estimate"
    state["captions"]["diagnostic"] = "Word alignment will run after narration is generated."
    state["timeline"]["duration"] = duration
    state["timeline"]["timing"] = "estimated"
    state["timeline"]["scene_ids"] = [scene["id"] for scene in state["scenes"]]
    replan_attention(state)
    annotate_story_roles(state)
    analyze_pacing(state)
    plan_viewer_reactions(state)


def build_initial_state(
    prompt: str,
    options: AdvancedOptions,
    settings: Settings,
    *,
    progress: ProgressCallback | None = None,
    script_writer_provider: ScriptWriterProvider | None = None,
    script_review_provider: ScriptReviewProvider | None = None,
) -> dict[str, Any]:
    intent = _intent(prompt, options)
    resolved_options = options.model_copy(update={"language": intent["language"]})
    sources: list[dict] = []
    facts: list[dict[str, Any]] = []
    research_status = "skipped"
    research_provider = "not_needed"
    research_error = None
    if intent["research_required"]:
        report_progress(progress, "research", "Researching the topic", phase="start")
        result = research_topic(prompt, intent["language"], settings)
        research_status = result.status
        research_provider = result.provider
        research_error = result.error
        sources = result.sources
        facts = result.facts
        report_progress(progress, "research", "Researching the topic", phase="complete")
    else:
        report_progress(progress, "research", "Researching the topic", phase="skipped")

    for index, fact in enumerate(facts, 1):
        fact["claim"] = clean_research_claim(fact.get("claim"))
        fact["id"] = f"fact_{index:02d}"
        fact["priority"] = fact.get("priority") or (
            "MUST_KNOW" if fact.get("importance", 0) >= 0.7 else "USEFUL"
        )
        if "sources" not in fact:
            label = fact.pop("source_label", None)
            url = fact.pop("source_url", None)
            fact["sources"] = [{"label": label, "url": url}] if label and url else []
        fact["verification"] = (
            fact.get("verification")
            or ("source_attributed" if fact["sources"] else "unverified_model_synthesis")
        )

    novelty_plan = safe_novelty_plan(intent, facts)
    report_progress(progress, "script", "Writing the narration", phase="start")
    ai_result = plan_with_openai(
        prompt,
        resolved_options,
        settings,
        evidence=[fact["claim"] for fact in facts if fact.get("claim")],
        novelty_plan=novelty_plan,
    )
    plan_language_mismatch = False
    if ai_result.plan:
        plan = ai_plan_to_dict(ai_result.plan)
        plan["intent"]["language"] = intent["language"]
        planned_text = " ".join(
            str(block.get("text", "")) for block in plan.get("script_blocks", [])
        )
        detected = detect_text_language(planned_text)
        if detected not in {"unknown", intent["language"]}:
            plan_language_mismatch = True
            plan = _fiction_plan(prompt, intent) if intent["content_type"] == "fictional_story" else {}
    elif intent["content_type"] == "fictional_story":
        plan = _fiction_plan(prompt, intent)
    else:
        plan = {}

    if facts:
        plan["facts"] = facts
    else:
        facts = plan.get("facts", [])
        for index, fact in enumerate(facts, 1):
            fact["claim"] = clean_research_claim(fact.get("claim"))
            fact["id"] = f"fact_{index:02d}"
            fact["priority"] = (
                "MUST_KNOW" if fact.get("importance", 0) >= 0.7 else "USEFUL"
            )
            label = fact.pop("source_label", None)
            url = fact.pop("source_url", None)
            fact["sources"] = [{"label": label, "url": url}] if label and url else []
            fact["verification"] = (
                "source_attributed" if fact["sources"] else "unverified_model_synthesis"
            )
    plan.setdefault(
        "research_questions",
        [
            f"What is the direct answer to: {prompt}",
            "Which facts are essential and attributable?",
        ]
        if intent["research_required"]
        else [],
    )
    plan.setdefault("answer_skeleton", ["ANSWER", "SUPPORT"])
    plan.setdefault("music_mood", "documentary")
    novelty_plan = safe_novelty_plan(intent, facts, plan.get("information_plan"))

    max_duration = min(options.max_duration, settings.shortform_max_duration)
    minimum_duration = options.min_duration
    legacy_body_blocks = plan.get("script_blocks") or _factual_blocks(intent, facts)
    format_plan = plan_format(intent, facts, legacy_body_blocks, novelty_plan)
    # Story arc: what the viewer learns, in which role and order.  It is the
    # shared semantic input of payoff, writer, hook, fitting and scene systems.
    supplied_payoff = plan.get("payoff_plan") if isinstance(plan.get("payoff_plan"), dict) else {}
    story_arc = safe_story_arc(
        intent,
        facts,
        format_plan,
        novelty_plan,
        protected=bool(supplied_payoff.get("hook_must_not_reveal")) or _is_protected_question(intent)
        or format_plan.get("selected_format") == "quiz",
        supplied=plan.get("story_arc") if isinstance(plan.get("story_arc"), dict) else None,
    )
    planner_blocks: list[dict[str, Any]] = []
    if plan.get("script_blocks"):
        legacy_body_blocks = _attach_story_fact_ids(copy.deepcopy(plan["script_blocks"]), facts)
        planner_blocks = legacy_body_blocks
    else:
        legacy_body_blocks = _factual_blocks(intent, facts, story_arc)
    initial_payoff_plan = _safe_payoff_plan(
        intent,
        legacy_body_blocks,
        supplied=plan.get("payoff_plan"),
        format_plan=format_plan,
        novelty_plan=novelty_plan,
        story_arc=story_arc,
    )
    raw_blocks, script_writer_diagnostics = _generate_body_with_v2_or_fallback(
        prompt,
        intent,
        resolved_options,
        settings,
        facts,
        legacy_body_blocks,
        initial_payoff_plan,
        format_plan,
        novelty_plan,
        provider=script_writer_provider,
        review_provider=script_review_provider,
        story_arc=story_arc,
    )
    raw_blocks, trimmed_post_payoff_fluff = trim_post_payoff_fluff(raw_blocks)
    payoff_plan = _safe_payoff_plan(
        intent,
        raw_blocks,
        supplied=initial_payoff_plan,
        format_plan=format_plan,
        novelty_plan=novelty_plan,
        story_arc=story_arc,
    )
    planned_reaction_arc = reaction_arc(intent, payoff_plan, format_plan)
    raw_blocks, selected_hook_candidate, hook_generation = _generate_authoritative_hook_blocks(
        raw_blocks,
        prompt,
        intent,
        facts,
        settings,
        payoff_plan,
        planned_reaction_arc,
        format_plan,
        novelty_plan,
        story_arc,
    )
    hook_candidates = hook_generation.candidates
    wpm = max(1, round(SPEAKING_RATE_WPM * float(options.voice_speed or 1.0)))
    blocks = _normalise_blocks(raw_blocks, max_duration, wpm, story_arc)
    # Normalization must preserve the authoritative hook intact. Re-apply the
    # pre-normalization selection so duration fitting cannot rewrite the opening.
    if selected_hook_candidate:
        blocks = _apply_selected_hook(blocks, selected_hook_candidate.text)
        for index, block in enumerate(blocks, 1):
            block["id"] = f"voice_block_{index:02d}"
    hook_block = next((block for block in blocks if _is_hook_block(block)), None)
    selected_hook = (
        selected_hook_candidate.text
        if selected_hook_candidate
        else (str(hook_block.get("text")) if hook_block else None)
    )
    triple_hook_fallback = fallback_triple_hook(
        intent,
        payoff_plan,
        selected_hook,
        selected_hook_candidate.strategy if selected_hook_candidate else None,
        planned_reaction_arc["hook_reaction"],
        format_plan,
    )
    triple_hook = normalise_triple_hook(
        hook_generation.triple_hook,
        triple_hook_fallback,
        payoff_plan,
    )
    # All three hook channels share the same story brief.
    triple_hook["story_brief"] = {
        "primary_question": story_arc.get("primary_question"),
        "curiosity_gap": story_arc.get("curiosity_gap"),
        "withhold_answer": bool((story_arc.get("curiosity_gap") or {}).get("withhold_answer")),
        "protected_fact_ids": list((story_arc.get("hook") or {}).get("protected_ids") or []),
        "key_surprise_id": story_arc.get("key_surprise_id"),
        "format": story_arc.get("format"),
    }
    script_text = " ".join(block["text"] for block in blocks)
    word_count = len(_words(script_text))
    natural_duration = round(max(4, word_count / wpm * 60), 2)
    effective_minimum = minimum_duration or AUTO_MIN_DURATION
    estimated_duration = round(
        min(max_duration, max(effective_minimum, natural_duration)), 2
    )
    report_progress(progress, "script", "Writing the narration", phase="complete")
    report_progress(progress, "storyboard", "Building the storyboard", phase="start")
    scenes = _build_scenes(
        blocks, estimated_duration, cut_pace=resolved_options.pacing,
        visual_intents=_aligned_visual_intents(
            blocks,
            triple_hook["visual_hook"],
            planner_blocks if plan.get("script_blocks") else [],
            list(plan.get("visual_intents") or []),
            hook_is_fallback=triple_hook.get("status") == "fallback",
        ),
        format_plan=format_plan,
    )
    report_progress(
        progress,
        "storyboard",
        "Building the storyboard",
        phase="complete",
        completed_units=len(scenes),
        total_units=len(scenes),
    )
    width, height = _dimensions(options.aspect_ratio)
    factual_ready = not intent["research_required"] or bool(facts and sources)
    now = datetime.now(UTC).isoformat()
    state: dict[str, Any] = {
        "version": 1,
        "created_at": now,
        "prompt": prompt,
        "mode": "auto",
        "options": resolved_options.model_dump(mode="json"),
        "intent": intent,
        "research": {
            "required": intent["research_required"],
            "questions": plan.get("research_questions", []),
            "status": research_status,
            "provider": research_provider,
            "error": research_error,
            "sources": sources,
        },
        "facts": facts,
        "information_plan": {
            "must_know": [fact["id"] for fact in facts if fact["priority"] == "MUST_KNOW"],
            "answer_skeleton": [block["role"].upper() for block in blocks],
            "story_order": list(story_arc.get("order") or []),
        },
        "story_arc": story_arc,
        "payoff_plan": {
            **payoff_plan,
            "post_payoff_fluff_trimmed": trimmed_post_payoff_fluff,
        },
        "format_plan": format_plan,
        "novelty_plan": novelty_plan,
        "script": {
            "text": script_text,
            "word_count": word_count,
            "blocks": blocks,
            "script_writer_v2": script_writer_diagnostics,
            "narration_owned_by_v2": script_writer_diagnostics.get("status") == "v2_success",
            "selected_hook": selected_hook,
            "selected_hook_strategy": selected_hook_candidate.strategy if selected_hook_candidate else None,
            "triple_hook": triple_hook,
            "hook_generation": {
                "status": hook_generation.status,
                "selected_strategy": hook_generation.selected_strategy,
                "error": hook_generation.error,
            },
            "hook_candidates": [
                {"strategy": str(item.get("strategy") or "") if str(item.get("strategy") or "") in STRATEGIES else "evidence_insight", "text": str(item.get("text") or "")}
                for item in hook_candidates
                if str(item.get("text") or "").strip()
            ][:5],
            "fact_map": [
                {
                    "block_id": block["id"],
                    "fact_ids": list(block.get("fact_ids") or []),
                    "fact_id": (
                        facts[min(index, len(facts) - 1)]["id"]
                        if not block.get("fact_ids") and facts
                        else None
                    ),
                }
                for index, block in enumerate(blocks[1:])
                if facts or block.get("fact_ids")
            ],
        },
        "duration": {
            "mode": "AUTO" if minimum_duration is None else "BOUNDED",
            "estimated_seconds": estimated_duration,
            "natural_seconds": natural_duration,
            "actual_seconds": None,
            "minimum_seconds": minimum_duration,
            "effective_minimum_seconds": effective_minimum,
            "max_seconds": max_duration,
            "speaking_rate_wpm": wpm,
        },
        "voice": {
            "provider": "openai" if settings.openai_api_key else "macos_say",
            "model": settings.openai_tts_model if settings.openai_api_key else "system",
            **initial_voice(
                options.voice,
                voice_id=options.voice_id,
                presentation=options.voice_presentation,
                tone=options.voice_tone,
                speed=options.voice_speed,
            ),
            "blocks": [{"id": block["id"], "status": "awaiting_tts"} for block in blocks],
            "status": "awaiting_tts",
        },
        "storyboard": {"status": "ready", "scene_count": len(scenes)},
        "assets": {"status": "search_required", "license_manifest": []},
        "scenes": scenes,
        "captions": {
            "enabled": options.captions_enabled,
            "style": options.caption_style,
            "position": options.caption_position,
            "font_size": options.caption_font_size,
            "text_color": options.caption_text_color,
            "highlight_color": options.caption_highlight_color,
            "words_per_group": options.caption_words_per_group,
            "items": _build_captions(
                script_text, estimated_duration, options.caption_words_per_group
            ),
            "timing": "phrase_estimate",
            "alignment_provider": "pending",
            "diagnostic": "Word alignment will run after narration is generated.",
        },
        "attention_preferences": resolve_attention_preferences(resolved_options.model_dump(mode="json")),
        "attention_events": [],
        "attention_plan": {"status": "pending", "event_count": 0},
        "music": automatic_music_layer(
            enabled=options.music_enabled,
            topic=intent.get("topic"),
            content_type=intent.get("content_type"),
            planned_mood=plan.get("music_mood"),
            requested_mood=options.music_mood,
            volume=options.music_volume,
            ducking=options.music_ducking,
            fades=options.music_fades,
            script=script_text,
            tone=intent.get("tone"),
            variation_seed=now,
        ),
        "timeline": {
            "duration": estimated_duration,
            "timing": "estimated",
            "width": width,
            "height": height,
            "aspect_ratio": options.aspect_ratio,
            "fps": 30,
            "cut_pace": options.pacing,
            "scene_ids": [scene["id"] for scene in scenes],
        },
        "render": {"status": "ready_to_render" if factual_ready else "blocked_by_research", "url": None},
        "ai_review": {
            "status": "pending",
            "rounds": 0,
            "items": [],
            "automatic_corrections": [],
        },
        "qc": {"status": "not_started", "round": 0, "max_rounds": 2, "issues": []},
        "integrations": {
            "director": ai_result.status,
            "research": research_provider if factual_ready else "unavailable",
            "media": "pexels_connected" if settings.pexels_api_key else "wikimedia_fallback",
            "voice": "openai_ready" if settings.openai_api_key else "system_voice",
            "render": "bundled_ffmpeg",
            "quality_review": "local_checks",
        },
        "provider_errors": {
            key: value
            for key, value in {
                "director": (
                    "Director returned the wrong script language; a safe local plan was used."
                    if plan_language_mismatch
                    else ai_result.error
                ),
                "research": research_error,
            }.items()
            if value
        },
        "pipeline": [
            {
                "id": stage,
                "label": label,
                "status": (
                    "complete"
                    if stage in {"intent", "assets", "captions"}
                    or (stage == "research" and factual_ready)
                    or (stage == "script" and factual_ready)
                    else ("blocked" if stage in {"research", "script"} and not factual_ready else "planned")
                ),
            }
            for stage, label in STAGE_LABELS
        ],
        "edit_history": [],
    }
    replan_attention(state)
    annotate_story_roles(state)
    analyze_pacing(state)
    plan_viewer_reactions(state, planned_reaction_arc)
    return attach_hashes(state)


def _requested_language(text: str) -> str | None:
    if any(term in text for term in ("auf deutsch", "to german", "in german", "übersetz", "uebersetz")):
        return "de"
    if any(term in text for term in ("auf englisch", "to english", "in english")):
        return "en"
    return None


def _timestamp_seconds(text: str) -> int | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"\b(?:sekunde|second|at)\s+(\d{1,3})\b", text)
    return int(match.group(1)) if match else None


def apply_edit(
    previous: dict[str, Any], instruction: str, settings: Settings
) -> tuple[dict[str, Any], list[str]]:
    text = instruction.casefold()
    directive = interpret_edit(instruction, previous, settings)
    if not directive.components:
        raise UnsupportedEdit(directive.clarification or "Please describe the change you want.")
    requested_language = _requested_language(text)
    state = copy.deepcopy(previous)
    state["version"] = int(previous.get("version", 1)) + 1
    roots = {
        "language": "script",
        "format": "timeline",
        "duration": "script",
    }
    changed = resolve_edit_scope(
        instruction, {roots.get(component, component) for component in directive.components}
    )
    applied: list[str] = []

    if requested_language and requested_language != state["intent"]["language"]:
        options = AdvancedOptions.model_validate(state.get("options", {}))
        options.language = requested_language
        translated = build_initial_state(state["prompt"], options, settings)
        translated["version"] = state["version"]
        translated["created_at"] = state["created_at"]
        translated["edit_history"] = copy.deepcopy(state.get("edit_history", []))
        state = translated
        applied.append("language")

    if "captions" in directive.components:
        captions = state["captions"]
        if directive.caption_action == "larger":
            captions["font_size"] = min(112, int(captions.get("font_size", 72)) + 10)
            applied.append("caption size")
        elif directive.caption_action == "smaller":
            captions["font_size"] = max(36, int(captions.get("font_size", 72)) - 10)
            applied.append("caption size")
        elif directive.caption_action == "style":
            captions["style"] = (
                "minimal" if captions.get("style") != "minimal" else "bold"
            )
            applied.append("caption style")
        elif directive.caption_action == "reduce":
            captions["density"] = "reduced"
            captions["items"] = captions.get("items", [])[::2]
            applied.append("caption density")
        elif directive.caption_action == "move":
            captions["position"] = "upper" if any(word in text for word in ("up", "higher", "oben")) else "lower"
            applied.append("caption position")
        elif directive.caption_action is None:
            captions["edit_instruction"] = instruction
            captions["style"] = "karaoke"
            applied.append("caption styling")
        colors = {
            "gelb": "#ffd166",
            "yellow": "#ffd166",
            "orange": "#ff6838",
            "weiss": "#ffffff",
            "white": "#ffffff",
        }
        for keyword, color in colors.items():
            if keyword in text:
                captions["highlight_color"] = color
                applied.append("caption color")
                break

    if "music" in directive.components:
        moods = {
            "ruh": "ambient",
            "calm": "ambient",
            "ambient": "ambient",
            "doku": "documentary",
            "documentary": "documentary",
            "tech": "tech",
            "dram": "cinematic",
            "spann": "cinematic",
            "cinematic": "cinematic",
        }
        disabled = any(
            phrase in text
            for phrase in ("no music", "music off", "ohne musik", "musik aus")
        )
        state["music"]["enabled"] = not disabled
        if not disabled:
            state["music"]["mood"] = next(
                (mood for marker, mood in moods.items() if marker in text),
                state["music"].get("mood", "ambient"),
            )
        state["music"]["status"] = "disabled" if disabled else "generation_required"
        applied.append("soundtrack")

    if "voice" in directive.components:
        apply_voice_preferences(
            state["voice"],
            gender=directive.voice_gender,
            tone=directive.voice_tone,
            speed=directive.voice_speed,
            change_speaker=directive.change_speaker,
        )
        applied.append("voice")

    blocks = state["script"]["blocks"]
    old_scenes = copy.deepcopy(state["scenes"])
    script_changed = bool(requested_language and requested_language != previous["intent"]["language"])

    if directive.script_action == "shorter":
        if len(blocks) > 2:
            blocks.pop(-2)
        state["duration"]["max_seconds"] = max(
            10, min(int(state["duration"]["max_seconds"]), round(previous["duration"]["estimated_seconds"] * 0.8))
        )
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"], story_arc=state.get("story_arc"))
        script_changed = True
        applied.append("shorter script")

    if directive.script_action in {"rewrite_intro", "stronger_hook"}:
        de = state["intent"]["language"] == "de"
        topic = state["intent"]["topic"]
        blocks[0]["text"] = (
            f"Was, wenn alles, was du über {topic} zu wissen glaubst, nur die halbe Wahrheit ist?"
            if de
            else f"What if everything you think you know about {topic} is only half the story?"
        )
        blocks[0]["role"] = "hook"
        state["script"]["selected_hook"] = blocks[0]["text"]
        script_changed = True
        applied.append("rewritten opening" if directive.script_action == "rewrite_intro" else "stronger hook")

    if directive.script_action == "clearer":
        state["intent"]["tone"] = "clear_explainer"
        state["script"]["clarity_instruction"] = instruction
        script_changed = True
        applied.append("clearer explanation")

    if any(word in text for word in ("entferne", "remove", "lösche", "loesche")):
        if len(blocks) <= 2:
            raise UnsupportedEdit("The script has no removable middle sentence.")
        target_match = re.search(r"(?:about|über|ueber|zu)\s+([\wäöüß -]+)", text)
        target_words = set(_words(target_match.group(1))) if target_match else set()
        candidates = range(1, len(blocks) - 1)
        remove_index = max(
            candidates,
            key=lambda index: len(target_words & set(_words(blocks[index]["text"].casefold()))),
        )
        blocks.pop(remove_index)
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"], story_arc=state.get("story_arc"))
        script_changed = True
        applied.append("removed sentence")

    facts_protected = any(
        phrase in text
        for phrase in ("don't change facts", "do not change facts", "fakten nicht ändern", "fakten nicht aendern")
    )
    if any(word in text for word in ("dramatisch", "dramatic", "spannender")):
        de = state["intent"]["language"] == "de"
        blocks[0]["text"] = (
            "Ein einziger Moment veränderte alles — und fast niemand erkannte ihn rechtzeitig."
            if de
            else "One moment changed everything — and almost nobody recognized it in time."
        )
        state["intent"]["tone"] = "dramatic_documentary"
        script_changed = True
        applied.append("dramatic opening")

    wants_more_facts = not facts_protected and any(
        word in text
        for word in ("füge", "fuege", "ergänze", "ergaenze", "add ", "recherch")
    )
    if wants_more_facts:
        result = research_topic(f"{state['prompt']} {instruction}", state["intent"]["language"], settings)
        existing_claims = {fact["claim"] for fact in state["facts"]}
        new_facts = [fact for fact in result.facts if fact["claim"] not in existing_claims]
        if not new_facts:
            raise UnsupportedEdit("No attributable new fact was found for that request.")
        next_fact = new_facts[0]
        next_fact["id"] = f"fact_{len(state['facts']) + 1:02d}"
        state["facts"].append(next_fact)
        state["research"]["sources"] = list(
            {source["url"]: source for source in state["research"]["sources"] + result.sources}.values()
        )
        state["research"]["status"] = result.status
        blocks.insert(-1, {"id": "", "role": "detail", "text": next_fact["claim"]})
        blocks[:] = _normalise_blocks(blocks, state["duration"]["max_seconds"], story_arc=state.get("story_arc"))
        script_changed = True
        applied.append("researched fact")

    if script_changed:
        _refresh_script_derivatives(state, old_scenes=old_scenes)
        for item in state["scenes"]:
            item.pop("media", None)
            item["asset_status"] = "search_required"
        state["assets"].update(status="search_required", license_manifest=[])

    if "assets" in directive.components:
        timestamp = _timestamp_seconds(text)
        scene = state["scenes"][0]
        if timestamp is not None:
            containing = [
                item for item in state["scenes"] if item["start"] <= timestamp <= item["end"]
            ]
            scene = containing[0] if containing else min(
                state["scenes"],
                key=lambda item: min(abs(item["start"] - timestamp), abs(item["end"] - timestamp)),
            )
        if any(word in text for word in ("schneller", "faster", "fast cuts", "schnelle schnitte")):
            state["timeline"]["cut_pace"] = "fast"
            state.setdefault("options", {})["pacing"] = "fast"
            state["scenes"] = _build_scenes(
                state["script"]["blocks"],
                float(state["timeline"]["duration"]),
                state["scenes"],
                "fast",
                format_plan=state.get("format_plan"),
            )
            state["storyboard"] = {"status": "ready", "scene_count": len(state["scenes"])}
            state["timeline"]["scene_ids"] = [item["id"] for item in state["scenes"]]
            for item in state["scenes"]:
                item.pop("media", None)
                item["asset_status"] = "search_required"
            state["assets"].update(status="search_required", license_manifest=[])
            applied.append("cut pace")
        else:
            targets = [scene] if timestamp is not None else state["scenes"]
            for target in targets:
                target["asset_status"] = "replacement_required"
                target["preferred_media"] = "video"
                target.pop("media", None)
                target["edit_instruction"] = instruction
                if directive.visual_action == "dynamic":
                    target["motion"] = "dynamic"
            state["assets"].update(
                {"status": "search_required", "preference": directive.visual_action or "different"}
            )
            applied.append("visual footage")

    duration_match = re.search(r"\b(\d{1,3})\s*(?:seconds?|sekunden?|s)\b", text)
    if "duration" in directive.components and duration_match:
        maximum = max(10, min(180, int(duration_match.group(1))))
        state["duration"]["max_seconds"] = maximum
        state.setdefault("options", {})["max_duration"] = maximum
        blocks[:] = _normalise_blocks(blocks, maximum, story_arc=state.get("story_arc"))
        script_changed = True
        _refresh_script_derivatives(state, old_scenes=old_scenes)
        for item in state["scenes"]:
            item.pop("media", None)
            item["asset_status"] = "search_required"
        state["assets"].update(status="search_required", license_manifest=[])
        applied.append(f"{maximum} second duration")

    aspect = next((ratio for ratio in ("9:16", "1:1", "16:9") if ratio in instruction), None)
    if aspect is None:
        aspect = next(
            (
                ratio
                for marker, ratio in (
                    ("portrait", "9:16"),
                    ("vertical", "9:16"),
                    ("landscape", "16:9"),
                    ("square", "1:1"),
                )
                if marker in text
            ),
            None,
        )
    if aspect and aspect != state["timeline"].get("aspect_ratio"):
        width, height = _dimensions(aspect)
        state["timeline"].update({"width": width, "height": height, "aspect_ratio": aspect})
        state.setdefault("options", {})["aspect_ratio"] = aspect
        applied.append("format")

    if not applied:
        raise UnsupportedEdit(
            directive.clarification
            or "I understood the area to change, but need a little more detail about the result you want."
        )

    for component in changed:
        if component == "render":
            previous_render = copy.deepcopy(state.get("render", {}))
            previous_url = previous_render.get("url")
            state["render"] = {
                **previous_render,
                "status": "regeneration_required",
                "url": previous_url,
                "stale": bool(previous_url),
            }
            state.setdefault("ai_review", {}).update(
                status="pending", items=[], automatic_corrections=[]
            )
        elif component == "qc":
            state["qc"].update({"status": "not_started", "issues": []})
    state["edit_history"].append(
        {
            "revision": state["version"],
            "instruction": instruction,
            "changed_components": changed,
            "summary": ", ".join(applied),
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    return attach_hashes(state), changed
