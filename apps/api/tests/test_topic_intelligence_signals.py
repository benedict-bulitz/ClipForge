"""Topic Intelligence: pure signals, German text rules, transformation and the scoring authority."""
from __future__ import annotations

import pytest
from topic_support import NOW, FakeCurator, curate_groups, curated, settings, spike

from clipforge.topic_intelligence import scoring, transform
from clipforge.topic_intelligence.candidate import (
    SIGNAL_NAMES,
    RawTopic,
    Signal,
    TopicCandidate,
    TopicGroup,
    candidate_id_for,
)
from clipforge.topic_intelligence.history import HistoryItem, is_duplicate, novelty_signal
from clipforge.topic_intelligence.signals import (
    competition_estimate,
    merge_trend,
    outlier_vs_channel,
    wikipedia_trend,
)
from clipforge.topic_intelligence.text import (
    classify_niche,
    content_tokens,
    question_issues,
    similarity,
    topic_key,
)

# --- German similarity / novelty -------------------------------------------------


def test_semantic_near_duplicates_are_recognized_without_exact_strings():
    assert similarity("Warum werden wir im Auto müde?", "Wieso macht Autofahren als Beifahrer müde?") >= 0.72
    assert similarity("Warum macht ein Nickerchen müde?", "Wieso bin ich nach dem Mittagsschlaf so schläfrig?") >= 0.72
    # related but different questions stay below the duplicate threshold
    assert similarity("Warum ist der Himmel blau?", "Warum ist das Meer blau?") < 0.72
    assert similarity("Warum ist der Himmel blau?", "Wie entstehen Polarlichter?") < 0.3


def test_topic_key_is_order_and_umlaut_independent():
    assert topic_key("Müdigkeit im Auto") == topic_key("auto MUEDIGKEIT")
    assert "mued" in content_tokens("Müdigkeit")


def test_recent_project_novelty_rejects_semantic_duplicate():
    history = [HistoryItem("Warum werden wir im Auto müde?", "project", "p1", NOW)]
    signal = novelty_signal("Wieso macht Autofahren als Beifahrer müde?", "Autofahren", "alltag_phaenomene", history, now=NOW)
    assert is_duplicate(signal)
    assert signal.value is not None and signal.value < 0.3
    assert signal.evidence["closest"]["kind"] == "project"


def test_archived_video_novelty_counts_like_a_project():
    history = [HistoryItem("Warum haben wir plötzlich Heißhunger auf Süßes?", "archive", "p9", NOW)]
    signal = novelty_signal("Wieso bekommen wir plötzlich Lust auf Süßigkeiten?", "Heißhunger", "essen_trinken", history, now=NOW)
    assert signal.evidence["closest"]["kind"] == "archive"
    assert signal.value is not None and signal.value < 0.5


def test_unrelated_history_keeps_high_novelty_and_empty_history_is_confident():
    history = [HistoryItem("Warum ist der Mond manchmal rot?", "upload", "u1", NOW)]
    related = novelty_signal("Wie entstehen Polarlichter?", "Polarlicht", "weltraum", history, now=NOW)
    empty = novelty_signal("Wie entstehen Polarlichter?", "Polarlicht", "weltraum", [], now=NOW)
    assert related.value is not None and related.value > 0.7 and not is_duplicate(related)
    assert empty.value == 1.0 and empty.confidence == "high"


def test_repeating_a_previous_answer_is_penalized():
    history = [HistoryItem("Polarlichter entstehen, wenn Sonnenwind Sauerstoff in der Atmosphäre anregt.", "answer", "p1", NOW)]
    signal = novelty_signal("Warum regt Sonnenwind den Sauerstoff an?", "Sonnenwind", "weltraum", history, now=NOW)
    assert "repeats_previous_answer" in signal.evidence
    assert signal.value is not None and signal.value <= 0.75


# --- Trend / outlier / competition -----------------------------------------------


def test_wikipedia_trend_is_relative_to_the_articles_own_baseline():
    spiking = wikipedia_trend(spike(400, 5_000))
    flat_but_huge = wikipedia_trend(spike(80_000, 80_000))
    assert spiking.value is not None and spiking.value > 0.9
    assert flat_but_huge.value == 0.0
    assert spiking.evidence["ratio"] == pytest.approx(12.5)
    assert wikipedia_trend([], rank=None).confidence == "unavailable"


def test_outlier_is_relative_to_channel_not_absolute_views():
    small_channel = outlier_vs_channel(10_000, [1_000] * 10)  # 10x its own normal
    big_channel = outlier_vs_channel(200_000, [200_000] * 10)  # far more views, but normal for it
    assert small_channel.value == 1.0 and small_channel.confidence == "high"
    assert big_channel.value == 0.0
    assert small_channel.evidence["sample_size"] == 10
    assert small_channel.evidence["method"] == "views_per_day_vs_channel_recent_median"


def test_outlier_needs_a_sample_and_reports_confidence_by_size():
    assert outlier_vs_channel(5_000, [100, 200]).confidence == "unavailable"
    assert outlier_vs_channel(5_000, [100, 200, 300]).confidence == "low"
    assert outlier_vs_channel(5_000, [100] * 6).confidence == "medium"
    # robust: one viral upload in the baseline does not hide the outlier
    assert outlier_vs_channel(10_000, [1_000] * 9 + [5_000_000]).value == 1.0


def test_competition_estimate_counts_related_established_videos():
    crowded = [
        {"title": f"Warum fliegen Zugvögel im V? Teil {index}", "views": 400_000, "channel_views": 10_000_000, "channel_videos": 200}
        for index in range(12)
    ]
    open_field = [{"title": "Die besten Rezepte für Kürbissuppe", "views": 1_000} for _ in range(12)]
    saturated, outlier = competition_estimate("Warum fliegen Zugvögel im V?", "Zugvögel", crowded)
    empty, _ = competition_estimate("Warum fliegen Zugvögel im V?", "Zugvögel", open_field)
    assert saturated.value is not None and saturated.value > 0.8
    assert saturated.evidence["estimate"] is True and saturated.evidence["strong"] == 12
    assert empty.value == 0.0
    assert outlier.available and outlier.confidence == "low"  # lifetime-mean fallback is weak evidence


def test_independent_sources_agreeing_raise_trend_confidence():
    wiki = Signal(0.7, "low", {"ratio": 3}, ["wikipedia_pageviews"])
    news = Signal(0.5, "low", {"outlets": 2}, ["brave_news_de"])
    merged = merge_trend([wiki, news])
    assert merged.value == 0.7 and merged.confidence == "medium"
    assert merged.sources == ["brave_news_de", "wikipedia_pageviews"]


# --- Niche / German question checks ---------------------------------------------


def test_niche_classification_prefers_knowledge_topics_and_flags_poor_fit():
    assert classify_niche("Warum fliegen Zugvögel im V?")[0] == "natur_tiere"
    assert classify_niche("Schlafträgheit nach dem Mittagsschlaf")[0] == "koerper_gesundheit"
    assert classify_niche("Bundesliga: FC Bayern gewinnt Topspiel")[0] == "sport"
    assert classify_niche("xyz")[0] == "unknown"


@pytest.mark.parametrize(
    ("question", "issue"),
    [
        ("Warum ist der Himmel blau, weil Licht gestreut wird?", "answer_embedded"),
        ("Why is the sky blue in Germany today?", "not_natural_german"),
        ("Unglaublich: Warum passiert DAS WIRKLICH?", "clickbait"),
        ("Der Himmel ist blau", "not_a_question"),
        ("Warum leben Igel 25 Jahre?", "unsupported_number"),
    ],
)
def test_question_rules(question, issue):
    assert issue in question_issues(question, evidence="Igel leben einige Jahre")


def test_good_german_question_passes():
    assert question_issues("Warum fühle ich mich nach einem kurzen Mittagsschlaf manchmal schlechter als vorher?") == []


# --- Topic -> German question ------------------------------------------------------


def _group(title: str, kind: str = "article", description: str = "", flags: set[str] | None = None) -> TopicGroup:
    raw = RawTopic(topic_key(title), title, "wikipedia_pageviews" if kind == "article" else "youtube_trending_de", kind, NOW, description, flags=flags or set())
    return TopicGroup(raw.key, [raw])


def test_curator_turns_a_raw_trend_into_a_natural_german_question(db):
    curator = FakeCurator({"Schlafträgheit": curated(
        "Warum fühle ich mich nach einem kurzen Mittagsschlaf manchmal schlechter als vorher?", "koerper_gesundheit")})
    results, outcome = curate_groups(db, [_group("Schlafträgheit", description="Benommenheit nach dem Aufwachen")], curator)
    assert outcome.requests == 1 and outcome.statuses == {topic_key("Schlafträgheit"): "curated"}
    assert results[0].question.startswith("Warum fühle ich mich")
    assert results[0].issues == [] and results[0].assessment["curiosity_gap"] == 0.8
    assert results[0].semantic is not None and results[0].semantic.evidence["status"] == "curated"
    request = curator.requests[0]
    assert request[0]["topic"] == "Schlafträgheit" and request[0]["evidence"][0]["text"].startswith("Benommenheit")


def test_curator_output_is_still_validated_deterministically(db):
    curator = FakeCurator({
        "Polarlicht": curated("Warum sieht man Polarlichter 300 Kilometer weiter südlich?", "weltraum"),
        "Blue Moon": curated("Why is it called a blue moon?", "weltraum"),
    })
    results, _ = curate_groups(db, [_group("Polarlicht", description="Leuchterscheinung"), _group("Blue Moon")], curator)
    assert "unsupported_number" in results[0].issues  # invented premise: number not in evidence
    assert "not_natural_german" in results[1].issues


def test_curator_failure_falls_back_to_the_deterministic_path(db):
    results, outcome = curate_groups(db, [_group("Polarlicht")], FakeCurator(fail=True))
    assert outcome.errors and "OpenAIError" in outcome.errors[0]
    assert outcome.statuses == {topic_key("Polarlicht"): "failed"}
    # No generic "Was steckt eigentlich hinter X?" fallback for a bare title.
    assert results[0].question == "" and results[0].issues == ["no_question_transformation"]


def test_deterministic_path_keeps_real_source_questions_and_never_templates_headlines():
    video = _group("🔥 Warum fliegen Zugvögel im V? #shorts", kind="video")
    headline = _group("Forscher messen neuen Rekord", kind="news")
    person = _group("Max Mustermann", flags={"person"})
    results = [transform.deterministic_transform(group) for group in (video, headline, person)]
    assert results[0].question == "Warum fliegen Zugvögel im V?" and results[0].method == "source_question"
    assert results[1].method == "none" and results[1].issues == ["no_question_transformation"]
    assert "person_centric" in results[2].flags


# --- Scoring authority -----------------------------------------------------------


def _candidate(**signals: Signal) -> TopicCandidate:
    base = {name: Signal.unavailable("not_measured") for name in SIGNAL_NAMES}
    base.update(signals)
    return TopicCandidate(
        candidate_id=candidate_id_for("x"), topic="x", question="Warum ist x so?", rationale="", source_signals=[],
        discovered_at=NOW, language="de", region="DE", niche="wissenschaft", signals=base, freshness_at=NOW, provenance={},
    )


def _score(candidate: TopicCandidate, **kwargs) -> TopicCandidate:
    weights, version = scoring.resolve_weights(settings())
    return scoring.score_candidate(candidate, weights=weights, version=version, now=NOW, **kwargs)


def test_missing_data_is_neutral_not_zero():
    nothing = _score(_candidate())
    assert nothing.final_score == pytest.approx(0.5)
    assert nothing.confidence == "low"
    assert all(item["effective"] == 0.5 for item in nothing.score_breakdown["components"].values())
    assert nothing.score_breakdown["components"]["own_performance"]["confidence"] == "unavailable"


def test_no_own_analytics_does_not_hurt_a_strong_candidate():
    strong = {
        "trend": Signal(0.9, "high", sources=["wikipedia_pageviews", "youtube_trending_de"]), "novelty": Signal(1.0, "high"),
        "channel_fit": Signal(0.9, "medium"), "suitability": Signal(0.85, "medium"), "visual": Signal(0.8, "medium"),
        "researchability": Signal(0.85, "medium"), "broad_appeal": Signal(0.85, "medium"),
        "accessibility": Signal(0.9, "medium"), "question_form": Signal(0.8, "high"),
    }
    without = _score(_candidate(**strong))
    with_neutral_own = _score(_candidate(**strong, own_performance=Signal(0.5, "high")))
    assert without.final_score == pytest.approx(with_neutral_own.final_score)
    assert without.final_score > 0.7 and not without.rejected


def test_score_breakdown_is_complete_and_adds_up():
    candidate = _score(_candidate(trend=Signal(0.8, "high"), competition=Signal(0.2, "medium"), novelty=Signal(0.9, "high")))
    components = candidate.score_breakdown["components"]
    assert sum(item["weight"] for item in components.values()) == pytest.approx(1.0)
    assert sum(item["contribution"] for item in components.values()) == pytest.approx(candidate.final_score, abs=1e-3)
    assert components["competition"]["directional_value"] == pytest.approx(0.8)  # openness = 1 - saturation
    assert candidate.score_breakdown["version"] == scoring.SCORE_VERSION


def test_low_confidence_evidence_is_shrunk_towards_neutral():
    high = _score(_candidate(trend=Signal(1.0, "high")))
    low = _score(_candidate(trend=Signal(1.0, "low")))
    assert high.final_score > low.final_score > 0.5


def test_exceptional_demand_softens_the_competition_penalty():
    calm = _score(_candidate(trend=Signal(0.5, "high"), competition=Signal(0.9, "medium")))
    hot = _score(_candidate(trend=Signal(0.95, "high"), competition=Signal(0.9, "medium")))
    assert hot.score_breakdown["components"]["competition"].get("exceptional_demand") is True
    assert "exceptional_demand" not in calm.score_breakdown["components"]["competition"]


def test_trend_decays_with_the_age_of_its_evidence():
    fresh = _candidate(trend=Signal(1.0, "high"))
    stale = _candidate(trend=Signal(1.0, "high"))
    stale.freshness_at = NOW.replace(day=26)
    assert _score(stale).final_score < _score(fresh).final_score


def test_hard_rejections_are_separate_from_the_score():
    dup = _candidate(novelty=Signal(0.1, "high", {"duplicate_of": "project"}), trend=Signal(1.0, "high"))
    scored = _score(dup, issues=["answer_embedded"], flags=["opinion", "vague"])
    assert scored.rejection_reasons == ["duplicate_of_previous_topic", "question_answer_embedded", "flag_opinion"]
    assert scored.score_breakdown["penalties"]["flags"] == ["vague"]
    assert scored.score_breakdown["penalties"]["value"] == 0.06
    assert _score(_candidate(channel_fit=Signal(0.2, "medium"))).rejection_reasons == ["poor_channel_fit"]


def test_degraded_sources_lower_confidence():
    signals = {name: Signal(0.8, "high") for name in SIGNAL_NAMES if name not in {"competition", "own_performance"}}
    assert _score(_candidate(**signals)).confidence == "high"
    assert _score(_candidate(**signals), degraded_sources=True).confidence == "medium"


def test_ranking_is_deterministic_with_stable_tie_breaks():
    items = []
    for key in ("b", "a", "c"):
        candidate = _candidate(trend=Signal(0.6, "high"))
        candidate.candidate_id = candidate_id_for(key)
        items.append(_score(candidate))
    rejected = _score(_candidate(channel_fit=Signal(0.1, "high"), trend=Signal(1.0, "high")))
    first = [item.candidate_id for item in scoring.rank([*items, rejected])]
    second = [item.candidate_id for item in scoring.rank(list(reversed([*items, rejected])))]
    assert first == second
    assert first[-1] == rejected.candidate_id
    assert first[:3] == sorted(item.candidate_id for item in items)


def test_weights_are_explicit_configurable_and_versioned():
    weights, version = scoring.resolve_weights(settings())
    assert version == scoring.SCORE_VERSION and sum(weights.values()) == pytest.approx(1.0)
    assert set(weights) == set(scoring.WEIGHT_RATIONALE)
    custom, custom_version = scoring.resolve_weights(settings(topic_score_weights='{"trend": 0.4}'))
    assert custom_version.startswith(f"{scoring.SCORE_VERSION}+w") and custom["trend"] > weights["trend"]
    assert sum(custom.values()) == pytest.approx(1.0, abs=1e-5)
    with pytest.raises(ValueError):
        scoring.resolve_weights(settings(topic_score_weights='{"views": 1}'))


def test_explanation_is_compact_and_derived_from_the_breakdown():
    candidate = _score(_candidate(
        trend=Signal(0.9, "high", sources=["wikipedia_pageviews", "brave_news_de"]), novelty=Signal(0.95, "high"),
        visual=Signal(0.85, "medium"), competition=Signal(0.5, "medium"), suitability=Signal(0.8, "medium"),
    ))
    lines = scoring.explain(candidate.score_breakdown)
    assert len(lines) <= 4
    labels = [(line["direction"], line["label"]) for line in lines]
    assert ("up", "Recent interest") in labels and ("up", "Strong novelty") in labels
    assert labels[-1] == ("neutral", "Moderate competition")
    assert all(line["signal"] != "own_performance" for line in lines)  # unavailable data never shows as a reason
