from __future__ import annotations

from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import build_novelty_plan, novelty_quality_issues, safe_novelty_plan
from clipforge.payoff import build_payoff_plan
from clipforge.schemas import ProjectCreate
from clipforge.script_writer import ScriptWriterRequest
from clipforge.services import create_project, serialize_project


def pyramids_intent() -> dict:
    return {
        "topic": "Which country has more pyramids, Egypt or Sudan?",
        "question": "Which country has more pyramids, Egypt or Sudan?",
        "content_type": "factual_explainer",
    }


def test_direct_answer_is_expected_core_not_automatically_distinctive() -> None:
    plan = build_novelty_plan(
        pyramids_intent(),
        [{"id": "f1", "claim": "Sudan has more pyramids than Egypt.", "confidence": 0.9, "importance": 0.95, "priority": "MUST_KNOW", "sources": [{"url": "https://one.test"}]}],
    )
    assert plan["core_expected_facts"] == ["f1"]
    assert plan["distinctive_facts"] == []


def test_explanatory_and_contrast_gain_are_prioritized() -> None:
    facts = [
        {"id": "f1", "claim": "Sudan has more pyramids than Egypt.", "confidence": 0.9, "importance": 0.95, "priority": "MUST_KNOW", "sources": [{"url": "https://one.test"}]},
        {"id": "f2", "claim": "Many were built by rulers of the ancient Kushite kingdoms.", "confidence": 0.9, "importance": 0.8, "sources": [{"url": "https://two.test"}]},
        {"id": "f3", "claim": "Kushite rulers built them for different religious traditions than Egyptian pharaohs.", "confidence": 0.9, "importance": 0.7, "sources": [{"url": "https://three.test"}]},
    ]
    plan = build_novelty_plan(pyramids_intent(), facts)
    assert plan["explanatory_gain"] == ["f2"]
    assert plan["comparison_gain"] == ["f3"]


def test_near_duplicate_claims_are_redundant() -> None:
    plan = build_novelty_plan(
        {"topic": "Why do fireflies glow?", "question": "Why do fireflies glow?"},
        [
            {"id": "f1", "claim": "Fireflies glow through a chemical reaction.", "confidence": 0.9, "importance": 0.9, "sources": [{"url": "https://one.test"}]},
            {"id": "f2", "claim": "Fireflies produce light through a chemical reaction.", "confidence": 0.9, "importance": 0.8, "sources": [{"url": "https://two.test"}]},
        ],
    )
    assert plan["redundant_candidates"] == ["f2"]


def test_repeated_supported_core_claim_is_common_context_and_sparse_research_is_conservative() -> None:
    plan = build_novelty_plan(
        pyramids_intent(),
        [{"id": "f1", "claim": "Sudan has more pyramids than Egypt.", "confidence": 0.9, "importance": 0.95, "priority": "MUST_KNOW", "sources": [{"url": "https://one.test"}, {"url": "https://two.test"}]}],
    )
    assert "f1" in plan["common_context"]
    assert plan["status"] == "low_confidence"
    assert plan["confidence"] < 0.7


def test_weak_evidence_is_not_promoted_to_distinctive_or_surprising() -> None:
    plan = build_novelty_plan(
        {"topic": "Why is the sky blue?", "question": "Why is the sky blue?"},
        [{"id": "f1", "claim": "An unexpected hidden cause exists.", "confidence": 0.3, "importance": 0.8, "sources": []}],
    )
    assert plan["distinctive_facts"] == []
    assert plan["confidence"] < 0.4


def test_novelty_reaches_format_payoff_and_script_context() -> None:
    novelty = build_novelty_plan(
        pyramids_intent(),
        [{"id": "f1", "claim": "Sudan has more pyramids than Egypt.", "confidence": 0.9, "importance": 0.95, "priority": "MUST_KNOW", "sources": [{"url": "https://one.test"}]}, {"id": "f2", "claim": "They were built by Kushite rulers.", "confidence": 0.9, "importance": 0.8, "sources": [{"url": "https://two.test"}]}],
    )
    format_plan = plan_format(pyramids_intent(), [], [], novelty)
    payoff = build_payoff_plan(pyramids_intent(), [{"role": "payoff", "text": "Sudan has more pyramids than Egypt."}], format_plan=format_plan, novelty_plan=novelty)
    request = ScriptWriterRequest(prompt="Which country has more pyramids?", language="en", tone="clear", audience="general", content_type="factual_explainer", novelty_plan=novelty)
    assert format_plan["novelty_guidance"]["explanatory_gain_ids"] == ["f2"]
    assert payoff["novelty_guidance"]["recommended_angle"]
    assert request.novelty_plan == novelty


def test_invalid_references_are_reviewed_without_blocking() -> None:
    state = {"facts": [{"id": "f1"}], "novelty_plan": {"distinctive_facts": ["missing"], "source_support": {}, "confidence": 0.2, "recommended_angle": "Use missing"}}
    issues = novelty_quality_issues(state)
    assert "novelty_references_unknown_fact" in issues
    assert "low_confidence_novelty_overstated" in issues
    assert safe_novelty_plan({}, [])["status"] == "low_confidence"


def test_novelty_plan_is_persisted_in_the_initial_project_revision(db) -> None:
    project = create_project(
        db,
        ProjectCreate(prompt="Why do fireflies glow?"),
        Settings(clipforge_ai_mode="local"),
    )
    persisted = serialize_project(project)["revision"]["state"]["novelty_plan"]
    assert persisted["status"] in {"low_confidence", "planned", "fallback"}
    assert "recommended_angle" in persisted
