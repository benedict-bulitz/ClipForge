"""Topic Intelligence V2 calibration: fair bounded evaluation, outlier safety, literal why-now, partial sources.

Real Mac (c2f61b0): raw_groups=71 budget=60 evaluated=20 target=9 accepted=9 remaining=51 - the
curator loop stopped the moment 9 topics were accepted, so the suggestions were the best of the
FIRST acceptable batches, not of the bounded pool.  Two of the top 3 also showed outlier=1.0 and
"ein Video dazu ist gerade in den deutschen YouTube-Charts": an evergreen subject ("QR-Code",
"Mikrowelle") had absorbed a chart video that merely contained the word ("Code", "Mikrowelle").
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from test_topic_intelligence_local_questions import StaticSource, raw, static_deps
from topic_support import NOW, FakeCurator, FakeYouTube, bad, settings, video

from clipforge.models import TopicCandidateRecord, TopicDiscoveryRun, TopicSourceCache
from clipforge.topic_intelligence import evergreen, semantic, service
from clipforge.topic_intelligence.cache import RATE_LIMITED_TTL, CallMeter
from clipforge.topic_intelligence.candidate import RawTopic, Signal
from clipforge.topic_intelligence.signals import outlier_vs_channel
from clipforge.topic_intelligence.sources import (
    DiscoveryContext,
    RateLimited,
    WikipediaPageviewsSource,
    YouTubeTrendingSource,
)
from clipforge.topic_intelligence.text import topic_key

KEY = {"openai_api_key": "sk-test"}
SOLID = bad(curiosity_strength=7, payoff_specificity=7)
EXCELLENT = bad(curiosity_strength=9, payoff_specificity=9, reveal_potential=9, concreteness=9, curiosity_gap=9)
THINGS = ("Schnee", "Eis", "Laub", "Holz", "Glas", "Papier", "Kies", "Sand", "Leder", "Stoff", "Gras", "Draht", "Seil",
          "Kerze", "Plastik", "Beton", "Asphalt", "Kork", "Wolle", "Torf", "Stein", "Metall", "Gummi", "Wachs", "Lehm")
LATE_STAR = "Warum klingt ein Gewitter in den Bergen anders als im Flachland?"


def quiet_questions(count: int) -> list[str]:
    return [f"Warum knistert {thing} bei Kälte?" for thing in THINGS[:count]]


def run(db, monkeypatch, topics, judged=None, config=None):
    curator = FakeCurator(by_question=judged or {}, default=SOLID)
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    result = service.suggestions(db, config or settings(**KEY), static_deps(StaticSource(topics)), count=3, now=NOW)
    run_record = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
    return result, service._evaluation_detail(run_record), curator


def sent(curator) -> list[str]:
    return [item.get("local_question") or item["topic"] for request in curator.requests for item in request]


# --- 1-3. early stop / representative shortlist / bounded stopping rule ----------------------------


def test_reaching_the_target_does_not_stop_before_fair_coverage(db, monkeypatch):
    topics = [raw(question, trend=0.6) for question in quiet_questions(25)]
    result, detail, curator = run(db, monkeypatch, topics)
    # c2f61b0 stopped after the first batch that reached 9 accepted (10 of 25 here).
    assert len(sent(curator)) >= service.MIN_EVALUATION_COVERAGE
    assert detail["stopped_by"] in {"enough_supply_coverage_and_no_competitive_candidate", "raw_pool_exhausted", "ai_budget_or_deadline"}
    assert detail["ai_requests"] <= service.AI_REQUEST_BUDGET  # bounded: never more requests than before
    assert result["status"] == "ok"


def test_a_high_potential_late_candidate_can_enter_the_final_shortlist(db, monkeypatch):
    # 24 acceptable topics come first; the strongest topic is the LAST raw topic and its cheap
    # pre-score is only slightly lower - the old rule never looked at it.
    topics = [raw(question, trend=0.6) for question in quiet_questions(24)] + [raw(LATE_STAR, trend=0.5)]
    result, detail, curator = run(db, monkeypatch, topics, judged={LATE_STAR: EXCELLENT})
    assert LATE_STAR in sent(curator)
    assert result["candidates"][0]["question"] == LATE_STAR
    assert detail["stop_rule"]["competitive_margin"] == service.COMPETITIVE_MARGIN


def test_evaluation_stops_once_nothing_competitive_remains(db, monkeypatch):
    # Strong supply, then only clearly weaker topics: no extra request is spent on them.
    strong = [raw(question, trend=0.95) for question in quiet_questions(20)]
    weak = [raw(f"Was ist {thing}?", trend=0.0) for thing in ("Kalk", "Zink", "Teer", "Ruß", "Moos")]
    _result, detail, curator = run(db, monkeypatch, strong + weak)
    assert detail["stopped_by"] == "enough_supply_coverage_and_no_competitive_candidate"
    # Two requests (coverage reached), then the remaining clearly weaker topics are not paid for.
    assert detail["ai_requests"] == 2 and len(sent(curator)) == service.MIN_EVALUATION_COVERAGE
    assert sum(question.startswith("Was ist") for question in sent(curator)) <= 1


def test_cheap_pre_ranking_does_not_let_raw_views_dominate():
    def group(title: str, *, views: int, outlier: Signal | None = None):
        sighting = RawTopic(key=topic_key(title), title=title, source="youtube_trending_de", kind="video", observed_at=NOW,
                            trend=Signal(0.5, "medium", {}, ["youtube_trending_de"]), outlier=outlier, metrics={"views": views})
        return service.group_topics([sighting])[0]

    giant = service._curation_priority(group("Warum knistert Schnee bei Kälte?", views=50_000_000), [], NOW)[0]
    small = service._curation_priority(group("Warum knistert Schnee bei Kälte?", views=5_000), [], NOW)[0]
    assert giant == small  # raw views are not a pre-score input at all
    relative = service._curation_priority(group("Warum knistert Laub bei Kälte?", views=5_000,
                                                outlier=outlier_vs_channel(10_000, [1_000] * 12)), [], NOW)[0]
    assert relative > giant  # a channel-relative outlier is evidence; size is not


PARTS = ("platte", "korn", "stueck", "bahn")


def _groups(prefix: str, count: int, *, source: str, kind: str, trend: float) -> list:
    """``count`` distinct subjects ("Schneeplatte", "Eiskorn", ...), one raw topic each."""
    words = [f"{prefix}{thing}{part}" for part in PARTS for thing in THINGS][:count]
    groups = service.group_topics([raw(f"Warum knistert {word} bei Kälte?", trend=trend, kind=kind, source=source) for word in words])
    assert len(groups) == count
    return groups


def test_source_diversity_prevents_first_source_monopolization(db, monkeypatch):
    youtube = [raw(question, trend=0.9) for question in quiet_questions(25)]
    wiki = [raw(question, trend=0.4, kind="article", source="wikipedia_pageviews")
            for question in ("Warum leuchtet Phosphor im Dunkeln?", "Warum rostet Eisen schneller am Meer?")]
    _result, detail, curator = run(db, monkeypatch, youtube + wiki)
    assert {"Warum leuchtet Phosphor im Dunkeln?", "Warum rostet Eisen schneller am Meer?"} <= set(sent(curator))
    assert detail["evaluated_sources"]["wikipedia_pageviews"] == 2
    assert detail["shortlist"]["shortlist_sources"]["wikipedia_pageviews"] == 2


def test_a_source_seed_never_pushes_stronger_topics_out_of_the_first_batch():
    strong = _groups("", 12, source="youtube_trending_de", kind="video", trend=0.9)
    seed = service.group_topics([raw("Warum leuchtet Phosphor im Dunkeln?", trend=0.1, kind="article", source="wikipedia_pageviews")])
    groups = strong + seed
    priorities = {group.key: service._curation_priority(group, [], NOW) for group in groups}
    order, report = service.representative_order(groups, priorities, slots=30)
    assert seed[0] in order[: report["shortlist"]]  # represented ...
    assert seed[0] not in order[:10]  # ... but not ahead of stronger topics


def test_evergreen_and_live_coverage_stays_bounded():
    def evergreen_group(index: int):
        sighting = RawTopic(key=f"subject{index}", title=f"Thema{index}", source="editorial_evergreen", kind="evergreen",
                            observed_at=NOW, seed_questions=(f"Warum knistert Thema{index} bei Kälte?",))
        return service.group_topics([sighting])[0]

    groups = [evergreen_group(index) for index in range(40)] + _groups("", 40, source="brave_news_de", kind="news", trend=0.2)
    priorities = {group.key: service._curation_priority(group, [], NOW) for group in groups}
    order, report = service.representative_order(groups, priorities, slots=30)
    shortlist = order[: report["shortlist"]]
    assert sum(group.evergreen for group in shortlist) <= round(30 * service.SOURCE_MIX_SHARE)
    assert sum(not group.evergreen for group in shortlist) >= 30 - round(30 * service.SOURCE_MIX_SHARE)


def test_duplicate_clusters_do_not_consume_the_semantic_budget(db, monkeypatch):
    mars = RawTopic(key="mars", title="Mars", source="editorial_evergreen", kind="evergreen", observed_at=NOW,
                    seed_questions=("Warum ist der Mars rot?",))
    paraphrase = raw("Was macht den Mars eigentlich rot?", trend=0.9)  # another source, the same video
    topics = [mars, paraphrase] + [raw(question, trend=0.6) for question in quiet_questions(12)]
    _result, detail, curator = run(db, monkeypatch, topics)
    mars_questions = [question for question in sent(curator) if "Mars" in question]
    assert len(mars_questions) == 1
    assert detail["shortlist"]["deferred_duplicates"] and detail["unevaluated"] == {"cheap_duplicate": 1}


# --- 4. outlier safety -----------------------------------------------------------------------------


def test_tiny_samples_cannot_produce_a_perfect_outlier():
    tiny = outlier_vs_channel(50_000, [500, 600, 400])  # 100x, but only 3 comparisons
    assert tiny.confidence == "low" and tiny.value <= 0.6 and tiny.evidence["caps"]["sample_size"] == 0.6
    few = outlier_vs_channel(50_000, [500, 600, 400, 550, 450, 500])
    assert few.value <= 0.85


def test_a_near_zero_channel_baseline_cannot_explode_to_a_false_outlier():
    dead_channel = outlier_vs_channel(800, [1, 2, 1, 3, 1, 2, 1, 1, 2, 1])  # 800x of ~1 view/day
    assert not dead_channel.available and dead_channel.evidence["reason"] == "channel_baseline_too_small"


def test_a_video_within_its_channels_normal_spread_is_not_a_perfect_outlier():
    volatile = [200, 5_000, 800, 12_000, 300, 9_000, 1_500, 20_000, 600, 7_000]
    capped = outlier_vs_channel(10_000, volatile)
    assert capped.evidence["robust_z"] < 2 and capped.value <= 0.5


def test_a_legitimate_strong_outlier_still_reaches_one():
    strong = outlier_vs_channel(15_000, [900, 1_000, 1_100, 950, 1_050, 1_000, 980, 1_020, 1_000, 990],
                                video={"title": "Warum funktioniert ein QR-Code trotz Kratzern?", "channel": "Kanal X", "views": 90_000, "age_days": 6})
    assert strong.value == 1.0 and strong.confidence == "high" and not strong.evidence["caps"]
    assert strong.evidence["video"]["channel"] == "Kanal X"  # traceable: which video, which channel


# --- 5. literal why-now ----------------------------------------------------------------------------


def test_a_lexically_related_chart_video_no_longer_joins_an_evergreen_subject():
    qr = RawTopic(key="qr code", title="QR-Code", source="editorial_evergreen", kind="evergreen", observed_at=NOW,
                  seed_questions=("Warum funktioniert ein QR-Code auch dann noch, wenn er zerkratzt ist?",))
    chart = RawTopic(key=topic_key("Dieser Code knackt jedes Passwort"), title="Dieser Code knackt jedes Passwort",
                     source="youtube_trending_de", kind="video", observed_at=NOW, trend=Signal(0.7, "medium", {}, ["youtube_trending_de"]))
    microwave = RawTopic(key="mikrowelle", title="Mikrowelle", source="editorial_evergreen", kind="evergreen", observed_at=NOW,
                         seed_questions=("Warum bleibt Essen in der Mikrowelle in der Mitte kalt?",))
    foil = RawTopic(key=topic_key("Was passiert, wenn man Alufolie in die Mikrowelle legt?"), title="Was passiert, wenn man Alufolie in die Mikrowelle legt?",
                    source="youtube_trending_de", kind="video", observed_at=NOW, trend=Signal(0.7, "medium", {}, ["youtube_trending_de"]))
    groups = service.group_topics([chart, foil, qr, microwave])
    assert len(groups) == 4  # c2f61b0 merged both pairs (one shared token = similarity 1.0)


def test_chart_wording_states_exactly_what_the_evidence_shows():
    trend = {"value": 0.6, "confidence": "medium", "evidence": {"method": "youtube_most_popular_de"}}
    signals = {"trend": trend, "curiosity": {"value": 0.8, "confidence": "medium"}, "payoff": {"value": 0.8, "confidence": "medium"}}
    question = "Warum funktioniert ein QR-Code auch dann noch, wenn er zerkratzt ist?"
    same = service.user_reason({}, signals, "EMERGING", question=question, source_signals=[
        {"source": "youtube_trending_de", "title": "Warum funktioniert ein QR-Code noch, wenn er zerkratzt ist?"}])
    related = service.user_reason({}, signals, "EMERGING", question=question, source_signals=[
        {"source": "youtube_trending_de", "title": "QR-Codes erklärt: So speichern sie Daten"}])
    assert "genau dieser Frage" in same
    assert "„QR-Codes erklärt: So speichern sie Daten“ zum selben Thema" in related and "genau" not in related
    # No chart sighting at all: no chart claim, whatever the trend method says.
    assert "Charts" not in service.user_reason({}, signals, "EMERGING", question=question, source_signals=[])


# --- 6. partial source failures stay isolated ----------------------------------------------------


def _ctx(db) -> DiscoveryContext:
    return DiscoveryContext(db=db, settings=settings(), now=NOW, meter=CallMeter(quota_budget=400))


def test_evergreen_stops_at_the_first_429_and_caches_only_briefly(db):
    calls: list[str] = []

    def http(url, params, headers):
        calls.append(url)
        if len(calls) > 2:
            raise RateLimited("HTTP 429")
        return {"items": [{"views": 1_000}] * 60}

    result = evergreen.EvergreenCatalogSource(http).discover(_ctx(db))
    assert len(calls) == 3  # 2 measured, the 429, then no further request (no retry storm)
    assert result.report.status == "partial" and "429" in result.report.error
    measured = [topic for topic in result.topics if topic.demand is not None]
    assert len(measured) == 2 and all(topic.demand is None for topic in result.topics if topic not in measured)
    entry = db.scalars(select(TopicSourceCache).where(TopicSourceCache.provider == evergreen.SOURCE_NAME)).one()
    assert entry.expires_at.replace(tzinfo=NOW.tzinfo) - NOW <= RATE_LIMITED_TTL


def test_wikipedia_history_requests_stop_at_a_429(db):
    from topic_support import FakeWiki

    wiki = FakeWiki()
    history_calls: list[str] = []

    def http(url, params, headers):
        if "/per-article/" in url:
            history_calls.append(url)
            raise RateLimited("HTTP 429")
        return wiki(url, params, headers)

    result = WikipediaPageviewsSource(http).discover(_ctx(db))
    assert len(history_calls) == 1 and result.topics  # topics still come; no trend is invented for them
    assert all(not (topic.trend and topic.trend.available) for topic in result.topics)


def test_a_missing_chart_category_is_partial_and_fabricates_nothing(db):
    youtube = FakeYouTube(popular={"28": [video("v1", "Warum ist Glas durchsichtig?", "c1", 50_000)]}, missing_categories={"27"})
    result = YouTubeTrendingSource(youtube, lambda: "token").discover(_ctx(db))
    assert result.report.status == "partial" and "not_found" in result.report.error
    assert [topic.title for topic in result.topics] == ["Warum ist Glas durchsichtig?"]
    # No general-chart fallback: only the knowledge categories are ever requested.
    assert {params["category"] for name, _units, params in youtube.calls if name == "videos.list(chart)"} == {"27", "28"}
    assert not result.topics[0].outlier.available  # no channel uploads known: no outlier claimed


def test_one_failing_source_never_breaks_the_pool(db, monkeypatch):
    class Broken:
        name = "broken_source"

        def discover(self, ctx):
            raise RateLimited("HTTP 429")

    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator(default=SOLID))
    deps = static_deps(StaticSource([raw(question, trend=0.6) for question in quiet_questions(12)]))
    deps.sources.insert(0, Broken())
    result = service.suggestions(db, settings(**KEY), deps, count=3, now=NOW + timedelta(minutes=1))
    assert result["status"] == "ok" and len(result["candidates"]) == 3
    sources = {item["name"]: item for item in db.scalar(select(TopicDiscoveryRun)).sources}
    assert sources["broken_source"]["status"] == "failed"
    assert db.scalars(select(TopicCandidateRecord)).first() is not None
