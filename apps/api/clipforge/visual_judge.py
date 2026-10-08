"""Visual Judge: structured, explainable scoring of scene visual candidates.

The judge runs AFTER the existing authorities (rights policy, deterministic
metadata relevance, OpenCLIP verification) and never overrides a rejection
they made. It adds what keyword relevance cannot express:

1. Deterministic pre-filters: invalid license, missing media, unsupported
   kind, tiny resolution, impossible 9:16 reframe, watermark/text markers.
2. Structured dimension scores in [0, 1]: semantic_match, factual_match,
   visual_impact, vertical_fit, quality, novelty, continuity,
   license_confidence.
3. Hard requirements: semantic_match and factual_match below their floors
   reject the candidate, so an attractive but wrong asset cannot win on
   impact or resolution.
4. An optional, bounded vision-language judge on the remaining shortlist
   (disabled by default). It can lower scores or veto, never resurrect a
   rejected candidate.

Every verdict carries ``reject``, ``reasons``, ``confidence`` and a concise
``rationale`` for debugging; scores are persisted with the candidate evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from .visual_rights import MediaRights, evaluate_rights

VERSION = 1

SEMANTIC_FLOOR = 0.38
FACTUAL_FLOOR = 0.40
# A real asset that passes but scores below this is "weak": the AI fallback
# rules may prefer a generated visual for non-factual scenes.
WEAK_SCORE = 0.55
WEAK_SEMANTIC = 0.50
WEAK_SEMANTIC_ABSTRACT = 0.55

WEIGHTS = {
    "semantic_match": 0.30,
    "factual_match": 0.25,
    "visual_impact": 0.12,
    "vertical_fit": 0.10,
    "quality": 0.08,
    "novelty": 0.08,
    "continuity": 0.04,
    "license_confidence": 0.03,
}

# Output frame (portrait ClipForge default). Reframing a source below these
# effective pixels means an upscale beyond ~3.2x: visibly soft on a phone.
OUTPUT_LONG_SIDE = 1920
MIN_REFRAME_LONG_PIXELS = 600
MIN_SHORT_SIDE = 480
MAX_REFRAME_ASPECT = 2.6  # wider than this cannot be reframed to 9:16 meaningfully
VERTICAL = 9 / 16

# Clip-score scale used by the existing verifier (ViT-B-32): 0.24 is the
# acceptance threshold, ~0.34 is a very clear match.
CLIP_LOW, CLIP_HIGH = 0.20, 0.34
STRONG_CLIP_MARGIN = 0.26  # media.STRONG_SCENE_VISUAL_SCORE (threshold + 0.02)

_WATERMARK_MARKERS = (
    "watermark", "watermarked", "all rights reserved", "©", "(c) ", "sample image", "preview only",
    "logo overlay", "stock watermark",
)
_FILLER_MARKERS = (
    "copy space", "isolated on white", "white background", "abstract background", "concept image",
    "business concept", "template", "mockup", "mock-up", "stock photo", "background texture",
    "wallpaper", "banner",
)
_IMPACT_MARKERS = (
    "close-up", "closeup", "close up", "macro", "aerial", "drone", "dramatic", "slow motion", "timelapse",
    "time-lapse", "detail", "explosion", "launch", "eruption", "silhouette",
)
_REPLICA_MARKERS = (
    "replica", "miniature", "toy", "lego", "cosplay", "costume", "figurine", "lookalike", "look-alike",
    "reenactment", "re-enactment", "souvenir", "model kit", "scale model", "theme park", "imitation",
)
_ARCHIVAL_PROVIDERS = {"loc", "europeana"}
_STOP = frozenset({
    "the", "and", "of", "in", "on", "at", "to", "a", "an", "for", "with", "from", "der", "die", "das",
    "und", "von", "im", "am", "des",
})


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 4)


def _tokens(text: str) -> set[str]:
    from .media import _normalize_term

    return {
        _normalize_term(word)
        for word in re.findall(r"[\wäöüß-]+", str(text or "").casefold())
        if len(word) > 2 and word not in _STOP
    }


def _matches(term_tokens: set[str], metadata: set[str]) -> int:
    from .media import _related_terms

    return sum(1 for token in term_tokens if any(_related_terms(token, other) for other in metadata))


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def reframe_geometry(width: int, height: int, *, portrait: bool = True) -> dict[str, Any]:
    """How a source maps into the 9:16 frame: retained share and upscale."""
    width, height = int(width or 0), int(height or 0)
    if width <= 0 or height <= 0:
        return {"known": False}
    target = VERTICAL if portrait else 1 / VERTICAL
    ratio = width / height
    if ratio >= target:
        crop_w, crop_h = height * target, float(height)
        retained = crop_w / width
        long_pixels = crop_h if portrait else crop_w
    else:
        crop_w, crop_h = float(width), width / target
        retained = crop_h / height
        long_pixels = crop_h if portrait else crop_w
    return {
        "known": True,
        "aspect": round(ratio, 4),
        "retained_share": round(retained, 4),
        "effective_long_pixels": round(long_pixels),
        "upscale": round(OUTPUT_LONG_SIDE / max(1.0, long_pixels), 3),
        "orientation": "portrait" if ratio < 0.9 else "square" if ratio <= 1.12 else "landscape",
    }


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------

def prefilter(candidate: Any, *, portrait: bool = True, seen_keys: set[str] | None = None) -> list[str]:
    """Cheap deterministic rejections before any model sees the candidate."""
    reasons: list[str] = []
    rights = evaluate_rights(_value(candidate, "rights"))
    if rights.status != "usable":
        reasons.append(f"license_{rights.status}")
    if _value(candidate, "kind") not in {"photo", "video"}:
        reasons.append("unsupported_media_type")
    if not str(_value(candidate, "download_url") or "").strip() and not _value(candidate, "cache_path"):
        reasons.append("missing_media")
    width, height = int(_value(candidate, "width") or 0), int(_value(candidate, "height") or 0)
    if width and height and min(width, height) < MIN_SHORT_SIDE:
        reasons.append("tiny_resolution")
    geometry = reframe_geometry(width, height, portrait=portrait)
    if geometry.get("known"):
        if portrait and geometry["aspect"] > MAX_REFRAME_ASPECT:
            reasons.append("reframe_infeasible_aspect")
        elif geometry["effective_long_pixels"] < MIN_REFRAME_LONG_PIXELS and "tiny_resolution" not in reasons:
            reasons.append("insufficient_resolution_after_reframe")
    text = " ".join((
        str(_value(candidate, "title") or ""), str(_value(candidate, "description") or "")[:600],
        " ".join(_value(candidate, "tags") or ()),
    )).casefold()
    if any(marker in text for marker in _WATERMARK_MARKERS):
        reasons.append("watermark_or_text_marker")
    if seen_keys is not None:
        from .visual_providers import asset_keys

        try:
            keys = asset_keys(candidate)
        except (TypeError, AttributeError):
            keys = set()
        if keys & seen_keys:
            reasons.append("duplicate_asset")
    return reasons


# ---------------------------------------------------------------------------
# Scene context
# ---------------------------------------------------------------------------

def scene_context(scene: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The judge's view of a scene: entities, period, sensitivity, neighbours."""
    from .visual_search_planner import scene_signals

    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    search_plan = plan.get("search_plan") if isinstance(plan.get("search_plan"), dict) else None
    signals = scene_signals(scene, state)
    if search_plan:
        signals = {**signals, **{key: value for key, value in (search_plan.get("signals") or {}).items() if value}}
    scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
    index = next((i for i, item in enumerate(scenes) if item is scene or item.get("id") == scene.get("id")), None)
    neighbours = []
    if index is not None:
        for offset in (-2, -1, 1):
            position = index + offset
            if 0 <= position < len(scenes) and isinstance(scenes[position].get("media"), dict):
                other = scenes[position]
                neighbours.append({
                    "offset": offset,
                    "media": other["media"],
                    "same_block": bool(scene.get("block_id")) and other.get("block_id") == scene.get("block_id"),
                    "objects": [str(v) for v in ((other.get("visual_intent") or {}).get("objects") or [])][:4],
                })
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    scene_text = " ".join(str(v) for v in (
        scene.get("narration"), scene.get("visual_goal"), intent.get("visual_goal"),
        " ".join(str(v) for v in intent.get("objects") or []), " ".join(str(v) for v in intent.get("context") or []),
    ) if v)
    return {
        "entities": list(signals.get("entities") or []),
        "alternate_terms": list(signals.get("alternate_terms") or []),
        "period": str(signals.get("period") or ""),
        "location": str(signals.get("location") or ""),
        "sensitivity": str(signals.get("sensitivity") or "none"),
        "domain": str(signals.get("domain") or "general"),
        "abstract": str(intent.get("visual_strategy") or "") in {"process", "diagram_or_card"},
        "objects": [str(v) for v in intent.get("objects") or []][:4],
        "scene_text": scene_text.casefold(),
        "neighbours": neighbours,
    }


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def _metadata_text(candidate: Any) -> str:
    from .media import MediaCandidate, _metadata_evidence, persisted_candidate

    item = candidate if isinstance(candidate, MediaCandidate) else persisted_candidate(dict(candidate))
    origin = item.origin or {}
    extra = " ".join(str(origin.get(key) or "") for key in ("subjects", "subject", "date", "date_created", "year", "location"))
    return f"{_metadata_evidence(item)} {extra}".strip()


def semantic_score(relevance: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    confidence = str(relevance.get("confidence") or "unknown")
    meta = {"high": 0.85, "acceptable": 0.65, "unknown": 0.35, "generated": 0.6}.get(confidence, 0.0)
    if int(relevance.get("selection_tier") or 0) >= 3:
        meta += 0.1
    visual = relevance.get("visual") or {}
    scene_score = visual.get("scene_score") if visual.get("scene_score") is not None else visual.get("score")
    verified = visual.get("status") == "verified" and scene_score is not None
    clip = (float(scene_score) - CLIP_LOW) / (CLIP_HIGH - CLIP_LOW) if verified else None
    if confidence == "rejected":
        value = 0.0
    elif clip is not None:
        value = 0.5 * min(1.0, meta) + 0.5 * max(0.0, min(1.0, clip))
    else:
        value = 0.9 * min(1.0, meta)
    return _clamp(value), {"metadata": confidence, "clip_scene_score": scene_score, "verified": verified}


def factual_score(candidate: Any, relevance: dict[str, Any], context: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    if (relevance.get("temporal_evidence") or {}).get("mismatch"):
        return 0.0, ["period_mismatch"]
    if (relevance.get("setting_evidence") or {}).get("mismatch") or (
        (relevance.get("visual") or {}).get("setting_evidence") or {}
    ).get("mismatch"):
        return 0.0, ["setting_contradiction"]
    metadata_text = _metadata_text(candidate)
    metadata = _tokens(metadata_text)
    folded = metadata_text.casefold()
    value = 1.0
    entities = [entity for entity in context["entities"] if _tokens(entity)]
    if entities and context["sensitivity"] != "none":
        from .media import _related_terms

        # The place a named thing stands in does not identify it: "Berlin"
        # in a church caption is not evidence of the Berlin Wall.
        location = _tokens(context.get("location") or "")
        best = 0.0
        for name in [*entities, *context["alternate_terms"]]:
            wanted = _tokens(name)
            distinctive = {t for t in wanted if not any(_related_terms(t, place) for place in location)} or wanted
            if not distinctive:
                continue
            best = max(best, _matches(distinctive, metadata) / len(distinctive))
        if not metadata:
            value, note = 0.3, "entity_unverifiable_without_metadata"
        elif best >= 0.999:
            value, note = 1.0, "entity_confirmed"
        elif best >= 0.5:
            value, note = 0.6, "entity_partial"
        elif best > 0:
            value, note = 0.45, "entity_weak"
        else:
            value, note = 0.15, "entity_not_named"
        notes.append(note)
    temporal = relevance.get("temporal_evidence") or {}
    if temporal.get("required"):
        if temporal.get("matched_years"):
            notes.append("period_confirmed")
        elif temporal.get("established"):
            value *= 0.75
            notes.append("period_by_archival_wording_only")
    if context["sensitivity"] != "none":
        markers = [marker for marker in _REPLICA_MARKERS if marker in folded and marker not in context["scene_text"]]
        if markers:
            value *= 0.3
            notes.append(f"staged_or_replica:{markers[0]}")
    return _clamp(value), notes


def impact_score(candidate: Any, relevance: dict[str, Any], context: dict[str, Any]) -> float:
    text = " ".join((str(_value(candidate, "title") or ""), str(_value(candidate, "description") or "")[:400])).casefold()
    value = 0.5
    if _value(candidate, "kind") == "video":
        value += 0.15
    if any(marker in text for marker in _IMPACT_MARKERS):
        value += 0.1
    if any(marker in text for marker in _FILLER_MARKERS):
        value -= 0.25
    visual = relevance.get("visual") or {}
    if (relevance.get("presentation_risk") or {}).get("rejected") or visual.get("presentation_risk"):
        value -= 0.3
    diagram, photo = visual.get("diagram_score"), visual.get("photographic_score")
    if diagram is not None and photo is not None and float(diagram) > float(photo) and context["domain"] != "diagram":
        value -= 0.1
    if min(int(_value(candidate, "width") or 0), int(_value(candidate, "height") or 0)) >= 1080:
        value += 0.05
    return _clamp(value)


def vertical_score(candidate: Any, *, portrait: bool = True) -> tuple[float, dict[str, Any]]:
    geometry = reframe_geometry(int(_value(candidate, "width") or 0), int(_value(candidate, "height") or 0), portrait=portrait)
    if not geometry.get("known"):
        return 0.5, geometry
    retained = geometry["retained_share"]
    if retained >= 0.8:
        value = 1.0
    elif retained >= 0.5:
        value = 0.85
    elif retained >= 0.3:
        value = 0.62 if _value(candidate, "kind") == "photo" else 0.58
    elif retained >= 0.22:
        value = 0.35
    else:
        value = 0.1
    if geometry["upscale"] > 2.4:
        value -= 0.15
    return _clamp(value), geometry


def quality_score(candidate: Any) -> float:
    short = min(int(_value(candidate, "width") or 0), int(_value(candidate, "height") or 0))
    if not short:
        return 0.5
    return 1.0 if short >= 1080 else 0.8 if short >= 720 else 0.6 if short >= 600 else 0.4


def _style_family(media: Any) -> str:
    provider = str(_value(media, "provider") or _value(media, "source") or "")
    if provider == "generated_openai":
        return "generated"
    if provider == "simple_graphic":
        return "graphic"
    if provider == "nasa":
        return "space"
    text = " ".join((str(_value(media, "title") or ""), str(_value(media, "description") or "")[:300])).casefold()
    years = [int(y) for y in re.findall(r"(?<!\d)(1[0-9]{3})(?!\d)", text)]
    if provider in _ARCHIVAL_PROVIDERS or "black and white" in text or (years and min(years) < 1980):
        return "archival"
    return "modern"


def _concept_terms(media: Any) -> set[str]:
    return _tokens(" ".join((str(_value(media, "title") or ""), str(_value(media, "description") or "")[:200])))


def novelty_continuity(candidate: Any, context: dict[str, Any]) -> tuple[float, float, list[str]]:
    from .visual_providers import asset_keys

    notes: list[str] = []
    try:
        keys = asset_keys(candidate)
    except (TypeError, AttributeError):
        keys = set()
    terms = _concept_terms(candidate)
    creator = str(_value(candidate, "creator") or "").casefold().strip()
    family = _style_family(candidate)
    novelty, continuity_votes = 1.0, []
    objects = _tokens(" ".join(context["objects"]))
    for neighbour in context["neighbours"]:
        media = neighbour["media"]
        same_subject = bool(objects) and len(objects & _tokens(" ".join(neighbour["objects"]))) >= max(1, len(objects) // 2)
        intentional = neighbour["same_block"] or same_subject
        try:
            other_keys = asset_keys(media)
        except (TypeError, AttributeError):
            other_keys = set()
        other_terms = _concept_terms(media)
        overlap = len(terms & other_terms) / max(1, len(terms | other_terms)) if terms and other_terms else 0.0
        if keys & other_keys:
            score, note = (0.7, "same_asset_intentional_continuity") if intentional else (0.0, "same_asset_repeat")
        elif creator and creator == str(media.get("creator") or "").casefold().strip() and overlap >= 0.5:
            score, note = (0.8, "same_shoot_intentional") if intentional else (0.3, "near_identical_shot")
        elif overlap >= 0.6:
            score, note = (0.85, "similar_concept_intentional") if intentional else (0.5, "similar_concept")
        else:
            score, note = 1.0, ""
        if note:
            notes.append(f"{note}@{neighbour['offset']:+d}")
        if abs(neighbour["offset"]) == 1 or score < novelty:
            novelty = min(novelty, score)
        if abs(neighbour["offset"]) == 1:
            continuity_votes.append(1.0 if intentional or _style_family(media) == family else 0.6)
    continuity = sum(continuity_votes) / len(continuity_votes) if continuity_votes else 0.75
    if continuity_votes and continuity < 0.7:
        notes.append(f"style_jump:{family}")
    return _clamp(novelty), _clamp(continuity), notes


def license_score(candidate: Any) -> float:
    rights = MediaRights.read(_value(candidate, "rights"))
    if evaluate_rights(rights).status != "usable":
        return 0.0
    if rights.public_domain is True:
        return 1.0
    if str(rights.rights_source or "").startswith("provider_terms:"):
        return 0.85
    return 0.95


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def judge_candidate(
    candidate: Any,
    relevance: dict[str, Any],
    scene: dict[str, Any],
    state: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    portrait: bool = True,
) -> dict[str, Any]:
    context = context or scene_context(scene, state)
    reasons = prefilter(candidate, portrait=portrait)
    semantic, semantic_detail = semantic_score(relevance)
    factual, factual_notes = factual_score(candidate, relevance, context)
    vertical, geometry = vertical_score(candidate, portrait=portrait)
    novelty, continuity, diversity_notes = novelty_continuity(candidate, context)
    scores = {
        "semantic_match": semantic,
        "factual_match": factual,
        "visual_impact": impact_score(candidate, relevance, context),
        "vertical_fit": vertical,
        "quality": quality_score(candidate),
        "novelty": novelty,
        "continuity": continuity,
        "license_confidence": license_score(candidate),
    }
    if relevance.get("confidence") == "rejected":
        reasons.append("semantic_authority_rejected")
    if semantic < SEMANTIC_FLOOR:
        reasons.append("semantic_below_floor")
    if factual < FACTUAL_FLOOR:
        reasons.append("factual_below_floor")
    if novelty == 0.0:
        reasons.append("unsafe_repeat")
    final = round(sum(scores[key] * weight for key, weight in WEIGHTS.items()), 4)
    reasons = list(dict.fromkeys(reasons))
    verified = semantic_detail["verified"]
    if verified and relevance.get("confidence") in {"high", "acceptable"} and factual >= 0.7:
        confidence = "high"
    elif verified or relevance.get("confidence") in {"high", "acceptable"}:
        confidence = "medium"
    else:
        confidence = "low"
    clip = semantic_detail["clip_scene_score"]
    rationale = (
        f"semantic {semantic:.2f} (metadata {semantic_detail['metadata']}"
        + (f", clip {float(clip):.3f}" if clip is not None else ", unverified")
        + f"); factual {factual:.2f}" + (f" [{', '.join(factual_notes)}]" if factual_notes else "")
        + (f"; reframe {geometry.get('orientation')} keeps {geometry.get('retained_share', 0):.0%}" if geometry.get("known") else "")
        + (f"; {', '.join(diversity_notes)}" if diversity_notes else "")
    )
    return {
        "version": VERSION,
        "scores": scores,
        "final_score": final if not reasons else round(final * 0.5, 4),
        "reject": bool(reasons),
        "reasons": reasons,
        "confidence": confidence,
        "weak": not reasons and (
            final < WEAK_SCORE
            or semantic < (WEAK_SEMANTIC_ABSTRACT if context["abstract"] else WEAK_SEMANTIC)
            # A verified pass without the existing strong-coverage margin.
            or (verified and clip is not None and float(clip) < STRONG_CLIP_MARGIN)
        ),
        "factual_notes": factual_notes,
        "diversity_notes": diversity_notes,
        "geometry": geometry,
        "rationale": rationale[:400],
    }


# ---------------------------------------------------------------------------
# Optional vision-language judge (bounded shortlist, disabled by default)
# ---------------------------------------------------------------------------

class VisionJudge(Protocol):
    name: str

    def judge(self, image_url: str, context: dict[str, Any]) -> dict[str, Any] | None: ...


VLM_INSTRUCTIONS = (
    "You judge one candidate image for one scene of a factual vertical short video. Score 0-10: "
    "semantic_match (does the image show what the scene describes), factual_match (is it the correct "
    "real subject/place/period named in the context, not a lookalike, replica, costume or different "
    "entity; 10 when nothing specific is required), visual_impact (would it stop a scroll), vertical_fit "
    "(does the main subject survive a centered 9:16 crop). Set watermark_or_text true for visible "
    "watermarks, logos or text that dominates the frame, wrong_subject true when it clearly shows "
    "something else, and reject when it must not be used. Keep rationale under 25 words."
)


def _openai_client(settings: Any) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=0)


VLM_CLIENT_FACTORY: Callable[[Any], Any] = _openai_client


class OpenAIVisionJudge:
    """Worker-model image judgement through the Responses API (opt-in)."""

    name = "openai"

    def __init__(self, settings: Any):
        self.settings = settings
        self.model = str(getattr(settings, "visual_judge_vlm_model", None) or settings.openai_worker_model)

    def judge(self, image_url: str, context: dict[str, Any]) -> dict[str, Any] | None:
        from pydantic import BaseModel, Field

        class Verdict(BaseModel):
            semantic_match: int = Field(ge=0, le=10)
            factual_match: int = Field(ge=0, le=10)
            visual_impact: int = Field(ge=0, le=10)
            vertical_fit: int = Field(ge=0, le=10)
            watermark_or_text: bool
            wrong_subject: bool
            reject: bool
            rationale: str = Field(max_length=200)

        try:
            response = VLM_CLIENT_FACTORY(self.settings).responses.parse(
                model=self.model,
                instructions=VLM_INSTRUCTIONS,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": json.dumps(context, ensure_ascii=False)[:1500]},
                    {"type": "input_image", "image_url": image_url, "detail": "low"},
                ]}],
                text_format=Verdict,
                max_output_tokens=200,
                store=False,
            )
            parsed = response.output_parsed
        except Exception:  # noqa: BLE001 - an optional judge never fails acquisition
            return None
        if parsed is None:
            return None
        return {key: getattr(parsed, key) for key in Verdict.model_fields}


VISION_JUDGES: dict[str, Callable[[Any], VisionJudge | None]] = {
    "openai": lambda settings: OpenAIVisionJudge(settings) if getattr(settings, "openai_api_key", None) else None,
}


def get_vision_judge(settings: Any) -> VisionJudge | None:
    provider = str(getattr(settings, "visual_judge_vlm_provider", "none") or "none").casefold()
    factory = VISION_JUDGES.get(provider)
    return factory(settings) if factory is not None else None


def apply_vision_verdict(verdict: dict[str, Any], vlm: dict[str, Any]) -> dict[str, Any]:
    """Blend a VLM verdict in. It may lower or veto; it never resurrects."""
    scores = dict(verdict["scores"])
    scores["semantic_match"] = _clamp(0.5 * scores["semantic_match"] + 0.05 * float(vlm.get("semantic_match") or 0))
    scores["factual_match"] = _clamp(min(scores["factual_match"], float(vlm.get("factual_match") or 0) / 10))
    scores["visual_impact"] = _clamp(0.5 * scores["visual_impact"] + 0.05 * float(vlm.get("visual_impact") or 0))
    scores["vertical_fit"] = _clamp(min(scores["vertical_fit"], 0.3 + 0.07 * float(vlm.get("vertical_fit") or 0)))
    reasons = list(verdict["reasons"])
    if vlm.get("reject"):
        reasons.append("vlm_reject")
    if vlm.get("wrong_subject"):
        reasons.append("vlm_wrong_subject")
    if vlm.get("watermark_or_text"):
        reasons.append("vlm_watermark_or_text")
    if scores["semantic_match"] < SEMANTIC_FLOOR:
        reasons.append("semantic_below_floor")
    if scores["factual_match"] < FACTUAL_FLOOR:
        reasons.append("factual_below_floor")
    reasons = list(dict.fromkeys(reasons))
    final = round(sum(scores[key] * weight for key, weight in WEIGHTS.items()), 4)
    return {
        **verdict,
        "scores": scores,
        "final_score": final if not reasons else round(final * 0.5, 4),
        "reject": bool(reasons),
        "reasons": reasons,
        "vlm": {key: vlm.get(key) for key in (
            "semantic_match", "factual_match", "visual_impact", "vertical_fit", "watermark_or_text",
            "wrong_subject", "reject", "rationale",
        )},
        "rationale": (verdict["rationale"] + f"; vlm: {str(vlm.get('rationale') or '')[:120]}")[:400],
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def judge_and_rank(
    rows: list[tuple[Any, dict[str, Any]]],
    scene: dict[str, Any],
    state: dict[str, Any],
    *,
    portrait: bool = True,
    vision_judge: VisionJudge | None = None,
    vision_budget: dict[str, int] | None = None,
    vision_per_scene: int = 3,
) -> list[tuple[Any, dict[str, Any]]]:
    """Annotate every row with ``relevance['judge']`` and order by the verdict.

    Rejected rows stay in the list (the scene gate rejects them with the
    recorded reason, keeping diagnostics complete) but sort last. Ties keep
    the incoming semantic order.
    """
    if not rows:
        return rows
    context = scene_context(scene, state)
    for candidate, relevance in rows:
        relevance["judge"] = judge_candidate(candidate, relevance, scene, state, context=context, portrait=portrait)
    order = {id(row): index for index, row in enumerate(rows)}
    ranked = sorted(rows, key=lambda row: (not row[1]["judge"]["reject"], row[1]["judge"]["final_score"], -order[id(row)]), reverse=True)
    if vision_judge is not None:
        budget = vision_budget if vision_budget is not None else {}
        per_scene = int(budget.get("per_scene", vision_per_scene))
        used = 0
        for candidate, relevance in ranked:
            if used >= per_scene or relevance["judge"]["reject"]:
                break
            if budget.get("remaining", 0) <= 0:
                relevance["judge"]["vlm_status"] = "budget_exhausted"
                break
            address = str(_value(candidate, "preview_url") or _value(candidate, "verification_url") or "")
            if not address.startswith("https://"):
                relevance["judge"]["vlm_status"] = "no_https_preview"
                continue
            budget["remaining"] = budget.get("remaining", 0) - 1
            budget["used"] = budget.get("used", 0) + 1
            used += 1
            verdict = vision_judge.judge(address, {
                "scene": context["scene_text"][:400], "entities": context["entities"], "period": context["period"],
                "candidate_title": str(_value(candidate, "title") or "")[:200],
            })
            if verdict is None:
                relevance["judge"]["vlm_status"] = "unavailable"
                continue
            relevance["judge"] = apply_vision_verdict(relevance["judge"], verdict)
            relevance["judge"]["vlm_status"] = "judged"
        ranked = sorted(ranked, key=lambda row: (not row[1]["judge"]["reject"], row[1]["judge"]["final_score"], -order[id(row)]), reverse=True)
    return ranked


def summarize(rows: list[tuple[Any, dict[str, Any]]]) -> dict[str, Any]:
    """Compact per-scene diagnostics: counts, rejection reasons, top scores."""
    reasons: dict[str, int] = {}
    judged = [row for row in rows if isinstance(row[1].get("judge"), dict)]
    for _candidate, relevance in judged:
        for reason in relevance["judge"]["reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    top = [
        {
            "identity": _value(candidate, "identity"),
            "final_score": relevance["judge"]["final_score"],
            "reject": relevance["judge"]["reject"],
            "scores": relevance["judge"]["scores"],
            "reasons": relevance["judge"]["reasons"],
            "rationale": relevance["judge"]["rationale"],
        }
        for candidate, relevance in judged[:5]
    ]
    return {
        "version": VERSION,
        "evaluated": len(judged),
        "rejected": sum(1 for row in judged if row[1]["judge"]["reject"]),
        "rejection_reasons": reasons,
        "top": top,
    }

