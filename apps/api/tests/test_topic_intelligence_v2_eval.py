"""Topic Intelligence V2: deterministic offline evaluation harness (ti-score-v7, semantic-curator-v3).

One fixture holds every candidate type the V2 spec names; it runs through the REAL
pipeline (sources -> grouping -> curation -> gates -> scoring -> dedupe -> diversity ->
Full Auto) with a scripted curator and time-stamped evidence.  The assertions are the
properties V2 must prove, plus regression fixtures for the weak V1 behaviour found in
the audit (docs/topic-intelligence-v2.md, Part 1).  No network, no real model.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_topic_intelligence_local_questions import StaticSource, static_deps
from topic_support import NOW, FakeCurator, bad, settings

from clipforge.models import Project, TopicCandidateRecord
from clipforge.schemas import ProjectCreate
from clipforge.topic_intelligence import scoring, semantic, service
from clipforge.topic_intelligence.candidate import RawTopic, Signal
from clipforge.topic_intelligence.service import DiscoveryDeps
from clipforge.topic_intelligence.signals import (
    DEMAND_TTL_HOURS,
    EVIDENCE_TTL_HOURS,
    outlier_vs_channel,
    stamp,
    trending_chart_trend,
    wikipedia_trend,
)
from clipforge.topic_intelligence.text import question_equivalence, similarity, topic_key

KEY = {"openai_api_key": "sk-test"}

# --- curator judgements (0-10) -------------------------------------------------------------------
EXCELLENT = bad(curiosity_strength=9, payoff_specificity=9, reveal_potential=9, concreteness=9, visual_potential=9, curiosity_gap=9)
STRONG = bad(curiosity_strength=8, payoff_specificity=8, reveal_potential=8, concreteness=8, curiosity_gap=8)
SOLID = bad(curiosity_strength=7, payoff_specificity=7, reveal_potential=7)
VALID_BUT_BORING = bad(**{name: 6 for name in (
    "self_contained_clarity", "clear_factual_payoff", "universal_12plus_relevance", "prior_knowledge_free", "natural_spoken_german",
    "knowledge_short_fit", "curiosity_gap", "visual_potential", "dach_relevance", "curiosity_strength", "payoff_specificity",
    "reveal_potential", "concreteness")})

# --- the fixture questions ---------------------------------------------------------------------------
EVERGREEN = "Warum kann man sich nicht selbst kitzeln?"          # excellent evergreen curiosity question
CURRENT = "Warum sind Polarlichter gerade bis nach Bayern zu sehen?"  # strong, fresh corroborated evidence
STALE_TREND = "Warum ist die Hitzewelle in diesem Sommer so extrem?"  # news-only, evidence 5 days old
HUGE_VIEWS = "Wie entsteht eigentlich ein Gewitter?"              # chart #1, millions of views, a normal day for its giant channel
OUTLIER = "Wie können Hagelkörner so groß wie Tennisbälle werden?"  # 10x its channel's normal level
DUPLICATE = "Woher hat der Rote Planet seine Farbe?"               # = MARS in other words, from an unrelated raw topic
MARS = "Warum ist der Mars rot?"
BROAD = "Wie funktioniert eigentlich die Natur?"                  # broad, no concrete payoff
TRIVIAL = "Ist ein Eiswürfel eigentlich kalt?"                     # trivial answer
LOW_VISUAL = "Warum hat es so lange gedauert, bis die Null erfunden wurde?"  # hard to visualize, still valid
SPECULATIVE = "Gibt es auf anderen Planeten intelligentes Leben?"  # no settled answer
SATURATED = "Warum ist der Himmel blau?"                           # many near-identical strong Shorts exist
SPECIFIC = "Warum gibt es auf Hawaii Vulkane mitten auf einer Erdplatte?"  # demand + no identical Short
NO_EVIDENCE = "Warum schwimmt ein Schiff aus Stahl?"                # same quality as EVERGREEN, no evidence at all
CLICKBAIT = "Was passiert mit deinem Körper, wenn du nie wieder schläfst?"  # curiosity without payoff
DRY_FACT = "Wie viele Knochen hat der menschliche Körper?"          # payoff without curiosity

JUDGED = {
    EVERGREEN: {**EXCELLENT, "niche": "koerper_gesundheit", "subject": "Kitzeln", "aspect": "selbst kitzeln"},
    # Timely by nature: it only makes sense while the solar storm is news - allowed while the evidence is fresh.
    CURRENT: {**STRONG, "niche": "weltraum", "subject": "Polarlicht", "aspect": "Sichtbarkeit Bayern", "issues": ["current_event_only"]},
    STALE_TREND: {**STRONG, "niche": "wetter_klima", "issues": ["current_event_only"]},
    HUGE_VIEWS: {**SOLID, "niche": "wetter_klima", "subject": "Gewitter", "aspect": "Entstehung"},
    OUTLIER: {**SOLID, "niche": "wetter_klima", "subject": "Hagel", "aspect": "Größe"},
    MARS: {**STRONG, "niche": "weltraum", "subject": "Mars", "aspect": "rote Farbe"},
    DUPLICATE: {**STRONG, "niche": "weltraum", "subject": "Mars", "aspect": "rote Farbe"},
    BROAD: bad(curiosity_strength=7, payoff_specificity=3, clear_factual_payoff=4, issues=["broad_overview"]),
    TRIVIAL: bad(curiosity_strength=5, payoff_specificity=7, issues=["trivial_answer"]),
    LOW_VISUAL: {**STRONG, "visual_potential": 3, "niche": "geschichte"},
    SPECULATIVE: bad(curiosity_strength=9, payoff_specificity=3, issues=["speculation_dependent"]),
    SATURATED: {**SOLID, "niche": "wetter_klima"},
    SPECIFIC: {**SOLID, "niche": "natur_tiere"},
    NO_EVIDENCE: {**EXCELLENT, "niche": "wissenschaft"},
    CLICKBAIT: bad(curiosity_strength=9, payoff_specificity=3, clear_factual_payoff=5, reveal_potential=4),
    DRY_FACT: bad(curiosity_strength=4, payoff_specificity=9, reveal_potential=3),
}


def fresh(signal: Signal, source: str, *, age_hours: float = 2.0) -> Signal:
    return stamp(signal, NOW - timedelta(hours=age_hours), ttl_hours=EVIDENCE_TTL_HOURS[source])  # type: ignore[return-value]


def demand(value: float, *, source: str = "wikipedia_pageviews") -> Signal:
    signal = Signal(value, "medium", {"method": "wikipedia_median_daily_views", "median_views_per_day": 2500}, [source])
    return stamp(signal, NOW - timedelta(hours=2), ttl_hours=DEMAND_TTL_HOURS)  # type: ignore[return-value]


def sighting(title: str, *, source: str, kind: str, trend: Signal | None = None, outlier: Signal | None = None,
             demand_signal: Signal | None = None, metrics: dict | None = None, seeds: tuple[str, ...] = ()) -> RawTopic:
    return RawTopic(
        key=topic_key(title), title=title, source=source, kind=kind, observed_at=NOW, description="",  # type: ignore[arg-type]
        trend=trend, outlier=outlier, demand=demand_signal, metrics=metrics or {}, seed_questions=seeds,
    )


def wiki_trend(ratio_days: int, *, age_hours: float = 2.0) -> Signal:
    return fresh(wikipedia_trend([800] * 28 + [800 * ratio_days] * 2), "wikipedia_pageviews", age_hours=age_hours)


def fixture_topics() -> list[RawTopic]:
    chart = "youtube_trending_de"
    return [
        sighting("Kitzeln", source="editorial_evergreen", kind="evergreen", demand_signal=demand(0.62), seeds=(EVERGREEN,)),
        sighting(CURRENT, source="wikipedia_pageviews", kind="article", trend=wiki_trend(6), demand_signal=demand(0.55)),
        sighting(CURRENT, source="brave_news_de", kind="news",
                 trend=fresh(Signal(0.75, "medium", {"method": "brave_news_de_last_day", "outlets": 4, "articles": 6}, ["brave_news_de"]), "brave_news_de")),
        sighting(STALE_TREND, source="brave_news_de", kind="news",
                 trend=fresh(Signal(0.9, "medium", {"method": "brave_news_de_last_day", "outlets": 5, "articles": 9}, ["brave_news_de"]), "brave_news_de", age_hours=120)),
        sighting(HUGE_VIEWS, source=chart, kind="video", trend=fresh(trending_chart_trend(1, 25, category="Bildung"), chart),
                 outlier=fresh(outlier_vs_channel(400_000, [400_000] * 12), chart), metrics={"views": 5_000_000, "rank": 1}),
        sighting(OUTLIER, source=chart, kind="video", trend=fresh(trending_chart_trend(20, 25, category="Bildung"), chart),
                 outlier=fresh(outlier_vs_channel(10_000, [1_000] * 12), chart), metrics={"views": 50_000, "rank": 20}),
        sighting(MARS, source=chart, kind="video", trend=fresh(trending_chart_trend(8, 25, category="Bildung"), chart)),
        sighting("Neue Bilder vom Roten Planeten", source="brave_news_de", kind="news",
                 trend=fresh(Signal(0.6, "low", {"method": "brave_news_de_last_day", "outlets": 1, "articles": 1}, ["brave_news_de"]), "brave_news_de")),
        sighting(BROAD, source=chart, kind="video", trend=fresh(trending_chart_trend(3, 25, category="Bildung"), chart)),
        sighting(TRIVIAL, source=chart, kind="video", trend=fresh(trending_chart_trend(2, 25, category="Bildung"), chart)),
        sighting(LOW_VISUAL, source="wikipedia_pageviews", kind="article", trend=wiki_trend(2), demand_signal=demand(0.5)),
        sighting(SPECULATIVE, source=chart, kind="video", trend=fresh(trending_chart_trend(4, 25, category="Bildung"), chart)),
        sighting(SATURATED, source="wikipedia_pageviews", kind="article", demand_signal=demand(0.6)),
        sighting(SPECIFIC, source="wikipedia_pageviews", kind="article", demand_signal=demand(0.6)),
        sighting(NO_EVIDENCE, source="static_de", kind="video"),
        sighting(CLICKBAIT, source=chart, kind="video", trend=fresh(trending_chart_trend(5, 25, category="Bildung"), chart)),
        sighting(DRY_FACT, source=chart, kind="video", trend=fresh(trending_chart_trend(6, 25, category="Bildung"), chart)),
    ]


class StubProbe:
    """YouTube search competition probe with scripted results per question (no quota, no network)."""

    name = "youtube_search_competition"
    available = True

    def __init__(self, results: dict[str, list[dict]]) -> None:
        self.results = results
        self.queries: list[str] = []

    def probe(self, ctx, question: str, topic: str):
        self.queries.append(question)
        return {"query": question, "videos": self.results.get(question, [])}, False


def short(title: str, views: int, *, days: int = 20) -> dict:
    return {"title": title, "views": views, "published_at": (NOW - timedelta(days=days)).isoformat(), "channel_views": 0, "channel_videos": 0}


PROBE_RESULTS = {
    SATURATED: [short(title, 900_000) for title in (
        "Warum ist der Himmel blau?", "Warum ist der Himmel eigentlich blau? 🤯", "Darum ist der Himmel blau",
        "Warum ist der Himmel blau? #shorts", "Himmel blau – warum?", "Warum ist unser Himmel blau?",
    )] + [short(f"Wetter Fakt {index}", 2_000) for index in range(6)],
    SPECIFIC: [short(title, 120_000) for title in (
        "Vulkanausbruch live erklärt", "Wie entsteht ein Vulkan?", "Die gefährlichsten Vulkane der Welt", "Vulkan unter Wasser",
        "Hawaii Lava Strand", "Vulkane in Deutschland", "Supervulkan Yellowstone", "Lava trifft Meer",
    )] + [short(f"Natur Fakt {index}", 3_000) for index in range(4)],
}


def deps_for(topics: list[RawTopic], probe: StubProbe | None = None) -> DiscoveryDeps:
    base = static_deps(StaticSource(topics))
    return DiscoveryDeps(sources=base.sources, probe=probe or base.probe)


def curator() -> FakeCurator:
    # The paraphrase comes from an unrelated headline: only semantic dedupe can catch it.
    return FakeCurator({"Neue Bilder vom Roten Planeten": {"question": DUPLICATE}}, by_question=JUDGED)


@pytest.fixture
def evaluated(db, monkeypatch):
    """The full fixture through the real pipeline with the curator; returns (suggestions, records by question)."""
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator())
    probe = StubProbe(PROBE_RESULTS)
    config = settings(**KEY, topic_youtube_search_probes=20)
    result = service.suggestions(db, config, deps_for(fixture_topics(), probe), count=3, now=NOW)
    records = {record.question: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    return result, records, config, probe


def served(result) -> list[str]:
    return [item["question"] for item in result["candidates"]]


# --- the properties V2 must prove -----------------------------------------------------------------


def test_strong_relevant_candidates_outrank_irrelevant_ones(evaluated):
    result, records, _config, _probe = evaluated
    top = served(result)
    assert len(top) == 3 and result["status"] == "ok"
    weak = {STALE_TREND, BROAD, TRIVIAL, SPECULATIVE, CLICKBAIT, DRY_FACT, DUPLICATE}
    assert not set(top) & weak
    assert EVERGREEN in top and CURRENT in top
    usable = [record for record in records.values() if not record.rejection_reasons]
    assert {record.question for record in usable} >= {EVERGREEN, CURRENT, OUTLIER, MARS, LOW_VISUAL, SPECIFIC, SATURATED, NO_EVIDENCE}
    assert all(records[question].rejection_reasons for question in weak)


def test_hard_eligibility_removes_weak_candidates_with_explicit_reasons(evaluated):
    _result, records, _config, _probe = evaluated
    assert "stale_current_event" in records[STALE_TREND].rejection_reasons
    assert "weak_payoff" in records[BROAD].rejection_reasons
    assert "semantic_trivial_answer" in records[TRIVIAL].rejection_reasons
    assert "semantic_speculation_dependent" in records[SPECULATIVE].rejection_reasons
    # Curiosity without payoff is clickbait; payoff without curiosity is a boring short: both removed.
    assert "weak_payoff" in records[CLICKBAIT].rejection_reasons
    assert "low_curiosity" in records[DRY_FACT].rejection_reasons


def test_stale_evidence_cannot_remain_trending(evaluated, db):
    _result, records, _config, _probe = evaluated
    stale = records[STALE_TREND]
    assert stale.score_breakdown["components"]["trend"].get("stale") is True
    assert stale.score_breakdown["signal_class"] is None
    current = records[CURRENT]
    assert current.score_breakdown["signal_class"] == "TRENDING"
    # The same pool served four days later: the momentum is past its TTL - the label is gone.
    later = service.serialize_candidate(current, NOW + timedelta(days=4))
    assert later["signal_class"] is None and later["signal_label"] is None
    assert service.serialize_candidate(current, NOW)["signal_label"] == "Trend"


def test_raw_views_do_not_dominate(evaluated):
    _result, records, _config, _probe = evaluated
    giant, outlier = records[HUGE_VIEWS], records[OUTLIER]
    # Same judgement; chart #1 with 5M views on a normal day loses to 50k views at 10x the channel's level.
    assert outlier.final_score > giant.final_score
    assert outlier.signals["outlier"]["evidence"]["ratio"] == 10.0
    assert giant.signals["outlier"]["value"] == 0.0
    # Raw view counts are never a score input.
    for component in giant.score_breakdown["components"].values():
        assert component["value"] is None or component["value"] <= 1.0


def test_duplicates_collapse_into_the_stronger_one(evaluated):
    _result, records, _config, _probe = evaluated
    duplicate = records[DUPLICATE]
    assert "duplicate_in_pool" in duplicate.rejection_reasons
    assert duplicate.provenance["duplicate_of"]["question"] == MARS
    assert duplicate.provenance["duplicate_of"]["equivalence"] >= 0.72
    assert duplicate.topic != records[MARS].topic  # two different raw topics, one video
    assert not records[MARS].rejection_reasons


def test_three_suggestions_are_meaningfully_distinct(evaluated):
    result, records, _config, _probe = evaluated
    top = served(result)
    for index, left in enumerate(top):
        for right in top[index + 1:]:
            assert question_equivalence(left, right) < 0.5
    families = {records[question].provenance["topic_family"] for question in top}
    assert len(families) == 3


def test_opportunity_separates_a_saturated_generic_angle_from_a_specific_one(evaluated):
    _result, records, _config, probe = evaluated
    saturated, specific = records[SATURATED], records[SPECIFIC]
    assert saturated.signals["opportunity"]["evidence"]["state"] == "saturated_generic"
    assert specific.signals["opportunity"]["evidence"]["state"] == "specific_opportunity"
    assert specific.final_score > saturated.final_score
    # Supply is read as demand too: many related Shorts are not "bad".
    assert specific.signals["demand"]["confidence"] in {"medium", "high"}
    assert SATURATED in probe.queries and SPECIFIC in probe.queries


def test_missing_evidence_is_not_positive_evidence(evaluated):
    _result, records, _config, _probe = evaluated
    backed, missing = records[EVERGREEN], records[NO_EVIDENCE]
    # Identical (excellent) judgement: the candidate with real demand evidence ranks higher ...
    assert backed.final_score > missing.final_score
    assert missing.score_breakdown["components"]["demand"]["effective"] == 0.0
    # ... and the one without any evidence makes no time or evergreen claim.
    assert missing.score_breakdown["signal_class"] is None


def test_low_visual_potential_ranks_lower_but_is_not_rejected(evaluated):
    _result, records, _config, _probe = evaluated
    low = records[LOW_VISUAL]
    assert not low.rejection_reasons
    assert low.signals["visual"]["value"] < 0.5
    assert low.score_breakdown["components"]["visual"]["effective"] < records[CURRENT].score_breakdown["components"]["visual"]["effective"]


def test_unsupported_trend_labels_never_appear(evaluated):
    result, records, _config, _probe = evaluated
    for item in result["candidates"]:
        record = records[item["question"]]
        if item["signal_class"] in {"TRENDING", "EMERGING", "TIMELY", "EVERGREEN_WITH_CURRENT_INTEREST"}:
            trend = Signal.from_dict(record.signals["trend"])
            assert trend.available and trend.evidence.get("fetched_at")
            age = (NOW - service._utc(__import__("datetime").datetime.fromisoformat(trend.evidence["fetched_at"]))).total_seconds() / 3600
            assert age <= trend.evidence["ttl_hours"]
        if item["signal_class"] in {"EVERGREEN", "EVERGREEN_WITH_CURRENT_INTEREST"}:
            assert record.score_breakdown["signal_class_basis"]["evergreen_evidence"] in {"editorial_catalog", "stable_demand"}
    # Every user-facing suggestion explains itself without numbers from the ranking.
    assert all(item["reason"] and "score" not in item["reason"] for item in result["candidates"])


def test_full_auto_selects_the_strongest_eligible_question(evaluated, db):
    _result, records, config, probe = evaluated
    auto = service.auto_topic(db, config, deps_for(fixture_topics(), probe), now=NOW + timedelta(minutes=1))
    assert auto["status"] == "selected"
    winner = auto["candidate"]["question"]
    assert winner in {EVERGREEN, CURRENT}
    assert auto["selection_reason"].startswith("score ")
    record = records[winner]
    assert record.provenance["auto_selection"]["thresholds"]["min_score"] == scoring.AUTO_MIN_SCORE


def test_cold_start_without_channel_analytics(evaluated, db):
    result, records, _config, _probe = evaluated
    # No connected channel, no uploads: own performance is unavailable for every candidate ...
    assert all(record.signals["own_performance"]["confidence"] == "unavailable" for record in records.values())
    # ... and costs nothing: three strong suggestions with high confidence exist anyway.
    assert len(result["candidates"]) == 3
    assert {item["confidence"] for item in result["candidates"]} <= {"medium", "high"}


# --- weak pools, Full Auto minimum, manual mode ---------------------------------------------------


def test_poor_questions_do_not_win_because_the_pool_is_weak(db, monkeypatch):
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator())
    weak = [topic for topic in fixture_topics() if topic.title in {BROAD, TRIVIAL, SPECULATIVE, CLICKBAIT, DRY_FACT, STALE_TREND}]
    result = service.suggestions(db, settings(**KEY), deps_for(weak), count=3, now=NOW)
    assert result["candidates"] == [] and result["status"] == "exhausted"
    auto = service.auto_topic(db, settings(**KEY), deps_for(weak), now=NOW + timedelta(minutes=1))
    assert auto["status"] == "no_strong_candidate" and auto["candidate"] is None
    assert auto["widened"] is True  # it tried once more before giving up - and did not pick garbage


def test_full_auto_respects_its_minimum_quality(db, monkeypatch):
    # Valid suggestion (passes every gate), but not strong enough to be picked without a human.
    acceptable = "Warum knistert Laub im Herbst unter den Füßen?"
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator(by_question={acceptable: SOLID}))
    topics = [sighting(acceptable, source="static_de", kind="video")]
    chips = service.suggestions(db, settings(**KEY), deps_for(topics), count=3, now=NOW)
    assert [item["question"] for item in chips["candidates"]] == [acceptable]
    auto = service.auto_topic(db, settings(**KEY), deps_for(topics), now=NOW + timedelta(minutes=1))
    assert auto["status"] == "no_strong_candidate"
    reasons = auto["considered"][0]["reasons"]
    assert "below_auto_min_score" in reasons or "auto_curiosity_too_low" in reasons


def test_full_auto_without_ai_or_network_uses_strong_evergreen_fallback_only(db):
    # No key, no live evidence: a well-formed trend title is not enough; an editorial evergreen question is.
    topics = [
        sighting("Kitzeln", source="editorial_evergreen", kind="evergreen", seeds=(EVERGREEN,)),
        sighting("Warum sinken Preise im Winter?", source="static_de", kind="video",
                 trend=fresh(trending_chart_trend(1, 25, category="Bildung"), "youtube_trending_de")),
    ]
    auto = service.auto_topic(db, settings(), deps_for(topics), now=NOW)
    assert auto["status"] == "selected" and auto["candidate"]["question"] == EVERGREEN
    assert auto["candidate"]["confidence"] == "low" and auto["candidate"]["signal_class"] == "EVERGREEN"


def test_manual_mode_is_untouched(db, monkeypatch):
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator())
    service.suggestions(db, settings(**KEY), deps_for(fixture_topics()), count=3, now=NOW)
    payload = service.resolve_topic_provenance(db, ProjectCreate(prompt="Warum ist Schnee weiß?"), now=NOW)
    assert payload.topic_source == "manual" and payload.topic_provenance == {"topic_source": "manual"}
    assert payload.prompt == "Warum ist Schnee weiß?"
    # Topic Intelligence never creates a project or starts generation by itself.
    assert db.scalar(select(func.count()).select_from(Project)) == 0


# --- regression fixtures: the weak V1 behaviour found in the audit --------------------------------


def test_regression_technically_valid_but_boring_is_no_longer_accepted(db, monkeypatch):
    # V1 (ti-score-v6) accepted a 6/10-everywhere question: every gate passed.
    boring = "Wie funktioniert eigentlich ein Kühlschrank?"
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator(by_question={boring: VALID_BUT_BORING}))
    result = service.suggestions(db, settings(**KEY), deps_for([sighting(boring, source="static_de", kind="video")]), count=3, now=NOW)
    assert result["candidates"] == []
    record = db.scalar(select(TopicCandidateRecord))
    assert record.rejection_reasons == ["low_curiosity"]
    assert record.score_breakdown["quality"]["short_worthiness"]["value"] >= scoring.SHORT_WORTHINESS_FLOOR  # V1's only gate


def test_regression_curiosity_and_payoff_carry_the_score():
    # V1: curiosity_strength 3.3 %, payoff_specificity 2.6 %, accessibility 12 % of the score.
    weights = scoring.DEFAULT_WEIGHTS
    assert weights["curiosity"] + weights["payoff"] >= 0.25
    assert weights["curiosity"] > 2 * weights["accessibility"]
    # No single LLM answer is counted five times any more: superseded V1 echoes weigh 0.
    assert weights["suitability"] == weights["semantic"] == 0.0


def test_regression_top_list_rank_is_not_momentum():
    # V1 turned a place in the Wikipedia top list (no history) into "trend".
    assert not wikipedia_trend([], rank=3, top_size=60).available
    # A YouTube chart place is moderate momentum, not 0.55-0.9 "demand".
    assert trending_chart_trend(1, 25, category="Bildung").value <= 0.7


def test_regression_dedupe_neither_over_collapses_nor_misses_paraphrases():
    # V1 overlap coefficient: 1.0 for distinct questions, 0.0 for the same question in other words.
    assert similarity("Warum schlafen wir?", "Warum träumen wir im Schlaf?") == 1.0
    assert question_equivalence("Warum schlafen wir?", "Warum träumen wir im Schlaf?") < 0.72
    assert question_equivalence("Warum gähnen wir?", "Ist Gähnen ansteckend?") < 0.72
    assert similarity("Warum ist der Mars rot?", "Woher hat der Rote Planet seine Farbe?") == 0.0
    assert question_equivalence("Warum ist der Mars rot?", "Woher hat der Rote Planet seine Farbe?") >= 0.72
    assert question_equivalence(MARS, DUPLICATE) >= 0.72
    assert question_equivalence(MARS, "Warum gibt es auf dem Mars so gewaltige Staubstürme?") < 0.72


def test_regression_a_used_subject_returns_with_another_question_after_its_cooldown(db):
    seeds = ("Warum ist der Mars rot?", "Warum gibt es auf dem Mars so gewaltige Staubstürme?")
    topics = [sighting("Mars", source="editorial_evergreen", kind="evergreen", seeds=seeds)]
    first = service.suggestions(db, settings(), deps_for(topics), count=1, now=NOW)
    used = db.get(TopicCandidateRecord, first["candidates"][0]["candidate_id"])
    db.add(Project(title=used.question, original_prompt=used.question))
    used.status, used.selected_at = "used", NOW
    db.commit()
    soon = service.suggestions(db, settings(), deps_for(topics), count=1, now=NOW + timedelta(hours=1))
    assert soon["candidates"] == []  # the subject rests during its cool-down
    later = NOW + service.USED_TOPIC_COOLDOWN + timedelta(days=1)
    back = service.suggestions(db, settings(), deps_for(topics), count=1, now=later)
    questions = [item["question"] for item in back["candidates"]]
    assert questions and questions[0] != used.question and questions[0] in seeds  # never the same question again


def test_regression_news_of_the_day_without_fresh_evidence_is_not_served(db, monkeypatch):
    # V1 attributed a trend to a reframed news question indefinitely (48 h half-life, no TTL).
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", FakeCurator(by_question={STALE_TREND: STRONG}))
    stale = [topic for topic in fixture_topics() if topic.title == STALE_TREND]
    result = service.suggestions(db, settings(**KEY), deps_for(stale), count=3, now=NOW)
    assert result["candidates"] == []
    assert "stale_current_event" in db.scalar(select(TopicCandidateRecord)).rejection_reasons
