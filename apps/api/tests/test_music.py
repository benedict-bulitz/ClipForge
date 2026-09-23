import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from clipforge.config import Settings
from clipforge.database import Base
from clipforge.models import Project, ProjectRevision
from clipforge.music import (
    MusicTrack,
    attach_discovered_track,
    automatic_music_layer,
    build_music_intent,
    discover_and_cache_track,
    load_local_catalog,
    ranked_music_tracks,
    recent_music_tracks,
    score_music_track,
    select_automatic_track,
)
from clipforge.music_providers import (
    InternetArchiveMusicProvider,
    MusicCandidate,
    MusicTrend,
    WikimediaMusicProvider,
    audio_suffix,
    is_supported_audio,
    music_like,
    reusable_license,
)
from clipforge.pipeline import build_initial_state
from clipforge.renderer import (
    _create_music_track,
    music_filter_graph,
    music_input_args,
    music_render_config,
)
from clipforge.schemas import AdvancedOptions


def settings(tmp_path: Path) -> Settings:
    return Settings(
        clipforge_ai_mode="local",
        openai_api_key=None,
        brave_search_api_key=None,
        render_root=tmp_path,
    )


def real_track(track_id: str = "field-notes") -> MusicTrack:
    return MusicTrack(
        id=track_id,
        title="Field Notes",
        file_path=f"tracks/{track_id}.mp3",
        mood="documentary",
        energy="low",
        tags=("science", "curious"),
        source="Example licensed library",
        license="CC BY 4.0",
        attribution="Example Artist — Field Notes, CC BY 4.0",
        duration_seconds=96.4,
        description="Soft instrumental piano song",
    )


def test_recent_usage_only_affects_competitive_legal_songs():
    tracks = [real_track(name) for name in ("a", "b", "c")]
    bad = replace(real_track("bad"), title="Noise soundscape", description="Field recording")
    illegal = replace(real_track("illegal"), license="CC BY-NC 4.0")
    def choose(seed, recent=(), catalog=None):
        return select_automatic_track(topic="science", content_type="factual_explainer",
            catalog=catalog or (*tracks, bad, illegal), variation_seed=seed, recent_track_ids=recent)
    first = choose("project-one")
    assert first == choose("project-one")
    assert choose("project-two", [first.id]).id != first.id
    assert len({choose(str(i)).id for i in range(20)}) > 1
    assert choose("only", [first.id], [first, bad, illegal]) == first
    assert choose("project-one", ["other"] * 5 + [first.id]) == first


def test_small_cache_expands_and_healthy_cache_stops_discovery(tmp_path):
    from clipforge.music import _record_cached_track

    cached = replace(real_track("cached"), file_path="tracks/cached.mp3")
    (tmp_path / "tracks").mkdir()
    (tmp_path / cached.file_path).write_bytes(b"audio")
    _record_cached_track(tmp_path, cached)
    candidate = _provider_candidate("internet_archive")
    provider = ProductionProvider("internet_archive", [
        replace(candidate, provider_id=name, title="Soft curious instrumental piano song")
        for name in ("a", "b", "c")
    ])
    report = {}
    kwargs = {"topic": "science", "content_type": "factual_explainer", "mood": "documentary",
        "library_root": tmp_path, "providers": (provider,), "validate": lambda _: True}
    assert discover_and_cache_track(**kwargs, diagnostics=report)
    assert provider.searches > 0
    assert report["pool_size"] == 3
    searches, downloads = provider.searches, provider.downloads
    assert discover_and_cache_track(**kwargs)
    assert (provider.searches, provider.downloads) == (searches, downloads)


def test_production_sqlite_history_drives_next_project_away_from_recent_track(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    before = datetime(2026, 1, 1, tzinfo=UTC)
    with factory.begin() as session:
        project = Project(id="project-a", original_prompt="Campfire", title="Campfire", current_revision=1, active_tip_revision=1, created_at=before)
        session.add(project)
        session.add(ProjectRevision(id="revision-a", project_id="project-a", number=1, instruction="Create", kind="initial", state={"music": {"track": {"id": "placid"}}}, changed_components=[]))
    monkeypatch.setattr("clipforge.database.SessionLocal", factory)
    history = recent_music_tracks({"created_at": (before + timedelta(minutes=1)).isoformat()})
    assert history == ("placid",)
    placid = replace(real_track("placid"), title="Placid Calm Ambient Instrumental Music", description="peaceful documentary music instrumental")
    fresh = replace(real_track("fresh"), title="Neutral Instrumental Music")
    selected = select_automatic_track(topic="aircraft lightning", content_type="factual_explainer", mood="documentary", catalog=(placid, fresh), variation_seed="project-b", recent_track_ids=history)
    assert selected == fresh


def test_duplicate_cache_result_does_not_stop_semantic_discovery(tmp_path):
    from clipforge.music import _record_cached_track

    cached = replace(real_track("wikimedia:placid"), title="Placid Calm Ambient Instrumental Music", description="peaceful documentary music instrumental", file_path="tracks/placid.mp3")
    (tmp_path / "tracks").mkdir()
    (tmp_path / cached.file_path).write_bytes(b"audio")
    _record_cached_track(tmp_path, cached)
    base = _provider_candidate("wikimedia")
    class Provider(ProductionProvider):
        def search(self, query, *, limit):
            self.searches += 1
            if query == "instrumental music":
                return [replace(base, provider_id="fresh", title="Neutral Instrumental Music")]
            return [replace(base, provider_id="placid", title="Placid Calm Ambient Instrumental Music", description="peaceful documentary music instrumental")]
    provider = Provider("wikimedia", [])
    report = {}
    selected = discover_and_cache_track(topic="aircraft lightning", content_type="factual_explainer", mood="documentary", library_root=tmp_path, providers=(provider,), validate=lambda _: True, diagnostics=report, recent_track_ids=("wikimedia:placid",))
    assert selected is not None and selected.id.endswith(":fresh")
    assert provider.searches == 6
    assert report["pool_size"] == 2
    assert {item["id"] for item in report["top_scores"]} == {"wikimedia:placid", "wikimedia:fresh"}


def test_existing_selected_track_is_not_reselected(tmp_path, monkeypatch):
    path = tmp_path / "selected.mp3"
    path.write_bytes(b"audio")
    state = {"music": {"enabled": False, "volume": .12, "track": {
        "id": "manual", "file": path.name, "title": "Piano instrumental song", "license": "CC BY 4.0"}}}
    monkeypatch.setattr("clipforge.music.discover_and_cache_track", lambda **_: pytest.fail("must preserve selection"))
    assert attach_discovered_track(state, library_root=tmp_path) == path
    assert state["music"]["enabled"] is False
    assert state["music"]["volume"] == .12


def scored_track(
    track_id: str,
    title: str,
    *,
    energy: str = "low",
    tags: tuple[str, ...] = (),
) -> MusicTrack:
    return MusicTrack(
        id=track_id,
        title=title,
        file_path=f"cache/{track_id}.ogg",
        mood="documentary",
        energy=energy,
        tags=tags,
        source="wikimedia",
        license="CC BY 4.0",
    )


def test_manifest_loads_only_a_licensed_real_local_audio_asset(tmp_path):
    manifest = {
        "version": 1,
        "tracks": [
            {
                "track_id": "field-notes",
                "title": "Field Notes",
                "file": "tracks/field-notes.mp3",
                "mood": "documentary",
                "energy": "low",
                "tags": ["science", "curious"],
                "source": "Example licensed library",
                "license": "CC BY 4.0",
                "attribution": "Example Artist — Field Notes, CC BY 4.0",
                "duration_seconds": 96.4,
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    catalog = load_local_catalog(tmp_path, path_exists=lambda path: path.name == "field-notes.mp3")

    assert len(catalog) == 1
    assert catalog[0].file_path == "tracks/field-notes.mp3"
    assert catalog[0].license == "CC BY 4.0"


def test_selection_is_deterministic_and_mood_matched_for_real_tracks():
    catalog = (real_track("field-notes"), real_track("quiet-lab"))
    first = select_automatic_track(
        topic="Why visible breath condenses in winter",
        content_type="factual_explainer",
        mood="documentary",
        catalog=catalog,
    )
    second = select_automatic_track(
        topic="Why visible breath condenses in winter",
        content_type="factual_explainer",
        mood="documentary",
        catalog=catalog,
    )

    assert first is not None and first == second
    assert first.mood == "documentary"
    assert first.file_path.endswith(".mp3")


def test_calm_educational_intent_rejects_battle_music_in_favor_of_soft_ambient():
    battle = scored_track("battle", "Sacred Battle Loop", tags=("epic", "action"))
    calm = scored_track("calm", "Soft Atmospheric Instrumental", tags=("ambient", "curious"))
    selection = {}

    selected = select_automatic_track(
        topic="Why can we see our breath in winter?",
        script="Cold air turns water vapor into a small cloud.",
        content_type="factual_explainer",
        mood="documentary",
        catalog=(battle, calm),
        variation_seed="project-one",
        selection_metadata=selection,
    )

    assert selected == calm
    assert selection["candidate_score"] >= 25
    intent = build_music_intent(
        topic="winter breath", content_type="factual_explainer", mood="documentary"
    )
    assert score_music_track(calm, intent)[0] > score_music_track(battle, intent)[0]
    assert score_music_track(battle, intent)[0] < 25


def test_controlled_variation_uses_only_similarly_strong_suitable_tracks():
    calm_one = scored_track("calm-one", "Calm Ambient Instrumental One")
    calm_two = scored_track("calm-two", "Calm Ambient Instrumental Two")
    battle = scored_track("battle", "Epic Battle Trailer")
    selected_ids = {
        select_automatic_track(
            topic="winter breath",
            content_type="factual_explainer",
            mood="documentary",
            catalog=(calm_one, calm_two, battle),
            variation_seed=f"project-{number}",
        ).id
        for number in range(16)
    }

    assert selected_ids == {"calm-one", "calm-two"}


def test_one_suitable_track_is_selected_even_when_variation_is_requested():
    calm = scored_track("calm", "Calm Soft Ambient Instrumental")
    battle = scored_track("battle", "Battle Boss Combat Theme")

    selected = select_automatic_track(
        topic="winter breath",
        content_type="factual_explainer",
        mood="documentary",
        catalog=(calm, battle),
        variation_seed="new-project",
    )

    assert selected == calm


def test_cached_tracks_are_ranked_as_a_pool_instead_of_a_permanent_winner(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    entries = []
    for track in (
        scored_track("calm-one", "Calm Ambient Instrumental One"),
        scored_track("calm-two", "Calm Ambient Instrumental Two"),
        scored_track("battle", "Sacred Battle Loop"),
    ):
        path = tmp_path / track.file_path
        path.write_bytes(b"cached-audio")
        entries.append(
            {
                "id": track.id,
                "title": track.title,
                "file": track.file_path,
                "mood": track.mood,
                "energy": track.energy,
                "tags": list(track.tags),
                "source": track.source,
                "license": track.license,
            }
        )
    (cache / "catalog.json").write_text(json.dumps({"tracks": entries}), encoding="utf-8")

    selected_ids = {
        discover_and_cache_track(
            topic="winter breath",
            content_type="factual_explainer",
            mood="documentary",
            library_root=tmp_path,
            providers=(),
            variation_seed=f"project-{number}",
        ).id
        for number in range(16)
    }

    assert selected_ids == {"calm-one", "calm-two"}


def test_selected_real_track_metadata_is_separate_and_replaceable():
    layer = automatic_music_layer(
        enabled=True,
        topic="Why visible breath condenses in winter",
        content_type="factual_explainer",
        planned_mood="documentary",
        catalog=(real_track(),),
    )

    assert layer["status"] == "planned"
    assert layer["track"]["file"] == "tracks/field-notes.mp3"
    assert layer["track"]["replaceable"] is True
    assert layer["track"]["license"] == "CC BY 4.0"
    assert music_render_config({"music": layer})["effective_volume"] <= 0.08


def test_ai_matched_recommendations_are_ranked_only_from_real_catalog_tracks():
    catalog = (real_track("first"), real_track("second"))
    recommendations = ranked_music_tracks(
        {"intent": {"topic": "Why breath condenses", "content_type": "factual_explainer"}, "script": {"text": "A calm science explanation."}, "music": {"mood": "documentary"}},
        catalog,
    )
    assert recommendations
    assert {track.id for track in recommendations} <= {track.id for track in catalog}


def test_missing_catalog_disables_music_instead_of_generating_a_hum(monkeypatch, tmp_path):
    layer = automatic_music_layer(
        enabled=True,
        topic="Any topic",
        content_type="factual_explainer",
        planned_mood="documentary",
        catalog=(),
    )
    monkeypatch.setattr("clipforge.renderer.attach_discovered_track", lambda _state: None)

    assert layer["enabled"] is False
    assert layer["status"] == "unavailable"
    assert _create_music_track("ffmpeg", {"music": {"enabled": True}}, 20, tmp_path) is None


def test_selected_real_track_path_reaches_renderer_without_a_procedural_fallback(monkeypatch, tmp_path):
    track_path = Path("/licensed-library/tracks/field-notes.mp3")
    monkeypatch.setattr("clipforge.renderer.attach_discovered_track", lambda _state: track_path)
    monkeypatch.setattr(
        "clipforge.renderer._run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not synthesize music")),
    )

    assert _create_music_track("ffmpeg", {"music": {"enabled": True}}, 20, tmp_path) == track_path


def test_real_track_path_is_looped_trimmed_and_mixed_under_narration(tmp_path):
    track = tmp_path / "licensed-track.mp3"
    config = music_render_config(
        {"music": {"enabled": True, "volume": 0.14, "ducking": True, "fades": True}}
    )

    assert music_input_args(track, 8) == ["-stream_loop", "-1", "-t", "8.000", "-i", str(track)]
    graph = music_filter_graph(config, 8)
    assert "volume=0.077" in graph
    assert "afade=t=out:st=7.000" in graph
    assert "amix=inputs=2" in graph


def test_initial_state_leaves_narration_and_media_unchanged_when_no_track_exists(tmp_path):
    no_music = build_initial_state(
        "Why can we see our breath in winter?",
        AdvancedOptions(music_enabled=False),
        settings(tmp_path),
    )
    with_music = build_initial_state(
        "Why can we see our breath in winter?",
        AdvancedOptions(),
        settings(tmp_path),
    )

    assert with_music["music"]["status"] == "unavailable"
    assert with_music["script"] == no_music["script"]
    assert with_music["scenes"] == no_music["scenes"]


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ArchiveClient:
    def get(self, url, **_kwargs):
        if "advancedsearch" in url:
            return Response({"response": {"docs": [{"identifier": "item", "title": "Instrumental Music", "licenseurl": "CC BY 4.0", "subject": "music; instrumental"}]}})
        return Response({"files": [{"name": "track.mp3"}]})


class CommonsClient:
    def get(self, _url, **_kwargs):
        return Response({"query": {"pages": {"1": {"pageid": 1, "title": "File:Instrumental.ogg", "imageinfo": [{"url": "https://cdn.test/track.ogg", "descriptionurl": "https://commons.test/file", "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "ImageDescription": {"value": "instrumental music"}}}]}}}})


def test_free_provider_candidates_are_normalized_and_license_gated():
    archive = InternetArchiveMusicProvider(client=ArchiveClient())
    commons = WikimediaMusicProvider(client=CommonsClient())

    assert archive.search("documentary music", limit=2)[0].provider == "internet_archive"
    assert commons.search("documentary music", limit=2)[0].provider == "wikimedia"
    assert reusable_license("CC BY 4.0") is True
    assert reusable_license("https://creativecommons.org/licenses/by/4.0/") is True
    assert reusable_license("CC BY-NC 4.0") is False
    assert reusable_license("https://creativecommons.org/licenses/by-nc/4.0/") is False
    assert reusable_license("All Rights Reserved") is False
    assert music_like("A quiet composition", "", ()) is True
    assert music_like("Podcast soundtrack", "instrumental music", ()) is False


def test_repository_providers_use_broad_music_queries_and_bounded_depth():
    calls = []

    class EmptyArchiveClient:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"response": {"docs": []}})

    InternetArchiveMusicProvider(client=EmptyArchiveClient()).search("light instrumental music", limit=99)
    url, kwargs = calls[0]
    assert "archive.org" in url
    assert '"light" OR "instrumental" OR "music"' in kwargs["params"]["q"]
    assert kwargs["params"]["rows"] == 12

    class EmptyCommonsClient:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"query": {"pages": {}}})

    WikimediaMusicProvider(client=EmptyCommonsClient()).search("instrumental music", limit=99)
    assert calls[-1][1]["params"]["gsrlimit"] == 12


def test_wikimedia_default_client_uses_identifying_user_agent(monkeypatch):
    client_options = {}

    def client_factory(**options):
        client_options.update(options)
        return CommonsClient()

    monkeypatch.setattr("clipforge.music_providers.httpx.Client", client_factory)

    WikimediaMusicProvider()

    assert client_options["headers"]["User-Agent"].startswith("ClipForge/")


def test_internet_archive_failure_is_persisted_with_a_specific_safe_reason(tmp_path):
    class TimeoutClient:
        def get(self, _url, **_kwargs):
            raise httpx.ReadTimeout("timed out")

    diagnostics = {}
    track = discover_and_cache_track(
        topic="winter breath",
        content_type="factual_explainer",
        mood="documentary",
        library_root=tmp_path,
        providers=(InternetArchiveMusicProvider(client=TimeoutClient()),),
        diagnostics=diagnostics,
    )

    assert track is None
    assert diagnostics["provider_failures"] == ["internet_archive: timeout"]


def test_normal_real_audio_extensions_and_mime_types_are_supported():
    for suffix in (".ogg", ".oga", ".opus", ".mp3", ".wav", ".flac", ".m4a"):
        assert is_supported_audio(f"https://commons.test/audio{suffix}?download=1")
    assert audio_suffix("https://commons.test/Special:Redirect/file/score", "audio/opus") == ".opus"
    assert audio_suffix("https://commons.test/Special:Redirect/file/score", "application/ogg") == ".ogg"
    assert is_supported_audio("https://commons.test/not-a-track.pdf", "application/pdf") is False
    assert is_supported_audio("https://commons.test/photo.jpg", "image/jpeg") is False


def test_wikimedia_keeps_supported_mime_audio_when_the_url_has_no_extension():
    class MimeClient:
        def get(self, _url, **_kwargs):
            return Response(
                {
                    "query": {
                        "pages": {
                            "1": {
                                "pageid": 1,
                                "title": "File:Instrumental opus",
                                "imageinfo": [
                                    {
                                        "url": "https://commons.test/Special:Redirect/file/score",
                                        "mime": "audio/opus",
                                        "descriptionurl": "https://commons.test/file",
                                        "extmetadata": {
                                            "LicenseShortName": {"value": "CC BY 4.0"},
                                            "ImageDescription": {"value": "instrumental music"},
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            )

    candidates = WikimediaMusicProvider(client=MimeClient()).search("music", limit=1)

    assert len(candidates) == 1
    assert candidates[0].content_type == "audio/opus"
    assert audio_suffix(candidates[0].download_url, candidates[0].content_type) == ".opus"


def test_selected_provider_track_is_cached_once_and_reused(tmp_path):
    candidate = MusicCandidate("internet_archive", "item", "Instrumental Music", "https://cdn.test/track.mp3", "https://archive.test/item", "CC BY 4.0", None, "documentary", "low", ("instrumental",))

    class Provider:
        def __init__(self):
            self.downloads = 0

        def search(self, _query, *, limit):
            return [candidate]

        def download(self, _candidate, destination):
            self.downloads += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x" * 2048)
            return destination

    provider = Provider()
    first = discover_and_cache_track(topic="winter breath", content_type="factual_explainer", mood="documentary", library_root=tmp_path, providers=(provider,), validate=lambda _path: True)
    second = discover_and_cache_track(topic="winter breath", content_type="factual_explainer", mood="documentary", library_root=tmp_path, providers=(provider,), validate=lambda _path: True)

    assert first is not None and second is not None
    assert first.file_path == second.file_path
    assert provider.downloads == 1


def _provider_candidate(provider: str) -> MusicCandidate:
    return MusicCandidate(
        provider,
        f"{provider}-item",
        "Licensed Instrumental Underscore",
        f"https://cdn.test/{provider}.mp3",
        f"https://{provider}.test/item",
        "CC BY 4.0",
        "Example Artist, CC BY 4.0",
        "documentary",
        "low",
        ("instrumental", "science"),
    )


class ProductionProvider:
    def __init__(self, provider_name: str, candidates: list[MusicCandidate]):
        self.provider_name = provider_name
        self.candidates = candidates
        self.searches = 0
        self.downloads = 0

    def search(self, _query, *, limit):
        self.searches += 1
        return self.candidates[:limit]

    def download(self, _candidate, destination):
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"real-audio-fixture" * 128)
        return destination


def test_provider_candidate_is_matched_to_the_requested_mood(tmp_path):
    archive = ProductionProvider(
        "internet_archive", [_provider_candidate("internet_archive")]
    )
    diagnostics = {}

    track = discover_and_cache_track(
        topic="A suspenseful fictional story",
        content_type="fictional_story",
        mood="cinematic",
        library_root=tmp_path,
        providers=(archive,),
        validate=lambda path: path.is_file(),
        diagnostics=diagnostics,
    )

    assert track is not None
    assert track.mood == "cinematic"
    assert archive.downloads == 1
    assert diagnostics["provider_failures"] == []


def _requested_music_state() -> dict:
    return {
        "intent": {"topic": "Why breath condenses in winter", "content_type": "factual_explainer"},
        "music": {
            "enabled": False,
            "requested_enabled": True,
            "mood": "documentary",
            "volume": 0.14,
            "ducking": True,
            "fades": True,
            "source": "local_curated_library",
        },
    }


def test_production_music_preparation_discovers_and_attaches_internet_archive_track(tmp_path, monkeypatch):
    archive = ProductionProvider("internet_archive", [_provider_candidate("internet_archive")])
    state = _requested_music_state()
    monkeypatch.setattr("clipforge.music.InternetArchiveMusicProvider", lambda: archive)
    monkeypatch.setattr("clipforge.music.WikimediaMusicProvider", lambda: ProductionProvider("wikimedia", []))

    path = attach_discovered_track(
        state,
        library_root=tmp_path,
        validate=lambda path: path.is_file(),
    )

    assert path is not None and path.is_file()
    assert archive.searches == 6
    assert archive.downloads == 1
    assert state["music"]["enabled"] is True
    assert state["music"]["source"] == "internet_archive"
    assert state["music"]["selected_provider"] == "internet_archive"
    assert state["music"]["providers_attempted"] == ["internet_archive", "wikimedia"]
    assert state["music"]["track"]["file"].startswith("cache/internet_archive/")
    monkeypatch.setattr("clipforge.music.music_library_root", lambda: tmp_path)
    assert _create_music_track("ffmpeg", state, 20, tmp_path) == path


def test_production_music_preparation_falls_back_to_wikimedia(tmp_path, monkeypatch):
    archive = ProductionProvider("internet_archive", [])
    wikimedia = ProductionProvider("wikimedia", [_provider_candidate("wikimedia")])
    state = _requested_music_state()
    monkeypatch.setattr("clipforge.music.InternetArchiveMusicProvider", lambda: archive)
    monkeypatch.setattr("clipforge.music.WikimediaMusicProvider", lambda: wikimedia)

    path = attach_discovered_track(
        state,
        library_root=tmp_path,
        validate=lambda path: path.is_file(),
    )

    assert path is not None
    assert archive.searches == 6
    assert wikimedia.searches == 6
    assert wikimedia.downloads == 1
    assert state["music"]["source"] == "wikimedia"
    assert state["music"]["providers_attempted"] == ["internet_archive", "wikimedia"]


def test_query_ladder_collects_generic_music_and_caches_a_varied_pool(tmp_path):
    class LadderProvider(ProductionProvider):
        def search(self, query, *, limit):
            self.queries.append(query)
            if query != "instrumental music":
                return []
            return self.candidates[:limit]

    base = _provider_candidate("wikimedia")
    candidates = [
        replace(base, provider_id=str(i), title=title)
        for i, title in enumerate([
            "Soft Ambient Instrumental", "Calm Piano Instrumental",
            "Soft Piano Instrumental", "Sacred Battle Loop",
        ])
    ]
    provider = LadderProvider("wikimedia", candidates)
    provider.queries = []
    diagnostics = {}
    selected = discover_and_cache_track(
        topic="Warum sieht man seinen Atem im Winter?", content_type="factual_explainer",
        mood="documentary", library_root=tmp_path, providers=(provider,),
        validate=lambda path: path.is_file(), diagnostics=diagnostics,
    )
    assert selected is not None and selected.title != "Sacred Battle Loop"
    assert len(provider.queries) == 6
    assert all("Winter" not in query and "Atem" not in query for query in provider.queries)
    assert provider.queries == [
        "calm instrumental music", "light instrumental music", "acoustic instrumental music",
        "electronic instrumental music", "documentary background music", "instrumental music",
    ]
    assert provider.downloads == 3
    assert diagnostics["searches"][-1]["threshold_valid_count"] == 3
    assert diagnostics["searches"][-1]["compatibility_valid_count"] == 3
    selected_ids = {
        discover_and_cache_track(
            topic="A different question", content_type="factual_explainer", mood="documentary",
            library_root=tmp_path, providers=(), variation_seed=f"generation-{i}",
        ).id for i in range(20)
    }
    assert len(selected_ids) > 1
    assert "wikimedia:3" not in selected_ids


def test_reusable_license_allowlist_rejects_restricted_or_unknown_grants():
    for license_name in ("CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-SA 4.0", "Unknown", "All Rights Reserved"):
        assert not reusable_license(license_name)
    for license_name in ("CC0", "Public Domain", "CC BY 4.0", "https://creativecommons.org/publicdomain/zero/1.0/"):
        assert reusable_license(license_name)


def test_real_song_gate_distinguishes_ambient_songs_from_noise():
    for title in ("Fractal Study -1", "Dark Ambient Music soundscape", "Meditation ambience", "Podcast music", "Speech", "SFX", "Noise composition", "Vastopia - Dark Ambient Music for Deep Relaxation and Focus"):
        assert not music_like(title, "", ())
    assert music_like("Soft ambient piano instrumental song", "", ())


def test_music_intent_ignores_debug_but_allows_actual_technical_topic():
    intent = build_music_intent(topic="Why winter breath is visible", script="PYTHONPATH=apps .venv/bin/python -c import json\nSELECT json FROM sqlite;\npytest git venv", content_type="factual_explainer")
    assert not set(intent.semantic_terms) & {"pythonpath", "apps", "venv", "python", "json", "sqlite", "pytest", "import"}
    technical = build_music_intent(topic="How does Python use SQLite?", content_type="factual_explainer")
    assert "python" in technical.semantic_terms and "sqlite" in technical.semantic_terms


def test_trends_only_boost_legal_suitable_songs():
    unknown = scored_track("unknown", "Soft Piano Instrumental Song")
    assert unknown.trend is None
    assert MusicTrend().popularity_score is None
    popular = replace(unknown, id="popular", trend=MusicTrend(popularity_score=0.95, trend_source="test-chart", trend_updated_at="2026-09-21", trend_confidence=1))
    bad = replace(popular, id="bad", title="Epic Battle Instrumental Song")
    illegal = replace(popular, id="illegal", license="All Rights Reserved")
    for seed in range(10):
        assert select_automatic_track(topic="winter breath", content_type="factual_explainer", catalog=(unknown, popular, bad, illegal), variation_seed=str(seed)) == popular


def test_old_soundscape_cache_is_revalidated(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "old.ogg").write_bytes(b"fixture")
    (cache / "catalog.json").write_text(json.dumps({"tracks": [{"id": "old", "title": "Fractal Study -1", "file": "cache/old.ogg", "mood": "documentary", "energy": "low", "source": "wikimedia", "license": "CC BY 4.0"}]}))
    assert discover_and_cache_track(topic="winter breath", content_type="factual_explainer", mood="documentary", library_root=tmp_path, providers=()) is None
    assert (cache / "old.ogg").exists()


def test_production_music_preparation_keeps_safe_unavailable_state_when_providers_fail(tmp_path, monkeypatch):
    archive = ProductionProvider("internet_archive", [])
    wikimedia = ProductionProvider("wikimedia", [])
    state = _requested_music_state()
    monkeypatch.setattr("clipforge.music.InternetArchiveMusicProvider", lambda: archive)
    monkeypatch.setattr("clipforge.music.WikimediaMusicProvider", lambda: wikimedia)

    assert attach_discovered_track(state, library_root=tmp_path) is None
    assert state["music"]["enabled"] is False
    assert state["music"]["status"] == "unavailable"
    assert state["music"]["source"] == "unavailable"
    assert state["music"]["providers_attempted"] == ["internet_archive", "wikimedia"]
    assert state["music"]["candidate_count"] == 0
