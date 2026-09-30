"""Topic Intelligence: local question transformation, universal 12+ relevance and raw-pool backfill.

Real Mac diagnosis (ti-score-v2, ai_mode=local): 96 raw topics, 16 evaluated,
0 accepted - 11x question_no_question_transformation, 5x below_quality_floor.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from topic_support import (
    NOW,
    FakeLLM,
    FakeValidator,
    FakeWiki,
    FakeYouTube,
    deps,
    good_assessment,
    settings,
    video,
)

from clipforge.models import TopicCandidateRecord, TopicDiscoveryRun, TopicSourceCache
from clipforge.topic_intelligence import scoring, semantic, service, transform
from clipforge.topic_intelligence.candidate import (
    SIGNAL_NAMES,
    RawTopic,
    Signal,
    TopicCandidate,
    TopicGroup,
    candidate_id_for,
)
from clipforge.topic_intelligence.service import DiscoveryDeps
from clipforge.topic_intelligence.sources import SourceReport, SourceResult, YouTubeCompetitionProbe
from clipforge.topic_intelligence.text import UNIVERSAL_CUES, extract_question, topic_key

REAL_MAC_TITLES = [
    "Beziehung: Wie gesund ist die Liebe?",
    "Scannt ein selbstgemalter QR-Code?",
    "Du hörst Musik FALSCH: Warum auch die besten Kopfhörer Nachhilfe brauchen",
    "Generationenwechsel bei den iPhones: Wer bekommt welches Modell?",
    "Wie ALLE YouTube Play Buttons hergestellt werden!",
]


def raw(title: str, *, trend: float = 0.5, kind: str = "video", source: str = "youtube_trending_de", description: str = "") -> RawTopic:
    return RawTopic(
        key=topic_key(title), title=title, source=source, kind=kind, observed_at=NOW, description=description,  # type: ignore[arg-type]
        trend=Signal(trend, "medium", {"method": "test"}, [source]),
    )


def group(title: str, **kwargs) -> TopicGroup:
    item = raw(title, **kwargs)
    return TopicGroup(item.key, [item])


class StaticSource:
    """A discovery source whose raw topics are fixed; counts provider calls."""

    name = "static_de"

    def __init__(self, topics: list[RawTopic]) -> None:
        self.topics = topics
        self.calls = 0

    def discover(self, ctx):
        self.calls += 1
        return SourceResult(list(self.topics), SourceReport(self.name, "ok", items=len(self.topics)))


def static_deps(source: StaticSource) -> DiscoveryDeps:
    return DiscoveryDeps(sources=[source], probe=YouTubeCompetitionProbe(None, None))


def scored(question: str, *, topic: str = "", niche: str = "unknown", notes: set[str] | None = None, **signals: Signal) -> TopicCandidate:
    base = {name: Signal.unavailable("not_measured") for name in SIGNAL_NAMES}
    features, _ = service.quality_signals(question, topic or question, "", niche, {}, "source_question", notes or set())
    base.update(features)
    base.update({
        "suitability": Signal(0.7, "low"), "visual": Signal(0.7, "low"), "novelty": Signal(1.0, "high"),
        "channel_fit": Signal(0.75, "low"),
    })
    base.update(signals)
    candidate = TopicCandidate(
        candidate_id=candidate_id_for(question), topic=topic or question, question=question, rationale="", source_signals=[],
        discovered_at=NOW, language="de", region="DE", niche=niche, signals=base, freshness_at=NOW, provenance={},
    )
    weights, version = scoring.resolve_weights(settings())
    return scoring.score_candidate(candidate, weights=weights, version=version, now=NOW)


# --- 1/2/11. The five real Mac titles ---------------------------------------------------


def test_already_good_question_after_a_label_is_preserved():
    result = transform.deterministic_transform(group(REAL_MAC_TITLES[0]))
    assert result.question == "Wie gesund ist die Liebe?"
    assert result.method == "source_question" and result.issues == []
    assert "extracted_clause" in result.notes


def test_verb_first_yes_no_question_is_accepted_unchanged():
    result = transform.deterministic_transform(group(REAL_MAC_TITLES[1]))
    assert result.question == "Scannt ein selbstgemalter QR-Code?"
    assert result.issues == []
    candidate = scored(result.question, notes=result.notes)
    assert candidate.signal("accessibility").value >= 0.85  # QR is an everyday acronym, not prior knowledge


def test_indirect_clause_after_a_clickbait_teaser_becomes_a_direct_question():
    result = transform.deterministic_transform(group(REAL_MAC_TITLES[2]))
    assert result.question == "Warum brauchen auch die besten Kopfhörer Nachhilfe?"
    assert "FALSCH" not in result.question and "Du hörst" not in result.question
    assert "clickbait_source" in result.flags  # not blindly accepted: styling removed, premise penalized
    candidate = scored(result.question, notes=result.notes)
    weights, version = scoring.resolve_weights(settings())
    flagged = scoring.score_candidate(candidate, weights=weights, version=version, now=NOW, flags=result.flags)
    assert flagged.score_breakdown["penalties"]["flags"] == ["clickbait_source"]


def test_question_that_needs_its_title_context_is_rejected_as_prior_knowledge():
    result = transform.deterministic_transform(group(REAL_MAC_TITLES[3]))
    assert result.question == "Wer bekommt welches Modell?"
    assert "needs_title_context" in result.notes
    candidate = scored(result.question, notes=result.notes)
    assert "requires_prior_knowledge" in candidate.rejection_reasons
    assert {"needs_title_context", "product_news"} <= set(candidate.score_breakdown["quality"]["prior_knowledge"])


def test_shouting_and_exclamation_are_cleaned_and_niche_brand_questions_need_prior_knowledge():
    result = transform.deterministic_transform(group(REAL_MAC_TITLES[4]))
    assert result.question == "Wie werden alle YouTube Play Buttons hergestellt?"
    assert "ALLE" not in result.question and "!" not in result.question
    candidate = scored(result.question, notes=result.notes)
    assert "requires_prior_knowledge" in candidate.rejection_reasons  # a creator-award niche, not 12+ universal


def test_no_real_mac_title_fails_with_no_question_transformation():
    for title in REAL_MAC_TITLES:
        assert transform.deterministic_transform(group(title)).issues != ["no_question_transformation"], title


def test_channel_suffixes_and_hashtags_do_not_pollute_the_question():
    assert extract_question("So funktioniert das Gehirn im Schlaf | Terra X")[0] == "Wie funktioniert das Gehirn im Schlaf?"
    assert extract_question("🔥 Warum fliegen Zugvögel im V? #shorts")[0] == "Warum fliegen Zugvögel im V?"
    assert extract_question("Warum ist der Himmel blau? - Quarks")[0] == "Warum ist der Himmel blau?"


def test_answer_headlines_become_questions_only_with_their_own_premise():
    assert extract_question("Darum werden wir im Herbst müde")[0] == "Warum werden wir im Herbst müde?"
    assert extract_question("Das passiert, wenn du nicht schläfst")[0] == "Was passiert, wenn du nicht schläfst?"
    assert extract_question("Wie viel Wasser man wirklich trinken muss")[0] == "Wie viel Wasser muss man wirklich trinken?"
    # A statement is never turned into invented curiosity, and generic clickbait yields nothing.
    assert extract_question("Forscher entdecken neue Tiefsee-Art")[0] is None
    assert extract_question("Du wirst nicht glauben, was dann passiert!")[0] is None


def test_unsupported_premises_are_rejected(monkeypatch):
    # A rewrite may not add a claim (here a number) that the source does not make.
    llm = FakeLLM({"Igel": good_assessment("Warum leben Igel bis zu 25 Jahre?", "natur_tiere")})
    monkeypatch.setattr(transform, "TRANSFORM_CLIENT_FACTORY", llm)
    results, _method, _ = transform.transform_topics(
        [group("Igel", kind="article", source="wikipedia_pageviews", description="Igel leben einige Jahre.")],
        settings(clipforge_ai_mode="openai", openai_api_key="sk-test"),
    )
    assert "unsupported_number" in results[0].issues


def test_no_generic_wrapper_is_generated_locally():
    for title, description in [("GICON-Höhenwindturm", "Windkraftanlage in Brandenburg"), ("29. September", "Tag im Kalender")]:
        result = transform.deterministic_transform(group(title, kind="article", source="wikipedia_pageviews", description=description))
        assert result.question == "" and result.issues == ["no_question_transformation"]


def test_quality_gates_are_unchanged():
    assert scoring.QUALITY_FLOOR == 0.55
    assert scoring.QUALITY_FLOOR_EXCEPTIONAL == 0.45
    assert scoring.OBSCURITY_PENALTIES["date_page"] == 0.15 and scoring.OBSCURITY_PENALTIES["generic_wrapper"] == 0.08
    assert scoring.PRIOR_KNOWLEDGE_GATE == 0.5


# --- 5/6/12. Universal 12+ relevance ------------------------------------------------------

STRONG_UNIVERSAL = [
    "Warum wird einem schwindelig, wenn man schnell aufsteht?",
    "Warum vergessen wir, warum wir einen Raum betreten haben?",
    "Was würde passieren, wenn die Erde plötzlich aufhören würde, sich zu drehen?",
    "Warum schmeckt Essen im Flugzeug anders?",
    "Warum können wir uns selbst nicht kitzeln?",
    "Wie weiß dein Handy eigentlich, wo du bist?",
    "Warum bekommen wir manchmal plötzlich Lust auf Süßes?",
    "Warum fühlt sich ein Wochenende kürzer an als eine Arbeitswoche?",
]


def test_a_typical_12_year_old_understands_the_strong_examples_without_context():
    for question in STRONG_UNIVERSAL:
        candidate = scored(question)
        access = candidate.signal("accessibility")
        assert access.value >= 0.85 and not access.evidence["prior_knowledge"], question
        assert "requires_prior_knowledge" not in candidate.rejection_reasons, question


def test_specialist_prior_knowledge_is_rejected():
    for question in (
        "Warum hat der Bundestag den Gesetzentwurf geändert?",
        "Wer bekommt welches Modell?",
        "Wie funktioniert die neue PlayStation Portal Remote?",
    ):
        candidate = scored(question, notes={"needs_title_context"} if question.startswith("Wer") else set())
        assert "requires_prior_knowledge" in candidate.rejection_reasons, question


def test_broad_12_plus_topic_beats_niche_topic_with_a_bigger_spike():
    niche = scored("Wie werden alle YouTube Play Buttons hergestellt?", trend=Signal(1.0, "high", sources=["youtube_trending_de"]))
    broad = scored("Warum wird einem schwindelig, wenn man schnell aufsteht?", niche="koerper_gesundheit",
                   trend=Signal(0.4, "medium", sources=["youtube_trending_de"]))
    assert niche.rejected and not broad.rejected
    assert broad.final_score > niche.final_score


def test_everyday_question_beats_obscure_named_entity_with_similar_trend():
    trend = Signal(0.8, "medium", sources=["wikipedia_pageviews"])
    named = scored("Wie funktioniert eigentlich der GICON-Höhenwindturm?", topic="GICON-Höhenwindturm", trend=trend)
    everyday = scored("Warum brummt der Kühlschrank manchmal nachts?", trend=trend)
    assert "requires_prior_knowledge" in named.rejection_reasons
    assert not everyday.rejected and everyday.final_score > named.final_score


def test_obscure_trend_spike_alone_is_insufficient():
    spike_only = scored("Was geschah beim Gol-Transportes-Aéreos-Flug 1907?", topic="Gol-Transportes-Aéreos-Flug 1907",
                        trend=Signal(1.0, "high", sources=["wikipedia_pageviews"]))
    assert "requires_prior_knowledge" in spike_only.rejection_reasons


def test_niche_origin_topic_passes_after_universal_reframing(monkeypatch):
    llm = FakeLLM({
        "GICON-Höhenwindturm": good_assessment(
            "Kann ein Windrad in 1.000 Metern Höhe viel mehr Strom erzeugen?", "technik", broad_appeal=8, accessibility=9),
        "Gol-Transportes-Aéreos-Flug 1907": good_assessment(
            "Warum können Piloten trotz moderner Technik zwei Flugzeuge übersehen?", "technik", broad_appeal=8, accessibility=8),
    })
    monkeypatch.setattr(transform, "TRANSFORM_CLIENT_FACTORY", llm)
    groups = [
        group("GICON-Höhenwindturm", kind="article", source="wikipedia_pageviews",
              description="Windkraftanlage, die Wind in 1.000 Metern Höhe nutzen soll"),
        group("Gol-Transportes-Aéreos-Flug 1907", kind="article", source="wikipedia_pageviews",
              description="Kollision zweier Flugzeuge trotz Kollisionswarnsystemen"),
    ]
    results, method, _ = transform.transform_topics(groups, settings(clipforge_ai_mode="openai", openai_api_key="sk-test"))
    assert method == "llm"
    for item, topic in zip(results, ("GICON-Höhenwindturm", "Gol-Transportes-Aéreos-Flug 1907"), strict=True):
        assert item.issues == [], item.question
        candidate = scored(item.question, topic=topic)
        assert "names_obscure_entity" not in candidate.signal("accessibility").evidence["prior_knowledge"]
        assert "requires_prior_knowledge" not in candidate.rejection_reasons, item.question


def test_broad_relevance_is_not_childrens_content():
    # Universal cues are about people and the physical world, not child-directed wording.
    assert not UNIVERSAL_CUES & {"kinder", "kind", "kids", "baby", "spielzeug", "schule"}
    adult = scored("Warum fühlt sich ein Wochenende kürzer an als eine Arbeitswoche?")
    assert not adult.rejected and adult.signal("accessibility").value >= 0.85


# --- 3/4/13. Raw-pool backfill -------------------------------------------------------------

WEAK_HEADLINES = [
    "Bundesliga-Topspiel endet unentschieden",
    "Konzern meldet Rekordquartal",
    "Generationenwechsel bei den iPhones: Wer bekommt welches Modell?",
    "Wie ALLE YouTube Play Buttons hergestellt werden!",
    "Forscher entdecken neue Tiefsee-Art",
    "Neue Staffel startet im Herbst",
    "Minister stellt Gesetzentwurf vor",
    "Streamer knackt Zuschauerrekord",
    "Warum hat der Bundestag den Gesetzentwurf geändert?",
    "Wie funktioniert die neue PlayStation Portal Remote?",
    "Stadtrat beschließt neuen Haushalt",
    "Autobauer ruft Modelle zurück",
    "Festival verkündet Headliner",
    "Aktienkurs springt nach Zahlen",
    "Verband kritisiert Reform",
    "Trainer verlängert Vertrag",
]
STRONG_LATER = [
    "Warum wird einem schwindelig, wenn man schnell aufsteht?",
    "Warum können wir uns selbst nicht kitzeln?",
    "Warum schmeckt Essen im Flugzeug anders?",
    "Warum vergessen wir, warum wir einen Raum betreten haben?",
    "Was würde passieren, wenn die Erde plötzlich aufhören würde, sich zu drehen?",
]
_PARTS = (["Nord", "Süd", "West", "Ost", "Berg", "Tal", "Stern", "Blitz", "Wald", "Fluss"],
          ["werk", "bau", "tech", "gruppe", "logistik", "energie", "media", "haus"])


def pool_of_96() -> list[RawTopic]:
    # The old top-16 shortlist: statements, niche/prior-knowledge questions, weak news.
    topics = [raw(title, trend=0.95 - index * 0.01, kind="news", source="brave_news_de") for index, title in enumerate(WEAK_HEADLINES)]
    # 75 more distinct weak statements further down.
    names = [left + right for left in _PARTS[0] for right in _PARTS[1]][:75]
    topics += [raw(f"{name} meldet Rekordquartal", trend=0.7 - index * 0.005, kind="news", source="brave_news_de") for index, name in enumerate(names)]
    # The strong universal questions sit deep in the raw pool.
    topics += [raw(question, trend=0.2 - index * 0.01) for index, question in enumerate(STRONG_LATER)]
    return topics


def test_backfill_evaluates_the_existing_raw_pool_until_three_strong_candidates(db):
    source = StaticSource(pool_of_96())
    assert len(source.topics) == 96
    result = service.suggestions(db, settings(), static_deps(source), count=3, now=NOW)
    assert result["status"] == "ok" and len(result["candidates"]) == 3
    assert source.calls == 1  # no new provider discovery for the backfill
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence))
    evaluation = next(item for item in run.sources if item["name"] == "candidate_evaluation")
    assert evaluation["items"] <= service.EVALUATION_BUDGET  # bounded (60 per pool)
    for item in result["candidates"]:
        record = db.get(TopicCandidateRecord, item["candidate_id"])
        assert not record.rejection_reasons
        assert record.score_breakdown["quality"]["passed"] and record.signals["accessibility"]["value"] >= 0.5
        assert item["question"] in STRONG_LATER


def test_backfill_budget_is_a_hard_bound(db):
    source = StaticSource(pool_of_96())
    result = service.suggestions(db, settings(), static_deps(source), count=3, now=NOW)
    runs = db.scalars(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence)).all()
    evaluated = sum(service.evaluated_in(run) for run in runs)
    assert evaluated <= service.EVALUATION_BUDGET  # 60, shared with the broadening pass
    assert source.calls <= 2  # first pass + at most one broadening pass (sources are cached in production)
    assert result["status"] in {"ok", "partial", "exhausted"}


def test_backfill_reaches_strong_topics_past_the_old_top_16(db):
    topics = [raw(title, trend=0.9 - index * 0.01, kind="news", source="brave_news_de") for index, title in enumerate(WEAK_HEADLINES)]
    names = [left + right for left in _PARTS[0] for right in _PARTS[1]][:8]
    topics += [raw(f"{name} meldet Rekordquartal", trend=0.6, kind="news", source="brave_news_de") for name in names]
    topics += [raw(question, trend=0.1) for question in STRONG_LATER]
    source = StaticSource(topics)
    result = service.suggestions(db, settings(), static_deps(source), count=3, now=NOW)
    assert len(result["candidates"]) == 3 and source.calls == 1


def test_llm_backfill_batches_topics_and_bounds_requests(db, monkeypatch):
    llm = FakeLLM({})  # nothing usable: worst case for the number of requests
    monkeypatch.setattr(transform, "TRANSFORM_CLIENT_FACTORY", llm)
    validator = FakeValidator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", validator)
    source = StaticSource(pool_of_96())
    service.suggestions(db, settings(clipforge_ai_mode="openai", openai_api_key="sk-test"), static_deps(source), count=3, now=NOW)
    # Rewriting and validation share one budget: at most 3 AI requests per pool, <= 20 items each.
    assert len(llm.requests) + len(validator.requests) <= service.AI_REQUEST_BUDGET
    assert all(len(request["topics"]) <= transform.MAX_BATCH for request in llm.requests)
    assert all(len(batch) <= semantic.MAX_VALIDATION_BATCH for batch in validator.requests)


# --- 9. Versioned invalidation (candidates only, provider caches kept) ----------------------


def test_old_transformation_pool_is_not_reused_but_provider_cache_is(db):
    wiki = FakeWiki()
    discovery = deps(wiki=wiki)
    service.suggestions(db, settings(), discovery, count=3, now=NOW)
    run = db.scalar(select(TopicDiscoveryRun))
    assert run.transformation == f"template:{transform.TRANSFORMATION_VERSION}"
    run.transformation = "template"  # a pool built by the previous question step
    db.commit()
    calls = len(wiki.calls)
    service.suggestions(db, settings(), discovery, count=3, now=NOW + timedelta(minutes=5))
    assert db.scalar(select(func.count()).select_from(TopicDiscoveryRun)) >= 2  # candidates re-evaluated
    assert len(wiki.calls) == calls  # raw provider data reused from cache
    assert db.scalar(select(func.count()).select_from(TopicSourceCache)) >= 1


# --- 10. YouTube status diagnostic ------------------------------------------------------------


def test_missing_youtube_chart_category_is_reported_as_partial_not_ok_with_error(db):
    youtube = FakeYouTube(
        popular={"27": [video("v1", "Warum können wir uns selbst nicht kitzeln?", "c1", 50_000)]},
        recent={"c1": [video(f"r{i}", f"Video {i}", "c1", 5_000) for i in range(5)]},
        missing_categories={"28"},
    )
    result = service.suggestions(db, settings(), deps(youtube=youtube), count=3, now=NOW)
    report = {item["name"]: item for item in result["pool"]["sources"]}["youtube_trending_de"]
    assert report["status"] == "partial"
    assert "Wissenschaft & Technik (28)" in report["error"] and "not_found" in report["error"]
    assert report["items"] >= 1


def test_local_mode_evaluates_transformable_topics_first_without_gating_the_rest(db):
    source = StaticSource(pool_of_96())
    service.suggestions(db, settings(), static_deps(source), count=9, now=NOW)
    accepted = {record.question for record in db.scalars(select(TopicCandidateRecord)).all() if not record.rejection_reasons}
    assert set(STRONG_LATER) <= accepted  # all five deep strong questions reached within the budget of 60
    run = db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence))
    evaluation = next(item for item in run.sources if item["name"] == "candidate_evaluation")
    assert evaluation["items"] <= service.EVALUATION_BUDGET
