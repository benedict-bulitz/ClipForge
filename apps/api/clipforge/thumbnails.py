from __future__ import annotations

import re
import subprocess
import tempfile
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .renderer import ffmpeg_path
from .visual_verifier import THUMBNAIL_VISUAL_SHORTLIST, get_visual_verifier


class ThumbnailGenerationError(RuntimeError):
    """A project cover could not be built from its persisted media."""


PLATFORMS = ("tiktok", "instagram", "youtube")
WIDTH, HEIGHT = 1080, 1920
SAFE_X, SAFE_TOP, SAFE_BOTTOM = 72, 180, 260
_WORD_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ]+", re.UNICODE)
_GENERIC_TERMS = {
    "comparison", "reveal", "explanation", "ranking", "question", "scene", "video",
    "visual", "setup", "context", "topic", "short", "format",
}
_STOP_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "in", "is", "it", "more", "of", "on", "or", "the", "this", "to", "what", "which", "who", "why",
}
_TERM_ALIASES = {
    "egyptian": "egypt",
    "egyptians": "egypt",
    "sudanese": "sudan",
    "nubian": "nubia",
    "nubians": "nubia",
    "kushite": "kush",
    "kushites": "kush",
}
_VISUAL_ALIASES = {
    "airplane": "airplane", "airplanes": "airplane", "aircraft": "airplane", "airliner": "airplane", "jet": "airplane", "jets": "airplane",
    "flugzeug": "airplane", "flugzeuge": "airplane", "passagierflugzeug": "airplane", "linienjet": "airplane",
    "airspace": "airspace", "luftraum": "airspace", "border": "border", "borders": "border", "grenze": "border", "landesgrenze": "border",
    "country": "country", "countries": "country", "land": "country", "länder": "country", "laender": "country",
    "sky": "sky", "himmel": "sky", "cloud": "sky", "clouds": "sky", "wolke": "sky", "wolken": "sky",
    "landscape": "landscape", "landschaft": "landscape", "nature": "nature", "natur": "nature", "map": "map", "karte": "map",
    "pyramid": "pyramid", "pyramids": "pyramid", "egypt": "egypt", "sudan": "sudan", "nubia": "nubia", "kush": "kush",
    "star": "star", "stars": "star", "tree": "tree", "trees": "tree", "breath": "breath", "atem": "breath", "winter": "winter",
    "animal": "animal", "animals": "animal", "tier": "animal", "tiere": "animal",
    "island": "island", "islands": "island", "insel": "island", "inseln": "island", "archipelago": "archipelago", "archipel": "archipelago",
    "sweden": "sweden", "schweden": "sweden", "indonesia": "indonesia", "indonesien": "indonesia",
}
_SUPPORTING_VISUAL_TERMS = {"sky", "landscape", "nature", "map", "country", "land", "cloud", "background", "road", "city", "person", "people", "aerial", "countryside", "panoramic", "view", "green", "above", "clouds"}
_VISUAL_SUBJECT_VOCAB = set(_VISUAL_ALIASES.values()) | {"airplane", "airspace", "border", "pyramid", "egypt", "sudan", "nubia", "kush", "star", "tree", "breath", "winter", "animal"}
_WEAK_COVER_PHRASES = {
    "this is why", "it changes everything", "but that's not all", "an okay is missing", "ein okay fehlt", "das ist der grund", "das aendert alles",
}
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
)


def _project_directory(project_id: str, settings: Settings) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", project_id):
        raise ThumbnailGenerationError("The project storage identity is invalid.")
    root = settings.render_root.resolve()
    directory = (root / project_id).resolve()
    if directory.parent != root:
        raise ThumbnailGenerationError("The project storage path is unsafe.")
    return directory


def _words(value: object) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(str(value or "")) if len(word) > 2}


def _semantic_words(value: object) -> set[str]:
    words = {
        word
        for word in _words(value)
        if word not in _GENERIC_TERMS and word not in _STOP_TERMS
    }
    normalized = {word[:-1] if word.endswith("s") and len(word) > 4 else word for word in words}
    return {_TERM_ALIASES.get(word, word) for word in normalized}


def _visual_words(value: object) -> set[str]:
    words = _semantic_words(value)
    return {_VISUAL_ALIASES.get(word, word) for word in words}


def _derive_visual_subjects(state: dict[str, Any]) -> dict[str, list[str]]:
    """Derive compact visual subjects from planning context, not image filenames."""
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    format_plan = state.get("format_plan") if isinstance(state.get("format_plan"), dict) else {}
    script = state.get("script") if isinstance(state.get("script"), dict) else {}
    triple = script.get("triple_hook") if isinstance(script.get("triple_hook"), dict) else {}
    visual_hook = triple.get("visual_hook") if isinstance(triple.get("visual_hook"), dict) else {}
    values: list[object] = [intent.get("topic"), intent.get("question"), format_plan.get("visual_structure"), visual_hook.get("subjects_to_show"), visual_hook.get("visual_priority")]
    primary: set[str] = set()
    supporting: set[str] = set()
    for value in values:
        words = _visual_words(value)
        primary.update(word for word in words if word in _VISUAL_SUBJECT_VOCAB and word not in _SUPPORTING_VISUAL_TERMS and word not in _GENERIC_TERMS)
        supporting.update(word for word in words if word in _SUPPORTING_VISUAL_TERMS)
    # Scene intent is useful for filling in the subject vocabulary, but never
    # promotes a generic background term to a primary subject by itself.
    for scene in state.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        visual = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
        words = _visual_words(" ".join(str(scene.get(key) or "") for key in ("visual_goal", "query", "search_queries")) + " " + " ".join(str(visual.get(key) or "") for key in ("visual_goal", "objects", "subjects_to_show", "media_queries")))
        primary.update(word for word in words if word in _VISUAL_SUBJECT_VOCAB and word not in _SUPPORTING_VISUAL_TERMS and word not in _GENERIC_TERMS)
        supporting.update(word for word in words if word in _SUPPORTING_VISUAL_TERMS)
    return {"primary": sorted(primary), "supporting": sorted(supporting - primary)}


def _protected_words(plan: dict[str, Any]) -> set[str]:
    return _words(plan.get("hook_must_not_reveal"))


def _reveals_protected(value: object, plan: dict[str, Any]) -> bool:
    hidden = _protected_words(plan)
    candidate = _words(value)
    if not hidden or not candidate:
        return False
    text = str(value or "")
    if len(hidden) == 1 and re.search(r"(?i)\b(?:vs\.?|versus|or|oder)\b", text) and re.search(r"(?i)\b(?:who|which|what|wer|welche|was)\b", text):
        return False
    overlap = hidden & candidate
    return bool(overlap) and (len(hidden) == 1 or len(overlap) >= 2)


def _question_parts(state: dict[str, Any]) -> tuple[str, str]:
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    return (
        str(intent.get("topic") or "").strip(" ?!."),
        str(intent.get("question") or state.get("prompt") or "").strip(" ?!."),
    )


def _short_line(value: object, limit: int = 44) -> str:
    text = " ".join(str(value or "").split()).strip(" ?!.")
    if len(text) <= limit:
        return text.upper()
    result = ""
    for word in text.split():
        candidate = f"{result} {word}".strip()
        if len(candidate) > limit:
            break
        result = candidate
    return (result or text[:limit]).upper()


def _comparison_text(question: str, language: str | None = None) -> str:
    match = re.search(r"(?i)\b([A-Za-zÀ-ÖØ-öø-ÿ]+)\s+(?:or|versus|vs\.?|oder)\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)\b", question)
    if match:
        return f"{match.group(1)} VS {match.group(2)}\n{'WER HAT MEHR?' if language == 'de' else 'WHO HAS MORE?'}"
    return "A VS B\nWELCHES?" if language == "de" else "A VS B\nWHICH ONE?"


def _comparison_subjects(question: str) -> tuple[str | None, str | None]:
    match = re.search(r"(?i)\b([A-Za-zÀ-ÖØ-öø-ÿ]+)\s+(?:or|versus|vs\.?|oder)\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)\b", question)
    return (match.group(1), match.group(2)) if match else (None, None)


def build_thumbnail_brief(state: dict[str, Any]) -> dict[str, Any]:
    intent = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    format_plan = state.get("format_plan") if isinstance(state.get("format_plan"), dict) else {}
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    novelty = state.get("novelty_plan") if isinstance(state.get("novelty_plan"), dict) else {}
    reaction = state.get("reaction_plan") if isinstance(state.get("reaction_plan"), dict) else {}
    selected_format = str(format_plan.get("selected_format") or "explanation")
    raw_question = str(intent.get("question") or state.get("prompt") or "").strip()
    question = raw_question.strip(" ?!.")
    protected = str(payoff.get("hook_must_not_reveal") or "")
    first_subject, second_subject = _comparison_subjects(question)
    scene_hints = [
        str(scene.get("id"))
        for scene in state.get("scenes", [])
        if isinstance(scene, dict) and scene.get("id")
    ][:3]
    if selected_format == "comparison":
        strategy = "SPLIT_COMPARISON"
        cover_text = _comparison_text(question, str(intent.get("language") or "").casefold() or None)
    elif selected_format == "ranking":
        strategy = "TYPOGRAPHY_FOCUS"
        cover_text = f"{_short_line(question, 30)}\nRANKED"
    elif selected_format in {"reveal", "misconception_correction"}:
        strategy = "IMAGE_PLUS_HEADER"
        cover_text = "WHAT'S ACTUALLY\nHAPPENING?"
    else:
        strategy = "IMAGE_PLUS_HEADER"
        subject = str(intent.get("topic") or question or "THIS PHENOMENON")
        cover_text = f"{question}?" if question and raw_question.endswith("?") else question or subject
    visual_subjects = _derive_visual_subjects(state)
    return {
        "format": selected_format,
        "primary_subject": str(intent.get("topic") or question or "ClipForge"),
        "secondary_subject": second_subject,
        "cover_text": cover_text,
        "protected_information": protected,
        "desired_reaction": str(reaction.get("primary_reaction") or payoff.get("desired_viewer_reaction") or "curiosity"),
        "visual_strategy": str(format_plan.get("visual_structure") or "relevant explanatory visual"),
        "recommended_angle": str((novelty.get("recommended_angle") if isinstance(novelty, dict) else "") or ""),
        "composition_strategy": strategy,
        "source_scene_hints": scene_hints,
        "text_emphasis": "question_and_contrast" if selected_format == "comparison" else "short_curiosity",
        "safe_zone": {"x": SAFE_X, "top": SAFE_TOP, "bottom": SAFE_BOTTOM},
        "novelty_fact_ids": list(novelty.get("distinctive_facts") or [])[:3],
        "comparison_subject": first_subject,
        "primary_visual_subjects": visual_subjects["primary"],
        "supporting_visual_context": visual_subjects["supporting"],
    }


def _cover_text_details(state: dict[str, Any], brief: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Choose concise, standalone planning-aware text."""
    brief = brief or build_thumbnail_brief(state)
    payoff = state.get("payoff_plan") if isinstance(state.get("payoff_plan"), dict) else {}
    triple = state.get("script", {}).get("triple_hook") if isinstance(state.get("script", {}).get("triple_hook"), dict) else {}
    topic, question = _question_parts(state)
    candidates = (
        (brief.get("cover_text"), "intent_format"),
        (triple.get("on_screen_text_hook"), "triple_hook"),
        (brief.get("recommended_angle"), "novelty_angle"),
        (question or topic, "topic_fallback"),
    )
    rejected: list[str] = []
    for candidate_index, (candidate, source) in enumerate(candidates):
        text = " ".join(str(candidate or "").split()).strip()
        evaluation_text = text.strip(" ?!.")
        if not evaluation_text:
            continue
        if _reveals_protected(evaluation_text, payoff):
            rejected.append(f"{source}:protected_payoff")
            continue
        words = _semantic_words(evaluation_text)
        lowered = evaluation_text.casefold()
        topic_words = _semantic_words(f"{topic} {question} {' '.join(brief.get('primary_visual_subjects') or [])}")
        meaningful_overlap = words & topic_words
        vague = any(phrase in lowered for phrase in _WEAK_COVER_PHRASES) or bool(re.search(r"\b(?:this|that|it|das|dies|es|okay)\b", lowered))
        quality = 0.9
        reasons: list[str] = []
        if vague:
            quality -= 0.55
            reasons.append("context_dependent_or_vague")
        if len(words) < 2 and "?" not in text:
            quality -= 0.35
            reasons.append("fragment_without_useful_question")
        if not meaningful_overlap and "?" not in text:
            quality -= 0.25
            reasons.append("missing_topic_entity")
        if len(text) > 100:
            quality -= 0.2
            reasons.append("too_long")
        quality = round(max(0.0, min(1.0, quality)), 3)
        if quality >= 0.6:
            normalized = text.upper()
            # Keep diagnostics useful even when a higher-priority candidate
            # wins before a weaker hook is considered for rendering.
            for later_candidate, later_source in candidates[candidate_index + 1:]:
                later_text = " ".join(str(later_candidate or "").split()).strip(" ?!.")
                if later_text and any(phrase in later_text.casefold() for phrase in _WEAK_COVER_PHRASES):
                    rejected.append(f"{later_source}:context_dependent_or_vague")
            return normalized, {"cover_text_source": source, "cover_text_quality": quality, "rejected_text_reasons": rejected}
        rejected.extend(f"{source}:{reason}" for reason in reasons or ["low_standalone_quality"])
    topic, question = _question_parts(state)
    fallback = _short_line(question or topic or "CLIPFORGE")
    if _reveals_protected(fallback, payoff):
        fallback = "KEEP WATCHING"
    return fallback, {"cover_text_source": "safe_fallback", "cover_text_quality": 0.45, "rejected_text_reasons": rejected}


def _cover_text(state: dict[str, Any], brief: dict[str, Any] | None = None) -> str:
    return _cover_text_details(state, brief)[0]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _brief_terms(brief: dict[str, Any]) -> set[str]:
    return _semantic_words(" ".join(str(brief.get(key) or "") for key in ("primary_subject", "secondary_subject", "comparison_subject", "format", "visual_strategy", "recommended_angle")))


def _thumbnail_visual_prompt_groups(brief: dict[str, Any]) -> dict[str, list[str]]:
    """Build a small, positive/negative concept set for local pixel verification."""
    primary = [str(value) for value in brief.get("primary_visual_subjects") or [] if str(value)]
    supporting = [str(value) for value in brief.get("supporting_visual_context") or [] if str(value)]
    comparison_a = _VISUAL_ALIASES.get(str(brief.get("comparison_subject") or "").strip().casefold(), str(brief.get("comparison_subject") or "").strip().casefold())
    comparison_b = _VISUAL_ALIASES.get(str(brief.get("secondary_subject") or "").strip().casefold(), str(brief.get("secondary_subject") or "").strip().casefold())
    core = [value for value in primary if value not in {comparison_a, comparison_b}]
    def concepts(entity: str) -> list[str]:
        return [f"a clear photograph of {entity}", f"{entity} {('islands or archipelago' if 'island' in core or 'archipelago' in core else 'landmark or landscape')}" , *[f"a photo showing {value}" for value in core]]
    if comparison_a or comparison_b:
        groups = {
            "primary": concepts(comparison_a) if comparison_a else [f"a photo showing {value}" for value in core],
            "secondary": concepts(comparison_b) if comparison_b else [],
        }
    else:
        groups = {"primary": [f"a photo showing {value}" for value in primary], "secondary": []}
    groups["context"] = [f"a generic {value} scene" for value in supporting] + [
        "a generic tropical road", "a generic city scene", "a generic landscape", "a generic sky"
    ]
    return {key: list(dict.fromkeys(value for value in values if value)) for key, values in groups.items()}


def _scene_context(scene: dict[str, Any]) -> str:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    visual = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    values: list[object] = [
        scene.get("narration"),
        scene.get("text"),
        scene.get("visual_goal"),
        scene.get("search_queries"),
        scene.get("query"),
        scene.get("fact_ids"),
        visual.get("visual_goal"),
        visual.get("objects"),
        visual.get("subjects_to_show"),
        visual.get("media_queries"),
        media.get("query"),
        media.get("title"),
        media.get("tags"),
        media.get("source_url"),
        media.get("provider"),
        media.get("provider_id"),
        media.get("identity"),
        media.get("description"),
    ]
    return " ".join(str(value or "") for value in values)


def _scene_provenance(scene: dict[str, Any]) -> dict[str, str]:
    media = scene.get("media") if isinstance(scene.get("media"), dict) else {}
    visual = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    high = " ".join(str(media.get(key) or "") for key in ("query", "title", "description", "tags", "source_url", "provider"))
    medium = " ".join(str(scene.get(key) or "") for key in ("narration", "text", "visual_goal", "search_queries", "query", "fact_ids"))
    medium += " " + " ".join(str(visual.get(key) or "") for key in ("visual_goal", "objects", "subjects_to_show", "media_queries"))
    low = " ".join(str(media.get(key) or "") for key in ("provider_id", "identity", "cache_path"))
    return {"high": high, "medium": medium, "low": low}


def _scene_payoff_safe(scene: dict[str, Any], brief: dict[str, Any]) -> bool:
    protected = str(brief.get("protected_information") or "")
    if not protected:
        return True
    context = _scene_context(scene)
    if not _reveals_protected(context, {"hook_must_not_reveal": protected}):
        return True
    return not bool(re.search(r"(?i)\b(?:winner|answer|result|has more|has fewer|actually|reveal|is the most|is number one)\b", context))


def _verify_thumbnail_shortlist(
    sources: list[dict[str, Any]],
    brief: dict[str, Any],
    *,
    verifier: Any | None = None,
) -> list[dict[str, Any]]:
    """Optionally verify only the metadata-ranked thumbnail shortlist."""
    visual = verifier or get_visual_verifier()
    if getattr(visual, "status", "") != "available" or not hasattr(visual, "verify_thumbnail_image"):
        for source in sources[:THUMBNAIL_VISUAL_SHORTLIST]:
            source["verifier_used"] = False
            source["verifier_model"] = getattr(visual, "model_identity", None)
            source["visual_verification"] = {"status": getattr(visual, "status", "unavailable_dependency")}
        return sources
    prompts = _thumbnail_visual_prompt_groups(brief)
    for source in sources[:THUMBNAIL_VISUAL_SHORTLIST]:
        try:
            result = visual.verify_thumbnail_image(
                source["path"],
                primary_texts=prompts["primary"],
                secondary_texts=prompts["secondary"],
                context_texts=prompts["context"],
                asset_identity=str(source.get("path") or source.get("scene_id") or "thumbnail"),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            source["verifier_used"] = False
            source["verifier_model"] = getattr(visual, "model_identity", None)
            source["visual_verification"] = {"status": "verification_error"}
            continue
        primary = result.primary_visual_score
        secondary = result.secondary_visual_score
        subject = result.subject_score
        if subject is None:
            subject = max((score for score in (primary, secondary) if score is not None), default=None)
        context = result.context_visual_score
        margin = round(subject - context, 6) if subject is not None and context is not None else result.visual_margin
        context_only = subject is not None and context is not None and (context >= subject - 0.01 or (subject - context) < 0.05)
        disagreement = bool(source.get("relevance", 0.0) >= 0.45 and (context_only or (subject is not None and subject < 0.18)))
        adjustment = 1.0
        if context_only:
            adjustment = 0.45
        elif subject is not None and subject < 0.18:
            adjustment = 0.7
        elif subject is not None and subject >= 0.32:
            adjustment = 1.1
        source["metadata_relevance"] = source.get("relevance", 0.0)
        source["visual_relevance"] = round(float(subject or 0.0), 3)
        source["relevance"] = round(min(1.0, float(source.get("relevance", 0.0)) * adjustment), 3)
        source["visual_subject_match"] = round(float(subject or 0.0), 3)
        source["visual_primary_match"] = round(float(primary or 0.0), 3) if primary is not None else None
        source["visual_secondary_match"] = round(float(secondary), 3) if secondary is not None else None
        source["visual_context_score"] = round(float(context), 3) if context is not None else None
        source["visual_confidence"] = result.confidence
        source["visual_margin"] = round(float(margin), 3) if margin is not None else None
        source["visual_context_only"] = context_only
        source["metadata_visual_disagreement"] = disagreement
        source["verifier_used"] = result.status == "verified"
        source["verifier_model"] = getattr(visual, "model_identity", None)
        source["visual_verification"] = {
            "status": result.status,
            "provenance": result.provenance,
            "primary_score": source["visual_primary_match"],
            "secondary_score": source["visual_secondary_match"],
            "context_score": source["visual_context_score"],
            "confidence": result.confidence,
            "margin": source["visual_margin"],
            "context_only": context_only,
            "metadata_disagreement": disagreement,
        }
    return sorted(sources, key=lambda item: (item["payoff_safe"], item["relevance"], item["quality_score"], item["pixels"]), reverse=True)


def _asset_quality(width: int, height: int, image: Image.Image | None = None) -> tuple[float, list[str]]:
    pixels = width * height
    score = 0.4 if pixels >= WIDTH * HEIGHT else 0.25 if pixels >= 800 * 600 else 0.0
    reasons = ["high_resolution" if score == 0.4 else "usable_resolution" if score else "low_resolution"]
    if image is not None:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()
        total = max(1, sum(histogram))
        mean = sum(index * count for index, count in enumerate(histogram)) / total
        variance = sum(((index - mean) ** 2) * count for index, count in enumerate(histogram)) / total
        contrast = variance**0.5
        if mean < 12:
            score -= 0.25
            reasons.append("nearly_black")
        elif contrast < 8:
            score -= 0.12
            reasons.append("low_contrast")
        else:
            score += 0.1
            reasons.append("usable_contrast")
    return round(max(0.0, min(1.0, score)), 3), reasons


def _source_record(
    *,
    scene_id: str,
    path: Path,
    context: str,
    brief: dict[str, Any],
    width: int,
    height: int,
    source_type: str = "scene_asset",
    timestamp: float | None = None,
    frame_quality: float | None = None,
    payoff_safe: bool = True,
    provenance: dict[str, str] | None = None,
    caption_risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provenance = provenance or {"high": "", "medium": context, "low": ""}
    brief_terms = _brief_terms(brief)
    subject_terms = _semantic_words(" ".join(str(brief.get(key) or "") for key in ("primary_subject", "secondary_subject", "comparison_subject")))
    primary_visual_terms = set(brief.get("primary_visual_subjects") or []) or _visual_words(" ".join(str(brief.get(key) or "") for key in ("primary_subject", "secondary_subject", "comparison_subject")))
    supporting_visual_terms = set(brief.get("supporting_visual_context") or [])
    generic_context_terms = _words(context) & _GENERIC_TERMS
    high_terms = _semantic_words(provenance.get("high"))
    medium_terms = _semantic_words(provenance.get("medium"))
    low_terms = _semantic_words(provenance.get("low"))
    high_visual_terms = _visual_words(provenance.get("high"))
    medium_visual_terms = _visual_words(provenance.get("medium"))
    matched_high = sorted(brief_terms & high_terms)
    matched_medium = sorted(brief_terms & medium_terms)
    matched_low = sorted(brief_terms & low_terms)
    matched = sorted(set(matched_high) | set(matched_medium) | set(matched_low))
    subject_high = sorted(subject_terms & high_terms)
    subject_medium = sorted(subject_terms & medium_terms)
    subject_matches = sorted(set(subject_high) | set(subject_medium))
    primary_high = sorted(primary_visual_terms & high_visual_terms)
    primary_medium = sorted(primary_visual_terms & medium_visual_terms)
    matched_supporting = sorted(supporting_visual_terms & (high_visual_terms | medium_visual_terms))
    if primary_high:
        subject_coverage = "FULL"
    elif primary_medium:
        subject_coverage = "PARTIAL"
    elif matched_supporting:
        subject_coverage = "WEAK"
    else:
        subject_coverage = "NONE"
    quality, quality_reasons = _asset_quality(width, height)
    if frame_quality is not None:
        quality = round((quality + frame_quality) / 2, 3)
    relevance = len(matched_high) * 0.2 + len(subject_high) * 0.18 + len(matched_medium) * 0.06 + len(subject_medium) * 0.05 + len(matched_low) * 0.01
    if not matched_high and not matched_medium:
        relevance = 0.0
    relevance = round(min(1.0, relevance), 3)
    meaningful_high = {
        term for term in matched_high
        if term not in _GENERIC_TERMS and not (_visual_words(term) & supporting_visual_terms)
    }
    trust = "high" if meaningful_high or primary_high else "medium" if matched_medium or subject_medium or primary_medium else "low"
    if trust == "low":
        relevance = 0.0
    return {
        "scene_id": scene_id,
        "path": path,
        "width": width,
        "height": height,
        "pixels": width * height,
        "source_type": source_type,
        "timestamp": timestamp,
        "context": context,
        "matched_terms": matched,
        "matched_high_trust": matched_high,
        "matched_medium_trust": matched_medium,
        "matched_low_trust": matched_low,
        "generic_matched_terms": sorted(generic_context_terms),
        "subject_matches": subject_matches,
        "matched_primary_subjects": sorted(set(primary_high) | set(primary_medium)),
        "matched_primary_high_trust": primary_high,
        "matched_primary_medium_trust": primary_medium,
        "matched_supporting_terms": matched_supporting,
        "subject_coverage": subject_coverage,
        "provenance_trust": trust,
        "provenance": {key: value for key, value in provenance.items() if value},
        "relevance": relevance,
        "quality_score": quality,
        "quality_reasons": quality_reasons,
        "payoff_safe": payoff_safe,
        "caption_risk": caption_risk or {"risk": 0.0, "active": False, "position": None, "active_count": 0},
    }


def _source_images(state: dict[str, Any], project_dir: Path, brief: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for index, scene in enumerate(state.get("scenes", [])):
        media = scene.get("media") if isinstance(scene.get("media"), dict) else None
        if not media or media.get("kind") != "photo":
            continue
        cache_path = str(media.get("cache_path") or "")
        if not cache_path:
            continue
        candidate = (project_dir.parent / cache_path).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(project_dir) or candidate in seen:
            continue
        seen.add(candidate)
        try:
            with Image.open(candidate) as image:
                width, height = image.size
        except (OSError, ValueError):
            continue
        sources.append(_source_record(
            scene_id=str(scene.get("id") or f"scene-{index + 1}"),
            path=candidate,
            context=_scene_context(scene),
            brief=brief,
            width=width,
            height=height,
            payoff_safe=_scene_payoff_safe(scene, brief),
            provenance=_scene_provenance(scene),
        ))
    asset_root = project_dir / "assets"
    if asset_root.is_dir():
        for candidate in sorted(asset_root.rglob("*")):
            if candidate in seen or candidate.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or not candidate.is_file():
                continue
            try:
                with Image.open(candidate) as image:
                    width, height = image.size
            except (OSError, ValueError):
                continue
            seen.add(candidate)
            relative_name = candidate.relative_to(asset_root).with_suffix("")
            sources.append(_source_record(
                scene_id=f"asset:{relative_name.as_posix()}",
                path=candidate,
                context=relative_name.as_posix().replace("/", " "),
                brief=brief,
                width=width,
                height=height,
                source_type="project_asset",
                provenance={"high": "", "medium": "", "low": relative_name.as_posix()},
            ))
    return sorted(sources, key=lambda item: (item["payoff_safe"], item["relevance"], item["quality_score"], item["pixels"]), reverse=True)


def _render_source(state: dict[str, Any], project_dir: Path) -> Path | None:
    root = project_dir.parent
    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    voice = state.get("voice") if isinstance(state.get("voice"), dict) else {}
    for value in (render.get("url"), render.get("path"), voice.get("picture_source")):
        raw = str(value or "")
        if raw.startswith("/media/"):
            raw = raw.removeprefix("/media/")
        if not raw:
            continue
        candidate = (root / raw).resolve()
        if candidate.is_file() and candidate.is_relative_to(project_dir):
            return candidate
    return None


def _caption_risk(state: dict[str, Any], timestamp: float) -> dict[str, Any]:
    captions = state.get("captions") if isinstance(state.get("captions"), dict) else {}
    if not captions.get("enabled"):
        return {"risk": 0.0, "active": False, "position": None, "active_count": 0}
    items = captions.get("items") if isinstance(captions.get("items"), list) else []
    active = [
        item for item in items
        if isinstance(item, dict) and float(item.get("start") or 0) <= timestamp <= float(item.get("end") or 0)
    ]
    if not active:
        return {"risk": 0.0, "active": False, "position": captions.get("position"), "active_count": 0}
    position = str(captions.get("position") or "center")
    base = 0.22 if position == "center" else 0.16
    return {"risk": round(min(1.0, base + len(active) * 0.06), 3), "active": True, "position": position, "active_count": len(active)}


def _extract_frame(ffmpeg: str, video: Path, timestamp: float, destination: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{max(0.0, timestamp):.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=1080:-2",
                "-q:v",
                "3",
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and destination.is_file() and destination.stat().st_size > 512


def _frame_metrics(path: Path) -> tuple[float, list[str], tuple[int, int]] | None:
    try:
        with Image.open(path) as image:
            width, height = image.size
            score, reasons = _asset_quality(width, height, image)
    except (OSError, ValueError):
        return None
    if "nearly_black" in reasons:
        return None
    return score, reasons, (width, height)


def _frame_candidates(state: dict[str, Any], project_dir: Path, brief: dict[str, Any], frame_dir: Path) -> list[dict[str, Any]]:
    video = _render_source(state, project_dir)
    ffmpeg = ffmpeg_path()
    if not video or not ffmpeg:
        return []
    scenes = [scene for scene in state.get("scenes", []) if isinstance(scene, dict)]
    timed = [scene for scene in scenes if float(scene.get("end") or 0) > float(scene.get("start") or 0)]
    if not timed:
        timed = scenes[:3]
    selected_scenes = timed[:3]
    candidates: list[dict[str, Any]] = []
    for index, scene in enumerate(selected_scenes):
        start = float(scene.get("start") or 0)
        end = float(scene.get("end") or (start + 1.0))
        frame_dir.mkdir(parents=True, exist_ok=True)
        duration = max(0.1, end - start)
        offsets = (min(max(0.35, duration * 0.35), max(0.35, duration - 0.1)), duration * 0.5, max(0.1, duration - 0.15))
        scene_candidates: list[dict[str, Any]] = []
        for sample_index, offset in enumerate(dict.fromkeys(round(min(duration - 0.05, max(0.05, offset)), 3) for offset in offsets)):
            timestamp = start + offset
            suffix = "" if sample_index == 0 else f"-near{sample_index}"
            destination = frame_dir / f"frame-{index + 1}{suffix}.jpg"
            if not _extract_frame(ffmpeg, video, timestamp, destination):
                continue
            metrics = _frame_metrics(destination)
            if metrics is None:
                continue
            quality, reasons, (width, height) = metrics
            risk = _caption_risk(state, timestamp)
            scene_candidates.append(_source_record(
                scene_id=str(scene.get("id") or f"scene-{index + 1}"),
                path=destination,
                context=_scene_context(scene),
                brief=brief,
                width=width,
                height=height,
                source_type="video_frame",
                timestamp=round(timestamp, 3),
                frame_quality=quality,
                payoff_safe=_scene_payoff_safe(scene, brief),
                provenance=_scene_provenance(scene),
                caption_risk=risk,
            ) | {"quality_reasons": reasons})
        if scene_candidates:
            candidates.append(max(scene_candidates, key=lambda item: (
                {"FULL": 3, "PARTIAL": 2, "WEAK": 1, "NONE": 0}.get(item.get("subject_coverage"), 0),
                float(item["relevance"]) - float(item["caption_risk"].get("risk") or 0.0) * 0.25,
                float(item["quality_score"]) - float(item["caption_risk"].get("risk") or 0.0) * 0.1,
            )))
    return sorted(candidates, key=lambda item: ({"FULL": 3, "PARTIAL": 2, "WEAK": 1, "NONE": 0}.get(item.get("subject_coverage"), 0), item["relevance"], item["payoff_safe"], item["quality_score"]), reverse=True)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(current)
                current = word
                if draw.textbbox((0, 0), current, font=font)[2] > max_width:
                    while current:
                        chunk = ""
                        for character in current:
                            proposed = chunk + character
                            if chunk and draw.textbbox((0, 0), proposed, font=font)[2] > max_width:
                                break
                            chunk = proposed
                        lines.append(chunk or current[0])
                        current = current[len(chunk or current[0]):]
            elif draw.textbbox((0, 0), word, font=font)[2] > max_width:
                if current:
                    lines.append(current)
                    current = ""
                remaining = word
                while remaining:
                    chunk = ""
                    for character in remaining:
                        proposed = chunk + character
                        if chunk and draw.textbbox((0, 0), proposed, font=font)[2] > max_width:
                            break
                        chunk = proposed
                    chunk = chunk or remaining[0]
                    lines.append(chunk)
                    remaining = remaining[len(chunk):]
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    *,
    max_size: int = 180,
    min_size: int = 64,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    for size in range(max_size, min_size - 1, -4):
        font = _font(size)
        lines = _wrap_text(draw, text, font, max_width)
        line_height = max(1, draw.textbbox((0, 0), "Ag", font=font)[3])
        if len(lines) <= 3 and line_height * len(lines) + 24 * (len(lines) - 1) <= max_height:
            return font, lines, line_height
    font = _font(min_size)
    lines = _wrap_text(draw, text, font, max_width)[:3]
    return font, lines, max(1, draw.textbbox((0, 0), "Ag", font=font)[3])


def _region_visual_density(image: Image.Image, region: tuple[float, float, float, float] | None = None) -> float:
    """Estimate useful visual detail without attempting object recognition."""
    small = image.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
    if region:
        left, top, right, bottom = region
        small = small.crop((round(left * 64), round(top * 64), round(right * 64), round(bottom * 64)))
    pixels = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    if not pixels:
        return 0.0
    mean = sum(pixels) / len(pixels)
    variance = sum((value - mean) ** 2 for value in pixels) / len(pixels)
    edges = [abs(a - b) for a, b in pairwise(pixels)]
    edge_density = min(1.0, (sum(edges) / max(1, len(edges))) / 42.0)
    contrast = min(1.0, (variance**0.5) / 64.0)
    non_blank = sum(value > 30 for value in pixels) / len(pixels)
    return round(max(0.0, min(1.0, edge_density * 0.45 + contrast * 0.35 + non_blank * 0.2)), 3)


def _crop_metrics(image: Image.Image) -> dict[str, float]:
    density = _region_visual_density(image)
    gray = image.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
    pixels = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    blank = sum(value <= 30 for value in pixels) / max(1, len(pixels))
    return {
        "focal_score": density,
        "visual_density": density,
        "blank_space_ratio": round(blank, 3),
    }


def _fit_source_with_metadata(
    source: Path,
    size: tuple[int, int],
    *,
    centering: tuple[float, float],
    composition: str = "SUBJECT_FOCUS",
    text_placement: str = "bottom",
) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(source) as original:
        image = original.convert("RGB")
    width, height = image.size
    target_width, target_height = size
    scale = max(target_width / width, target_height / height)
    resized = image.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)
    max_left = max(0, resized.width - target_width)
    max_top = max(0, resized.height - target_height)
    options = [
        centering,
        (0.5, 0.5),
        (0.35, 0.35),
        (0.65, 0.35),
        (0.35, 0.65),
        (0.65, 0.65),
    ]
    best: tuple[float, Image.Image, dict[str, Any]] | None = None
    for x_bias, y_bias in options:
        left = max(0, min(max_left, round(max_left * x_bias)))
        top = max(0, min(max_top, round(max_top * y_bias)))
        crop = resized.crop((left, top, left + target_width, top + target_height))
        metrics = _crop_metrics(crop)
        text_region = {
            "top": (0.0, 0.0, 1.0, 0.34),
            "bottom": (0.0, 0.66, 1.0, 1.0),
            "center": (0.0, 0.33, 1.0, 0.67),
        }.get(text_placement, (0.0, 0.0, 1.0, 0.34))
        collision = _region_visual_density(crop, text_region)
        crop_score = metrics["focal_score"] - collision * 0.12
        if composition == "TYPOGRAPHY_FOCUS":
            crop_score -= collision * 0.2
        elif composition == "SPLIT_COMPARISON":
            crop_score += metrics["visual_density"] * 0.05
        candidate = (crop_score, crop, {
            **metrics,
            "crop_box": [left, top, left + target_width, top + target_height],
            "crop_strategy": f"{composition.lower()}:{x_bias:.2f},{y_bias:.2f}",
            "text_collision": round(collision, 3),
        })
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise ThumbnailGenerationError("A project image could not be cropped.")
    return best[1], best[2]


def _fit_source(source: Path, size: tuple[int, int], *, centering: tuple[float, float]) -> Image.Image:
    return _fit_source_with_metadata(source, size, centering=centering)[0]


def _text_block(
    image: Image.Image,
    text: str,
    *,
    placement: str,
    max_height: int,
    max_size: int,
    backing_opacity: int = 178,
    emphasis: str = "first",
) -> tuple[Image.Image, dict[str, Any]]:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text_x = SAFE_X + 3
    font, lines, line_height = _fit_text(
        draw,
        text,
        WIDTH - SAFE_X * 2 - 6,
        max_height,
        max_size=max_size,
    )
    line_spacing = max(20, round(line_height * 0.14))
    line_fonts: list[ImageFont.ImageFont] = []
    for index, line in enumerate(lines):
        multiplier = 1.2 if (emphasis == "last" and index == len(lines) - 1) or (emphasis != "last" and index == 0) else 1.0
        size = min(max_size + 20, round(getattr(font, "size", max_size) * multiplier))
        candidate_font = _font(size)
        while size > 64 and draw.textbbox((0, 0), line, font=candidate_font, stroke_width=3)[2] > WIDTH - SAFE_X * 2 - 6:
            size -= 2
            candidate_font = _font(size)
        line_fonts.append(candidate_font)
    total_height = sum(max(1, draw.textbbox((0, 0), "Ag", font=item)[3]) for item in line_fonts) + line_spacing * max(0, len(lines) - 1)
    if total_height > max_height:
        scale = max_height / total_height
        line_fonts = [_font(max(64, round(getattr(item, "size", max_size) * scale))) for item in line_fonts]
        total_height = sum(max(1, draw.textbbox((0, 0), "Ag", font=item)[3]) for item in line_fonts) + line_spacing * max(0, len(lines) - 1)
    if placement == "top":
        y = SAFE_TOP
    elif placement == "center":
        y = (HEIGHT - total_height) // 2
    else:
        y = HEIGHT - SAFE_BOTTOM - total_height
    box = (SAFE_X - 30, y - 30, WIDTH - SAFE_X + 30, y + total_height + 34)
    draw.rounded_rectangle(box, radius=34, fill=(0, 0, 0, backing_opacity))
    text_boxes: list[tuple[int, int, int, int]] = []
    for line, line_font in zip(lines, line_fonts):
        line_height = max(1, draw.textbbox((0, 0), "Ag", font=line_font)[3])
        bbox = draw.textbbox((text_x, y), line, font=line_font, stroke_width=3)
        text_boxes.append(bbox)
        draw.text((text_x, y), line, fill=(255, 255, 255, 255), font=line_font, stroke_width=3, stroke_fill=(0, 0, 0, 230))
        y += line_height + line_spacing
    text_bbox = (
        min(box[0] for box in text_boxes),
        min(box[1] for box in text_boxes),
        max(box[2] for box in text_boxes),
        max(box[3] for box in text_boxes),
    )
    text_area = max(0, text_bbox[2] - text_bbox[0]) * max(0, text_bbox[3] - text_bbox[1])
    backing_area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"), {
        "text_fits": text_bbox[0] >= SAFE_X and text_bbox[2] <= WIDTH - SAFE_X and text_bbox[1] >= SAFE_TOP and text_bbox[3] <= HEIGHT - SAFE_BOTTOM,
        "clipping_safe": text_bbox[0] >= SAFE_X and text_bbox[2] <= WIDTH - SAFE_X and text_bbox[1] >= SAFE_TOP and text_bbox[3] <= HEIGHT - SAFE_BOTTOM,
        "line_count": len(lines),
        "font_size": max(getattr(item, "size", max_size) for item in line_fonts),
        "text_bbox": list(text_bbox),
        "text_area_ratio": round(text_area / (WIDTH * HEIGHT), 4),
        "backing_area_ratio": round(backing_area / (WIDTH * HEIGHT), 4),
        "dead_space_ratio": round(max(0.0, 1 - backing_area / (WIDTH * HEIGHT)), 4),
        "safe_zone": {"x": SAFE_X, "top": SAFE_TOP, "bottom": SAFE_BOTTOM},
    }


def _preferred_text_placement(image: Image.Image, strategy: str) -> str:
    if strategy == "TYPOGRAPHY_FOCUS":
        return "center"
    top = _region_visual_density(image, (0.0, 0.0, 1.0, 0.34))
    bottom = _region_visual_density(image, (0.0, 0.66, 1.0, 1.0))
    if strategy == "IMAGE_PLUS_HEADER":
        return "bottom" if top > bottom + 0.12 else "top"
    return "top" if top <= bottom else "bottom"


def _compose(sources: list[dict[str, Any]], destination: Path, brief: dict[str, Any], strategy: str, index: int) -> dict[str, Any]:
    primary = sources[index % len(sources)]["path"]
    secondary = sources[(index + 1) % len(sources)]["path"]
    try:
        crop_metadata: dict[str, Any]
        if strategy == "SPLIT_COMPARISON" and len(sources) >= 2:
            left, left_crop = _fit_source_with_metadata(
                primary,
                (WIDTH // 2, HEIGHT),
                centering=(0.5, 0.42),
                composition=strategy,
                text_placement="top",
            )
            right, right_crop = _fit_source_with_metadata(
                secondary,
                (WIDTH - WIDTH // 2, HEIGHT),
                centering=(0.5, 0.42),
                composition=strategy,
                text_placement="top",
            )
            image = Image.new("RGB", (WIDTH, HEIGHT))
            image.paste(left, (0, 0))
            image.paste(right, (WIDTH // 2, 0))
            divider = Image.new("RGBA", image.size, (0, 0, 0, 0))
            divider_draw = ImageDraw.Draw(divider)
            divider_draw.rectangle((WIDTH // 2 - 6, 0, WIDTH // 2 + 6, HEIGHT), fill=(255, 255, 255, 235))
            divider_draw.rectangle((WIDTH // 2 - 2, 0, WIDTH // 2 + 2, HEIGHT), fill=(20, 25, 35, 255))
            image = Image.alpha_composite(image.convert("RGBA"), divider).convert("RGB")
            placement = "top"
            max_height, max_size, backing_opacity = 620, 174, 154
            emphasis = "last"
            crop_metadata = {
                "crop_strategy": "split_independent",
                "crop_box": {"left": left_crop["crop_box"], "right": right_crop["crop_box"]},
                "focal_score": round((left_crop["focal_score"] + right_crop["focal_score"]) / 2, 3),
                "visual_density": round((left_crop["visual_density"] + right_crop["visual_density"]) / 2, 3),
                "blank_space_ratio": round((left_crop["blank_space_ratio"] + right_crop["blank_space_ratio"]) / 2, 3),
                "text_collision": round((left_crop["text_collision"] + right_crop["text_collision"]) / 2, 3),
                "balance_score": round(1.0 - abs(left_crop["visual_density"] - right_crop["visual_density"]), 3),
            }
        elif strategy == "TYPOGRAPHY_FOCUS":
            image, crop_metadata = _fit_source_with_metadata(
                primary,
                (WIDTH, HEIGHT),
                centering=(0.35 if index % 2 == 0 else 0.65, 0.5),
                composition=strategy,
                text_placement="center",
            )
            image = Image.eval(image, lambda pixel: round(pixel * 0.38))
            placement = "center"
            max_height, max_size, backing_opacity = 980, 214, 182
            emphasis = "first"
        else:
            image, crop_metadata = _fit_source_with_metadata(
                primary,
                (WIDTH, HEIGHT),
                centering=(0.5, 0.28 if index % 2 == 0 else 0.58),
                composition=strategy,
                text_placement="top" if strategy == "IMAGE_PLUS_HEADER" else "bottom",
            )
            placement = _preferred_text_placement(image, strategy)
            max_height, max_size, backing_opacity = (600, 174, 148) if strategy == "IMAGE_PLUS_HEADER" else (650, 184, 160)
            emphasis = "first"
        image, metadata = _text_block(
            image,
            str(brief["cover_text"]),
            placement=placement,
            max_height=max_height,
            max_size=max_size,
            backing_opacity=backing_opacity,
            emphasis=emphasis,
        )
        metadata.update(crop_metadata)
        metadata["text_placement"] = placement
        metadata["balance_score"] = metadata.get("balance_score", round(1.0 - metadata.get("text_collision", 0.0), 3))
    except (OSError, ValueError, TypeError) as exc:
        raise ThumbnailGenerationError("A project image could not be composed.") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.jpg")
    image.save(temporary, format="JPEG", quality=90, optimize=True)
    temporary.replace(destination)
    return metadata


def _candidate_score_breakdown(
    source: dict[str, Any],
    brief: dict[str, Any],
    strategy: str,
    metadata: dict[str, Any],
    source_count: int,
    *,
    diverse_source: bool = False,
) -> tuple[float, list[str], dict[str, float]]:
    components: dict[str, float] = {}
    reasons: list[str] = []
    quality = float(source.get("quality_score") or 0.0)
    if source["pixels"] >= WIDTH * HEIGHT:
        reasons.append("high_resolution")
    elif source["pixels"] >= 800 * 600:
        reasons.append("usable_resolution")
    relevance = float(source.get("relevance") or 0.0)
    if relevance >= 0.45:
        reasons.append("strong_semantic_relevance")
    elif relevance >= 0.2:
        reasons.append("semantic_relevance")
    elif relevance > 0:
        reasons.append("weak_semantic_relevance")
    else:
        reasons.append("no_semantic_match")
    components["semantic"] = min(1.0, relevance * 0.8 + min(0.2, len(source.get("subject_matches") or []) * 0.05))
    if source.get("verifier_used"):
        visual_subject = float(source.get("visual_subject_match") or 0.0)
        components["visual_subject"] = round(max(0.0, min(1.0, (visual_subject - 0.15) / 0.2)), 3)
        if source.get("visual_context_only"):
            reasons.append("visual_context_only")
        elif components["visual_subject"] >= 0.5:
            reasons.append("visual_primary_subject_match")
        else:
            reasons.append("weak_visual_subject_match")
        if source.get("metadata_visual_disagreement"):
            reasons.append("metadata_visual_disagreement")
    else:
        components["visual_subject"] = 0.5
        reasons.append("visual_verifier_fallback")
    coverage = str(source.get("subject_coverage") or "NONE")
    components["subject_coverage"] = {"FULL": 1.0, "PARTIAL": 0.55, "WEAK": 0.25, "NONE": 0.0}.get(coverage, 0.0)
    if coverage == "FULL":
        reasons.append("full_primary_subject_coverage")
    elif coverage == "PARTIAL":
        reasons.append("scene_context_primary_coverage")
    elif coverage == "WEAK":
        reasons.append("supporting_context_only")
    else:
        reasons.append("missing_primary_subject_coverage")
    components["provenance"] = {"high": 1.0, "medium": 0.55, "low": 0.15}.get(source.get("provenance_trust"), 0.15)
    components["visual_quality"] = min(1.0, quality + (0.08 if source["pixels"] >= WIDTH * HEIGHT else 0.04 if source["pixels"] >= 800 * 600 else 0.0))
    if source.get("subject_matches"):
        reasons.append("subject_coverage")
    if source.get("provenance_trust") == "high":
        reasons.append("trusted_asset_provenance")
    elif source.get("provenance_trust") == "medium":
        reasons.append("scene_context_only")
    else:
        reasons.append("sparse_asset_provenance")
    if source.get("generic_matched_terms"):
        reasons.append("generic_terms_ignored")
    if source.get("quality_reasons"):
        reasons.append("source_quality")
    components["safety"] = 1.0 if source.get("payoff_safe", True) else 0.0
    reasons.append("payoff_safe_asset" if components["safety"] else "payoff_reveal_penalty")
    if source.get("source_type") == "video_frame":
        reasons.append("representative_frame")
    else:
        reasons.append("clean_source_asset")
    if diverse_source:
        reasons.append("asset_diversity")
    caption = source.get("caption_risk") if isinstance(source.get("caption_risk"), dict) else {}
    caption_penalty = float(caption.get("risk") or 0.0)
    if caption_penalty:
        reasons.append("caption_overlay_risk")
    if brief["format"] == "comparison" and strategy == "SPLIT_COMPARISON" and source_count >= 2:
        reasons.append("format_fit")
    else:
        reasons.append("valid_composition")
    components["crop"] = min(1.0, max(0.0, float(metadata.get("focal_score") or 0.0)))
    if metadata.get("text_fits"):
        reasons.append("text_fits_safe_area")
    else:
        reasons.append("text_clipping_risk")
    if components["crop"] >= 0.35:
        reasons.append("strong_focal_crop")
    elif components["crop"] >= 0.18:
        reasons.append("usable_focal_crop")
    else:
        reasons.append("weak_focal_crop")
    blank_space = float(metadata["blank_space_ratio"]) if metadata.get("blank_space_ratio") is not None else 1.0
    if blank_space > 0.82:
        reasons.append("blank_crop_penalty")
    elif blank_space <= 0.62:
        reasons.append("visual_information_retained")
    balance = float(metadata.get("balance_score") or 0.0)
    if strategy == "SPLIT_COMPARISON":
        if balance >= 0.72:
            reasons.append("split_visual_balance")
        else:
            reasons.append("split_imbalance")
    if float(metadata.get("text_collision") or 0.0) > 0.75:
        reasons.append("text_image_collision")
    if metadata.get("line_count", 0) <= 3:
        reasons.append("mobile_readability")
    text_area_ratio = float(metadata.get("text_area_ratio") or 0.0)
    if text_area_ratio >= 0.1:
        reasons.append("text_prominence")
    elif text_area_ratio < 0.045:
        reasons.append("text_too_small")
    dead_space = float(metadata["dead_space_ratio"]) if metadata.get("dead_space_ratio") is not None else 1.0
    if dead_space <= 0.84:
        reasons.append("canvas_utilization")
    elif dead_space > 0.94:
        reasons.append("excessive_dead_space")
    expected_strategy = {
        "comparison": "SPLIT_COMPARISON",
        "ranking": "TYPOGRAPHY_FOCUS",
    }.get(brief["format"])
    if expected_strategy == strategy:
        reasons.append("strategy_fit")
    if strategy == "TYPOGRAPHY_FOCUS" and text_area_ratio >= 0.24:
        reasons.append("typography_hierarchy")
    elif strategy == "SUBJECT_FOCUS" and float(metadata.get("backing_area_ratio") or 1.0) <= 0.38:
        reasons.append("image_hero_balance")
    elif strategy == "IMAGE_PLUS_HEADER" and metadata.get("text_bbox", [0, 0, 0, 0])[1] <= SAFE_TOP + 40:
        reasons.append("header_placement")
    components["typography"] = min(1.0, (0.45 if metadata.get("text_fits") else 0.0) + min(0.35, text_area_ratio) + (0.2 if metadata.get("line_count", 0) <= 3 else 0.0))
    components["composition"] = min(1.0, (0.55 if balance >= 0.65 else balance) + (0.25 if expected_strategy == strategy else 0.12) + (0.1 if diverse_source else 0.0))
    components["source_cleanliness"] = max(0.0, 1.0 - caption_penalty) if source.get("source_type") == "video_frame" else 1.0
    components["penalties"] = min(1.0, caption_penalty * 0.65 + (0.25 if blank_space > 0.82 else 0.0) + (0.25 if float(metadata.get("text_collision") or 0.0) > 0.75 else 0.0) + (0.3 if not metadata.get("text_fits") else 0.0) + (0.15 if source.get("source_type") == "video_frame" and caption_penalty else 0.0) + ({"NONE": 0.18, "WEAK": 0.06}.get(coverage, 0.0)) + (0.18 if source.get("visual_context_only") else 0.0) + (0.12 if source.get("metadata_visual_disagreement") else 0.0))
    weights = {"semantic": 0.20, "subject_coverage": 0.14, "provenance": 0.07, "visual_quality": 0.10, "visual_subject": 0.09, "crop": 0.13, "typography": 0.11, "composition": 0.08, "safety": 0.06, "source_cleanliness": 0.02}
    score = sum(components[key] * weight for key, weight in weights.items()) - components["penalties"] * 0.16
    score = round(max(0.0, min(1.0, score)), 4)
    return score, reasons + [f"component_{key}={value:.2f}" for key, value in components.items()], components


def _candidate_score(
    source: dict[str, Any],
    brief: dict[str, Any],
    strategy: str,
    metadata: dict[str, Any],
    source_count: int,
    *,
    diverse_source: bool = False,
) -> tuple[float, list[str]]:
    score, reasons, _ = _candidate_score_breakdown(
        source, brief, strategy, metadata, source_count, diverse_source=diverse_source
    )
    return score, reasons


def _quality_gate(metadata: dict[str, Any], source: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not metadata.get("text_fits"):
        reasons.append("text_outside_safe_area")
    if float(metadata.get("focal_score") or 0.0) < 0.1:
        reasons.append("insufficient_visual_information")
    blank_space = float(metadata["blank_space_ratio"]) if metadata.get("blank_space_ratio") is not None else 1.0
    if blank_space > 0.9:
        reasons.append("excessive_blank_area")
    if float(metadata.get("balance_score") or 0.0) < 0.3:
        reasons.append("poor_composition_balance")
    if not source.get("payoff_safe", True):
        reasons.append("protected_payoff_risk")
    return not reasons, reasons


def build_project_thumbnails(
    state: dict[str, Any],
    project_id: str,
    settings: Settings,
    *,
    visual_verifier: Any | None = None,
) -> dict[str, Any]:
    """Build deterministic, planning-aware cover variants from project media."""
    project_dir = _project_directory(project_id, settings)
    brief = build_thumbnail_brief(state)
    brief["cover_text"], text_details = _cover_text_details(state, brief)
    brief.update(text_details)
    with tempfile.TemporaryDirectory(prefix="clipforge-thumbnail-frames-") as frame_directory:
        frame_dir = Path(frame_directory)
        sources = _source_images(state, project_dir, brief)
        sources.extend(_frame_candidates(state, project_dir, brief, frame_dir))
        sources.sort(key=lambda item: (item["payoff_safe"], item["relevance"], item["quality_score"], item["pixels"]), reverse=True)
        sources = _verify_thumbnail_shortlist(sources, brief, verifier=visual_verifier)
        sources.sort(key=lambda item: (item["payoff_safe"], item["relevance"], item["quality_score"], item["pixels"]), reverse=True)
        if not sources:
            return {"status": "unavailable", "error": "No suitable project image or rendered frame is available for a cover.", "selected_variant_id": None, "variants": []}
        semantically_relevant = [source for source in sources if source["relevance"] >= 0.2 and source["payoff_safe"]]
        visually_usable = [
            source for source in semantically_relevant
            if not source.get("visual_context_only")
            and (not source.get("verifier_used") or float(source.get("visual_subject_match") or 0.0) >= 0.18)
        ]
        candidate_pool = visually_usable or semantically_relevant
        trusted_relevant = [source for source in candidate_pool if source.get("provenance_trust") == "high"]
        medium_comparison_relevant = [
            source
            for source in candidate_pool
            if source.get("provenance_trust") == "medium"
            and len(source.get("matched_medium_trust") or []) <= 2
            and len(source.get("subject_matches") or []) >= 2
        ]
        visual_checked = any(source.get("verifier_used") for source in candidate_pool)
        visual_primary = [
            source for source in candidate_pool
            if float(source.get("visual_primary_match") or 0.0) >= 0.18
        ]
        visual_secondary = [
            source for source in candidate_pool
            if float(source.get("visual_secondary_match") or 0.0) >= 0.18
        ]
        visual_comparison_pair = any(
            primary["path"] != secondary["path"]
            for primary in visual_primary
            for secondary in visual_secondary
        )
        comparison_ready = brief["format"] == "comparison" and (
            len(trusted_relevant) >= 2
            or (len(trusted_relevant) == 0 and len(medium_comparison_relevant) >= 2)
            or all(source["relevance"] == 0 for source in sources)
        )
        if comparison_ready and visual_checked:
            comparison_ready = visual_comparison_pair
        # Once a trustworthy, topic-relevant pool exists, never spend a variant on
        # an unrelated fallback merely to create visual diversity. Reuse a strong
        # source when necessary; relevance is more important than uniqueness.
        variant_sources = trusted_relevant or candidate_pool or [source for source in sources if source["payoff_safe"]] or sources
        strategies = ["SPLIT_COMPARISON", "SUBJECT_FOCUS", "TYPOGRAPHY_FOCUS"] if comparison_ready else ["IMAGE_PLUS_HEADER", "SUBJECT_FOCUS", "TYPOGRAPHY_FOCUS"]
        variants: list[dict[str, Any]] = []
        used_sources: set[Path] = set()
        for index, platform in enumerate(PLATFORMS):
            strategy = strategies[index]
            variant_id = f"{platform}-cover-{index + 1}"
            relative = Path(project_id) / "thumbnails" / f"{variant_id}.jpg"
            destination = settings.render_root.resolve() / relative
            try:
                metadata = _compose(variant_sources, destination, brief, strategy, index)
            except ThumbnailGenerationError:
                if strategy == "SUBJECT_FOCUS":
                    raise
                strategy = "SUBJECT_FOCUS"
                metadata = _compose(variant_sources, destination, brief, strategy, index)
            source = variant_sources[index % len(variant_sources)]
            score, reasons, components = _candidate_score_breakdown(
                source,
                brief,
                strategy,
                metadata,
                len(variant_sources),
                diverse_source=source["path"] not in used_sources,
            )
            quality_valid, rejection_reasons = _quality_gate(metadata, source)
            if not quality_valid and variant_sources:
                fallback_source = variant_sources[0]
                fallback_strategy = "TYPOGRAPHY_FOCUS" if strategy != "TYPOGRAPHY_FOCUS" else "IMAGE_PLUS_HEADER"
                fallback_metadata = _compose([fallback_source], destination, brief, fallback_strategy, 0)
                fallback_valid, fallback_rejections = _quality_gate(fallback_metadata, fallback_source)
                if fallback_valid:
                    source = fallback_source
                    strategy = fallback_strategy
                    metadata = fallback_metadata
                    score, reasons, components = _candidate_score_breakdown(
                        source,
                        brief,
                        strategy,
                        metadata,
                        1,
                        diverse_source=source["path"] not in used_sources,
                    )
                    quality_valid, rejection_reasons = True, []
                    reasons.append("safe_fallback_after_quality_gate")
                else:
                    rejection_reasons.extend(fallback_rejections)
            if not quality_valid:
                score = round(max(0.0, score - 0.12), 3)
                reasons.extend(f"rejected:{reason}" for reason in rejection_reasons)
            used_sources.add(source["path"])
            variants.append({
            "id": variant_id,
            "platform": platform,
            "url": f"/media/{relative.as_posix()}",
            "source_scene_id": source["scene_id"],
            "text": brief["cover_text"],
            "cover_text_source": brief.get("cover_text_source"),
            "cover_text_quality": brief.get("cover_text_quality"),
            "rejected_text_reasons": brief.get("rejected_text_reasons", []),
            "primary_visual_subjects": brief.get("primary_visual_subjects", []),
            "supporting_visual_context": brief.get("supporting_visual_context", []),
            "width": WIDTH,
            "height": HEIGHT,
            "composition_strategy": strategy,
            "score": score,
            "score_reasons": reasons,
            "font_size": metadata.get("font_size"),
            "text_bbox": metadata.get("text_bbox"),
            "text_area_ratio": metadata.get("text_area_ratio"),
            "backing_area_ratio": metadata.get("backing_area_ratio"),
            "dead_space_ratio": metadata.get("dead_space_ratio"),
            "crop_strategy": metadata.get("crop_strategy"),
            "crop_box": metadata.get("crop_box"),
            "focal_score": metadata.get("focal_score"),
            "visual_density": metadata.get("visual_density"),
            "blank_space_ratio": metadata.get("blank_space_ratio"),
            "text_placement": metadata.get("text_placement"),
            "text_collision": metadata.get("text_collision"),
            "balance_score": metadata.get("balance_score"),
            "clipping_safe": metadata.get("clipping_safe", metadata.get("text_fits", False)),
            "quality_valid": quality_valid,
            "rejection_reasons": rejection_reasons,
            "caption_risk": source.get("caption_risk", {"risk": 0.0, "active": False}),
            "score_components": components,
            "source_type": source.get("source_type", "scene_asset"),
            "source_timestamp": source.get("timestamp"),
            "matched_terms": source.get("matched_terms", []),
            "matched_high_trust": source.get("matched_high_trust", []),
            "matched_medium_trust": source.get("matched_medium_trust", []),
            "generic_matched_terms": source.get("generic_matched_terms", []),
            "provenance_trust": source.get("provenance_trust", "low"),
            "provenance": source.get("provenance", {}),
            "subject_matches": source.get("subject_matches", []),
            "matched_primary_subjects": source.get("matched_primary_subjects", []),
            "matched_supporting_terms": source.get("matched_supporting_terms", []),
            "subject_coverage": source.get("subject_coverage", "NONE"),
            "verifier_used": source.get("verifier_used", False),
            "verifier_model": source.get("verifier_model"),
            "visual_subject_match": source.get("visual_subject_match"),
            "visual_primary_match": source.get("visual_primary_match"),
            "visual_secondary_match": source.get("visual_secondary_match"),
            "visual_context_score": source.get("visual_context_score"),
            "visual_confidence": source.get("visual_confidence"),
            "visual_margin": source.get("visual_margin"),
            "visual_context_only": source.get("visual_context_only", False),
            "metadata_visual_disagreement": source.get("metadata_visual_disagreement", False),
            "visual_verification": source.get("visual_verification", {}),
            "semantic_relevance": source.get("relevance", 0.0),
            "source_quality_score": source.get("quality_score", 0.0),
            "payoff_safe": source.get("payoff_safe", True),
            "selection_reason": reasons,
            })
        selected = max(variants, key=lambda item: item["score"])
        return {"status": "available", "error": None, "brief": brief, "selected_variant_id": selected["id"], "variants": variants}
