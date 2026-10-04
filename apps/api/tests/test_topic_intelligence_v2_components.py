"""Topic Intelligence V2 components: evergreen discovery, evidence hook, source mix, diversity, freshness."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import unquote

from sqlalchemy import select
from topic_support import NOW, settings

from clipforge.models import TopicCandidateRecord
from clipforge.topic_intelligence import evergreen, scoring, service
from clipforge.topic_intelligence.cache import CallMeter
from clipforge.topic_intelligence.candidate import (
    SIGNAL_NAMES,
    RawTopic,
    Signal,
    TopicCandidate,
    TopicGroup,
    candidate_id_for,
)
from clipforge.topic_intelligence.evidence import apply_evidence
from clipforge.topic_intelligence.service import DiscoveryDeps
from clipforge.topic_intelligence.signals import is_stale, opportunity, stamp, wikipedia_demand
from clipforge.topic_intelligence.sources import (
    DiscoveryContext,
    SourceFailed,
    YouTubeCompetitionProbe,
)


class FakePageviews:
    def __init__(self, *, fail: bool = False, level: int = 2_000, spike: int = 1) -> None:
        self.fail, self.level, self.spike = fail, level, spike
        self.calls: list[str] = []

    def __call__(self, url, params, headers):
        self.calls.append(unquote(url.split("/user/")[1].split("/daily/")[0]))
        if self.fail:
            raise SourceFailed("HTTP 403")
        return {"items": [{"views": self.level} for _ in range(58)] + [{"views": self.level * self.spike}] * 2}


def ctx(db, **kwargs) -> DiscoveryContext:
    return DiscoveryContext(db=db, settings=settings(), now=NOW, meter=CallMeter(quota_budget=400), **kwargs)


# --- evergreen discovery ----------------------------------------------------------------------------


def test_catalog_is_a_supply_of_questions_not_scores():
    assert len(evergreen.CATALOG) >= 60
    for subject in evergreen.CATALOG:
        assert subject.questions and all(question.endswith("?") for question in subject.questions)
        assert subject.wiki and subject.niche
    assert not hasattr(evergreen.CATALOG[0], "score")


def test_evergreen_source_is_bounded_cached_and_measures_real_demand(db):
    http = FakePageviews(level=2_500, spike=4)
    source = evergreen.EvergreenCatalogSource(http)
    result = source.discover(ctx(db))
    assert len(http.calls) == evergreen.SAMPLE_SIZE == result.report.calls
    assert result.report.status == "ok" and result.report.items == evergreen.SAMPLE_SIZE
    topic = result.topics[0]
    assert topic.kind == "evergreen" and topic.seed_questions
    assert topic.demand.available and topic.demand.evidence["median_views_per_day"] == 2_500
    assert topic.trend.available and topic.trend.evidence["ratio"] == 4.0
    assert topic.trend.evidence["fetched_at"] and topic.trend.evidence["ttl_hours"]
    # Cached for the day: a second refresh costs no request.
    again = source.discover(ctx(db))
    assert len(http.calls) == evergreen.SAMPLE_SIZE and again.report.status == "cached"


def test_evergreen_source_without_network_offers_subjects_without_evidence(db):
    http = FakePageviews(fail=True)
    result = evergreen.EvergreenCatalogSource(http).discover(ctx(db))
    assert result.report.status == "partial" and "pageview evidence unavailable" in result.report.error
    assert len(http.calls) == evergreen.MAX_CONSECUTIVE_FAILURES  # no request storm against a dead network
    assert len(result.topics) == evergreen.SAMPLE_SIZE
    assert all(topic.demand is None and topic.trend is None for topic in result.topics)  # never invented


def test_widening_takes_the_next_window():
    day = NOW - timedelta(days=1)
    _offset, first = evergreen.sample_window(day)
    _offset, wider = evergreen.sample_window(day, widen=True)
    assert not {item.subject for item in first} & {item.subject for item in wider}


def test_default_deps_include_evergreen_discovery(db):
    from clipforge.integrations import get_secret_store
    from clipforge.youtube.routes import get_youtube_provider

    deps = service.default_deps(db, settings(), get_secret_store(), get_youtube_provider())
    assert [source.name for source in deps.sources][-1] == evergreen.SOURCE_NAME
    assert deps.evidence_providers == []  # cold start: no analytics evidence


def test_evergreen_and_trending_sightings_of_one_subject_merge(db):
    trending = RawTopic(key="polarlicht", title="Polarlicht", source="wikipedia_pageviews", kind="article", observed_at=NOW,
                        trend=Signal(0.8, "medium", {}, ["wikipedia_pageviews"]))
    catalog = RawTopic(key="polarlicht", title="Polarlicht", source="editorial_evergreen", kind="evergreen", observed_at=NOW,
                       seed_questions=("Warum leuchten Polarlichter in verschiedenen Farben?",))
    groups = service.group_topics([trending, catalog])
    assert len(groups) == 1 and groups[0].evergreen and groups[0].seed_questions


def test_local_mode_serves_evergreen_seeds_and_reports_the_alternatives(db):
    http = FakePageviews(level=3_000, spike=1)
    deps = DiscoveryDeps(sources=[evergreen.EvergreenCatalogSource(http)], probe=YouTubeCompetitionProbe(None, None))
    result = service.suggestions(db, settings(), deps, count=3, now=NOW)
    assert result["status"] == "ok"
    for item in result["candidates"]:
        assert item["signal_class"] == "EVERGREEN" and item["signal_label"] == "Zeitlos"
        assert "Wikipedia-Aufrufe" in item["reason"]
    records = db.scalars(select(TopicCandidateRecord)).all()
    assert all(record.provenance["origin"] == "evergreen" and record.provenance["transformation"] == "evergreen_seed" for record in records)
    mars = next((record for record in records if record.topic == "Mars"), None)
    if mars is not None:
        assert mars.provenance["alternatives"]  # one topic, several possible questions


# --- signals, freshness, opportunity ---------------------------------------------------------------


def test_stale_evidence_is_detected_by_ttl():
    signal = stamp(Signal(0.9, "high", {}, ["brave_news_de"]), NOW - timedelta(hours=40), ttl_hours=36)
    assert is_stale(signal, NOW) and not is_stale(signal, NOW - timedelta(hours=10))


def test_demand_is_the_typical_level_not_the_spike():
    calm = wikipedia_demand([1_000] * 30)
    spiking = wikipedia_demand([1_000] * 28 + [50_000, 50_000])
    assert calm.value == spiking.value
    assert not wikipedia_demand([5, 5]).available


def test_opportunity_reads_supply_against_demand():
    def competition(**evidence):
        return Signal(0.5, "medium", {"sample": 12, **evidence}, ["youtube_search_competition"])

    strong_demand = Signal(0.7, "medium", {}, ["wikipedia_pageviews"])
    assert opportunity(competition(related=8, strong=4, near_identical=3), strong_demand).evidence["state"] == "saturated_generic"
    assert opportunity(competition(related=8, strong=4, near_identical=0), strong_demand).evidence["state"] == "specific_opportunity"
    assert opportunity(competition(related=0, strong=0, near_identical=0), Signal.unavailable("x")).evidence["state"] == "low_demand"
    assert not opportunity(Signal.unavailable("not_probed"), strong_demand).available


# --- evidence hook (future analytics) ---------------------------------------------------------------


def _candidate() -> TopicCandidate:
    return TopicCandidate(
        candidate_id=candidate_id_for("x"), topic="x", question="Warum ist x so?", rationale="", source_signals=[], discovered_at=NOW,
        language="de", region="DE", niche="wissenschaft", signals={name: Signal.unavailable("n") for name in SIGNAL_NAMES},
        freshness_at=NOW, provenance={},
    )


def test_evidence_providers_are_allow_listed_and_failure_isolated():
    class Retention:
        name = "retention"

        def signals(self, candidate):
            return {"own_performance": Signal(0.8, "medium", {"method": "retention_by_family"}, ["own_analytics"]),
                    "trend": Signal(1.0, "high", {}, ["own_analytics"])}  # may never create momentum

    class Broken:
        name = "broken"

        def signals(self, candidate):
            raise RuntimeError("analytics down")

    candidate = _candidate()
    applied = apply_evidence(candidate, [Broken(), Retention()])
    assert applied == ["retention:own_performance"]
    assert candidate.signals["own_performance"].value == 0.8
    assert not candidate.signals["trend"].available
    assert any("broken" in error for error in candidate.provenance["evidence_errors"])
    assert any("trend not allowed" in error for error in candidate.provenance["evidence_errors"])


def test_no_analytics_costs_a_cold_start_candidate_no_confidence():
    weights, version = scoring.resolve_weights(settings())
    signals = {"curiosity": Signal(0.8, "medium"), "payoff": Signal(0.8, "medium"), "demand": Signal(0.6, "medium")}
    without = _candidate()
    without.signals.update(signals)
    with_analytics = _candidate()
    with_analytics.signals.update({**signals, "own_performance": Signal(0.5, "high")})
    scoring.score_candidate(without, weights=weights, version=version, now=NOW)
    scoring.score_candidate(with_analytics, weights=weights, version=version, now=NOW)
    assert without.confidence == with_analytics.confidence and without.final_score == with_analytics.final_score


# --- curation order and diversity ---------------------------------------------------------------------


def _group(title: str, kind: str) -> TopicGroup:
    return TopicGroup(title, [RawTopic(key=title, title=title, source="s", kind=kind, observed_at=NOW)])  # type: ignore[arg-type]


def test_source_mix_keeps_both_classes_in_every_curator_batch():
    groups = [_group(f"e{index}", "evergreen") for index in range(20)] + [_group(f"l{index}", "news") for index in range(20)]
    mixed = service.mix_sources(groups, 10)
    first_batch = mixed[:10]
    assert sum(group.evergreen for group in first_batch) == 6 and len(mixed) == 40
    assert [group.title for group in mixed if group.evergreen] == [f"e{index}" for index in range(20)]  # order kept within a class


def test_diversity_prefers_another_subject_but_never_a_much_weaker_one():
    items = [
        scoring.RankedItem("a", 0.70, "weltraum", "why", "mars"),
        scoring.RankedItem("b", 0.69, "weltraum", "how", "mars"),
        scoring.RankedItem("c", 0.64, "natur_tiere", "why", "kraken"),
        scoring.RankedItem("d", 0.40, "geschichte", "why", "wikinger"),
    ]
    order = [item.candidate_id for item in scoring.diversify(items)]
    assert order[:2] == ["a", "c"]  # a second Mars question waits behind a different, slightly weaker subject
    assert order.index("d") == 3  # ... but a dramatically weaker one never jumps ahead
