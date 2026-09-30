"""Compact, immutable production fingerprint of one uploaded render.

Built from the persisted state of the exact render revision that produced the
uploaded MP4 (measured timeline, Triple Hook, Story Arc, Visual Director,
Final Critic).  Only IDs and compact values are copied, never media blobs, so
analytics can later be joined to the decisions that made the video.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..media import GENERATED_ASSET_SOURCE, GRAPHIC_ASSET_SOURCE, REAL_MEDIA_PROVIDERS, media_source
from ..models import ProductionFingerprint

FINGERPRINT_VERSION = 1


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _words(text: Any) -> int:
    return len(str(text or "").split())


def _round(value: Any, digits: int = 3) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def media_origin(media: dict[str, Any] | None) -> str:
    if not media:
        return "missing"
    source = media_source(media)
    if source == GENERATED_ASSET_SOURCE:
        return "generated"
    if source in REAL_MEDIA_PROVIDERS:
        return "real"
    if source == GRAPHIC_ASSET_SOURCE or media.get("graphic"):
        return "graphic"
    return "other"


def _hook(state: dict[str, Any]) -> dict[str, Any]:
    script = _dict(state.get("script"))
    hook = _dict(script.get("triple_hook"))
    visual = _dict(hook.get("visual_hook"))
    selection = _dict(hook.get("selection"))
    candidates = []
    for item in (selection.get("candidates") or [])[:8]:
        if not isinstance(item, dict):
            continue
        candidates.append({
            key: item.get(key)
            for key in ("id", "strategy", "score", "eligible", "selected", "reason_codes")
            if key in item
        })
    return {
        "hook_id": hook.get("hook_id"),
        "status": hook.get("status"),
        "source": hook.get("source"),
        "strategy": hook.get("selected_strategy") or script.get("selected_hook_strategy"),
        "verbal_hook": hook.get("verbal_hook") or script.get("selected_hook"),
        "verbal_origin": hook.get("verbal_origin"),
        "on_screen_hook": hook.get("on_screen_text_hook") or None,
        "on_screen_hook_status": hook.get("on_screen_hook_status"),
        "visual_hook": {
            "subject": visual.get("subject"),
            "visual_strategy": visual.get("visual_strategy"),
            "visual_goal": visual.get("visual_goal"),
            "source": visual.get("source"),
        },
        "intended_reaction": hook.get("intended_reaction"),
        "score": hook.get("score"),
        "reason_codes": list(hook.get("reason_codes") or [])[:16],
        "candidate_count": selection.get("candidate_count"),
        "eligible_count": selection.get("eligible_count"),
        "selected_candidate_id": selection.get("selected_id"),
        "candidates": candidates,
    }


def _topic(state: dict[str, Any]) -> dict[str, Any]:
    """Where the question came from (manual or Topic Intelligence), compactly."""
    provenance = _dict(state.get("topic_provenance"))
    return {
        key: provenance.get(key)
        for key in ("topic_source", "candidate_id", "score_version", "final_score", "niche", "edited")
        if key in provenance
    } or {"topic_source": "manual"}


def _scene_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    review = _dict(state.get("final_quality_review"))
    issues_by_scene: dict[str, int] = {}
    for issue in review.get("issues") or []:
        if isinstance(issue, dict) and issue.get("scene_id"):
            issues_by_scene[str(issue["scene_id"])] = issues_by_scene.get(str(issue["scene_id"]), 0) + 1
    repaired = {str(item) for item in review.get("repaired_scenes") or []}
    seen_media: set[str] = set()
    rows = []
    for index, scene in enumerate(item for item in state.get("scenes") or [] if isinstance(item, dict)):
        media = _dict(scene.get("media"))
        director = _dict(scene.get("visual_director"))
        intent = _dict(scene.get("visual_intent"))
        overlays = [item for item in scene.get("overlays") or [] if isinstance(item, dict)]
        identity = str(media.get("identity") or "") or None
        origin = media_origin(media or None)
        start, end = _round(scene.get("start")), _round(scene.get("end"))
        rows.append({
            "index": index + 1,
            "scene_id": scene.get("id"),
            "block_id": scene.get("block_id"),
            "start": start,
            "end": end,
            "duration": _round((end or 0) - (start or 0)),
            "narration_words": _words(scene.get("narration")),
            "story_role": scene.get("story_role"),
            "story_stage": scene.get("story_stage"),
            "story_unit_ids": list(scene.get("story_unit_ids") or [])[:8],
            "is_primary_answer": bool(scene.get("is_primary_answer")),
            "is_final_payoff": bool(scene.get("is_final_payoff")),
            "visual_strategy": director.get("visual_strategy") or intent.get("visual_strategy"),
            "director_decision": director.get("decision"),
            "director_resolved_type": director.get("resolved_type"),
            "director_reason": director.get("decision_reason"),
            "composition": director.get("composition"),
            "media_source": media_source(media) if media else None,
            "media_kind": media.get("kind"),
            "media_origin": origin,
            "media_identity": identity,
            "media_reused": bool(identity and identity in seen_media),
            "overlay_kinds": [str(_dict(item.get("spec")).get("kind") or item.get("kind") or "") for item in overlays],
            "text_heavy": bool(overlays) or origin == "graphic",
            "critic_issue_count": issues_by_scene.get(str(scene.get("id")), 0),
            "critic_repaired": str(scene.get("id")) in repaired,
        })
        if identity:
            seen_media.add(identity)
    return rows


def build_fingerprint(
    render_state: dict[str, Any],
    *,
    upload_state: dict[str, Any] | None = None,
    project_id: str,
    render_revision: int,
    render_sha256: str,
    file_size: int,
) -> dict[str, Any]:
    state = render_state
    scenes = _scene_rows(state)
    duration = _round(_dict(state.get("duration")).get("actual_seconds") or _dict(state.get("timeline")).get("duration"))
    timeline = _dict(state.get("timeline"))
    review = _dict(state.get("final_quality_review"))
    repairs = [item for item in review.get("repairs") or [] if isinstance(item, dict)]
    music = _dict(state.get("music"))
    track = _dict(music.get("track"))
    voice = _dict(state.get("voice"))
    captions = _dict(state.get("captions"))
    story = _dict(state.get("story_arc"))
    durations = [row["duration"] for row in scenes if row["duration"] is not None]
    answer = next((row["start"] for row in scenes if row["is_primary_answer"]), None)
    payoff = next((row["start"] for row in scenes if row["is_final_payoff"]), None)
    origins: dict[str, int] = {}
    for row in scenes:
        origins[row["media_origin"]] = origins.get(row["media_origin"], 0) + 1
    metadata = _dict(_dict(_dict((upload_state or state).get("social_metadata")).get("platforms")).get("youtube"))
    return {
        "version": FINGERPRINT_VERSION,
        "render": {
            "project_id": project_id,
            "render_revision": render_revision,
            "render_sha256": render_sha256,
            "file_size": file_size,
            "content_hash": _dict(state.get("content_hashes")).get("render"),
            "width": timeline.get("width"),
            "height": timeline.get("height"),
            "fps": timeline.get("fps"),
            "aspect_ratio": timeline.get("aspect_ratio"),
        },
        "content": {
            "format": _dict(state.get("format_plan")).get("selected_format") or story.get("format"),
            "content_type": _dict(state.get("intent")).get("content_type"),
            "language": _dict(state.get("intent")).get("language"),
            "duration_seconds": duration,
            "script_word_count": _dict(state.get("script")).get("word_count") or _words(_dict(state.get("script")).get("text")),
            "story_structure": story.get("structure"),
            "story_roles": [row["story_role"] for row in scenes],
            "answer_reveal_seconds": answer,
            "payoff_seconds": payoff,
            "title_word_count": _words(metadata.get("title")),
            "topic": _topic(state),
        },
        "hook": _hook(state),
        "visual": {
            "media_origin_counts": origins,
            "generated_scene_count": origins.get("generated", 0),
            "real_scene_count": origins.get("real", 0),
            "graphic_scene_count": origins.get("graphic", 0),
            "overlay_scene_count": sum(1 for row in scenes if row["overlay_kinds"]),
            "reused_media_scene_count": sum(1 for row in scenes if row["media_reused"]),
            "director_summary": {
                key: value
                for key, value in _dict(_dict(state.get("visual_director")).get("summary")).items()
                if key in {"decisions", "resolved_types", "auto_generated_images", "manual_generated_images"}
            },
        },
        "pacing": {
            "scene_count": len(scenes),
            "scene_durations": durations,
            "mean_scene_seconds": _round(sum(durations) / len(durations)) if durations else None,
            "cut_pace": timeline.get("cut_pace"),
            "pacing_score": _dict(state.get("pacing_analysis")).get("overall_score"),
        },
        "quality": {
            "critic_status": review.get("status"),
            "critic_score": review.get("overall_score"),
            "issue_count": len([item for item in review.get("issues") or [] if isinstance(item, dict)]),
            "unresolved_issue_count": len(review.get("unresolved") or []),
            "repairs_attempted": len(repairs),
            "repairs_successful": sum(1 for item in repairs if item.get("repair_effective")),
            "repair_pass_count": review.get("repair_pass_count"),
        },
        "audio": {
            "voice_provider": voice.get("provider"),
            "voice_id": voice.get("voice_id"),
            "voice_profile": voice.get("profile"),
            "voice_tone": voice.get("tone"),
            "voice_speed": voice.get("speed"),
            "music_enabled": bool(music.get("enabled")),
            "music_track_id": track.get("id"),
            "music_volume": music.get("volume"),
            "music_ducking": music.get("ducking"),
            "captions_enabled": bool(captions.get("enabled")),
            "caption_style": captions.get("style"),
        },
        "scenes": scenes,
    }


def get_or_create_fingerprint(
    db: Session,
    render_state: dict[str, Any],
    *,
    upload_state: dict[str, Any] | None,
    project_id: str,
    render_revision: int,
    render_sha256: str,
    file_size: int,
) -> ProductionFingerprint:
    statement = select(ProductionFingerprint).where(
        ProductionFingerprint.project_id == project_id,
        ProductionFingerprint.render_revision == render_revision,
        ProductionFingerprint.render_sha256 == render_sha256,
    )
    existing = db.scalar(statement)
    if existing is not None:
        return existing
    record = ProductionFingerprint(
        project_id=project_id,
        render_revision=render_revision,
        render_sha256=render_sha256,
        version=FINGERPRINT_VERSION,
        fingerprint=build_fingerprint(
            render_state,
            upload_state=upload_state,
            project_id=project_id,
            render_revision=render_revision,
            render_sha256=render_sha256,
            file_size=file_size,
        ),
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(statement)
        if existing is None:
            raise
        return existing
    db.refresh(record)
    return record
