"""Topic Intelligence: curator reliability - priority order, small batches, partial failure.

Real Mac (6e347e5, semantic-curator-v2): 95 raw topics, 47 evaluated, 3/3 requests, one
batch of 20 hit APITimeoutError, 27 curated, 0 accepted - and promising topics
(Biolumineszenz, Hundehirn) were never judged because their batch failed.  v2 asks
14 ratings + 14 issue codes per topic (v1: 9 + 8) without the low reasoning effort every
other ClipForge structured call uses.

Now: the best raw topics (cheap evidence only) are curated first, 10 per request, a
failed batch is "not evaluated" and retried once (smaller after a timeout) within the
same 3-request budget, and nothing unjudged is ever served.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from openai import APITimeoutError
from sqlalchemy import select
from test_topic_intelligence_local_questions import (
    STRONG_LATER,
    StaticSource,
    pool_of_96,
    raw,
    static_deps,
)
from topic_support import GOOD_JUDGEMENT, NOW, FakeCurator, bad, settings

from clipforge.models import TopicCandidateRecord, TopicDiscoveryRun
from clipforge.topic_intelligence import runtime, scoring, semantic, service

KEY = {"openai_api_key": "sk-test"}
REJECTED = bad(clear_factual_payoff=2)  # judged, and not good enough


class ScriptedCurator(FakeCurator):
    """``plan[i]`` = "timeout" makes request i time out; ``good[i]`` = positions judged good in request i."""

    def __init__(self, plan: list[str] | None = None, good: dict[int, set[int]] | None = None) -> None:
        super().__init__(default=REJECTED)
        self.plan = plan or []
        self.good = good or {}
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any):
        index = len(self.requests)
        self.calls.append({key: value for key, value in kwargs.items() if key != "input"})
        if index < len(self.plan) and self.plan[index] == "timeout":
            self.requests.append(json.loads(kwargs["input"])["topics"])
            raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        topics = json.loads(kwargs["input"])["topics"]
        good = {topics[position]["local_question"] for position in self.good.get(index, set()) if position < len(topics)}
        self.by_question = {question: GOOD_JUDGEMENT for question in good}
        return super().parse(**kwargs)


def questions(n: int) -> list[str]:
    nouns = ["Honig", "Glas", "Schnee", "Kupfer", "Sand", "Nebel", "Salz", "Eis", "Rost", "Wachs", "Seife", "Kreide",
             "Lehm", "Ton", "Harz", "Zucker", "Milch", "Mehl", "Essig", "Kaffee", "Tinte", "Gummi", "Leder", "Kork", "Wolle"]
    return [f"Warum verändert sich {noun} mit der Zeit?" for noun in nouns[:n]]


def run(db, monkeypatch, curator, titles, **kwargs):
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    topics = [raw(title, trend=0.9 - index * 0.01) for index, title in enumerate(titles)]
    return service.suggestions(db, settings(**KEY), static_deps(StaticSource(topics)), count=9, now=NOW, **kwargs)


def latest_run(db) -> TopicDiscoveryRun:
    return db.scalar(select(TopicDiscoveryRun).order_by(TopicDiscoveryRun.sequence.desc()))


def asked(request: list[dict[str, Any]]) -> list[str]:
    return [item["local_question"] for item in request]


# --- Prioritization before the paid curation ---------------------------------------------------


def test_highest_potential_raw_topics_are_curated_first(db, monkeypatch):
    # The strong universal questions sit deep in the raw pool with the LOWEST trend.
    curator = ScriptedCurator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    service.suggestions(db, settings(**KEY), static_deps(StaticSource(pool_of_96())), count=9, now=NOW)
    first = [item["topic"] for item in curator.requests[0]]
    assert set(STRONG_LATER) <= set(first)
    order = service.pool_summary(db, latest_run(db))["evaluation"]["curation_order"]
    assert order[0]["priority"] >= order[-1]["priority"] and {"features", "penalties", "niche"} <= set(order[0])


def test_obvious_garbage_does_not_take_early_ai_slots(db, monkeypatch):
    garbage = [
        "Bundesliga-Topspiel endet unentschieden",
        "Minister stellt Gesetzentwurf vor",
        "Generationenwechsel bei den iPhones: Wer bekommt welches Modell?",
        "Konzern meldet Rekordquartal",
        "Neue Staffel startet im Herbst",
        "Welche Faktoren beeinflussen den Aktienkurs, und was bringt die Zukunft?",
    ]
    good = questions(12)
    curator = ScriptedCurator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    # Garbage has the strongest, corroborated demand; the good questions are quiet.
    topics = [raw(title, trend=0.99, kind="news", source="brave_news_de") for title in garbage]
    topics += [raw(title, trend=0.98) for title in garbage]
    topics += [raw(title, trend=0.3) for title in good]
    service.suggestions(db, settings(**KEY), static_deps(StaticSource(topics)), count=9, now=NOW)
    first = {item["topic"] for item in curator.requests[0]}
    assert len(first) == semantic.MAX_CURATION_BATCH == 10
    assert not first & set(garbage)


def test_priority_is_work_order_only_and_uses_no_semantic_judgement():
    value, applied = scoring.curation_priority(
        {"demand": 1.0, "question_strength": 0.8, "universal": 1.0, "evidence": 1.0, "mass_appeal": 0.8, "corroboration": 1.0, "novelty": 1.0},
        {"prior_knowledge": 0, "obscure_entity": 0, "poor_fit_niche": False, "weak_question_shape": False, "duplicate_of_previous_topic": False},
    )
    assert 0.9 < value <= 1.0 and applied == {}
    duplicate, _ = scoring.curation_priority({"demand": 1.0}, {"duplicate_of_previous_topic": True})
    assert duplicate < 0
    assert abs(sum(scoring.CURATION_PRIORITY_WEIGHTS.values()) - 1.0) < 1e-9


# --- Small batches, partial failure, retry within the budget ------------------------------------


def test_one_timed_out_batch_keeps_the_other_results_and_the_budget_continues(db, monkeypatch):
    curator = ScriptedCurator(plan=["ok", "timeout", "ok"], good={0: {1, 2}, 2: {2}})
    result = run(db, monkeypatch, curator, questions(20))
    sizes = [len(request) for request in curator.requests]
    # 10 -> timeout -> the failed topics first again, in a smaller batch; still 3 requests in total.
    assert sizes == [10, 10, 5] and len(curator.requests) <= service.AI_REQUEST_BUDGET
    assert asked(curator.requests[2]) == asked(curator.requests[1])[:5]
    expected = {asked(curator.requests[0])[1], asked(curator.requests[0])[2], asked(curator.requests[2])[2]}
    served = {item["question"] for item in result["candidates"]}
    assert served == expected  # the first batch's accepted candidates survive the timeout
    summary = result["summary"]
    assert [item["status"] for item in summary["semantic_validation"]["batches"]] == ["ok", "timeout", "ok"]
    assert summary["semantic_validation"]["batches"][2]["retry"] is True
    assert summary["semantic_validation"]["status"] == "partial"
    # The 5 failed topics that could not be retried are "not evaluated" - no record, no rejection.
    never_judged = set(asked(curator.requests[1])[5:])
    assert summary["evaluation"]["unevaluated"] == {"curator_failed": 5}
    records = {record.question: record for record in db.scalars(select(TopicCandidateRecord)).all()}
    assert not never_judged & set(records)
    assert all(records[question].status == "proposed" for question in expected)
    assert not runtime.FLIGHT.locked() and latest_run(db).status == "ok"


def test_timed_out_topics_are_not_evaluated_and_never_served_as_local_filler(db, monkeypatch):
    curator = ScriptedCurator(plan=["timeout", "timeout", "timeout"])
    # Every question would pass the strict local rules ("Warum ...", no prior knowledge).
    result = run(db, monkeypatch, curator, questions(15))
    assert [len(request) for request in curator.requests] == [10, 5, 4]  # smaller after each timeout, 3 in total
    assert result["candidates"] == []
    assert db.scalars(select(TopicCandidateRecord)).all() == []  # not persisted as low quality
    evaluation = result["summary"]["evaluation"]
    assert evaluation["unevaluated"] == {"curator_failed": 10, "ai_budget_exhausted": 5}
    assert service.diagnose(db, settings(**KEY), now=NOW) == "curator_failed"
    assert not runtime.FLIGHT.locked() and latest_run(db).status == "ok"
    # The next refresh judges them for real: a timeout was never a verdict.
    retry = ScriptedCurator(good={0: {0}})
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", retry)
    topics = [raw(title, trend=0.9 - index * 0.01) for index, title in enumerate(questions(15))]
    later = service.suggestions(db, settings(**KEY), static_deps(StaticSource(topics)), count=9, now=NOW + service.MIN_REFRESH_INTERVAL * 2)
    assert retry.requests and [item["question"] for item in later["candidates"]] == [asked(retry.requests[0])[0]]


def test_cached_judgements_are_used_first_and_cost_no_request(db, monkeypatch):
    first = ScriptedCurator(good={0: {0, 1}})
    run(db, monkeypatch, first, questions(10))
    for record in db.scalars(select(TopicCandidateRecord)).all():
        record.status = "pooled"
    db.commit()
    again = ScriptedCurator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", again)
    topics = [raw(title, trend=0.9 - index * 0.01) for index, title in enumerate(questions(10))]
    result = service.suggestions(db, settings(**KEY), static_deps(StaticSource(topics)), count=9, now=NOW + service.MIN_REFRESH_INTERVAL * 6)
    assert again.requests == []
    assert len(result["candidates"]) == 2


def test_the_curator_request_is_small_bounded_and_uses_low_reasoning_effort(db, monkeypatch):
    curator = ScriptedCurator(good={0: {0}})
    result = run(db, monkeypatch, curator, questions(3))
    call = curator.calls[0]
    assert call["reasoning"] == {"effort": "low"} and call["model"].startswith("gpt-5")
    assert curator.client_options == {"timeout": runtime.CURATOR_TIMEOUT_SECONDS, "max_retries": 0}
    batch = result["summary"]["semantic_validation"]["batches"][0]
    assert batch["status"] == "ok" and batch["size"] == 3 and batch["seconds"] >= 0
    assert result["summary"]["evaluation"]["curator_batch_size"] == 10


def test_batch_size_is_configurable_and_the_request_budget_still_holds(db, monkeypatch):
    curator = ScriptedCurator()
    monkeypatch.setattr(semantic, "SEMANTIC_CLIENT_FACTORY", curator)
    topics = [raw(title, trend=0.5) for title in questions(25)]
    service.suggestions(db, settings(topic_curator_batch_size=8, **KEY), static_deps(StaticSource(topics)), count=9, now=NOW)
    assert [len(request) for request in curator.requests] == [8, 8, 8]


def test_calibration_lists_every_curated_candidate_with_its_gates(db, monkeypatch):
    curator = ScriptedCurator(good={0: {0}})
    run(db, monkeypatch, curator, questions(4))
    report = service.curation_calibration(db, latest_run(db))
    assert report["curated"] == 4 and report["accepted"] == 1
    rejected = [row for row in report["candidates"] if not row["accepted"]]
    assert all(row["gates"] == ["semantic_unclear_payoff"] for row in rejected)
    assert report["sole_gate"] == {"semantic_unclear_payoff": 3}
    row = report["candidates"][0]
    assert set(row["dimensions"]) == set(semantic.DIMENSIONS)
    assert set(semantic.SHORT_DIMENSIONS) <= set(row["short_dimensions"]) and row["short_worthiness"] is not None
    status = service.discovery_status(db, settings(**KEY), now=NOW)
    assert status["calibration"]["gate_counts"] == {"semantic_unclear_payoff": 3} and "candidates" not in status["calibration"]


def test_quality_gates_are_unchanged():
    # V2 (ti-score-v7 / semantic-curator-v3) adds gates; the V1 thresholds below stay exactly as they were.
    assert scoring.SCORE_VERSION == "ti-score-v7" and semantic.SEMANTIC_CURATOR_VERSION == "semantic-curator-v3"
    assert scoring.SEMANTIC_DIMENSION_MIN == 0.6 and scoring.SHORT_WORTHINESS_FLOOR == 0.45
    assert scoring.SINGLE_QUESTION_FOCUS_MIN == 0.6 and scoring.QUALITY_FLOOR == 0.55 and scoring.PRIOR_KNOWLEDGE_GATE == 0.5
