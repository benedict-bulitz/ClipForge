"""Within-video information gain (novelty.py is the one content authority).

Topic novelty ("have we made this video?") stays with Topic Intelligence;
these tests cover level B (every unit adds something) and level C (the video
carries at least one supported, useful gain and a real payoff).
"""
from __future__ import annotations

import ast
import copy
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace

from test_triple_hook import Judge, ai_candidate, generation

from clipforge.config import Settings
from clipforge.novelty import (
    assess_information_gain,
    classify_gain,
    prune_redundant_information,
    refresh_information_gain,
)
from clipforge.pacing import analyze_pacing
from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.review import local_review_items, pre_render_quality_gate
from clipforge.schemas import AdvancedOptions
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.topic_intelligence.history import HistoryItem, is_duplicate, novelty_signal

CLIPFORGE = pathlib.Path(__file__).resolve().parents[1] / "clipforge"
STAND_Q = "Why does your vision go black when you stand up too fast?"


def fact(identifier: str, claim: str, *, sources: int = 1, verification: str = "source_attributed") -> dict:
    return {
        "id": identifier, "claim": claim, "confidence": 0.9, "importance": 0.8, "verification": verification,
        "sources": [{"label": f"s{n}", "url": f"https://{identifier}-{n}.test/a"} for n in range(sources)],
    }


STAND_FACTS = [
    fact("fact_01", "When you stand up, gravity pulls blood toward your legs and away from your brain."),
    fact("fact_02", "Blood pressure sensors called baroreceptors need a few seconds to tighten blood vessels and raise the heart rate."),
    fact("fact_03", "Until circulation compensates, the brain briefly gets less oxygen, so vision can go dark."),
]


def block(identifier: str, role: str, text: str, fact_ids: list[str] | None = None) -> dict:
    return {"id": identifier, "role": role, "text": text, "fact_ids": list(fact_ids or [])}


def project(blocks: list[dict], *, facts: list[dict] | None = None, question: str = STAND_Q, arc: dict | None = None,
            novelty: dict | None = None) -> dict:
    return {
        "intent": {"question": question, "topic": question, "research_required": True, "content_type": "factual_explainer"},
        "facts": copy.deepcopy(STAND_FACTS if facts is None else facts),
        "script": {"blocks": blocks, "text": " ".join(item["text"] for item in blocks)},
        "story_arc": arc or {},
        "novelty_plan": novelty or {},
        "scenes": [],
    }


def unit(report: dict, block_id: str) -> dict:
    return next(item for item in report["units"] if item["block_id"] == block_id)


# ---------------------------------------------------------------------------
# Hook -> body
# ---------------------------------------------------------------------------

def test_hook_paraphrase_immediately_after_the_hook_is_detected():
    report = assess_information_gain(project([
        block("b1", "hook", "Your body does something strange when you stand up too fast."),
        block("b2", "answer", "Standing up quickly can make your body react strangely.", ["fact_01"]),
        block("b3", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
    ]))
    assert report["hook_transition"]["status"] == "fail"
    assert unit(report, "b2")["redundancy"] == "restates_hook"
    assert unit(report, "b2")["counts_as_gain"] is False
    assert "hook_body_no_information_gain" in {issue["code"] for issue in report["issues"]}


def test_a_real_new_fact_after_the_hook_passes():
    report = assess_information_gain(project([
        block("b1", "hook", STAND_Q),
        block("b2", "answer", "For a moment, gravity pulls blood away from your brain toward your legs.", ["fact_01"]),
    ]))
    assert report["hook_transition"]["status"] == "pass"
    assert {"gravity", "blood", "brain"} <= set(report["hook_transition"]["gain_terms"])
    assert unit(report, "b2")["counts_as_gain"] and unit(report, "b2")["evidence"]["status"] == "supported"


# ---------------------------------------------------------------------------
# Redundancy across units and scenes
# ---------------------------------------------------------------------------

def test_a_repeated_fact_across_two_scenes_is_detected_by_the_content_authority_and_pacing():
    blocks = [
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "support", "Your brain loses blood because gravity pulls it away.", ["fact_01"]),
        block("b4", "explanation", "Baroreceptors need a few seconds to tighten your blood vessels.", ["fact_02"]),
    ]
    state = project(blocks)
    state["scenes"] = [
        {"id": f"scene_{item['id']}", "block_id": item["id"], "narration": item["text"], "start": index * 3.0, "end": index * 3.0 + 3,
         "visual_goal": "different shot", "visual_intent": {"visual_goal": "different shot", "objects": []}, "search_queries": []}
        for index, item in enumerate(blocks)
    ]
    state["captions"], state["duration"], state["timeline"] = {"items": []}, {"speaking_rate_wpm": 150}, {}
    report = assess_information_gain(state)
    assert unit(report, "b3")["redundancy"] != "none" and unit(report, "b3")["repeats_block_id"] == "b2"
    assert unit(report, "b4")["redundancy"] == "none"
    pacing = analyze_pacing(state)
    row = next(item for item in pacing["scene_assessments"] if item["scene_id"] == "scene_b3")
    assert row["redundancy"] != "none" and row["recommendation"] in {"MERGE_WITH_PREVIOUS", "TRIM"}
    assert row["information_gain"] <= 0.1
    # The same report is persisted for inspection.
    assert state["information_gain"]["units"] and state["information_gain"]["signature"] == report["signature"]


def test_paraphrased_repetition_is_detected_not_only_exact_duplicates():
    earlier = "Fireflies glow through a chemical reaction."
    assert classify_gain(earlier, "Fireflies produce light through a chemical reaction.")["category"] == "paraphrase"
    assert classify_gain(earlier, earlier)["category"] == "restatement"
    # A genuinely new element is not mistaken for a paraphrase.
    assert classify_gain("Egypt is famous for pyramids.", "Sudan has more pyramids than Egypt.")["category"] not in {"paraphrase", "restatement"}


def test_mechanism_counts_as_new_information():
    result = classify_gain(
        "Your vision goes dark when you stand up.",
        "That happens because baroreceptors need a few seconds to raise your heart rate.",
    )
    assert result["category"] == "mechanism" and {"baroreceptors", "heart", "rate"} <= set(result["gain_terms"])


# ---------------------------------------------------------------------------
# Payoff
# ---------------------------------------------------------------------------

def test_a_payoff_that_merely_restates_the_question_fails():
    report = assess_information_gain(project([
        block("b1", "hook", "Stand up fast and the world can go dark for a second."),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "payoff", "That's why your vision goes black when you stand up too fast.", ["fact_03"]),
    ]))
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "restates_question"
    assert "payoff_restates_question" in {issue["code"] for issue in report["issues"]}


def test_a_strong_factual_payoff_passes():
    report = assess_information_gain(project([
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "payoff", "Until baroreceptors tighten your vessels, your brain gets less oxygen, so vision goes dark.", ["fact_02", "fact_03"]),
    ]))
    assert report["payoff"]["status"] == "pass" and report["payoff"]["result"] == "strong"


def test_a_comparison_payoff_that_picks_a_side_is_an_answer():
    facts = [fact("fact_01", "Sudan has more pyramids than Egypt.")]
    report = assess_information_gain(project([
        block("b1", "hook", "Egypt is famous for pyramids - but is it the record holder?"),
        block("b2", "payoff", "Sudan has more pyramids than Egypt.", ["fact_01"]),
    ], facts=facts, question="Which country has more pyramids, Egypt or Sudan?"))
    assert report["payoff"]["status"] == "pass"


def test_the_protected_payoff_stays_protected_during_repair():
    arc = {
        "primary_answer_id": "fact_03", "final_payoff_id": "fact_03", "curiosity_gap": {"withhold_answer": True},
        "hook": {"protected_ids": ["fact_03"]},
        "units": [{"id": "fact_01", "depends_on": []}, {"id": "fact_02", "depends_on": []}, {"id": "fact_03", "depends_on": ["fact_01"]}],
    }
    blocks = [
        block("b1", "hook", "Your body does something strange when you stand up too fast."),
        block("b2", "support", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "detail", "Gravity pulls blood away from your brain.", ["fact_02"]),
        # The reveal repeats a word of the hook - still never removed or moved.
        block("b4", "payoff", "Your brain briefly gets less oxygen, so vision goes dark.", ["fact_03"]),
    ]
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), project(blocks, arc=arc))
    assert [item["id"] for item in pruned] == ["b1", "b2", "b4"]
    assert pruned[-1] == blocks[-1]
    # The dropped repetition's fact moves to the unit that already says it.
    assert pruned[1]["fact_ids"] == ["fact_01", "fact_02"] and repairs[0]["action"] == "merge_redundant"
    # The reveal keeps at least one unit before it, even a redundant one.
    tight = [blocks[0], block("b2", "support", "Your body does something strange when you stand up.", ["fact_01"]), blocks[3]]
    kept, _ = prune_redundant_information(copy.deepcopy(tight), project(tight, arc=arc))
    assert [item["id"] for item in kept] == ["b1", "b2", "b4"]


# ---------------------------------------------------------------------------
# Filler and safe repair
# ---------------------------------------------------------------------------

def test_generic_filler_is_flagged_and_removed_and_empty_lead_ins_are_trimmed():
    blocks = [
        block("b1", "hook", STAND_Q),
        block("b2", "support", "Let's find out."),
        block("b3", "explanation", "Believe it or not, gravity pulls blood away from your brain.", ["fact_01"]),
        block("b4", "payoff", "Your brain briefly gets less oxygen, so vision goes dark.", ["fact_03"]),
    ]
    report = assess_information_gain(project(copy.deepcopy(blocks)))
    assert unit(report, "b2")["category"] == "filler" and "filler_segment" in {issue["code"] for issue in report["issues"]}
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), project(blocks))
    assert [item["text"] for item in pruned] == [STAND_Q, "Gravity pulls blood away from your brain.", blocks[3]["text"]]
    assert {repair["action"] for repair in repairs} == {"trim_lead_in", "remove_filler"}
    # Idempotent, and nothing is ever added.
    again, more = prune_redundant_information(copy.deepcopy(pruned), project(pruned))
    assert again == pruned and more == []
    original_words = set(" ".join(item["text"] for item in blocks).casefold().split())
    assert set(" ".join(item["text"] for item in pruned).casefold().split()) <= original_words


def test_a_paraphrase_gives_way_to_the_next_richer_supported_unit():
    blocks = [
        block("b1", "hook", STAND_Q),
        block("b2", "support", "Gravity pulls blood.", ["fact_01"]),
        block("b3", "explanation", "Gravity pulls blood away from your brain toward your legs.", ["fact_01"]),
        block("b4", "payoff", "Your brain briefly gets less oxygen, so vision goes dark.", ["fact_03"]),
    ]
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), project(blocks))
    assert [item["id"] for item in pruned] == ["b1", "b3", "b4"]
    assert repairs == [{"action": "replace_with_stronger", "block_id": "b2", "text": "Gravity pulls blood.", "category": "subsumed",
                        "repeats_block_id": "b3", "moved_fact_ids": ["fact_01"]}]


# ---------------------------------------------------------------------------
# Evidence grounding
# ---------------------------------------------------------------------------

def test_an_evidence_supported_surprising_fact_passes():
    facts = [*STAND_FACTS, fact("fact_04", "Surprisingly, fit athletes faint more often on standing because their resting heart rate is low.", sources=2)]
    novelty = {"distinctive_facts": ["fact_04"], "status": "planned"}
    report = assess_information_gain(project([
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "payoff", "Surprisingly, fit athletes faint more often, because their resting heart rate is low.", ["fact_04"]),
    ], facts=facts, novelty=novelty))
    row = unit(report, "b3")
    assert row["counts_as_gain"] and row["evidence"]["status"] == "supported" and row["novelty_class"] == "distinctive"
    assert report["summary"]["audience_value"] == "strong"
    assert report["summary"]["strongest"]["block_id"] == "b3"
    assert report["payoff"]["status"] == "pass"


def test_an_unsupported_novel_fact_never_passes():
    blocks = [
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        # Uncited and not in any research fact: a "surprising" invention.
        block("b3", "support", "Astronauts train with spinning centrifuges to prevent this blackout."),
        # Cites a real fact but adds an invented figure.
        block("b4", "payoff", "About 73 percent of people black out this way.", ["fact_03"]),
    ]
    state = project(blocks)
    report = assess_information_gain(state)
    assert not unit(report, "b3")["counts_as_gain"] and unit(report, "b3")["evidence"]["status"] == "unsupported"
    assert not unit(report, "b4")["counts_as_gain"] and "73" in unit(report, "b4")["evidence"]["reason"]
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "unsupported"
    assert report["summary"]["strongest"]["block_id"] == "b2"
    # Unverified facts are not evidence either.
    weak = project([block("b1", "hook", STAND_Q), block("b2", "payoff", "Gravity pulls blood away from your brain.", ["fact_01"])],
                   facts=[fact("fact_01", STAND_FACTS[0]["claim"], sources=0, verification="unverified_model_synthesis")])
    weak_report = assess_information_gain(weak)
    assert weak_report["summary"]["gain_units"] == 0
    gate = pre_render_quality_gate(weak)
    assert gate["status"] == "fallback" and "information_gain_no_supported_information_gain" in gate["severe_issues"]


def test_review_and_quality_gate_consume_the_same_findings():
    state = project([
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "support", "Blood gets pulled away from your brain by gravity.", ["fact_01"]),
        block("b4", "payoff", "That's why your vision goes black when you stand up too fast.", ["fact_03"]),
    ])
    items = local_review_items(state)
    assert any(item["check"] == "repetition" for item in items)
    assert any(item["check"] == "payoff" and "restates the question" in item["message"] for item in items)
    gate = pre_render_quality_gate(state)
    codes = {issue["code"] for issue in gate["issues"]}
    assert {"information_gain_redundant_segment", "information_gain_payoff_restates_question"} <= codes
    assert gate["status"] == "passed_with_warnings"


def test_shorter_high_density_script_beats_longer_repetitive_script():
    dense = project([
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "payoff", "Your brain briefly gets less oxygen, so vision goes dark.", ["fact_03"]),
    ])
    long = project([
        block("b1", "hook", STAND_Q),
        block("b2", "explanation", "Gravity pulls blood away from your brain.", ["fact_01"]),
        block("b3", "support", "Blood gets pulled away from your brain by gravity.", ["fact_01"]),
        block("b4", "support", "So gravity pulls blood from your brain.", ["fact_01"]),
        block("b5", "payoff", "Your brain briefly gets less oxygen, so vision goes dark.", ["fact_03"]),
    ])
    short_report, long_report = assess_information_gain(dense), assess_information_gain(long)
    assert short_report["summary"]["score"] > long_report["summary"]["score"]
    assert short_report["status"] == "dense" and long_report["status"] == "diluted"
    assert short_report["summary"]["density"] == 1.0 > long_report["summary"]["density"]


def test_refresh_keeps_repair_history_and_survives_bad_input():
    state = project([block("b1", "hook", STAND_Q), block("b2", "payoff", "Gravity pulls blood away from your brain.", ["fact_01"])])
    state["information_gain"] = {"repairs": [{"action": "remove_filler", "text": "Let's find out."}]}
    report = refresh_information_gain(state)
    assert report["repairs"] == [{"action": "remove_filler", "text": "Let's find out."}] and report["status"] == "thin"
    broken = {"script": {"blocks": [{"id": "x", "text": "Some words here."}]}, "facts": "not-a-list"}
    assert refresh_information_gain(broken)["status"] in {"fallback", "thin", "empty"}


# ---------------------------------------------------------------------------
# Topic novelty stays with Topic Intelligence
# ---------------------------------------------------------------------------

def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def test_no_duplicate_topic_level_novelty_system_is_created():
    # The content authority never looks at other projects/history ...
    assert not any("topic_intelligence" in module or "youtube" in module or module in {"models", "database"} for module in _imports(CLIPFORGE / "novelty.py"))
    source = (CLIPFORGE / "novelty.py").read_text()
    assert "Session" not in source and "HistoryItem" not in source
    # ... and Topic Intelligence keeps its own single novelty signal.
    definitions = [
        path.name for path in CLIPFORGE.rglob("*.py")
        if "def novelty_signal" in path.read_text()
    ]
    assert definitions == ["history.py"]
    assert not any("novelty" == module.rsplit(".", 1)[-1] for path in (CLIPFORGE / "topic_intelligence").glob("*.py") for module in _imports(path))


def test_topic_intelligence_novelty_is_unchanged():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    history = [HistoryItem("Why do cats purr?", "project", "p1", now)]
    duplicate = novelty_signal("Why do cats purr?", "cats purr", "animals", history, now=now)
    fresh = novelty_signal("How do volcanoes form new islands?", "volcano islands", "geography", history, now=now)
    assert is_duplicate(duplicate) and duplicate.value is not None and duplicate.value < 0.3
    assert not is_duplicate(fresh) and fresh.value == 1.0
    assert duplicate.evidence["method"] == "soft_token_overlap_vs_history"


# ---------------------------------------------------------------------------
# End to end: thin evidence -> a shorter video, never invented content
# ---------------------------------------------------------------------------

FIREFLY_Q = "Why do fireflies glow?"
FIREFLY_FACTS = [
    {"claim": "Fireflies glow because a chemical reaction in their abdomen produces light.", "importance": 0.95, "confidence": 0.9},
    {"claim": "The reaction needs an enzyme called luciferase and oxygen.", "importance": 0.8, "confidence": 0.9},
]
FIREFLY_BODY = [
    ScriptBlockV2(role="answer", text="Fireflies glow because a chemical reaction in their abdomen produces light.", fact_ids=["fact_01"]),
    ScriptBlockV2(role="support", text="In other words, a chemical reaction makes fireflies glow.", fact_ids=["fact_01"]),
    ScriptBlockV2(role="support", text="Let's find out.", fact_ids=["fact_01"]),
    ScriptBlockV2(role="payoff", text="That reaction needs an enzyme called luciferase and oxygen.", fact_ids=["fact_02"]),
]


class FireflyWriter:
    name = "fixture-v2"

    def __init__(self, _settings=None):
        pass

    def generate(self, _request):
        return ScriptWriterResult(ScriptDraftV2(language="en", blocks=FIREFLY_BODY), "connected")


class OfflineReviewer:
    name = "fixture-review"

    def __init__(self, _settings=None):
        pass

    def review(self, _request):
        raise RuntimeError("offline")


def test_generation_produces_a_shorter_video_when_evidence_is_thin(monkeypatch, tmp_path):
    source = {"label": "Source", "url": "https://source.test/fireflies"}
    research = [{**item, "sources": [source]} for item in FIREFLY_FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", FireflyWriter)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    candidates = [ai_candidate("A", "curiosity_gap", "What makes a firefly light up in the dark?", "firefly glowing at night",
                               ["firefly glowing night"], action="glowing", detail="light", payoff_fact="fact_01")]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    settings = Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)
    state = build_initial_state(FIREFLY_Q, AdvancedOptions(), settings)

    body = [item for item in state["script"]["blocks"] if item["role"] != "hook"]
    texts = [item["text"] for item in body]
    writer_sentences = {item.text for item in FIREFLY_BODY} | {"A chemical reaction makes fireflies glow."}
    # Shorter: the paraphrase and the filler are gone ...
    assert len(body) < len(FIREFLY_BODY)
    assert "Let's find out." not in texts and not any("chemical reaction makes fireflies glow" in text for text in texts)
    # ... nothing was invented, the payoff and every fact survive.
    assert set(texts) <= writer_sentences
    assert texts[-1] == FIREFLY_BODY[-1].text
    assert {fact_id for item in body for fact_id in item.get("fact_ids") or []} == {"fact_01", "fact_02"}
    gain = state["information_gain"]
    assert gain["repairs"] and {repair["action"] for repair in gain["repairs"]} & {"remove_filler", "remove_redundant", "merge_redundant"}
    assert gain["summary"]["redundant_units"] == 0 and gain["payoff"]["status"] == "pass"
    assert all(item["redundancy"] is not None for item in state["pacing_analysis"]["scene_assessments"] if item["purpose"] != "hook")
    hook_words = len(state["script"]["blocks"][0]["text"].split())
    assert state["script"]["word_count"] < hook_words + sum(len(item.text.split()) for item in FIREFLY_BODY)
