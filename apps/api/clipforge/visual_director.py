"""Visual Director V2: per-scene visual strategy and bounded, quality-gated fallbacks.

The director sits on top of the existing media pipeline; it never replaces it.
For every scene it decides which *kind* of visual communicates the scene best
(real stock, a simple explanatory graphic, a number or comparison visual) from
signals that already exist — the Story Arc role derived from script blocks, the
payoff plan's reveal policy, the canonical visual intent and query plan, the
selected format — without any topic vocabulary.

Free real media is always searched first by ``prepare_project_media``.  Only
when no real candidate survives the strict quality gate does the director
consider, in the scene's own fallback order:

* a deterministic simple graphic (number, A-vs-B, process steps),
* one paid generated image (bounded per project and per scene, verified with
  the existing OpenCLIP path, never shown before the Story Arc allows it),
* reusing an already accepted project visual, or
* leaving the scene missing (the renderer keeps its existing nonfatal policy).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .image_generation import (
    DEFAULT_PORTRAIT_SIZE,
    ImageGenerationError,
    model_label,
    quality_label,
    resolve_image_size,
)
from .media import (
    _RELEVANCE_STOP,
    _VISUAL_QUERY_STOP,
    GENERATED_ASSET_SOURCE,
    GRAPHIC_ASSET_SOURCE,
    _mentions,
    _semantic_query,
    _visual_query_tokens,
    scene_coverage_targets,
    visual_target_key,
)
from .payoff import reveals_protected_payoff
from .simple_graphics import GraphicSpecError, normalise_graphic_spec, render_simple_graphic
from .visual_verifier import SCENE_VISUAL_THRESHOLD, visual_intent_text

VERSION = 2

# Visual strategy types.
STOCK_VIDEO = "stock_video"
STOCK_PHOTO = "stock_photo"
GENERATED_IMAGE = "generated_image"
SIMPLE_GRAPHIC = "simple_graphic"
TEXT_NUMBER_VISUAL = "text_number_visual"
COMPARISON_VISUAL = "comparison_visual"
REUSE_PREVIOUS_VISUAL = "reuse_previous_visual"
VISUAL_TYPES = (
    STOCK_VIDEO,
    STOCK_PHOTO,
    GENERATED_IMAGE,
    SIMPLE_GRAPHIC,
    TEXT_NUMBER_VISUAL,
    COMPARISON_VISUAL,
    REUSE_PREVIOUS_VISUAL,
)

# Final per-scene media decisions.
ACCEPTED_REAL = "ACCEPTED_REAL"
GENERATE_FALLBACK = "GENERATE_FALLBACK"
DEGRADED = "DEGRADED"
MISSING = "MISSING"
DECISIONS = (ACCEPTED_REAL, GENERATE_FALLBACK, DEGRADED, MISSING)

# Story Arc roles the director reasons about.
STORY_ROLES = ("hook", "evidence", "primary_answer", "explanation", "secondary_insight", "final_payoff")
_BLOCK_ROLE_MAP = {
    "hook": "hook",
    "answer": "primary_answer",
    "primary_answer": "primary_answer",
    "explanation": "explanation",
    "cause": "explanation",
    "mechanism": "explanation",
    "process": "explanation",
    "support": "evidence",
    "evidence": "evidence",
    "context": "evidence",
    "setup": "evidence",
    "status": "evidence",
    "detail": "secondary_insight",
    "turn": "secondary_insight",
    "insight": "secondary_insight",
    "secondary_insight": "secondary_insight",
    "payoff": "final_payoff",
    "final_payoff": "final_payoff",
}
_REVEAL_ROLES = {"primary_answer", "final_payoff"}

_NUMBER_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[.,   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(\s*(?:%|prozent\b|percent\b))?",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:[,;:→]|\s[-–—]\s)\s*")
_WORD_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ-]+", re.UNICODE)
_LABEL_STOP = _RELEVANCE_STOP | _VISUAL_QUERY_STOP | {
    "rund", "etwa", "fast", "über", "ueber", "knapp", "mehr", "als", "around", "about", "nearly", "over",
    "than", "roughly", "approximately", "circa", "ca",
}
_GRAPHIC_INTENT_STRATEGIES = {"process", "diagram_or_card"}
_COMPARISON_FORMATS = {"comparison", "quiz"}


# ---------------------------------------------------------------------------
# Story Arc
# ---------------------------------------------------------------------------

def story_arc(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Story role, stage and reveal permission per script block id.

    Derived from the authoritative script block roles and the payoff plan; the
    protected answer may only be shown from the first answer/payoff block on.
    """
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    blocks = [block for block in script.get("blocks") or [] if isinstance(block, dict)]
    roles = [
        _BLOCK_ROLE_MAP.get(str(block.get("role") or "").strip().casefold(), "evidence") for block in blocks
    ]
    body = [index for index, role in enumerate(roles) if role != "hook"]
    if body and "final_payoff" not in roles and len(body) >= 2 and roles[body[-1]] != "primary_answer":
        roles[body[-1]] = "final_payoff"
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    protected = payoff.get("reveal_policy") == "after_supporting_information" and bool(
        str(payoff.get("hook_must_not_reveal") or "").strip() or str(payoff.get("protected_visual_target") or "").strip()
    )
    reveal_index = next((index for index, role in enumerate(roles) if role in _REVEAL_ROLES), len(roles) - 1)
    arc: dict[str, dict[str, Any]] = {}
    for index, (block, role) in enumerate(zip(blocks, roles, strict=True)):
        stage = "setup" if index < reveal_index else "reveal" if index == reveal_index else "resolution"
        arc[str(block.get("id") or f"block_{index}")] = {
            "story_role": role,
            "story_stage": stage,
            "reveal_allowed": not protected or index >= reveal_index,
            "block_index": index,
            "fact_ids": [str(value) for value in block.get("fact_ids") or []],
        }
    return arc


# Story Intelligence V2 scene annotations (``story_arc.annotate_story_roles``).
_ARC_STAGES = {"before_reveal", "reveal", "after_reveal", "open"}
_ARC_REVEALED_STAGES = {"reveal", "after_reveal", "open"}
_ARC_ROLE_MAP = {
    "primary_answer": "primary_answer",
    "explanation": "explanation",
    "secondary_insight": "secondary_insight",
    "evidence": "evidence",
    "essential_context": "evidence",
    "supporting_fact": "evidence",
    "comparison": "evidence",
    "ranked_item": "evidence",
}


def _block_role(scene: dict[str, Any], state: dict[str, Any]) -> str:
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    block_id = str(scene.get("block_id") or "")
    block = next((item for item in script.get("blocks") or [] if isinstance(item, dict) and str(item.get("id") or "") == block_id), {})
    return str(block.get("role") or "").strip().casefold()


def _block_text(scene: dict[str, Any], state: dict[str, Any]) -> str:
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    block_id = str(scene.get("block_id") or "")
    block = next((item for item in script.get("blocks") or [] if isinstance(item, dict) and str(item.get("id") or "") == block_id), {})
    return " ".join(str(block.get("text") or "").split())


def arc_scene_context(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Story context from the Story Arc's own scene annotations, when present.

    The arc is the single source of truth for role, stage, primary answer and
    final payoff; nothing is re-derived from text.
    """
    stage = str(scene.get("story_stage") or "")
    if stage not in _ARC_STAGES:
        return None
    arc_role = str(scene.get("story_role") or "") or None
    if scene.get("is_primary_answer"):
        role = "primary_answer"
    elif scene.get("is_final_payoff"):
        role = "final_payoff"
    elif _block_role(scene, state) == "hook":
        role = "hook"
    else:
        role = _ARC_ROLE_MAP.get(arc_role or "", "evidence")
    unit_ids = [str(value) for value in scene.get("story_unit_ids") or []]
    if not unit_ids:
        script = state.get("script") if isinstance(state.get("script"), dict) else {}
        block = next((item for item in script.get("blocks") or [] if isinstance(item, dict) and item.get("id") == scene.get("block_id")), {})
        unit_ids = [str(value) for value in block.get("fact_ids") or []]
    return {
        # The Story Arc's own role, unchanged (None for scenes carrying no fact).
        "story_role": arc_role,
        # Presentation role the director reasons with: reveal identity first.
        "visual_role": role,
        "story_stage": stage,
        "reveal_allowed": stage in _ARC_REVEALED_STAGES,
        "is_primary_answer": bool(scene.get("is_primary_answer")),
        "is_final_payoff": bool(scene.get("is_final_payoff")),
        "block_index": None,
        "fact_ids": unit_ids,
        "source": "story_arc",
    }


def scene_story_context(scene: dict[str, Any], state: dict[str, Any], arc: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Story context for one scene.

    Projects annotated by the Story Arc use its fields directly.  The block-role
    inference below exists only for projects created before the Story Arc.
    """
    annotated = arc_scene_context(scene, state)
    if annotated is not None:
        return annotated
    arc = arc if arc is not None else story_arc(state)
    context = arc.get(str(scene.get("block_id") or ""))
    if context is None:
        # Unknown block (legacy or edited state): never assume the reveal is allowed
        # while the payoff plan protects an answer.
        payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
        protected = payoff.get("reveal_policy") == "after_supporting_information"
        return {"story_role": "evidence", "visual_role": "evidence", "story_stage": "setup", "reveal_allowed": not protected, "block_index": None, "fact_ids": [], "source": "legacy_inference"}
    return {**context, "visual_role": context["story_role"], "source": "legacy_inference"}


def answer_withheld(state: dict[str, Any]) -> bool:
    """Whether the project protects an answer until its reveal (arc first)."""
    arc = state.get("story_arc") if isinstance(state.get("story_arc"), dict) else None
    if arc is not None:
        return bool((arc.get("curiosity_gap") or {}).get("withhold_answer"))
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    return payoff.get("reveal_policy") == "after_supporting_information"


def structured_protection(state: dict[str, Any]) -> bool:
    """Structured reveal identity exists: the arc or a planner target key."""
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    return isinstance(state.get("story_arc"), dict) or bool(visual_target_key(payoff.get("protected_visual_target")))


def visual_reveal_safe(
    state: dict[str, Any],
    strategy: dict[str, Any],
    query_plan: dict[str, Any] | None,
    text: str = "",
    *,
    query: str | None = None,
) -> bool:
    """Whether a visual chosen for this scene may appear before the reveal.

    Structural: a visual picked while protection was active is safe; one picked
    at/after the reveal of a withheld answer without active protection (the
    Story Arc lifts it there) may depict the answer.  Payoff-text matching is
    only used for legacy projects without structured identity.
    """
    plan = query_plan or {}
    protected_terms = set(plan.get("protected_entities") or [])
    if text and _mentions(_visual_query_tokens(text), protected_terms):
        return False
    if not answer_withheld(state):
        return True
    if strategy.get("reveal_allowed") and (plan.get("protection_scope") == "revealed" or strategy.get("is_primary_answer")):
        if strategy.get("is_primary_answer"):
            return False
        # A real asset found by a query the planner keyed to a different
        # target (the other comparison side, shared context) cannot depict the
        # protected answer; anything else from a revealed scene might.
        payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
        protected_key = visual_target_key(payoff.get("protected_visual_target"))
        query_key = visual_target_key((plan.get("query_targets") or {}).get(str(query or "")))
        return bool(protected_key and query_key and query_key != protected_key)
    if text and not structured_protection(state):
        payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
        return not reveals_protected_payoff(text, payoff)
    return True


# ---------------------------------------------------------------------------
# Strategy planning
# ---------------------------------------------------------------------------

def _content_words(text: str) -> list[str]:
    return [
        word.strip("-")
        for word in _WORD_RE.findall(text)
        if len(word.strip("-")) > 3 and word.casefold() not in _LABEL_STOP and not word.isdigit()
    ]


def salient_number(narration: str) -> dict[str, str] | None:
    """The most prominent statistic in the narration, with a short label."""
    best: tuple[int, re.Match[str]] | None = None
    for match in _NUMBER_RE.finditer(narration):
        digits = re.sub(r"\D", "", match.group(1))
        percent = bool(match.group(2))
        plain_year = not percent and re.fullmatch(r"(1[5-9]|20)\d\d", match.group(1)) is not None
        if plain_year or (len(digits) < 3 and not percent):
            continue
        weight = len(digits) + (3 if percent else 0)
        if best is None or weight > best[0]:
            best = (weight, match)
    if best is None:
        return None
    match = best[1]
    value = " ".join(match.group(1).split())
    if match.group(2):
        value = f"{value} %"
    label_words = _content_words(narration[match.end():])[:2]
    return {"kind": "number", "value": value, "label": " ".join(label_words)}


def process_steps(narration: str) -> list[str]:
    """Up to three short, ordered labels taken from the narration itself."""
    clauses = [clause for clause in _CLAUSE_SPLIT_RE.split(narration.strip(" .!?")) if clause.strip()]
    if len(clauses) >= 2:
        steps = [" ".join(_content_words(clause)[:3]) for clause in clauses]
    else:
        steps = _content_words(narration)
    return list(dict.fromkeys(step for step in steps if step))[:3]


def _title(value: str) -> str:
    return " ".join(word[:1].upper() + word[1:] for word in value.split())


def _protected_text_blocked(text: str, state: dict[str, Any], reveal_allowed: bool, protected_terms: set[str]) -> bool:
    if reveal_allowed:
        return False
    if _mentions(_visual_query_tokens(text), protected_terms):
        return True
    if isinstance(state.get("story_arc"), dict):
        # On-screen text repeats this scene's own narration, which the Story
        # Arc keeps free of its primary answer before the reveal (the answer
        # scene itself is the reveal).  No payoff-text matching.
        return False
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    return reveals_protected_payoff(text, payoff)


def plan_scene_strategy(
    scene: dict[str, Any],
    state: dict[str, Any],
    query_plan: dict[str, Any],
    *,
    arc: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Structured visual strategy for one scene; deterministic and topic-agnostic."""
    story = scene_story_context(scene, state, arc)
    narration = " ".join(str(scene.get("narration") or "").split())
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    intent_strategy = str(intent.get("visual_strategy") or "literal")
    protected_terms = set(query_plan.get("protected_entities") or [])
    reveal_allowed = bool(story["reveal_allowed"])
    format_name = str((state.get("format_plan") or {}).get("selected_format") or "")

    graphic: dict[str, Any] | None = None
    planned = STOCK_PHOTO if str(scene.get("preferred_media") or "video") == "photo" else STOCK_VIDEO
    reason = "concrete_subject_real_media"

    targets = scene_coverage_targets(scene, state, query_plan)["targets"]
    sides = [target for target, role in targets.items() if role in {"subject_a", "subject_b"}]
    number = salient_number(narration)
    if format_name in _COMPARISON_FORMATS and len(sides) >= 2:
        spec = {"kind": "comparison", "left": _title(sides[0]), "right": _title(sides[1])}
        if not _protected_text_blocked(f"{sides[0]} {sides[1]}", state, reveal_allowed, protected_terms):
            planned, graphic, reason = COMPARISON_VISUAL, spec, "scene_compares_two_subjects"
    if (
        planned in {STOCK_VIDEO, STOCK_PHOTO}
        and number is not None
        and not _protected_text_blocked(f"{number['value']} {number['label']}", state, reveal_allowed, protected_terms)
    ):
        planned, graphic, reason = TEXT_NUMBER_VISUAL, number, "scene_states_a_statistic"
    if planned in {STOCK_VIDEO, STOCK_PHOTO} and intent_strategy in _GRAPHIC_INTENT_STRATEGIES:
        steps = process_steps(narration)
        spec = {"kind": "process", "steps": steps}
        if len(steps) >= 2 and not _protected_text_blocked(" ".join(steps), state, reveal_allowed, protected_terms):
            planned, graphic, reason = SIMPLE_GRAPHIC, spec, f"intent_{intent_strategy}_better_explained"
    graphic = normalise_graphic_spec(graphic)
    if graphic is None and planned in {SIMPLE_GRAPHIC, COMPARISON_VISUAL, TEXT_NUMBER_VISUAL}:
        planned = STOCK_PHOTO if str(scene.get("preferred_media") or "video") == "photo" else STOCK_VIDEO
        reason = "concrete_subject_real_media"

    if planned == SIMPLE_GRAPHIC:
        # Better explained than illustrated: only strongly covering real media
        # beats the graphic, and no paid generation is needed.
        chain = ["real_media_strong", SIMPLE_GRAPHIC, REUSE_PREVIOUS_VISUAL]
    elif planned in {TEXT_NUMBER_VISUAL, COMPARISON_VISUAL}:
        # A relevant real visual (numbers get the existing statistic callout
        # overlay); otherwise a number or split graphic instead of unrelated
        # footage.  Generated images could invent maps, flags or figures.
        chain = ["real_media", SIMPLE_GRAPHIC, REUSE_PREVIOUS_VISUAL]
    else:
        chain = ["real_media", GENERATED_IMAGE, REUSE_PREVIOUS_VISUAL]
        if story["visual_role"] in {"explanation", "final_payoff"} or story["story_role"] == "explanation":
            # An explanation (or the closing idea) is better served by a free
            # process graphic than by reused, unrelated filler when neither
            # real media nor a generated image is available.  Steps come from
            # the whole block: scenes may split a sentence mid-clause.
            steps = process_steps(_block_text(scene, state) or narration)
            spec = normalise_graphic_spec({"kind": "process", "steps": steps})
            if spec and not _protected_text_blocked(" ".join(steps), state, reveal_allowed, protected_terms):
                graphic = spec
                chain = ["real_media", GENERATED_IMAGE, SIMPLE_GRAPHIC, REUSE_PREVIOUS_VISUAL]
    return {
        "version": VERSION,
        "story_role": story["story_role"],
        "visual_role": story["visual_role"],
        "story_stage": story["story_stage"],
        "story_source": story.get("source", "legacy_inference"),
        "reveal_allowed": reveal_allowed,
        "is_primary_answer": bool(story.get("is_primary_answer")),
        "is_final_payoff": bool(story.get("is_final_payoff")),
        "fact_ids": story["fact_ids"],
        "planned_type": planned,
        "reason": reason,
        "graphic": graphic,
        "overlay": "statistic_callout" if planned == TEXT_NUMBER_VISUAL else None,
        "fallback_chain": chain,
    }


def requires_strong_real_media(strategy: dict[str, Any]) -> bool:
    return "real_media_strong" in (strategy.get("fallback_chain") or [])


# ---------------------------------------------------------------------------
# Generation policy and budget (project level)
# ---------------------------------------------------------------------------

def generation_policy(state: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Project-level generation policy; created once with safe defaults.

    The global setting remains a kill switch: a project can never enable paid
    generation that the installation disabled.
    """
    director = state.setdefault("visual_director", {})
    stored = director.get("policy") if isinstance(director.get("policy"), dict) else {}
    policy = {
        "generated_image_fallback_enabled": bool(stored.get("generated_image_fallback_enabled", True)),
        # The model always follows configuration (one source of truth, and no
        # stale identifier from an earlier build survives in project state);
        # the model actually used is persisted per generation record.
        "generated_image_model": settings.generated_image_model,
        "generated_image_quality": str(stored.get("generated_image_quality") or settings.generated_image_quality),
        "generated_image_size": resolve_image_size(
            settings.generated_image_model,
            str(stored.get("generated_image_size") or settings.generated_image_size or DEFAULT_PORTRAIT_SIZE),
        ),
        "max_auto_generated_images_per_project": max(0, int(stored.get("max_auto_generated_images_per_project", settings.max_auto_generated_images_per_project))),
        "max_generation_attempts_per_scene": max(0, int(stored.get("max_generation_attempts_per_scene", settings.max_generation_attempts_per_scene))),
    }
    director["version"] = VERSION
    director["policy"] = policy
    director.setdefault("generations", [])
    return {**policy, "generated_image_fallback_enabled": policy["generated_image_fallback_enabled"] and settings.generated_image_fallback_enabled}


def generation_counts(state: dict[str, Any]) -> dict[str, int]:
    director = state.get("visual_director") if isinstance(state.get("visual_director"), dict) else {}
    records = [item for item in director.get("generations") or [] if isinstance(item, dict)]
    billed = [item for item in records if item.get("billed")]
    return {
        "auto_generated_images": sum(1 for item in billed if item.get("trigger") == "auto"),
        "manual_generated_images": sum(1 for item in billed if item.get("trigger") == "manual"),
        "generated_images": len(billed),
        "attempts": len(records),
    }


def _scene_auto_attempts(state: dict[str, Any], scene_id: str) -> int:
    director = state.get("visual_director") if isinstance(state.get("visual_director"), dict) else {}
    return sum(
        1
        for item in director.get("generations") or []
        if isinstance(item, dict) and item.get("trigger") == "auto" and item.get("scene_id") == scene_id
        and item.get("status") != "reused_cached"
    )


def auto_generation_block_reason(
    state: dict[str, Any], scene: dict[str, Any], settings: Settings, generator: Any | None
) -> str | None:
    """Why automatic generation may not run for this scene (``None`` = allowed)."""
    policy = generation_policy(state, settings)
    if not policy["generated_image_fallback_enabled"]:
        return "disabled"
    if generator is None:
        return "unavailable_no_api_key"
    if generation_counts(state)["auto_generated_images"] >= policy["max_auto_generated_images_per_project"]:
        return "project_budget_exhausted"
    if _scene_auto_attempts(state, str(scene.get("id") or "")) >= policy["max_generation_attempts_per_scene"]:
        return "scene_attempts_exhausted"
    return None


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

_ROLE_DIRECTION = {
    "hook": "an intriguing, eye-catching close-up that makes the viewer curious",
    "evidence": "a clear documentary-style photo showing the observation",
    "primary_answer": "a clear, instructive photo that shows the key idea",
    "explanation": "a clear, instructive photo that makes the mechanism easy to see",
    "secondary_insight": "a clear photo of this specific detail, visually distinct from the main answer",
    "final_payoff": "a strong, memorable closing image that resolves the idea",
}


def _prompt_phrases(scene: dict[str, Any], reveal_allowed: bool, protected_terms: set[str]) -> list[str]:
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    # The planner's protected-filtered provider queries, but only those that
    # came from the explicit visual intent: narration/topic fallback queries
    # would pass raw narration words to the image model.
    explicit = {
        _semantic_query(str(value), _VISUAL_QUERY_STOP, limit=6) for value in intent.get("media_queries") or []
    }
    phrases = [str(value) for value in scene.get("search_queries") or [] if str(value).strip() and str(value) in explicit][:2]
    phrases += [str(value) for key in ("actions", "objects", "context") for value in intent.get(key) or [] if str(value).strip()]
    goal = str(intent.get("visual_goal") or scene.get("visual_goal") or "").strip()
    if goal:
        phrases.insert(0, goal)
    must_not = {str(value).casefold() for value in intent.get("must_not_show") or [] if str(value).strip()}
    kept: list[str] = []
    for phrase in phrases:
        phrase = " ".join(phrase.split())[:120]
        if not phrase or phrase.casefold() in must_not:
            continue
        if not reveal_allowed and _mentions(_visual_query_tokens(phrase), protected_terms):
            continue
        if phrase.casefold() not in {item.casefold() for item in kept}:
            kept.append(phrase)
    return kept[:6]


def build_generation_prompt(
    scene: dict[str, Any], state: dict[str, Any], strategy: dict[str, Any], query_plan: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Concise visual prompt from structured scene semantics (never raw narration).

    Returns ``None`` when no concrete, reveal-safe subject is available.
    """
    plan = query_plan or (scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {})
    protected_terms = set(plan.get("protected_entities") or [])
    reveal_allowed = bool(strategy.get("reveal_allowed", True))
    phrases = _prompt_phrases(scene, reveal_allowed, protected_terms)
    if not phrases:
        return None
    subject = phrases[0]
    details = "; ".join(phrases[1:])
    fiction = str((state.get("intent") or {}).get("content_type") or "").casefold() in {"fiction", "story", "fictional_story"}
    style = "Cinematic, realistic still image" if fiction else "Photorealistic photograph"
    direction = _ROLE_DIRECTION.get(str(strategy.get("visual_role") or strategy.get("story_role") or ""), "a clear photo of the subject")
    prompt = (
        f"{style}: {subject}."
        + (f" Details: {details}." if details else "")
        + f" Purpose: {direction}."
        " Composition: vertical 9:16 short-video frame, one clear main subject centered in the middle"
        " of the frame with generous margins so a centered 9:16 crop keeps it fully visible;"
        " uncluttered background; calm lower third left free for captions."
        " Natural lighting, sharp focus, realistic colors."
        " Do not include any text, letters, numbers, labels, captions, logos, watermarks or UI elements."
    )
    reveal_safe = visual_reveal_safe(state, strategy, plan, " ".join(phrases))
    if not reveal_allowed and not reveal_safe:
        return None
    return {"prompt": prompt, "summary": "; ".join(phrases[:3])[:200], "reveal_safe": reveal_safe}


def prompt_reveals_protected(prompt: str, scene: dict[str, Any], state: dict[str, Any], strategy: dict[str, Any]) -> bool:
    if strategy.get("reveal_allowed", True):
        return False
    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    return not visual_reveal_safe(state, strategy, plan, prompt)


# ---------------------------------------------------------------------------
# Generated image verification and persistence
# ---------------------------------------------------------------------------

def _verify_generated(path: Path, scene: dict[str, Any], state: dict[str, Any], verifier: Any | None) -> dict[str, Any]:
    """Existing OpenCLIP thresholds decide; unavailable verification is recorded, not faked."""
    if verifier is None or getattr(verifier, "status", "") != "available" or not hasattr(verifier, "verify_local_image"):
        return {"status": getattr(verifier, "status", "unavailable_dependency") or "unavailable_dependency", "accepted": True, "verified": False}
    try:
        result = verifier.verify_local_image(path, visual_intent_text(scene, state), asset_identity=f"generated:{path.name}")
    except Exception:  # noqa: BLE001 - verification is advisory when it cannot run
        return {"status": "verification_failed", "accepted": True, "verified": False}
    if result is None or getattr(result, "status", "") != "verified":
        return {"status": getattr(result, "status", "verification_failed"), "accepted": True, "verified": False}
    scene_score = result.scene_score if result.scene_score is not None else result.score
    accepted = bool(scene_score is not None and scene_score >= SCENE_VISUAL_THRESHOLD and not result.presentation_risk)
    return {
        "status": "verified",
        "accepted": accepted,
        "verified": True,
        "score": result.score,
        "scene_score": scene_score,
        "presentation_risk": bool(result.presentation_risk),
        "threshold": SCENE_VISUAL_THRESHOLD,
    }


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def _generated_metadata(
    *, digest: str, relative: str, width: int, height: int, prompt: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    label = model_label(record["model"])
    return {
        "identity": f"{GENERATED_ASSET_SOURCE}:photo:{digest}",
        "provider": GENERATED_ASSET_SOURCE,
        "source": GENERATED_ASSET_SOURCE,
        "provider_id": digest,
        "kind": "photo",
        "cache_path": relative,
        "source_url": "",
        "creator": f"AI-generated · {label}",
        "creator_url": None,
        "width": width,
        "height": height,
        "duration": None,
        "query": prompt["summary"],
        "title": f"AI-generated image: {prompt['summary']}",
        "description": prompt["summary"],
        "tags": [],
        "license": "AI-generated with the OpenAI API; not stock media",
        # Platform AI-content disclosure reads this flag.
        "ai_generated": True,
        "reveal_safe": bool(record.get("reveal_safe")),
        "story_role": record.get("story_role"),
        "is_primary_answer": bool(record.get("is_primary_answer")),
        "generation": {
            key: record.get(key)
            for key in (
                "model", "model_label", "quality", "quality_label", "size", "prompt", "prompt_summary", "created_at",
                "trigger", "reason", "scene_id", "block_id", "fact_ids", "story_role", "visual_role", "story_stage", "story_source",
                "is_primary_answer", "is_final_payoff",
                "ai_generated", "reveal_safe", "verification",
                "usage", "cost_usd", "cost_source",
            )
        },
        "relevance": {"confidence": "generated", "visual": record.get("verification") or {}},
    }


def generate_scene_image(
    scene: dict[str, Any],
    state: dict[str, Any],
    strategy: dict[str, Any],
    *,
    project_id: str,
    settings: Settings,
    generator: Any,
    verifier: Any | None,
    trigger: str,
    reason: str,
    prompt_override: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One bounded generation attempt shared by automatic and manual paths.

    Returns ``(media_metadata | None, record)``.  The record is appended to the
    project's generation log in every case (including failures) so attempts and
    spend stay auditable.  Never raises for provider or verification problems.
    """
    policy = generation_policy(state, settings)
    director = state["visual_director"]
    now = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "scene_id": str(scene.get("id") or ""),
        "block_id": str(scene.get("block_id") or ""),
        "fact_ids": list(strategy.get("fact_ids") or []),
        "story_role": strategy.get("story_role"),
        "visual_role": strategy.get("visual_role"),
        "is_primary_answer": bool(strategy.get("is_primary_answer")),
        "is_final_payoff": bool(strategy.get("is_final_payoff")),
        "story_stage": strategy.get("story_stage"),
        "story_source": strategy.get("story_source"),
        "ai_generated": True,
        "trigger": trigger,
        "reason": reason,
        "model": getattr(generator, "model", None) or policy["generated_image_model"],
        "quality": policy["generated_image_quality"],
        "size": policy["generated_image_size"],
        "created_at": now,
        "billed": False,
        "usage": None,
        "cost_usd": None,
        "cost_source": "not_reported_by_api",
    }
    record["model_label"] = model_label(record["model"])
    record["quality_label"] = quality_label(record["quality"])
    built = build_generation_prompt(scene, state, strategy)
    if prompt_override is not None and " ".join(prompt_override.split()):
        # A user-edited prompt is still bound by the Story Arc reveal rules.
        text = " ".join(prompt_override.split())[:1200]
        built = None if prompt_reveals_protected(text, scene, state, strategy) else {
            "prompt": text,
            "summary": text[:200],
            "reveal_safe": visual_reveal_safe(
                state, strategy, scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}, text
            ),
        }
    if built is None:
        record.update(status="skipped_no_safe_prompt")
        director["generations"].append(record)
        return None, record
    record.update(prompt=built["prompt"], prompt_summary=built["summary"], reveal_safe=built["reveal_safe"])
    project_dir = settings.render_root.resolve() / project_id
    digest = hashlib.sha256(
        "|".join((record["model"], record["quality"], record["size"], built["prompt"])).encode("utf-8")
    ).hexdigest()[:20]
    final_path = project_dir / "assets" / GENERATED_ASSET_SOURCE / f"{digest}.png"
    sidecar = final_path.with_suffix(".json")
    if final_path.is_file() and sidecar.is_file():
        # Identical accepted prompt already paid for (e.g. after undo): reuse it.
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
            size = _image_size(final_path)
            if size is not None and isinstance(cached, dict):
                record.update(status="reused_cached", verification=cached.get("verification"), cached_from=cached.get("created_at"))
                director["generations"].append(record)
                relative = final_path.relative_to(settings.render_root.resolve()).as_posix()
                return _generated_metadata(digest=digest, relative=relative, width=size[0], height=size[1], prompt=built, record=record), record
        except (OSError, ValueError):
            pass
    try:
        image = generator.generate(built["prompt"], quality=record["quality"], size=record["size"])
    except ImageGenerationError as exc:
        record.update(status="failed", error=exc.category)
        director["generations"].append(record)
        return None, record
    except Exception as exc:  # noqa: BLE001 - one visual must never fail the video
        record.update(status="failed", error=type(exc).__name__)
        director["generations"].append(record)
        return None, record
    record.update(
        billed=True,
        model=getattr(image, "model", record["model"]) or record["model"],
        quality=getattr(image, "quality", record["quality"]) or record["quality"],
        size=getattr(image, "size", record["size"]) or record["size"],
        usage=getattr(image, "usage", None) or None,
    )
    record["model_label"] = model_label(record["model"])
    staging = project_dir / "generation-staging" / f"{digest}.png"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(image.data)
    size = _image_size(staging)
    if size is None:
        staging.unlink(missing_ok=True)
        record.update(status="rejected", rejection="unreadable_image")
        director["generations"].append(record)
        return None, record
    verification = _verify_generated(staging, scene, state, verifier)
    record["verification"] = verification
    if not verification["accepted"]:
        staging.unlink(missing_ok=True)
        record.update(status="rejected", rejection="visual_verification_failed")
        director["generations"].append(record)
        return None, record
    final_path.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(final_path)
    record["status"] = "accepted"
    sidecar.write_text(
        json.dumps({key: record.get(key) for key in ("model", "quality", "size", "prompt_summary", "created_at", "verification")}),
        encoding="utf-8",
    )
    director["generations"].append(record)
    relative = final_path.relative_to(settings.render_root.resolve()).as_posix()
    return _generated_metadata(digest=digest, relative=relative, width=size[0], height=size[1], prompt=built, record=record), record


# ---------------------------------------------------------------------------
# Simple graphic fallback
# ---------------------------------------------------------------------------

def render_scene_graphic(
    scene: dict[str, Any], state: dict[str, Any], strategy: dict[str, Any], *, project_id: str, settings: Settings
) -> dict[str, Any] | None:
    spec = normalise_graphic_spec(strategy.get("graphic"))
    if spec is None:
        return None
    width, height = int(state["timeline"]["width"]), int(state["timeline"]["height"])
    digest = hashlib.sha256(json.dumps([spec, width, height], sort_keys=True).encode("utf-8")).hexdigest()[:20]
    destination = settings.render_root.resolve() / project_id / "graphics" / f"{digest}.png"
    try:
        if not destination.is_file():
            render_simple_graphic(spec, destination, width=width, height=height)
    except (GraphicSpecError, OSError, ValueError):
        return None
    return {
        "identity": f"{GRAPHIC_ASSET_SOURCE}:photo:{digest}",
        "provider": GRAPHIC_ASSET_SOURCE,
        "source": GRAPHIC_ASSET_SOURCE,
        "provider_id": digest,
        "kind": "photo",
        "cache_path": destination.relative_to(settings.render_root.resolve()).as_posix(),
        "source_url": "",
        "creator": "ClipForge graphic",
        "creator_url": None,
        "width": width,
        "height": height,
        "duration": None,
        "query": "",
        "title": "",
        "description": "",
        "tags": [],
        "license": "Generated locally by ClipForge",
        "graphic": spec,
    }


# ---------------------------------------------------------------------------
# Fallback orchestration (called by prepare_project_media)
# ---------------------------------------------------------------------------

def resolve_scene_fallback(
    scene: dict[str, Any],
    state: dict[str, Any],
    strategy: dict[str, Any],
    *,
    project_id: str,
    settings: Settings,
    generator: Any | None,
    verifier: Any | None,
    failure_reason: str,
    run_state: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Walk the scene's fallback chain after real media failed the quality gate.

    Returns ``(metadata, resolved_type)``; ``(None, None)`` lets the caller try
    reuse of accepted project media and finally mark the scene missing.
    ``run_state`` carries a per-run circuit breaker so an unreachable provider
    is not retried for every scene.
    """
    generation = strategy.setdefault("generation", {"status": "not_needed"})
    for step in strategy.get("fallback_chain") or []:
        if step == GENERATED_IMAGE:
            blocked = run_state.get("generation_blocked") or auto_generation_block_reason(state, scene, settings, generator)
            if blocked:
                generation.update(status=blocked)
                continue
            metadata, record = generate_scene_image(
                scene,
                state,
                strategy,
                project_id=project_id,
                settings=settings,
                generator=generator,
                verifier=verifier,
                trigger="auto",
                reason=failure_reason,
            )
            generation.update(status=record["status"], model=record.get("model"), quality=record.get("quality"))
            if record.get("error") in {"invalid_credentials", "model_unavailable", "network_error", "timeout", "rate_limited"}:
                run_state["generation_blocked"] = f"provider_{record['error']}"
            if metadata is not None:
                return metadata, GENERATED_IMAGE
        elif step == SIMPLE_GRAPHIC:
            metadata = render_scene_graphic(scene, state, strategy, project_id=project_id, settings=settings)
            if metadata is not None:
                return metadata, SIMPLE_GRAPHIC
    return None, None


def record_decision(
    scene: dict[str, Any], strategy: dict[str, Any], decision: str, resolved_type: str | None, reason: str
) -> None:
    strategy["decision"] = decision
    strategy["resolved_type"] = resolved_type
    strategy["decision_reason"] = reason
    strategy.setdefault("generation", {"status": "not_needed"})
    scene["visual_director"] = strategy


def summarize_project(state: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Persisted project-level summary (decisions per type, generation spend)."""
    policy = generation_policy(state, settings)
    decisions: dict[str, int] = {}
    resolved: dict[str, int] = {}
    for scene in state.get("scenes") or []:
        director = scene.get("visual_director") if isinstance(scene.get("visual_director"), dict) else {}
        if director.get("decision"):
            decisions[director["decision"]] = decisions.get(director["decision"], 0) + 1
        if director.get("resolved_type"):
            resolved[director["resolved_type"]] = resolved.get(director["resolved_type"], 0) + 1
    counts = generation_counts(state)
    summary = {
        "decisions": decisions,
        "resolved_types": resolved,
        **counts,
        "auto_budget_remaining": max(0, policy["max_auto_generated_images_per_project"] - counts["auto_generated_images"]),
        "model": policy["generated_image_model"],
        "quality": policy["generated_image_quality"],
    }
    state["visual_director"]["summary"] = summary
    return summary
