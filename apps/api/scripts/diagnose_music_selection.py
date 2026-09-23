"""Read-only cache/history diagnostic; run from apps/api with PYTHONPATH=."""

import argparse
import json

from sqlalchemy import select

from clipforge.database import SessionLocal
from clipforge.models import Project, ProjectRevision
from clipforge.music import (
    build_music_intent,
    load_cached_catalog,
    load_local_catalog,
    recent_music_tracks,
    score_music_track,
    select_automatic_track,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    args = parser.parse_args()
    with SessionLocal() as session:
        state = session.scalar(select(ProjectRevision.state).join(Project)
            .where(Project.id == args.project_id, ProjectRevision.number == Project.current_revision))
    if state is None:
        parser.error("Project not found")
    music = state.get("music", {})
    intent = state.get("intent", {})
    catalog = (*load_local_catalog(), *load_cached_catalog())
    music_intent = build_music_intent(
        topic=intent.get("topic"), content_type=intent.get("content_type"),
        mood=music.get("mood"), script=state.get("script", {}).get("text"),
    )
    valid = [track for track in catalog if score_music_track(track, music_intent)[0] >= 25]
    diagnostics = {}
    selected = select_automatic_track(topic=intent.get("topic"), content_type=intent.get("content_type"),
        mood=music.get("mood"), script=state.get("script", {}).get("text"),
        variation_seed=state.get("created_at"), catalog=catalog,
        recent_track_ids=recent_music_tracks(state), selection_metadata=diagnostics)
    searches = music.get("searches", [])
    stored_funnel = music.get("funnel") or {
        "found": sum(int(item.get("result_count", 0) or 0) for item in searches if isinstance(item, dict)),
        "license_ok": sum(int(item.get("license_valid_count", 0) or 0) for item in searches if isinstance(item, dict)),
        "real_song_ok": sum(int(item.get("real_song_count", item.get("audio_valid_count", 0)) or 0) for item in searches if isinstance(item, dict)),
        "audio_ok": sum(int(item.get("audio_valid_count", 0) or 0) for item in searches if isinstance(item, dict)),
        "deduped": sum(int(item.get("duplicate_candidate_count", 0) or 0) for item in searches if isinstance(item, dict)),
        "quality_ok": sum(int(item.get("threshold_valid_count", 0) or 0) for item in searches if isinstance(item, dict)),
    }
    print(json.dumps({"music_intent": str(diagnostics.pop("intent", None)),
        "candidate_count": len(catalog),
        "provider_funnel": stored_funnel,
        "provider_queries": [{key: item.get(key) for key in (
            "provider", "query", "result_count", "license_valid_count", "real_song_count",
            "audio_valid_count", "duplicate_candidate_count", "threshold_valid_count",
            "license_rejected_count", "not_song_rejected_count", "audio_rejected_count",
            "candidate_titles",
        )} for item in searches if isinstance(item, dict)],
        "final_reusable_library_size": len(valid),
        "valid_track_titles": [track.title for track in valid],
        **diagnostics,
        "selected_track_preview": selected.title if selected else None,
        "persisted_track": music.get("track", {}).get("title"),
        "cache_hit": music.get("cache_hit"), "providers_searched": music.get("providers_attempted", []),
        "read_only": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
