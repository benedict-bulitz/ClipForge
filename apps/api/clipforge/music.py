"""Curated, local, license-safe background music selection."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .music_providers import (
    InternetArchiveMusicProvider,
    MusicCandidate,
    MusicProvider,
    MusicProviderError,
    MusicTrend,
    TrendSignalProvider,
    WikimediaMusicProvider,
    audio_suffix,
    is_supported_audio,
    music_like,
    provider_error_code,
    reusable_license,
)

MUSIC_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "music-library"
MUSIC_MANIFEST = "manifest.json"
CACHE_MANIFEST = "cache/catalog.json"
_AUDIO_SUFFIXES = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
)


@dataclass(frozen=True)
class MusicTrack:
    id: str
    title: str
    file_path: str
    mood: str
    energy: str
    tags: tuple[str, ...]
    source: str
    license: str
    attribution: str | None = None
    duration_seconds: float | None = None
    description: str | None = None
    trend: MusicTrend | None = None
    mode: str = "LOCAL_EMBEDDABLE_TRACK"


@dataclass(frozen=True)
class MusicIntent:
    mood: str
    energy: str
    style_terms: tuple[str, ...]
    conflict_terms: tuple[str, ...]
    semantic_terms: tuple[str, ...]


_CALM_STYLE_TERMS = (
    "calm", "curious", "educational", "documentary", "soft", "atmospheric",
    "ambient", "instrumental",
)
_CALM_CONFLICT_TERMS = (
    "battle", "war", "combat", "epic", "trailer", "horror", "aggressive",
    "metal", "boss", "action", "dramatic tension",
)
_MINIMUM_SELECTION_SCORE = 25
_SELECTION_BAND = 8
_SELECTION_POOL_LIMIT = 3
# A neutral instrumental starts at 36 from the documentary/low-energy fit.
# Keep that as the quality floor for a competitive alternative; the immediate
# repeat penalty exceeds the maximum style-metadata advantage (20) so an
# equally eligible fresh song can win without admitting weak candidates.
_COMPETITIVE_FLOOR = 36
_RECENCY_PENALTIES = (22, 12, 6, 3, 1)
_CONTEXT_STOPWORDS = frozenset(
    {
        "about", "aber", "auch", "dabei", "diese", "dieser", "durch", "eine",
        "einen", "einer", "einem", "eines", "haben", "ihren", "ihres", "immer",
        "nicht", "seinen", "seiner", "seinem", "that", "their", "there", "these",
        "they", "this", "unter", "unser", "unserer", "warum", "weil", "when",
        "with", "your",
    }
)


_MOOD_ALIASES = {
    "cinematic_suspense": "cinematic",
    "suspense": "cinematic",
    "editorial": "documentary",
}
_KNOWN_MOODS = frozenset({"ambient", "documentary", "tech", "cinematic"})


def _discovery_queries(intent: MusicIntent) -> tuple[str, ...]:
    """Repository-search labels, deliberately independent of the video topic."""
    if intent.energy == "low":
        return (
            "calm instrumental music",
            "light instrumental music",
            "acoustic instrumental music",
            "electronic instrumental music",
            "documentary background music",
            "instrumental music",
        )
    return (
        "upbeat instrumental music",
        "electronic instrumental music",
        "technology instrumental music",
        "cinematic instrumental music",
        "instrumental music",
    )


def music_library_root() -> Path:
    return MUSIC_LIBRARY_ROOT


def _normalise_mood(value: object, content_type: object) -> str:
    mood = _MOOD_ALIASES.get(str(value or "").casefold(), str(value or "").casefold())
    if mood in _KNOWN_MOODS:
        return mood
    return "cinematic" if str(content_type or "") == "fictional_story" else "documentary"


def build_music_intent(
    *,
    topic: object,
    script: object = None,
    content_type: object,
    mood: object = None,
    tone: object = None,
) -> MusicIntent:
    """Translate video context into compact, stock-music-friendly semantics."""
    normalized_mood = _normalise_mood(mood, content_type)
    def content_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(
            line for line in value.splitlines()
            if not re.search(r"```|PYTHONPATH=|(?:^|\s)(?:import |from \w+ import |git |pytest |sqlite3 |python(?:3)? -)|/\.venv/|\.venv/bin|SELECT .+ FROM|^\s*[\{\[]", line, re.IGNORECASE)
        )

    clean_topic = content_text(topic).casefold()
    clean_script = content_text(script).casefold()
    debug_terms = {"pythonpath", "venv", "sqlite", "sqlite3", "import", "git", "pytest", "apps", "python", "json"}
    topic_words = set(re.findall(r"\w+", clean_topic))
    context = f"{clean_topic} {clean_script}"
    tone_terms = tuple(
        term for term in _CALM_STYLE_TERMS if term in str(tone or "").casefold()
    )
    semantic_terms = tuple(
        dict.fromkeys(
            term
            for term in re.findall(r"[a-zäöüß]{4,}", context)
            if term not in _CONTEXT_STOPWORDS and (term not in debug_terms or term in topic_words)
        )
    )[:8]
    if str(content_type or "") in {
        "factual_explainer", "hypothetical_explainer", "current_explainer",
    }:
        return MusicIntent(
            mood=normalized_mood,
            energy="low",
            style_terms=tuple(dict.fromkeys((*_CALM_STYLE_TERMS, *tone_terms))),
            conflict_terms=_CALM_CONFLICT_TERMS,
            semantic_terms=semantic_terms,
        )
    return MusicIntent(
        mood=normalized_mood,
        energy="low" if normalized_mood == "ambient" else "medium",
        style_terms=(normalized_mood, "atmospheric", "instrumental", "underscore"),
        conflict_terms=(),
        semantic_terms=semantic_terms,
    )


def _track_metadata(track: MusicTrack) -> str:
    return " ".join(
        (
            track.title,
            track.description or "",
            " ".join(track.tags),
            track.attribution or "",
            track.source,
        )
    ).casefold()


def score_music_track(track: MusicTrack, intent: MusicIntent) -> tuple[int, tuple[str, ...]]:
    """Score metadata compatibility; a high-ranked track must still be suitable."""
    if track.mode != "LOCAL_EMBEDDABLE_TRACK" or not reusable_license(track.license):
        return -100, ("license_or_mode_rejected",)
    if not music_like(track.title, track.description, track.tags):
        return -100, ("not_song_like",)
    score = 0
    reasons: list[str] = []
    if track.mood == intent.mood:
        score += 20
        reasons.append("mood_match")
    if track.energy == intent.energy:
        score += 16
        reasons.append("energy_match")
    elif intent.energy == "low" and track.energy in {"medium", "high"}:
        score -= 10
        reasons.append("energy_conflict")
    metadata = _track_metadata(track)
    style_matches = sum(term in metadata for term in intent.style_terms)
    if style_matches:
        score += style_matches * 5
        reasons.append("style_match")
    semantic_matches = sum(term in metadata for term in intent.semantic_terms)
    if semantic_matches:
        score += semantic_matches * 2
        reasons.append("semantic_match")
    conflicts = tuple(term for term in intent.conflict_terms if re.search(r"\b" + re.escape(term) + r"\b", metadata))
    if conflicts:
        score = min(score - len(conflicts) * 50, _MINIMUM_SELECTION_SCORE - 1)
        reasons.append("style_conflict")
    # Hard fit gates precede popularity. Unknown trends contribute no invented signal.
    if score >= _MINIMUM_SELECTION_SCORE and not conflicts:
        quality = min(15, 5 * sum(term in metadata for term in ("song", "instrumental", "piano", "guitar", "composition", "beat")))
        score += quality
        trend = track.trend
        if trend and trend.trend_source and trend.trend_updated_at and trend.trend_confidence is not None and 0.5 <= trend.trend_confidence <= 1:
            signals = [v for v in (trend.popularity_score, trend.trend_score) if isinstance(v, (int, float)) and 0 <= v <= 1]
            if signals:
                score += round(45 * max(signals) * trend.trend_confidence)
                reasons.append("trusted_trend")
    return score, tuple(reasons)


def _selection_state(selection: dict[str, Any]) -> dict[str, Any]:
    intent = selection.get("intent")
    return {
        "target_mood": intent.mood if isinstance(intent, MusicIntent) else None,
        "target_energy": intent.energy if isinstance(intent, MusicIntent) else None,
        "target_style_terms": list(intent.style_terms) if isinstance(intent, MusicIntent) else [],
        "target_semantic_terms": list(intent.semantic_terms) if isinstance(intent, MusicIntent) else [],
        "candidate_score": selection.get("candidate_score"),
        "pool_size": selection.get("pool_size", 0),
        "reason": list(selection.get("reason", ())),
        "variation_seed": selection.get("variation_seed"),
        "recently_used_tracks": selection.get("recently_used_tracks", []),
        "top_scores": selection.get("top_scores", []),
    }


def _valid_relative_audio_path(value: object) -> str | None:
    path = Path(str(value or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts or path.suffix.casefold() not in _AUDIO_SUFFIXES:
        return None
    return path.as_posix()


def load_local_catalog(
    library_root: Path | None = None,
    *,
    path_exists: Callable[[Path], bool] | None = None,
) -> tuple[MusicTrack, ...]:
    """Load only manifest-backed, local, real audio files with license metadata."""
    root = (library_root or music_library_root()).resolve()
    manifest = root / MUSIC_MANIFEST
    exists = path_exists or Path.is_file
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    values = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return ()
    tracks: list[MusicTrack] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        relative_path = _valid_relative_audio_path(value.get("file"))
        track_id = str(value.get("track_id") or "").strip()
        title = str(value.get("title") or "").strip()
        mood = _normalise_mood(value.get("mood"), None)
        energy = str(value.get("energy") or "").strip()
        source = str(value.get("source") or "").strip()
        license_name = str(value.get("license") or "").strip()
        asset = (root / relative_path).resolve() if relative_path else None
        if (
            not track_id
            or not title
            or not energy
            or not source
            or not license_name
            or asset is None
            or root not in asset.parents
            or not exists(asset)
        ):
            continue
        tags = tuple(str(tag).strip() for tag in value.get("tags", []) if str(tag).strip())
        duration = value.get("duration_seconds")
        tracks.append(
            MusicTrack(
                id=track_id,
                title=title,
                file_path=relative_path,
                mood=mood,
                energy=energy,
                tags=tags,
                source=source,
                license=license_name,
                attribution=str(value.get("attribution") or "").strip() or None,
                duration_seconds=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
                description=str(value.get("description") or "").strip() or None,
            )
        )
    return tuple(tracks)


def select_automatic_track(
    *,
    topic: object,
    content_type: object,
    mood: object = None,
    catalog: Iterable[MusicTrack] | None = None,
    script: object = None,
    tone: object = None,
    variation_seed: object = None,
    selection_metadata: dict[str, Any] | None = None,
    trend_provider: TrendSignalProvider | None = None,
    recent_track_ids: Iterable[str] = (),
) -> MusicTrack | None:
    """Choose from a close, suitable band with a persisted deterministic seed."""
    intent = build_music_intent(
        topic=topic, script=script, content_type=content_type, mood=mood, tone=tone
    )
    tracks = tuple({track.id: track for track in (catalog if catalog is not None else load_local_catalog())}.values())
    if trend_provider is not None:
        tracks = tuple(replace(track, trend=trend_provider.lookup(track.source, track.id)) for track in tracks)
    ranked = sorted(
        ((score_music_track(track, intent), track) for track in tracks),
        key=lambda value: (-value[0][0], value[1].id),
    )
    eligible = [(scored, track) for scored, track in ranked if scored[0] >= _MINIMUM_SELECTION_SCORE]
    if not eligible:
        if selection_metadata is not None:
            selection_metadata.update(intent=intent, candidate_score=None, pool_size=0)
        return None
    best_score = eligible[0][0][0]
    pool = [
        (scored, track)
        for scored, track in eligible
        if scored[0] >= max(_COMPETITIVE_FLOOR, best_score - _RECENCY_PENALTIES[0])
    ][:_SELECTION_POOL_LIMIT]
    if not pool:
        pool = [eligible[0]]
    seed = str(variation_seed or f"{intent.mood}\n{content_type or ''}\n{str(topic or '').casefold()}")
    recent = tuple(recent_track_ids)[:len(_RECENCY_PENALTIES)]
    penalties = {track_id: max(_RECENCY_PENALTIES[i] for i, value in enumerate(recent) if value == track_id) for track_id in recent}
    best_adjusted = max(scored[0] - penalties.get(track.id, 0) for scored, track in pool)
    # Preserve seeded variation among fresh candidates; recently used tracks
    # must earn their place through their base score after the recency cost.
    finalists = [(scored, track) for scored, track in pool if scored[0] - penalties.get(track.id, 0) >= best_adjusted - 1]
    selected_score, selected = finalists[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(finalists)]
    if selection_metadata is not None:
        selection_metadata.update(
            intent=intent,
            candidate_score=selected_score[0],
            pool_size=len(pool),
            reason=selected_score[1],
            variation_seed=seed,
            recently_used_tracks=list(recent),
            top_scores=[{
                "id": track.id,
                "score": scored[0],
                "recency_penalty": penalties.get(track.id, 0),
                "adjusted_score": scored[0] - penalties.get(track.id, 0),
            } for scored, track in pool],
        )
    return selected


def automatic_music_layer(
    *,
    enabled: bool,
    topic: object,
    content_type: object,
    planned_mood: object = None,
    requested_mood: object = None,
    volume: object = 0.14,
    ducking: bool = True,
    fades: bool = True,
    catalog: Iterable[MusicTrack] | None = None,
    script: object = None,
    tone: object = None,
    variation_seed: object = None,
) -> dict[str, Any]:
    """Build persisted music state independently from narration and video."""
    mood = requested_mood if requested_mood else planned_mood
    normalized_mood = _normalise_mood(mood, content_type)
    safe_volume = max(0.0, min(0.5, float(volume)))
    base = {
        "mood": normalized_mood,
        "volume": safe_volume,
        "ducking": bool(ducking),
        "fades": bool(fades),
        "source": "local_curated_library",
    }
    if not enabled:
        return {"enabled": False, "status": "disabled", **base}
    selection: dict[str, Any] = {}
    track = select_automatic_track(
        topic=topic,
        content_type=content_type,
        mood=normalized_mood,
        catalog=catalog,
        script=script,
        tone=tone,
        variation_seed=variation_seed,
        selection_metadata=selection,
    )
    if track is None:
        return {
            "enabled": False,
            "requested_enabled": True,
            "status": "unavailable",
            "diagnostic": "No valid licensed local music track is available.",
            **base,
        }
    return {
        "enabled": True,
        "mood": track.mood,
        "status": "planned",
        **base,
        "track": {
            "id": track.id,
            "title": track.title,
            "file": track.file_path,
            "mood": track.mood,
            "energy": track.energy,
            "tags": list(track.tags),
            "source": track.source,
            "license": track.license,
            "attribution": track.attribution,
            "duration_seconds": track.duration_seconds,
            "description": track.description,
            "replaceable": True,
        },
        "selection": {
            "mode": "automatic",
            "basis": "music_intent_metadata",
            "requested_mood": normalized_mood,
            **_selection_state(selection),
        },
    }


def resolve_track_path(music: dict[str, Any], library_root: Path | None = None) -> Path | None:
    track = music.get("track") if isinstance(music.get("track"), dict) else {}
    relative_path = _valid_relative_audio_path(track.get("file"))
    if not relative_path:
        return None
    root = (library_root or music_library_root()).resolve()
    path = (root / relative_path).resolve()
    return path if root in path.parents and path.is_file() else None


def _candidate_track(
    candidate: MusicCandidate, relative_path: str, *, mood: str | None = None
) -> MusicTrack:
    return MusicTrack(
        id=f"{candidate.provider}:{candidate.provider_id}",
        title=candidate.title,
        file_path=relative_path,
        mood=mood or candidate.mood,
        energy=candidate.energy,
        tags=candidate.tags,
        source=candidate.provider,
        license=candidate.license,
        attribution=candidate.attribution,
        duration_seconds=candidate.duration_seconds,
        description=candidate.description,
        trend=candidate.trend,
    )


def valid_audio_file(path: Path) -> bool:
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False
    return result.returncode == 0


def load_cached_catalog(library_root: Path | None = None) -> tuple[MusicTrack, ...]:
    root = (library_root or music_library_root()).resolve()
    try:
        entries = json.loads((root / CACHE_MANIFEST).read_text(encoding="utf-8")).get("tracks", [])
    except (OSError, ValueError):
        return ()
    tracks: list[MusicTrack] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        relative_path = _valid_relative_audio_path(entry.get("file"))
        if not relative_path or not (root / relative_path).is_file():
            continue
        try:
            tracks.append(MusicTrack(
                id=str(entry["id"]), title=str(entry["title"]), file_path=relative_path,
                mood=str(entry["mood"]), energy=str(entry["energy"]),
                tags=tuple(str(tag) for tag in entry.get("tags", [])), source=str(entry["source"]),
                license=str(entry["license"]), attribution=entry.get("attribution"),
                duration_seconds=entry.get("duration_seconds"),
                description=entry.get("description"),
                trend=MusicTrend(**entry["trend"]) if isinstance(entry.get("trend"), dict) else None,
                mode=str(entry.get("mode", "LOCAL_EMBEDDABLE_TRACK")),
            ))
        except (KeyError, TypeError):
            continue
    return tuple(tracks)


def available_music_tracks(library_root: Path | None = None) -> tuple[MusicTrack, ...]:
    """Return only real, license-safe catalog assets that can be selected later."""
    root = library_root or music_library_root()
    tracks = (*load_local_catalog(root), *load_cached_catalog(root))
    return tuple({track.id: track for track in tracks}.values())


def ranked_music_tracks(state: dict[str, Any], catalog: Iterable[MusicTrack] | None = None) -> tuple[MusicTrack, ...]:
    """Deterministically rank available tracks from persisted project context."""
    intent_state = state.get("intent") if isinstance(state.get("intent"), dict) else {}
    music_state = state.get("music") if isinstance(state.get("music"), dict) else {}
    tracks = tuple(catalog if catalog is not None else available_music_tracks())
    intent = build_music_intent(
        topic=intent_state.get("topic"),
        script=(state.get("script") or {}).get("text") if isinstance(state.get("script"), dict) else None,
        content_type=intent_state.get("content_type"),
        mood=music_state.get("mood"),
        tone=intent_state.get("tone"),
    )
    return tuple(
        track for _score, track in sorted(
            ((score_music_track(track, intent), track) for track in tracks),
            key=lambda item: (-item[0][0], item[1].id),
        )
        if _score[0] >= _MINIMUM_SELECTION_SCORE
    )


def music_track_state(track: MusicTrack) -> dict[str, Any]:
    return {
        "id": track.id, "title": track.title, "file": track.file_path,
        "mood": track.mood, "energy": track.energy, "tags": list(track.tags),
        "source": track.source, "license": track.license, "attribution": track.attribution,
        "duration_seconds": track.duration_seconds, "description": track.description,
        "replaceable": True,
    }


def _record_cached_track(root: Path, track: MusicTrack) -> None:
    manifest = root / CACHE_MANIFEST
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"tracks": []}
    entries = [entry for entry in payload.get("tracks", []) if entry.get("id") != track.id]
    entries.append({"id": track.id, "title": track.title, "file": track.file_path, "mood": track.mood, "energy": track.energy, "tags": list(track.tags), "source": track.source, "license": track.license, "attribution": track.attribution, "duration_seconds": track.duration_seconds, "description": track.description})
    entries[-1].update(trend=asdict(track.trend) if track.trend else None, mode=track.mode)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"tracks": entries}, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_and_cache_track(
    *,
    topic: object,
    content_type: object,
    mood: object,
    script: object = None,
    tone: object = None,
    variation_seed: object = None,
    library_root: Path | None = None,
    providers: Iterable[MusicProvider] | None = None,
    validate: Callable[[Path], bool] = valid_audio_file,
    diagnostics: dict[str, Any] | None = None,
    recent_track_ids: Iterable[str] = (),
) -> MusicTrack | None:
    """Broaden metadata searches and cache at most three suitable real tracks."""
    root = (library_root or music_library_root()).resolve()
    intent = build_music_intent(
        topic=topic, script=script, content_type=content_type, mood=mood, tone=tone
    )
    queries = _discovery_queries(intent)
    report = diagnostics if diagnostics is not None else {}
    report.update(
        providers_attempted=[],
        candidate_count=0,
        provider_failures=[],
        selected_provider=None,
        cache_hit=False,
        searches=[],
        funnel={"found": 0, "license_ok": 0, "real_song_ok": 0, "audio_ok": 0, "deduped": 0, "quality_ok": 0},
    )
    selection: dict[str, Any] = {}
    cached_catalog = tuple({track.id: track for track in (*load_local_catalog(root), *load_cached_catalog(root))}.values())
    recent_track_ids = tuple(recent_track_ids)
    cached = select_automatic_track(
        topic=topic,
        script=script,
        content_type=content_type,
        mood=mood,
        tone=tone,
        variation_seed=variation_seed,
        catalog=cached_catalog,
        selection_metadata=selection,
        recent_track_ids=recent_track_ids,
    )
    report.update(cache_candidate_count=len(cached_catalog), **_selection_state(selection))
    if cached is not None and selection.get("pool_size", 0) >= _SELECTION_POOL_LIMIT:
        report.update(cache_hit=True, selected_provider=cached.source)
        return cached
    source_providers = (
        tuple(providers)
        if providers is not None
        else (InternetArchiveMusicProvider(), WikimediaMusicProvider())
    )
    available = {track.id: track for track in cached_catalog}
    original_ids = set(available)
    for provider in source_providers:
        provider_name = str(
            getattr(provider, "provider_name", type(provider).__name__)
        )
        report["providers_attempted"].append(provider_name)
        candidates_by_id: dict[str, MusicCandidate] = {}
        search_failed = False
        for query in queries:
            try:
                found = provider.search(query, limit=8)
            except (MusicProviderError, OSError, ValueError, httpx.HTTPError) as exc:
                report["provider_failures"].append(f"{provider_name}: {provider_error_code(exc)}")
                search_failed = True
                break
            counts = dict(getattr(provider, "last_search_counts", {}))
            counts.setdefault("result_count", len(found))
            counts.setdefault("license_valid_count", sum(reusable_license(c.license) for c in found))
            counts.setdefault("real_song_count", len(found))
            counts.setdefault("audio_valid_count", len(found))
            compatible = 0
            eligible = 0
            duplicates = 0
            for candidate in found:
                if not reusable_license(candidate.license) or not is_supported_audio(candidate.download_url, candidate.content_type):
                    continue
                track = _candidate_track(candidate, "cache/pending.mp3", mood=intent.mood)
                score, reasons = score_music_track(track, intent)
                compatible += "style_conflict" not in reasons
                if score >= _MINIMUM_SELECTION_SCORE:
                    track_id = f"{candidate.provider}:{candidate.provider_id}"
                    if track_id not in available:
                        eligible += 1
                        candidates_by_id[track_id] = candidate
                    else:
                        duplicates += 1
            counts.update(
                compatibility_valid_count=compatible,
                threshold_valid_count=eligible,
                duplicate_candidate_count=duplicates,
                candidate_titles=[candidate.title[:120] for candidate in found[:8]],
            )
            report["searches"].append({"provider": provider_name, "query": query, **counts})
            report["candidate_count"] += len(found)
            for key, source_key in (
                ("found", "result_count"), ("license_ok", "license_valid_count"),
                ("real_song_ok", "real_song_count"), ("audio_ok", "audio_valid_count"),
                ("deduped", "duplicate_candidate_count"), ("quality_ok", "threshold_valid_count"),
            ):
                report["funnel"][key] += int(counts.get(source_key, 0) or 0)
            probe: dict[str, Any] = {}
            select_automatic_track(topic=topic, content_type=content_type, mood=mood, script=script, tone=tone,
                catalog=(*available.values(), *(_candidate_track(c, "cache/pending.mp3", mood=intent.mood) for c in candidates_by_id.values())), selection_metadata=probe)
            if probe.get("pool_size", 0) >= _SELECTION_POOL_LIMIT:
                break
        candidates = tuple(candidates_by_id.values())
        supported = tuple(
            candidate
            for candidate in candidates
            if is_supported_audio(candidate.download_url, candidate.content_type)
        )
        selection = {}
        selected = select_automatic_track(
            topic=topic,
            script=script,
            content_type=content_type,
            mood=intent.mood,
            tone=tone,
            variation_seed=variation_seed,
            catalog=tuple(
                _candidate_track(candidate, "cache/pending.mp3", mood=intent.mood)
                for candidate in supported
            ),
            selection_metadata=selection,
        )
        report.update(**_selection_state(selection))
        if selected is None:
            if not search_failed:
                report["provider_failures"].append(f"{provider_name}: no_usable_candidate")
            continue
        # Cache a bounded, high-quality shortlist so later projects can vary.
        ranked = sorted(supported, key=lambda c: (-score_music_track(_candidate_track(c, "cache/pending.mp3", mood=intent.mood), intent)[0], c.provider_id))
        downloaded: list[MusicTrack] = []
        for candidate in ranked[:_SELECTION_POOL_LIMIT]:
            suffix = audio_suffix(candidate.download_url, candidate.content_type)
            relative_path = f"cache/{candidate.provider}/{hashlib.sha256(candidate.provider_id.encode()).hexdigest()[:20]}{suffix}"
            destination = root / relative_path
            try:
                if not destination.is_file() or not validate(destination):
                    provider.download(candidate, destination)
                if not validate(destination):
                    report["provider_failures"].append(f"{provider_name}: invalid_audio")
                    continue
                track = _candidate_track(candidate, relative_path, mood=intent.mood)
                _record_cached_track(root, track)
                downloaded.append(track)
            except (OSError, ValueError, httpx.HTTPError) as exc:
                report["provider_failures"].append(f"{provider_name}: download_{provider_error_code(exc)}")
        selection = {}
        available.update((track.id, track) for track in downloaded)
        chosen = select_automatic_track(topic=topic, content_type=content_type, mood=mood, script=script, tone=tone, variation_seed=variation_seed, catalog=available.values(), selection_metadata=selection, recent_track_ids=recent_track_ids)
        report.update(**_selection_state(selection))
        if chosen is not None and selection.get("pool_size", 0) >= _SELECTION_POOL_LIMIT:
            report["selected_provider"] = chosen.source
            report["cache_hit"] = chosen.id in original_ids
            return chosen
    selection = {}
    chosen = select_automatic_track(topic=topic, content_type=content_type, mood=mood, script=script, tone=tone, variation_seed=variation_seed, catalog=available.values(), selection_metadata=selection, recent_track_ids=recent_track_ids)
    report.update(**_selection_state(selection))
    if chosen is not None:
        report.update(selected_provider=chosen.source, cache_hit=chosen.id in original_ids)
    return chosen


def recent_music_tracks(state: dict[str, Any]) -> tuple[str, ...]:
    """Read previous projects once, never count revisions or this project twice."""
    from datetime import datetime

    from sqlalchemy import select

    from .database import SessionLocal
    from .models import Project, ProjectRevision

    if not state.get("created_at"):
        return ()
    with SessionLocal() as session:
        rows = session.scalars(
            select(ProjectRevision.state).join(Project, Project.id == ProjectRevision.project_id)
            .where(ProjectRevision.number == Project.current_revision,
                   Project.created_at < datetime.fromisoformat(state["created_at"]))
            .order_by(Project.created_at.desc()).limit(len(_RECENCY_PENALTIES))
        )
        return tuple(str((row.get("music", {}).get("track") or {}).get("id") or "") for row in rows)


def attach_discovered_track(
    state: dict[str, Any],
    *,
    library_root: Path | None = None,
    providers: Iterable[MusicProvider] | None = None,
    validate: Callable[[Path], bool] = valid_audio_file,
) -> Path | None:
    """Resolve a cached selection or discover one real reusable free track."""
    music = state.setdefault("music", {})
    existing = resolve_track_path(music, library_root)
    if existing is not None:
        data = music.get("track") or {}
        if not reusable_license(data.get("license")) or not music_like(data.get("title"), data.get("description"), tuple(data.get("tags") or ())):
            existing = None
            music["requested_enabled"] = bool(music.get("requested_enabled") or music.get("enabled"))
    if existing is not None:
        music.setdefault("providers_attempted", [])
        music.setdefault("candidate_count", 0)
        music.setdefault("selected_provider", (music.get("track") or {}).get("source"))
        return existing
    if not music.get("requested_enabled"):
        return None
    diagnostics: dict[str, Any] = {}
    # Snapshot history into the selection state so retries use the same context.
    if "recent_track_ids" not in music:
        music["recent_track_ids"] = list(recent_music_tracks(state))
    track = discover_and_cache_track(
        topic=(state.get("intent") or {}).get("topic"),
        content_type=(state.get("intent") or {}).get("content_type"),
        mood=music.get("mood"),
        script=(state.get("script") or {}).get("text"),
        tone=(state.get("intent") or {}).get("tone"),
        variation_seed=state.get("created_at"),
        library_root=library_root,
        providers=providers,
        validate=validate,
        diagnostics=diagnostics,
        recent_track_ids=music["recent_track_ids"],
    )
    if track is None:
        attempted = diagnostics.get("providers_attempted", [])
        music.update(
            enabled=False,
            status="unavailable",
            source="unavailable",
            diagnostic=(
                "No reusable licensed music track found."
                if attempted
                else "No music providers were available."
            ),
            **diagnostics,
        )
        return None
    music.update(
        enabled=True,
        mood=track.mood,
        status="cached",
        source=track.source,
        diagnostic=None,
        **diagnostics,
        track={
            "id": track.id, "title": track.title, "file": track.file_path,
            "mood": track.mood, "energy": track.energy, "tags": list(track.tags),
            "source": track.source, "license": track.license,
            "attribution": track.attribution, "duration_seconds": track.duration_seconds,
            "description": track.description,
            "trend": asdict(track.trend) if track.trend else None,
            "mode": track.mode,
            "replaceable": True,
        },
        selection={
            "mode": "automatic",
            "basis": "music_intent_metadata",
            "target_mood": diagnostics.get("target_mood"),
            "target_energy": diagnostics.get("target_energy"),
            "target_style_terms": diagnostics.get("target_style_terms", []),
            "target_semantic_terms": diagnostics.get("target_semantic_terms", []),
            "candidate_score": diagnostics.get("candidate_score"),
            "pool_size": diagnostics.get("pool_size", 0),
            "reason": diagnostics.get("reason", []),
            "variation_seed": diagnostics.get("variation_seed"),
            "from_cache": diagnostics.get("cache_hit", False),
        },
    )
    return resolve_track_path(music, library_root)
