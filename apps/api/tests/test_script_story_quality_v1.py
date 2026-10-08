"""Script & Story Quality V1: deterministic content editing and safety gates."""
from __future__ import annotations

import copy
import inspect

from clipforge import pipeline
from clipforge.script_story_quality import (
    assess_script_story_quality,
    run_script_story_quality_v1,
)


def fact(identifier: str, claim: str) -> dict:
    return {
        "id": identifier,
        "claim": claim,
        "confidence": 0.95,
        "importance": 0.9,
        "verification": "source_attributed",
        "sources": [{"label": identifier, "url": f"https://{identifier}.test/source"}],
    }


FACTS = [
    fact("fact_01", "When you stand, gravity pulls blood from your upper body toward your legs."),
    fact("fact_02", "Baroreceptors respond by tightening blood vessels and raising the heart rate."),
    fact("fact_03", "Before circulation compensates, the brain briefly gets less oxygen, so vision can go dark."),
]


ARC = {
    "primary_question": "Why can your vision go dark when you stand up quickly?",
    "primary_answer_id": "fact_03",
    "final_payoff_id": "fact_03",
    "order": ["fact_01", "fact_02", "fact_03"],
    "curiosity_gap": {"withhold_answer": True},
    "hook": {"protected_ids": ["fact_03"], "allowed_ids": ["fact_01", "fact_02"]},
    "units": [
        {"id": "fact_01", "role": "supporting_fact", "depends_on": [], "may_be_omitted": False},
        {"id": "fact_02", "role": "explanation", "depends_on": ["fact_01"], "may_be_omitted": False},
        {"id": "fact_03", "role": "primary_answer", "depends_on": ["fact_01", "fact_02"], "may_be_omitted": False},
    ],
    "question_contract": {
        "core_question": "Why can your vision go dark when you stand up quickly?",
        "essential_explanation_chain": ["fact_01", "fact_02", "fact_03"],
        "explanation_spine": {"status": "complete", "mechanism": ["fact_01", "fact_02"]},
    },
}


def block(identifier: str, role: str, text: str, fact_ids: list[str] | None = None) -> dict:
    return {"id": identifier, "role": role, "text": text, "fact_ids": list(fact_ids or [])}


def context(*, facts: list[dict] | None = None, arc: dict | None = None) -> dict:
    return {
        "prompt": "Why can your vision go dark when you stand up quickly?",
        "intent": {
            "question": "Why can your vision go dark when you stand up quickly?",
            "topic": "vision after standing",
            "language": "en",
            "research_required": True,
            "content_type": "factual_explainer",
        },
        "facts": copy.deepcopy(FACTS if facts is None else facts),
        "story_arc": copy.deepcopy(ARC if arc is None else arc),
        "novelty_plan": {"explanatory_gain": ["fact_01", "fact_02", "fact_03"]},
        "payoff_plan": {
            "reveal_policy": "after_supporting_information",
            "hook_must_not_reveal": FACTS[2]["claim"],
            "primary_answer_id": "fact_03",
            "final_payoff_id": "fact_03",
        },
        "script": {"triple_hook": {"verbal_hook": "Stand up fast and your circulation has to react."}},
        "reaction_plan": {"planned_arc": {"hook_reaction": "curiosity", "payoff_reaction": "insight"}},
    }


def strong_blocks() -> list[dict]:
    return [
        block("b1", "hook", "Stand up fast and your circulation has to react."),
        block("b2", "explanation", FACTS[0]["claim"], ["fact_01"]),
        block("b3", "explanation", FACTS[1]["claim"], ["fact_02"]),
        block("b4", "payoff", FACTS[2]["claim"], ["fact_03"]),
    ]


AIRPLANE_FACTS = [
    fact(
        "air_fact_01",
        "Airplanes can leave white trails called contrails, which are clouds of ice crystals rather than smoke.",
    ),
    fact("air_fact_02", "Jet engine exhaust contains water vapor."),
    fact("air_fact_03", "Air at cruising altitude can be extremely cold."),
    fact(
        "air_fact_04",
        "In that cold air, the water vapor condenses and freezes into tiny ice crystals that form a contrail.",
    ),
    fact(
        "air_fact_05",
        "A contrail forms through a process similar to visible breath on a cold winter day.",
    ),
]


AIRPLANE_ARC = {
    "primary_question": "Why do airplanes leave white trails?",
    "primary_answer_id": "air_fact_01",
    "final_payoff_id": "air_fact_01",
    "order": ["air_fact_02", "air_fact_03", "air_fact_04", "air_fact_01"],
    "curiosity_gap": {"withhold_answer": False},
    "hook": {"protected_ids": [], "allowed_ids": [item["id"] for item in AIRPLANE_FACTS]},
    "units": [
        {
            "id": "air_fact_01", "claim": AIRPLANE_FACTS[0]["claim"], "role": "primary_answer",
            "depends_on": ["air_fact_04"], "may_be_omitted": False, "novelty": "core",
        },
        {
            "id": "air_fact_02", "claim": AIRPLANE_FACTS[1]["claim"], "role": "explanation",
            "depends_on": [], "may_be_omitted": False, "novelty": "explanatory_gain",
        },
        {
            "id": "air_fact_03", "claim": AIRPLANE_FACTS[2]["claim"], "role": "explanation",
            "depends_on": [], "may_be_omitted": False, "novelty": "explanatory_gain",
        },
        {
            "id": "air_fact_04", "claim": AIRPLANE_FACTS[3]["claim"], "role": "explanation",
            "depends_on": ["air_fact_02", "air_fact_03"], "may_be_omitted": False,
            "novelty": "explanatory_gain",
        },
        {
            "id": "air_fact_05", "claim": AIRPLANE_FACTS[4]["claim"], "role": "supporting_fact",
            "depends_on": ["air_fact_04"], "may_be_omitted": True, "novelty": "common",
        },
    ],
    "question_contract": {
        "core_question": "Why do airplanes leave white trails?",
        "subject_terms": ["airplanes", "white", "trails"],
        "essential_explanation_chain": ["air_fact_02", "air_fact_03", "air_fact_04", "air_fact_01"],
        "explanation_spine": {
            "status": "complete",
            "mechanism": ["air_fact_02", "air_fact_03", "air_fact_04"],
        },
    },
}


def airplane_context() -> dict:
    return {
        "prompt": "Why do airplanes leave white trails?",
        "intent": {
            "question": "Why do airplanes leave white trails?",
            "topic": "airplane white trails",
            "language": "en",
            "research_required": True,
            "content_type": "factual_explainer",
        },
        "facts": copy.deepcopy(AIRPLANE_FACTS),
        "story_arc": copy.deepcopy(AIRPLANE_ARC),
        "novelty_plan": {
            "explanatory_gain": ["air_fact_02", "air_fact_03", "air_fact_04"],
            "core_expected_facts": ["air_fact_01"],
            "common_context": ["air_fact_05"],
        },
        "payoff_plan": {"primary_answer_id": "air_fact_01", "final_payoff_id": "air_fact_01"},
        "script": {"triple_hook": {"verbal_hook": "Those white lines are more like winter breath than smoke."}},
        "reaction_plan": {},
    }


def weak_airplane_blocks() -> list[dict]:
    return [
        block("air_b1", "hook", "Those white lines are more like winter breath than smoke.", ["air_fact_05"]),
        block("air_b2", "answer", "That white line is a trail left behind by the plane.", ["air_fact_01"]),
        block(
            "air_b3", "explanation",
            "It forms when hot, humid air from the plane mixes with cold, dry air high in the sky.",
            ["air_fact_02", "air_fact_03", "air_fact_04"],
        ),
        block(
            "air_b4", "detail", "It is the same thing as your breath turning white on a winter day.",
            ["air_fact_05"],
        ),
        block("air_b5", "payoff", "That is why airplanes leave white trails.", ["air_fact_01"]),
    ]


def strong_airplane_blocks() -> list[dict]:
    return [
        block(
            "air_b1", "hook", "Those white trails behind airplanes are basically man-made clouds.",
            ["air_fact_01"],
        ),
        block("air_b2", "explanation", AIRPLANE_FACTS[1]["claim"], ["air_fact_02"]),
        block("air_b3", "detail", AIRPLANE_FACTS[2]["claim"], ["air_fact_03"]),
        block("air_b4", "detail", AIRPLANE_FACTS[3]["claim"], ["air_fact_04"]),
        block("air_b5", "payoff", AIRPLANE_FACTS[0]["claim"], ["air_fact_01"]),
    ]


def issue_types(report: dict) -> set[str]:
    return {str(item["issue_type"]) for item in report["issues"]}


def test_pipeline_runs_quality_before_production_derivatives():
    source = inspect.getsource(pipeline._build_initial_state)
    quality = source.index("run_script_story_quality(")
    assert quality < source.index("script_text =", quality)
    assert quality < source.index("_build_scenes(", quality)


def test_filler_detection_and_removal():
    blocks = strong_blocks()
    blocks.insert(2, block("filler", "support", "You won't believe what happens next."))
    report = assess_script_story_quality(blocks, context())
    assert "filler" in issue_types(report)
    edited, diagnostics = run_script_story_quality_v1(blocks, context())
    assert all(item["id"] != "filler" for item in edited)
    assert any(action["action"] == "remove" for action in diagnostics["actions"])


def test_redundancy_detection_and_merge():
    blocks = strong_blocks()
    blocks.insert(2, block("repeat", "support", FACTS[0]["claim"], ["fact_01"]))
    report = assess_script_story_quality(blocks, context())
    assert "redundancy" in issue_types(report)
    edited, _ = run_script_story_quality_v1(blocks, context())
    assert sum(item["text"] == FACTS[0]["claim"] for item in edited) == 1


def test_repeated_paraphrase_is_distinguished_from_exact_repetition():
    blocks = strong_blocks()
    blocks.insert(2, block("paraphrase", "support", "Gravity moves blood toward your legs when you stand.", ["fact_01"]))
    report = assess_script_story_quality(blocks, context())
    assert "repeated_paraphrase" in issue_types(report)


def test_low_information_line_is_reported():
    blocks = strong_blocks()
    blocks.insert(2, block("weak", "support", "This is important.", []))
    report = assess_script_story_quality(blocks, context())
    assert {"generic_statement", "low_information_line"} & issue_types(report)


def test_generic_statement_uses_exact_research_backed_specificity():
    blocks = strong_blocks()
    blocks[2] = block("b3", "explanation", "This is important.", ["fact_02"])
    edited, report = run_script_story_quality_v1(blocks, context())
    replacement = next(item for item in edited if item["id"] == "b3")
    assert replacement["text"] == FACTS[1]["claim"]
    assert any(action["action"] == "replace_with_supported_specific" for action in report["actions"])


def test_premature_reveal_is_detected_and_restored_after_dependencies():
    blocks = [strong_blocks()[0], strong_blocks()[3], strong_blocks()[1], strong_blocks()[2]]
    report = assess_script_story_quality(blocks, context())
    assert "premature_reveal" in issue_types(report)
    edited, diagnostics = run_script_story_quality_v1(blocks, context())
    assert [item["id"] for item in edited] == ["b1", "b2", "b3", "b4"]
    assert any(action["action"] == "reorder" for action in diagnostics["actions"])


def test_payoff_and_selected_hook_survive_safe_edits():
    blocks = strong_blocks()
    blocks.insert(2, block("filler", "support", "Pretty wild, right?"))
    edited, _ = run_script_story_quality_v1(blocks, context())
    assert edited[0]["text"] == blocks[0]["text"]
    payoff = next(item for item in edited if item["role"] == "payoff")
    assert payoff["fact_ids"] == ["fact_03"] and payoff["text"] == FACTS[2]["claim"]


def test_post_payoff_fluff_is_removed():
    blocks = [*strong_blocks(), block("outro", "support", "Thanks for watching. Follow for more!")]
    report = assess_script_story_quality(blocks, context())
    assert "post_payoff_fluff" in issue_types(report)
    edited, _ = run_script_story_quality_v1(blocks, context())
    assert edited[-1]["role"] == "payoff" and all(item["id"] != "outro" for item in edited)


def test_logical_dependency_violation_is_reported_and_reordered():
    blocks = [strong_blocks()[0], strong_blocks()[2], strong_blocks()[1], strong_blocks()[3]]
    report = assess_script_story_quality(blocks, context())
    assert "poor_fact_ordering" in issue_types(report)
    # The existing reveal-order authority only moves protected answers; a
    # non-reveal dependency violation is diagnosed rather than guessed away.
    edited, final = run_script_story_quality_v1(blocks, context())
    assert [item["id"] for item in edited] == ["b1", "b3", "b2", "b4"]
    assert final["gate"]["status"] == "passed_with_warnings"


def test_too_thin_script_requests_research_instead_of_padding():
    blocks = [
        block("b1", "hook", "Something happens when you stand."),
        block("b2", "payoff", "This is important."),
    ]
    report = assess_script_story_quality(blocks, context(facts=[]))
    assert report["length_assessment"] == "too_thin"
    assert report["research_insufficient"] is True
    assert "too_thin" in issue_types(report)


def test_artificial_lengthening_is_blocked():
    blocks = [
        strong_blocks()[0],
        block("f1", "support", "You won't believe what happens next."),
        block("f2", "support", "The answer may surprise you."),
        strong_blocks()[1],
        strong_blocks()[3],
    ]
    report = assess_script_story_quality(blocks, context())
    assert report["length_assessment"] == "unnecessary_length"
    assert "artificial_lengthening" in report["gate"]["blocking"]


def test_conclusion_that_only_restates_the_question_has_weak_payoff():
    blocks = strong_blocks()
    blocks[-1] = block("b4", "payoff", "That is why vision can go dark when you stand up quickly.", ["fact_03"])
    report = assess_script_story_quality(blocks, context())
    assert "weak_payoff" in issue_types(report)
    assert report["gate"]["ready"] is False


def test_real_weak_airplane_fixture_is_blocked_for_meaning_not_style():
    report = assess_script_story_quality(weak_airplane_blocks(), airplane_context())
    assert {
        "tautological_answer", "analogy_repetition", "weak_payoff", "low_information_gain",
        "vague_mechanism",
    } <= issue_types(report)
    assert report["gate"]["ready"] is False
    assert report["dimensions"]["information_density"] < 50
    assert report["dimensions"]["information_gain"] < 50


def test_tautological_answer_is_rejected_for_an_explanatory_question():
    report = assess_script_story_quality(weak_airplane_blocks(), airplane_context())
    issue = next(item for item in report["issues"] if item["issue_type"] == "tautological_answer")
    assert issue["segment_id"] == "air_b2"
    assert issue["severity"] == "error"


def test_question_restating_payoff_is_rejected():
    report = assess_script_story_quality(weak_airplane_blocks(), airplane_context())
    payoff = next(item for item in report["issues"] if item["issue_type"] == "weak_payoff")
    assert payoff["segment_id"] == "air_b5"
    assert payoff["severity"] == "error"


def test_duplicate_analogy_across_hook_and_detail_is_detected():
    report = assess_script_story_quality(weak_airplane_blocks(), airplane_context())
    analogy = next(item for item in report["issues"] if item["issue_type"] == "analogy_repetition")
    assert analogy["segment_id"] == "air_b4"
    assert analogy["severity"] == "error"


def test_payoff_with_zero_new_information_is_rejected():
    blocks = strong_airplane_blocks()
    blocks[-1] = block("air_b5", "payoff", AIRPLANE_FACTS[3]["claim"], ["air_fact_04"])
    report = assess_script_story_quality(blocks, airplane_context())
    assert "weak_payoff" in report["gate"]["blocking"]


def test_low_information_gain_floor_blocks_supported_labels_and_closure():
    report = assess_script_story_quality(weak_airplane_blocks(), airplane_context())
    assert "low_information_gain" in report["gate"]["blocking"]
    assert report["information_gain"]["summary"]["density"] == 1.0  # lexical baseline was fooled
    assert report["dimensions"]["information_density"] < 50  # semantic floor is not


def test_research_supported_mechanism_replaces_vague_mechanism_wording():
    edited, diagnostics = run_script_story_quality_v1(weak_airplane_blocks(), airplane_context())
    explanation = next(item for item in edited if item["id"] == "air_b3")
    assert explanation["text"] == " ".join(item["claim"] for item in AIRPLANE_FACTS[1:4])
    assert any(
        action["action"] == "replace_with_supported_specific" and action["segment_ids"] == ["air_b3"]
        for action in diagnostics["actions"]
    )


def test_strong_concise_factual_script_passes():
    report = assess_script_story_quality(strong_airplane_blocks(), airplane_context())
    assert report["gate"]["ready"] is True
    assert not ({"tautological_answer", "weak_payoff", "low_information_gain"} & issue_types(report))


def test_stronger_factual_version_is_not_over_edited():
    blocks = strong_airplane_blocks()
    edited, diagnostics = run_script_story_quality_v1(blocks, airplane_context())
    assert edited == blocks
    assert diagnostics["actions"] == []


def test_safe_specificity_rewrite_introduces_no_unresearched_text():
    _, diagnostics = run_script_story_quality_v1(weak_airplane_blocks(), airplane_context())
    claims = {item["id"]: item["claim"] for item in AIRPLANE_FACTS}
    rewrites = [
        action for action in diagnostics["actions"]
        if action["action"] == "replace_with_supported_specific"
    ]
    assert rewrites
    for action in rewrites:
        assert action["after"] == " ".join(claims[fact_id] for fact_id in action["fact_ids"])
    assert diagnostics["provider"]["status"] == "not_requested"


def test_valid_protected_hook_remains_byte_for_byte_unchanged():
    blocks = weak_airplane_blocks()
    hook = blocks[0]["text"]
    edited, _ = run_script_story_quality_v1(blocks, airplane_context())
    assert edited[0]["role"] == "hook"
    assert edited[0]["text"] == hook


def test_already_strong_script_is_a_no_op():
    blocks = strong_blocks()
    edited, report = run_script_story_quality_v1(blocks, context())
    assert edited == blocks
    assert report["actions"] == []
    assert set(report["dimensions"]) == {
        "information_density", "information_gain", "relevance_to_core_question", "redundancy",
        "specificity", "clarity", "logical_progression", "curiosity_progression", "payoff_strength",
        "post_payoff_efficiency", "factual_support", "spoken_naturalness",
    }


def test_second_pass_is_idempotent():
    blocks = strong_blocks()
    blocks.insert(2, block("filler", "support", "You won't believe what happens next."))
    first, _ = run_script_story_quality_v1(blocks, context())
    second, report = run_script_story_quality_v1(first, context())
    assert second == first
    assert report["actions"] == []
