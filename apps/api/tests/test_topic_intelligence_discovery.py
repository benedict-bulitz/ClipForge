"""Topic Intelligence: discovery, degradation, German targeting, cache/quota and Try another."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from topic_support import (
    NOW,
    FakeCurator,
    FakeWiki,
    FakeYouTube,
    curated,
    deps,
    settings,
    spike,
    video,
)

from clipforge.models import (
    Project,
    TopicCandidateRecord,
    TopicDiscoveryRun,
    TopicSourceCache,
    YouTubeConnection,
    YouTubeLearningArchive,
    YouTubeUpload,
)
from clipforge.topic_intelligence import semantic, service
from clipforge.topic_intelligence.scoring import SCORE_VERSION
from clipforge.topic_intelligence.sources import BraveNewsSource, WikipediaPageviewsSource


def youtube_with_chart() -> FakeYouTube:
    return FakeYouTube(
        popular={
            "27": [
                video("v1", "Warum fliegen Zugvögel im V? #shorts", "small", 50_000),
                video("v2", "Why do cats purr? Explained", "intl", 900_000, language="en"),
            ],
            "28": [video("v3", "Wie funktioniert ein Wasserstoffauto?", "big", 1_000_000)],
        },
        recent={
            "small": [video(f"s{i}", f"Kleines Video {i}", "small", 5_000) for i in range(10)],
            "big": [video(f"b{i}", f"Großes Video {i}", "big", 1_000_000) for i in range(10)],
        },
    )


def propose(db, *, youtube=None, wiki=None, now=NOW, config=None, **kwargs):
    return service.next_topic(db, config or settings(), deps(wiki=wiki, youtube=youtube, **kwargs), now=now)


def test_generate_next_video_discovers_scores_and_proposes_one_german_topic(db):
    result = propose(db, youtube=youtube_with_chart())

    assert result["status"] == "proposed"
    candidate = result["candidate"]
    assert candidate["question"].endswith("?")
    assert candidate["language"] == "de" and candidate["region"] == "DE"
    assert candidate["explanation"] and candidate["rationale"]
    assert candidate["score_version"] == SCORE_VERSION
    assert set(candidate["details"]["components"]) >= {"trend", "outlier", "competition", "novelty", "channel_fit", "own_performance"}
    assert db.get(TopicCandidateRecord, candidate["candidate_id"]).status == "proposed"
    run = db.scalar(select(TopicDiscoveryRun))
    assert run.status == "ok" and run.ranked_candidate_ids[0] == candidate["candidate_id"]


def test_multiple_independent_sources_feed_one_pool(db):
    propose(db, youtube=youtube_with_chart())
    records = db.scalars(select(TopicCandidateRecord)).all()
    sources = {item["source"] for record in records for item in record.source_signals}
    assert {"wikipedia_pageviews", "youtube_trending_de"} <= sources
    run = db.scalar(select(TopicDiscoveryRun))
    statuses = {item["name"]: item["status"] for item in run.sources}
    assert statuses["wikipedia_pageviews"] == "ok" and statuses["youtube_trending_de"] == "ok"
    assert statuses["brave_news_de"] == "skipped"  # not configured is not a failure


def test_outlier_relative_to_channel_beats_absolute_views(db):
    propose(db, youtube=youtube_with_chart())
    by_topic = {record.topic: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    small = by_topic["Warum fliegen Zugvögel im V?"].signals["outlier"]
    big = by_topic["Wie funktioniert ein Wasserstoffauto?"].signals["outlier"]
    assert small["value"] == 1.0 and small["evidence"]["ratio"] >= 9
    assert big["value"] == 0.0  # 1M views, but normal for that channel
    assert small["evidence"]["sample_size"] == 10


def test_german_dach_targeting_filters_foreign_videos_and_asks_for_germany(db):
    youtube = youtube_with_chart()
    propose(db, youtube=youtube)
    topics = {record.topic for record in db.scalars(select(TopicCandidateRecord)).all()}
    assert "Why do cats purr? Explained" not in topics
    chart_calls = [params for name, _units, params in youtube.calls if name == "videos.list(chart)"]
    assert chart_calls and all(params["regionCode"] == "DE" for params in chart_calls)
    search_calls = [params for name, _units, params in youtube.calls if name == "search.list"]
    assert search_calls and all(params["regionCode"] == "DE" and params["relevanceLanguage"] == "de" for params in search_calls)


def test_wikipedia_source_is_the_german_wikipedia_and_prefilters_people(db):
    wiki = FakeWiki()
    propose(db, wiki=wiki)
    assert all("de.wikipedia" in url for url in wiki.calls)
    topics = {record.topic for record in db.scalars(select(TopicCandidateRecord)).all()}
    assert "Max Mustermann" not in topics and "Hauptseite" not in topics
    assert "Schlafträgheit" in topics


def test_curated_questions_are_gated_with_the_main_ai_mode_still_local(db, monkeypatch):
    curator = FakeCurator({
        "Polarlicht": curated("Warum leuchtet der Himmel bei Polarlichtern grün?", "weltraum"),
        "Schlafträgheit": curated("Why do naps make me groggy?", "koerper_gesundheit"),
        "Deutschland": curated("Was ist Deutschland?", "geografie", clear_factual_payoff=2, issues=["unclear_payoff"]),
    })
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    config = settings(openai_api_key="sk-test")  # CLIPFORGE_AI_MODE stays "local"
    assert config.clipforge_ai_mode == "local"
    result = propose(db, config=config)
    assert result["candidate"]["question"] == "Warum leuchtet der Himmel bei Polarlichtern grün?"
    by_topic = {record.topic: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    assert "question_not_natural_german" in by_topic["Schlafträgheit"].rejection_reasons
    assert "semantic_unclear_payoff" in by_topic["Deutschland"].rejection_reasons
    assert len(curator.requests) == 1  # one combined curation call for this small pool


def _zugvoegel(db) -> TopicCandidateRecord:
    return next(record for record in db.scalars(select(TopicCandidateRecord)).all() if record.topic.startswith("Warum fliegen"))


def test_partial_source_failure_uses_remaining_sources_with_lower_confidence(db):
    propose(db, youtube=youtube_with_chart())
    healthy_confidence = _zugvoegel(db).confidence
    for table in (TopicCandidateRecord, TopicDiscoveryRun, TopicSourceCache):
        db.query(table).delete()
    db.commit()

    degraded = propose(db, youtube=youtube_with_chart(), wiki=FakeWiki(fail=True))

    assert degraded["status"] == "proposed"
    assert degraded["pool"]["status"] == "partial"
    statuses = {item["name"]: item["status"] for item in degraded["pool"]["sources"]}
    assert statuses["wikipedia_pageviews"] == "failed" and statuses["youtube_trending_de"] == "ok"
    assert all(record.score_breakdown["degraded_sources"] for record in db.scalars(select(TopicCandidateRecord)).all())
    order = ["unavailable", "low", "medium", "high"]
    assert healthy_confidence == "medium"
    assert order.index(_zugvoegel(db).confidence) < order.index(healthy_confidence)


def test_total_discovery_failure_is_reported_never_fabricated(db):
    result = propose(db, wiki=FakeWiki(fail=True))
    assert result == {
        "status": "unavailable",
        "message": "Topic discovery is temporarily unavailable.",
        "candidate": None,
        "pool": result["pool"],
    }
    assert db.scalar(select(func.count()).select_from(TopicCandidateRecord)) == 0
    assert db.scalar(select(TopicDiscoveryRun)).status == "unavailable"


def test_a_crashing_source_is_isolated(db):
    class Broken:
        name = "broken"

        def discover(self, ctx):
            raise RuntimeError("boom")

    discovery = deps()
    discovery.sources.append(Broken())
    result = service.next_topic(db, settings(), discovery, now=NOW)
    assert result["status"] == "proposed"
    assert {item["name"]: item["status"] for item in result["pool"]["sources"]}["broken"] == "failed"


def test_novelty_uses_recent_projects_and_learning_archive(db):
    db.add(Project(original_prompt="Warum fliegen Zugvögel in V-Formation?", title="Zugvögel"))
    db.add(YouTubeLearningArchive(project_id="gone", title="Wasserstoff", prompt="Wie funktioniert eigentlich ein Wasserstoffauto?"))
    db.commit()
    propose(db, youtube=youtube_with_chart())
    by_topic = {record.topic: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    assert "duplicate_of_previous_topic" in by_topic["Warum fliegen Zugvögel im V?"].rejection_reasons
    assert by_topic["Warum fliegen Zugvögel im V?"].signals["novelty"]["evidence"]["closest"]["kind"] == "project"
    assert "duplicate_of_previous_topic" in by_topic["Wie funktioniert ein Wasserstoffauto?"].rejection_reasons
    assert by_topic["Wie funktioniert ein Wasserstoffauto?"].signals["novelty"]["evidence"]["closest"]["kind"] == "archive"


def test_uploaded_video_titles_count_for_novelty(db):
    db.add(YouTubeUpload(project_id="p", project_revision=1, render_revision=1, render_sha256="x" * 64, render_file_size=1,
                         channel_id="c", title="Warum fliegen Zugvögel im V?"))
    db.commit()
    propose(db, youtube=youtube_with_chart())
    record = next(record for record in db.scalars(select(TopicCandidateRecord)).all() if record.topic.startswith("Warum fliegen"))
    assert record.signals["novelty"]["evidence"]["closest"]["kind"] == "upload"
    assert record.rejection_reasons


def test_missing_own_analytics_is_unavailable_and_neutral(db):
    db.add(YouTubeConnection(channel_id="mine", channel_title="Mein Kanal", granted_scopes=[]))
    db.commit()
    result = propose(db)
    own = result["candidate"]["details"]["components"]["own_performance"]
    assert own["confidence"] == "unavailable" and own["value"] is None and own["effective"] == 0.5
    record = db.get(TopicCandidateRecord, result["candidate"]["candidate_id"])
    assert record.signals["own_performance"]["evidence"]["reason"] == "insufficient_own_analytics"


def test_repeated_click_reuses_the_pool_and_proposes_the_same_topic(db):
    youtube = youtube_with_chart()
    wiki = FakeWiki()
    discovery = deps(wiki=wiki, youtube=youtube)
    first = service.next_topic(db, settings(), discovery, now=NOW)
    calls, units = len(wiki.calls), youtube.units
    second = service.next_topic(db, settings(), discovery, now=NOW + timedelta(minutes=5))
    assert second["candidate"]["candidate_id"] == first["candidate"]["candidate_id"]
    assert len(wiki.calls) == calls and youtube.units == units  # no external call at all
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == 1


def test_provider_cache_survives_an_expired_pool_and_expires_itself(db):
    youtube = youtube_with_chart()
    wiki = FakeWiki()
    discovery = deps(wiki=wiki, youtube=youtube)
    service.next_topic(db, settings(), discovery, now=NOW)
    calls, units = len(wiki.calls), youtube.units
    # Pool (45 min) expired, provider caches (2-24 h) still fresh: rescoring without new calls.
    service.next_topic(db, settings(), discovery, now=NOW + timedelta(hours=1))
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == 2
    assert len(wiki.calls) == calls and youtube.units == units
    # After the trend caches expire the sources are asked again (search stays cached for a day).
    service.next_topic(db, settings(), discovery, now=NOW + timedelta(hours=4))
    assert len(wiki.calls) > calls
    assert youtube.count("search.list") == 2
    status = service.discovery_status(db, now=NOW + timedelta(hours=4))
    assert {entry["provider"] for entry in status["caches"]} >= {"wikipedia_pageviews", "youtube_trending_de", "youtube_search_competition"}
    assert status["run"]["fresh"] is True


def test_youtube_quota_is_bounded_per_refresh(db):
    youtube = youtube_with_chart()
    propose(db, youtube=youtube, config=settings(topic_youtube_search_probes=2))
    assert youtube.count("search.list") <= 2
    assert youtube.units <= 400
    run = db.scalar(select(TopicDiscoveryRun))
    assert run.youtube_quota_units == youtube.units
    assert len(youtube.calls) < 20  # never hundreds of calls per click


def test_quota_budget_stops_expensive_calls_before_they_are_sent(db):
    youtube = youtube_with_chart()
    result = propose(db, youtube=youtube, config=settings(topic_youtube_quota_budget=50))
    assert youtube.count("search.list") == 0
    statuses = {item["name"]: item for item in result["pool"]["sources"]}
    assert statuses["youtube_search_competition"]["status"] == "failed"
    assert "budget" in statuses["youtube_search_competition"]["error"]
    assert result["status"] == "proposed"


def test_search_probes_can_be_disabled(db):
    youtube = youtube_with_chart()
    propose(db, youtube=youtube, config=settings(topic_youtube_search_probes=0))
    assert youtube.count("search.list") == 0


def test_try_another_returns_the_next_candidate_and_remembers_skips(db):
    discovery = deps(youtube=youtube_with_chart())
    first = service.next_topic(db, settings(), discovery, now=NOW)["candidate"]
    second = service.skip_topic(db, settings(), discovery, first["candidate_id"], now=NOW + timedelta(minutes=1))["candidate"]
    assert second["candidate_id"] != first["candidate_id"]
    assert db.get(TopicCandidateRecord, first["candidate_id"]).status == "skipped"
    again = service.next_topic(db, settings(), discovery, now=NOW + timedelta(minutes=2))["candidate"]
    assert again["candidate_id"] == second["candidate_id"]
    run = db.scalar(select(TopicDiscoveryRun))
    ranked = [candidate_id for candidate_id in run.ranked_candidate_ids if not db.get(TopicCandidateRecord, candidate_id).rejection_reasons]
    assert ranked.index(second["candidate_id"]) == ranked.index(first["candidate_id"]) + 1


def test_exhausted_pool_refreshes_discovery_then_reports_exhaustion(db):
    discovery = deps(wiki=FakeWiki(articles=[
        {"title": "Polarlicht", "views": 60_000, "description": "Leuchterscheinung", "extract": "Ein Polarlicht ist eine Leuchterscheinung am Himmel.", "history": spike(2_000, 24_000)},
    ]))
    first = service.next_topic(db, settings(), discovery, now=NOW)
    assert first["status"] == "proposed"
    runs_before = db.scalar(select(func.count()).select_from(TopicDiscoveryRun))
    result = service.skip_topic(db, settings(), discovery, first["candidate"]["candidate_id"], now=NOW + timedelta(minutes=1))
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) == runs_before + 1  # refreshed
    assert result["status"] == "exhausted" and result["candidate"] is None
    # the skipped topic is not proposed again within the skip memory
    later = service.next_topic(db, settings(), discovery, now=NOW + timedelta(hours=5))
    assert later["status"] == "exhausted"


def test_brave_news_source_targets_germany_when_configured(db):
    requests = []

    def brave_http(url, params, headers):
        requests.append(params)
        return {"results": [
            {"title": "Warum Zugvögel jetzt nach Süden fliegen", "description": "Forscher erklären den Vogelzug", "url": "https://a.de/1", "meta_url": {"hostname": "a.de"}},
            {"title": "Vogelzug: Warum Zugvögel nach Süden fliegen", "description": "", "url": "https://b.de/1", "meta_url": {"hostname": "b.de"}},
        ]}

    result = propose(db, brave=BraveNewsSource("brave-key", brave_http))
    assert result["status"] == "proposed"
    assert requests and all(params["country"] == "DE" and params["search_lang"] == "de" for params in requests)
    news = [record for record in db.scalars(select(TopicCandidateRecord)).all() if any(item["source"] == "brave_news_de" for item in record.source_signals)]
    assert news and news[0].signals["trend"]["evidence"]["outlets"] == 2


def test_wikipedia_waits_for_the_published_ranking(db):
    wiki = FakeWiki()
    served_missing = []

    def yesterday_not_published(url, params, headers):
        if "/top/" in url and not served_missing:
            served_missing.append(url)
            raise FileNotFoundError(url)
        return wiki(url, params, headers)

    discovery = deps()
    discovery.sources[0] = WikipediaPageviewsSource(yesterday_not_published)
    result = service.next_topic(db, settings(), discovery, now=NOW)
    assert result["status"] == "proposed"
    assert "/2026/09/29" in served_missing[0]
    assert any("/2026/09/28" in url for url in wiki.calls)
