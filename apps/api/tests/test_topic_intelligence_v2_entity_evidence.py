"""Topic Intelligence V2: entity evidence must not bleed across subjects.

Real Mac (ba863de): "Warum wird Honig praktisch nie schlecht?" (topic "Honig") received the
Wikipedia spike of the article "Honigfrauen" (7,124 vs 50 views/day, ratio 142.48 -> trend 1.0,
high) and was labelled EVERGREEN_WITH_CURRENT_INTEREST.  Cause: ``token_match`` scores a shorter
token contained in a longer one as a compound (0.8); with one token per title the Dice
equivalence is 0.8 >= 0.72, so ``same_subject("Honig", "Honigfrauen")`` grouped them and every
grouped sighting supplied evidence.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_topic_intelligence_local_questions import StaticSource, static_deps
from topic_support import NOW, FakeWiki, settings

from clipforge.models import TopicCandidateRecord
from clipforge.topic_intelligence import evergreen, service
from clipforge.topic_intelligence.cache import CallMeter
from clipforge.topic_intelligence.candidate import RawTopic, Signal, TopicGroup, same_entity
from clipforge.topic_intelligence.signals import (
    DEMAND_TTL_HOURS,
    EVIDENCE_TTL_HOURS,
    outlier_vs_channel,
    stamp,
    wikipedia_demand,
    wikipedia_trend,
)
from clipforge.topic_intelligence.sources import DiscoveryContext, WikipediaPageviewsSource
from clipforge.topic_intelligence.text import (
    canonical_article,
    question_equivalence,
    subject_equivalent,
    topic_key,
)

HONIG_Q = "Warum wird Honig praktisch nie schlecht?"
MICROWAVE_Q = "Warum bleibt Essen in der Mikrowelle in der Mitte kalt?"


def article(title: str, history: list[int]) -> RawTopic:
    """A Wikipedia top-list sighting exactly as WikipediaPageviewsSource builds it."""
    return RawTopic(
        key=topic_key(title), title=title, source="wikipedia_pageviews", kind="article", observed_at=NOW,
        entity=canonical_article(title),
        trend=stamp(wikipedia_trend(history), NOW, ttl_hours=EVIDENCE_TTL_HOURS["wikipedia_pageviews"]),
        demand=stamp(wikipedia_demand(history), NOW, ttl_hours=DEMAND_TTL_HOURS),
    )


def honig(history: list[int] | None = None) -> RawTopic:
    """The editorial evergreen subject, with its OWN article's pageviews (or none)."""
    history = history or []
    return RawTopic(
        key="honig", title="Honig", source=evergreen.SOURCE_NAME, kind="evergreen", observed_at=NOW,
        entity=canonical_article("Honig"), seed_questions=(HONIG_Q,),
        trend=stamp(wikipedia_trend(history), NOW, ttl_hours=72) if history else None,
        demand=stamp(wikipedia_demand(history), NOW, ttl_hours=DEMAND_TTL_HOURS) if history else None,
    )


HONIGFRAUEN_SPIKE = [50] * 28 + [7_124, 7_124]  # the Mac trace: ratio 142.48
HONIG_FLAT = [1_800] * 30


# --- identity rules -----------------------------------------------------------------------------


@pytest.mark.parametrize("short, compound", [
    ("Honig", "Honigfrauen"), ("Mars", "Marsriegel"), ("Mars", "Mars-Riegel"), ("Sonne", "Sonnencreme"),
    ("Berlin", "Berliner Zeitung"), ("Apple", "Apple TV+"), ("Schwarzes Loch", "Schwarzes Loch (Film)"),
    ("Eis", "Eisbär"), ("Wolke", "Wolkenkratzer"),
])
def test_a_short_noun_is_not_the_same_subject_as_a_longer_compound(short, compound):
    assert not subject_equivalent(short, compound) and not subject_equivalent(compound, short)
    left = RawTopic(key=topic_key(short), title=short, source="a", kind="video", observed_at=NOW)
    right = RawTopic(key=topic_key(compound), title=compound, source="b", kind="video", observed_at=NOW)
    assert not service.same_subject(left, right) and not same_entity(left, right)


@pytest.mark.parametrize("left, right", [
    ("Mikrowelle", "Mikrowellen"), ("Schwarzes Loch", "Schwarze Löcher"), ("Polarlicht", "Polarlichter"),
    ("Sonne", "Sonnen"), ("Hagelkorn", "Hagelkörner"), ("QR-Code", "QR Code"), ("QR-Code", "QR‑Code"), ("qr code", "QR-CODE"),
])
def test_inflections_and_punctuation_variants_are_the_same_subject(left, right):
    assert subject_equivalent(left, right)


def test_honig_and_honigfrauen_differ_and_the_old_rule_said_otherwise():
    assert question_equivalence("Honig", "Honigfrauen") == 0.8  # the c2f61b0/ba863de grouping rule (>= 0.72)
    assert not subject_equivalent("Honig", "Honigfrauen")
    assert not service.same_subject(honig(), article("Honigfrauen", HONIGFRAUEN_SPIKE))
    assert len(service.group_topics([article("Honigfrauen", HONIGFRAUEN_SPIKE), honig()])) == 2


# --- canonical Wikipedia identity --------------------------------------------------------------------


def test_wikipedia_sightings_keep_their_canonical_article_identity(db):
    wiki = FakeWiki([{"title": "Honigfrauen", "views": 200_000, "description": "Fernsehserie", "extract": "Honigfrauen ist eine Serie.",
                      "history": HONIGFRAUEN_SPIKE}])
    ctx = DiscoveryContext(db=db, settings=settings(), now=NOW, meter=CallMeter(quota_budget=400))
    topic = WikipediaPageviewsSource(wiki).discover(ctx).topics[0]
    assert topic.entity == "dewiki:honigfrauen" and topic.source_signal()["entity"] == "dewiki:honigfrauen"
    assert canonical_article("Mars_(Planet)") == canonical_article("Mars (Planet)") == "dewiki:mars (planet)"
    subjects = {subject.subject: subject for subject in evergreen.CATALOG}
    catalog = evergreen.EvergreenCatalogSource(lambda *a: {"items": [{"views": 1}] * 60}).topics([subjects["Honig"]], {}, NOW)[0]
    assert catalog.entity == canonical_article(subjects["Honig"].wiki) == "dewiki:honig"


def test_two_different_articles_never_merge_even_with_similar_titles():
    assert not service.same_subject(article("Mars (Planet)", HONIG_FLAT), article("Mars (Mythologie)", HONIG_FLAT))
    assert service.same_subject(article("Mars (Planet)", HONIG_FLAT), article("Mars_(Planet)", HONIG_FLAT))


# --- discovery vs evidence attribution ---------------------------------------------------------------


def test_discovery_grouping_does_not_automatically_become_evidence():
    # Even if something groups them (here: a hand-built group), only the anchor's own entity counts.
    group = TopicGroup("honig", [article("Honigfrauen", HONIGFRAUEN_SPIKE), honig(HONIG_FLAT)])
    assert group.anchor.title == "Honig"
    assert [item.title for item in group.evidence_sightings] == ["Honig"]
    assert group.excluded_evidence == [{"title": "Honigfrauen", "source": "wikipedia_pageviews", "reason": "different_entity"}]


def _pool(db, topics, *, now=NOW):
    service.suggestions(db, settings(), static_deps(StaticSource(topics)), count=3, now=now)
    return {record.question: record for record in db.scalars(select(TopicCandidateRecord)).all()}


def test_an_unrelated_pageview_spike_cannot_create_trend_or_current_interest(db):
    records = _pool(db, [article("Honigfrauen", HONIGFRAUEN_SPIKE), honig(HONIG_FLAT)])
    record = records[HONIG_Q]
    trend = record.signals["trend"]
    assert trend["evidence"].get("ratio") in (None, 1.0) and (trend["value"] or 0.0) == 0.0  # Honig's own flat views
    assert record.signals["demand"]["evidence"]["median_views_per_day"] == 1_800  # Honig's own level, not 7,124
    assert record.score_breakdown["signal_class"] == "EVERGREEN"
    serialized = service.serialize_candidate(record, NOW)
    assert serialized["signal_label"] == "Zeitlos" and "Wikipedia-Aufrufe zum Thema" not in serialized["reason"]
    assert "Honigfrauen" not in {item["title"] for item in record.source_signals}  # not even grouped any more


def test_without_its_own_evidence_honig_claims_nothing_current(db):
    # The Mac case: Honig's own pageview request was rate-limited (no history) - the other article
    # must not fill the gap.
    record = _pool(db, [article("Honigfrauen", HONIGFRAUEN_SPIKE), honig()])[HONIG_Q]
    assert record.signals["trend"]["confidence"] == "unavailable"
    assert record.score_breakdown["signal_class"] == "EVERGREEN"  # editorial evergreen, no current-interest claim


def test_the_current_interest_label_disappears_with_its_evidence(db):
    spiking = _pool(db, [honig([1_000] * 28 + [6_000, 6_000])])[HONIG_Q]
    assert spiking.score_breakdown["signal_class"] == "EVERGREEN_WITH_CURRENT_INTEREST"
    assert "rund 6× so viele Wikipedia-Aufrufe zum Thema wie üblich" in service.serialize_candidate(spiking, NOW)["reason"]
    later = NOW + timedelta(days=4)  # the spike is past its TTL in the reused record ...
    assert service.serialize_candidate(spiking, later)["signal_class"] == "EVERGREEN"
    calm = _pool(db, [honig(HONIG_FLAT)], now=NOW + timedelta(hours=2))  # ... and a new pool without a spike
    assert calm[HONIG_Q].score_breakdown["signal_class"] == "EVERGREEN"


def test_legitimate_microwave_chart_evidence_still_attaches(db):
    chart = RawTopic(
        key=topic_key(MICROWAVE_Q), title=MICROWAVE_Q, source="youtube_trending_de", kind="video", observed_at=NOW,
        trend=stamp(Signal(0.6, "medium", {"method": "youtube_most_popular_de", "rank": 5}, ["youtube_trending_de"]), NOW, ttl_hours=24),
        outlier=stamp(outlier_vs_channel(12_000, [1_000] * 12, video={"title": MICROWAVE_Q, "channel": "Kanal M"}), NOW, ttl_hours=24),
    )
    plural = RawTopic(key=topic_key(MICROWAVE_Q.replace("Mikrowelle", "Mikrowellen")), title=MICROWAVE_Q.replace("Mikrowelle ", "Mikrowellen "),
                      source="brave_news_de", kind="news", observed_at=NOW)
    record = _pool(db, [chart, plural, article("Mikrowellenherd", HONIG_FLAT)])[MICROWAVE_Q]
    assert record.signals["outlier"]["value"] == 1.0 and record.signals["outlier"]["evidence"]["video"]["channel"] == "Kanal M"
    assert {item["source"] for item in record.source_signals if item["attributed"]} == {"youtube_trending_de", "brave_news_de"}
    assert "ein Video mit genau dieser Frage ist gerade in den deutschen YouTube-Charts" in service.serialize_candidate(record, NOW)["reason"]


def test_user_facing_multiples_are_readable():
    assert service.times(142.48, "Wikipedia-Aufrufe", "üblich") == "deutlich mehr Wikipedia-Aufrufe als üblich"
    assert service.times(3.2, "Wikipedia-Aufrufe", "üblich") == "rund 3× so viele Wikipedia-Aufrufe wie üblich"
    assert "." not in service.times(2.49, "Aufrufe", "üblich")
