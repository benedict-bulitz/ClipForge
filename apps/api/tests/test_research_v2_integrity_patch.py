"""Targeted regressions for Research V2 query, grounding and audit integrity."""

from __future__ import annotations

from research_v2_support import FakeWeb, brave_hit, research_settings

from clipforge.research import ResearchResult, research_with_strengthening
from clipforge.research_v2 import run_research
from clipforge.research_v2.answer_relation import core_issues, mechanism_issues, question_frame
from clipforge.research_v2.corroboration import ClaimGroup
from clipforge.research_v2.evidence import EvidenceUnit
from clipforge.research_v2.package import select_claims, validate_synthesized
from clipforge.research_v2.routing import route_question
from clipforge.research_v2.synthesis import (
    DecompositionOut,
    deterministic_sub_questions,
    sub_questions_from_llm,
)

QUESTION = "Why do injured cats purr?"
GENERAL_MECHANISM = "Cats purr because laryngeal muscles vibrate as air moves through the throat."
QUESTION_MECHANISM = (
    "Injured cats may purr because endorphins could reduce pain and stress during recovery."
)


def _unit(
    uid: str,
    source: str,
    text: str,
    *,
    basis: str = "full_text",
    provenance: str | None = None,
    kinds: list[str] | None = None,
) -> EvidenceUnit:
    return EvidenceUnit(
        id=uid,
        text=text,
        excerpt=text,
        source_id=source,
        sub_question="q_core",
        kinds=kinds or ["mechanism"],
        relevance=0.9,
        matched=["cats", "purr"],
        basis=basis,
        provenance=provenance,
    )


def test_english_queries_never_inherit_german_planner_suffixes():
    deterministic = deterministic_sub_questions(QUESTION, QUESTION, "en", focus="strengthen")
    assert all(
        "wissenschaft" not in sub.query.casefold() and "erklärung" not in sub.query.casefold()
        for sub in deterministic
    )

    planned = DecompositionOut(
        domain="science",
        sub_questions=[
            {
                "kind": "core",
                "question": QUESTION,
                "query": "injured cats wissenschaftliche Erklärung",
            },
            {
                "kind": "mechanism",
                "question": QUESTION,
                "query": "cats purr Ursache wie funktioniert",
            },
        ],
    )
    subs, _domain = sub_questions_from_llm(planned, QUESTION, QUESTION, "en")
    assert [sub.query for sub in subs] == [
        "do injured cats purr",
        "do injured cats purr cause explanation how it works",
    ]


def test_mechanism_must_answer_the_condition_and_observation_is_not_explanation():
    frame = question_frame(QUESTION, "en")
    assert "misses_question_condition" in mechanism_issues(
        frame, GENERAL_MECHANISM, "Injured cats often purr."
    )
    assert "misses_question_condition" in core_issues(frame, GENERAL_MECHANISM)
    assert not mechanism_issues(frame, QUESTION_MECHANISM)
    observation = "Injured cats often purr while they recover from surgery or trauma."
    assert "no_cause_or_mechanism" in core_issues(frame, observation)
    assert "no_cause_or_mechanism" in mechanism_issues(frame, observation)


def test_uncertain_evidence_cannot_be_synthesized_as_certain_fact():
    evidence = _unit(
        "ev_01",
        "src_01",
        QUESTION_MECHANISM,
        kinds=["mechanism", "caveat"],
    )
    by_id = {evidence.id: evidence}
    certain = "Injured cats purr because endorphins reduce pain and stress during recovery."
    uncertain = (
        "Injured cats may purr because endorphins could reduce pain and stress during recovery."
    )
    assert validate_synthesized(certain, [evidence.id], by_id) == "uncertainty_not_preserved"
    assert validate_synthesized(uncertain, [evidence.id], by_id) is None


def test_core_basis_authority_and_provenance_use_only_answering_evidence():
    frame = question_frame(QUESTION, "en")
    general = _unit("ev_01", "src_high", GENERAL_MECHANISM)
    answer = _unit(
        "ev_02",
        "src_snippet",
        QUESTION_MECHANISM,
        basis="snippet",
        provenance="search_snippet",
        kinds=["mechanism", "caveat"],
    )
    group = ClaimGroup("claim_01", [general, answer], {"src_high", "src_snippet"})
    sources = {
        "src_high": {"authority": "high", "source_type": "institutional", "cluster": "src_high"},
        "src_snippet": {"authority": "unknown", "source_type": "unknown", "cluster": "src_snippet"},
    }
    claims = select_claims([group], sources, route_question(QUESTION), frame=frame)
    core = next(claim for claim in claims if claim.role == "core_answer").ref()
    assert core["evidence_ids"] == ["ev_02"]
    assert core["basis"] == "snippet" and core["authority_tier"] == 3
    assert core["evidence_provenance"] == ["search_snippet"]


def test_404_fallback_is_persisted_as_search_snippet_not_full_text():
    web = FakeWeb()
    url = "https://example.org/injured-cats"
    web.brave = {"injured cats": [brave_hit(url, "Why injured cats purr", QUESTION_MECHANISM)]}
    web.page(url, "not found", status=404)
    run = run_research(
        QUESTION,
        "en",
        research_settings(brave_search_api_key="test-key"),
        context={"question": QUESTION},
        transport=web.transport(),
        llm=None,
        cache_root=None,
    )
    assert run.package["core_answer"]["basis"] == "snippet"
    assert run.package["core_answer"]["evidence_provenance"] == ["search_snippet"]
    assert {item["provenance"] for item in run.package["evidence"]} == {"search_snippet"}
    source = run.package["source_summary"]["sources"][0]
    assert source["retrieval"] == "not_found"
    assert source["basis"] == "snippet" and source["evidence_provenance"] == "search_snippet"
    diagnostic = next(item for item in run.diagnostics["sources"] if item["url"] == url)
    assert diagnostic["retrieval"] == "not_found" and diagnostic["evidence_provenance"] == "search_snippet"


def test_non_page_provenance_survives_evidence_serialization():
    abstract = _unit("ev_01", "src_01", QUESTION_MECHANISM, provenance="abstract")
    metadata = _unit("ev_02", "src_02", QUESTION_MECHANISM, basis="snippet", provenance="metadata")
    assert abstract.as_dict()["provenance"] == "abstract"
    assert metadata.as_dict()["provenance"] == "metadata"


def test_retry_attempt_and_budget_counter_cannot_contradict():
    weak = {"status": "partial", "core_answer": {"basis": "snippet", "authority_tier": 3}}
    strong = {"status": "sufficient", "core_answer": {"basis": "full_text", "authority_tier": 0}}
    calls = 0

    def research(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        package = weak if calls == 1 else strong
        diagnostics = {"budget": {"used": {"retries": 0}}}
        return ResearchResult(
            [], [], "verified_sources", "research_v2", package=package, diagnostics=diagnostics
        )

    result, report = research_with_strengthening(
        QUESTION,
        "en",
        research_settings(),
        context={"question": QUESTION},
        research=research,
    )
    assert report["attempted"] is True and result.diagnostics["retry_attempted"] is True
    assert result.diagnostics["budget"]["used"]["retries"] >= 1
