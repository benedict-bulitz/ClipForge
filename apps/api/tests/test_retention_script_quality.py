"""Retention-driven script quality: core question, open hooks, progressive beats.

Three real failure shapes:

* time perception: the hook ("Je weniger neue Erlebnisse hängen bleiben,
  desto schneller ...") already said the mechanism, so the body had nothing
  left to explain;
* mirror / photo: must keep its full explanation chain (no over-pruning);
* TikTok (reconstructed - no fixture of that run was kept): true facts about
  the same app (cache, user numbers) drifted into a video asking why we open
  it without wanting to.

Topics are fixtures only; production code has no topic vocabulary.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import test_semantic_redundancy as mirror
import test_time_perception_redundancy as time_case
from test_triple_hook import Judge, ai_candidate, generation

from clipforge import ai, script_review, script_writer
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import (
    assess_information_gain,
    build_novelty_plan,
    information_gain_quality_issues,
    prune_redundant_information,
)
from clipforge.pipeline import RETENTION_REQUIREMENTS, build_initial_state
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_review import ScriptReviewResponse, ScriptReviewResult
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import build_story_arc, mechanism_claims, story_brief
from clipforge.triple_hook import blend_judgement
from clipforge.verbal_hook import explains_mechanism


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


def intent(question: str) -> dict:
    return {"topic": question, "question": question, "content_type": "factual_explainer", "language": "de", "research_required": True}


def arc_for(question: str, facts: list[dict], supplied: dict | None = None) -> dict:
    novelty = build_novelty_plan(intent(question), facts)
    return build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty, supplied=supplied)


def state_for(question: str, facts: list[dict], blocks: list[dict], supplied: dict | None = None) -> dict:
    novelty = build_novelty_plan(intent(question), facts)
    return {
        "intent": intent(question), "facts": copy.deepcopy(facts), "novelty_plan": novelty,
        "story_arc": arc_for(question, facts, supplied), "script": {"blocks": copy.deepcopy(blocks)},
    }


def body_texts(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


# --- TikTok (reconstructed) --------------------------------------------------

TIKTOK_Q = "Warum öffnen wir TikTok, obwohl wir es gar nicht wollten?"
TIKTOK_FACTS = [
    fact(1, "Push-Benachrichtigungen lösen einen Reflex aus, die App zu öffnen, oft bevor wir bewusst entscheiden.", 0.9),
    fact(2, "Der endlose Feed belohnt uns unvorhersehbar, wie ein Spielautomat, deshalb greifen wir immer wieder zur App."),
    fact(3, "TikTok speichert Videos im Cache, deshalb wird der Speicher auf dem Handy voll."),
    fact(4, "TikTok hat weltweit über eine Milliarde aktive Nutzer."),
]
T_HOOK = "Du wolltest nur kurz die Uhrzeit checken – und plötzlich bist du wieder auf TikTok."
T_ANSWER = "Push-Benachrichtigungen lösen einen Reflex aus: Wir öffnen die App, bevor wir uns bewusst entscheiden."
T_CACHE = "TikTok speichert außerdem Videos im Cache, deshalb wird der Speicher auf deinem Handy voll."
T_USERS = "Und TikTok hat weltweit über eine Milliarde aktive Nutzer."
T_PAYOFF = "Der endlose Feed belohnt dich unvorhersehbar, wie ein Spielautomat – deshalb greifst du immer wieder zur App."
TIKTOK_BLOCKS = [
    {"id": "b1", "role": "hook", "text": T_HOOK},
    {"id": "b2", "role": "answer", "text": T_ANSWER, "fact_ids": ["fact_01"]},
    {"id": "b3", "role": "support", "text": T_CACHE, "fact_ids": ["fact_03"]},
    {"id": "b4", "role": "support", "text": T_USERS, "fact_ids": ["fact_04"]},
    {"id": "b5", "role": "payoff", "text": T_PAYOFF, "fact_ids": ["fact_02"]},
]


# ---------------------------------------------------------------------------
# 1. Core question contract
# ---------------------------------------------------------------------------

def test_the_arc_carries_one_core_question_contract():
    arc = arc_for(TIKTOK_Q, TIKTOK_FACTS)
    contract = arc["question_contract"]
    assert contract["core_question"] == TIKTOK_Q
    assert contract["hook_promise"]
    assert contract["primary_answer_id"] == "fact_01" and contract["final_resolution_id"] == "fact_02"
    assert contract["final_resolution"] == TIKTOK_FACTS[1]["claim"]
    assert contract["essential_explanation_chain"] == ["fact_01", "fact_02"]
    assert contract["off_question_ids"] == ["fact_03", "fact_04"]
    assert contract["subject_terms"] == ["tiktok"]
    # The writer sees the same contract and every fact's relevance.
    brief = story_brief(arc)
    assert brief["question_contract"]["essential_explanation_chain"] == ["fact_01", "fact_02"]
    assert {item["fact_id"]: item["serves_question"] for item in brief["information_order"]} == {
        "fact_01": True, "fact_02": True, "fact_03": False, "fact_04": False,
    }


def test_a_same_subject_tangent_never_becomes_the_closing_beat():
    # Research order puts the cache explanation ("deshalb ...") last; it used
    # to become the final payoff of an answer-first arc.
    units = {unit["id"]: unit for unit in arc_for(TIKTOK_Q, TIKTOK_FACTS)["units"]}
    assert units["fact_03"]["off_question"] and units["fact_03"]["may_be_omitted"]
    assert units["fact_04"]["off_question"] and units["fact_04"]["may_be_omitted"]
    assert "fact_03" not in units["fact_02"]["depends_on"]


def test_the_planners_semantic_relevance_wins():
    supplied = {"units": [
        {"fact_index": 4, "role": "evidence", "serves_question": False},
        {"fact_index": 3, "role": "supporting_fact", "serves_question": True},
    ], "primary_answer_index": 1, "final_payoff_index": 2}
    units = {unit["id"]: unit for unit in arc_for(TIKTOK_Q, TIKTOK_FACTS, supplied)["units"]}
    assert units["fact_04"]["question_link"] == "off_question" and units["fact_04"]["may_be_omitted"]
    # The planner judged the lexically isolated fact relevant: it stays.
    assert units["fact_03"]["question_link"] == "planner" and not units["fact_03"]["off_question"]


def test_mirror_keeps_its_whole_explanation_chain():
    # Sparse paraphrased concepts are no evidence of a tangent: nothing is dropped.
    arc = arc_for(mirror.QUESTION, mirror.FACTS)
    assert arc["question_contract"]["off_question_ids"] == []
    assert all(not unit["may_be_omitted"] for unit in arc["units"])
    assert arc["question_contract"]["essential_explanation_chain"] == [unit["id"] for unit in arc["units"]]


def test_a_required_fact_is_never_off_the_question():
    supplied = {"units": [{"fact_index": 3, "role": "explanation", "serves_question": False}], "final_payoff_index": 3, "primary_answer_index": 1}
    units = {unit["id"]: unit for unit in arc_for(TIKTOK_Q, TIKTOK_FACTS, supplied)["units"]}
    assert not units["fact_03"]["off_question"] and not units["fact_03"]["may_be_omitted"]


# ---------------------------------------------------------------------------
# 2. The hook opens, it does not finish
# ---------------------------------------------------------------------------

def test_the_real_time_hook_gives_away_the_mechanism():
    arc = arc_for(time_case.QUESTION, time_case.FACTS)
    spent = explains_mechanism(time_case.HOOK, time_case.QUESTION, mechanism_claims(arc))
    assert {"+novel", "+memorable"} <= set(spent)


def test_openers_that_leave_the_why_open_pass():
    mechanisms = mechanism_claims(arc_for(time_case.QUESTION, time_case.FACTS))
    for opener in (
        "Ein Jahr dauert immer gleich lang – trotzdem rast es für Ältere schneller vorbei.",
        "Warum fühlen sich Jahre im Rückblick plötzlich so kurz an?",
        "Je älter du wirst, desto schneller rast die Zeit.",  # the observation, not the cause
        "Für Ältere vergeht die Zeit im Rückblick schneller – aber nicht, weil die Uhr anders tickt.",
    ):
        assert explains_mechanism(opener, time_case.QUESTION, mechanisms) == [], opener
    assert explains_mechanism(mirror.HOOK, mirror.QUESTION, mechanism_claims(arc_for(mirror.QUESTION, mirror.FACTS))) == []


def test_the_hook_judge_can_veto_a_spent_explanation():
    assessment = {"hard_fail": [], "reason_codes": [], "dimensions": {}}
    blended = blend_judgement(assessment, {"veto": "spends_explanation"})
    assert not blended["eligible"] and "judge_spends_explanation" in blended["hard_fail"]
    assert "spends_explanation" in ai.AITripleHookJudgement.model_fields["veto"].annotation.__args__


def test_hook_prompts_say_open_do_not_finish():
    assert "OPEN, DO NOT FINISH" in ai.HOOK_GENERATION_INSTRUCTIONS
    assert "spends_explanation" in ai.TRIPLE_HOOK_JUDGE_INSTRUCTIONS
    assert "serves_question" in ai.DIRECTOR_INSTRUCTIONS
    assert "serves_question" in ai.AIStoryUnit.model_fields


def _time_generation(monkeypatch, tmp_path, hooks: list[str]) -> dict:
    source = {"label": "Quelle", "url": "https://source.test/zeit"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in time_case.FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", time_case.Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", time_case.OfflineReviewer)
    candidates = [
        ai_candidate(chr(65 + index), "curiosity_gap" if index else "direct_reframe", text, "older person looking at a calendar",
                     ["older person calendar"], action="looking", detail="calendar", payoff_fact="fact_04")
        for index, text in enumerate(hooks)
    ]
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    return build_initial_state(time_case.QUESTION, AdvancedOptions(), Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path))


HONEST = "Ein Jahr dauert immer gleich lang – trotzdem rast es für Ältere schneller vorbei."


def test_time_example_an_honest_opening_beats_the_mechanism_hook(monkeypatch, tmp_path):
    state = _time_generation(monkeypatch, tmp_path, [time_case.HOOK, HONEST])
    blocks = state["script"]["blocks"]
    assert blocks[0]["role"] == "hook" and blocks[0]["text"] == HONEST
    rejected = {item["verbal_hook"]: item for item in state["script"]["triple_hook"]["selection"]["candidates"]}
    assert "explains_mechanism" in rejected[time_case.HOOK]["hard_fail"]
    body = body_texts(blocks)
    # The mechanism now lives in the body, where it is explained ...
    assert time_case.S_EXPLANATION in body
    # ... before the "that's why" payoff; no restatement survives, nothing is invented.
    assert blocks[-1]["role"] == "payoff" and blocks[-1]["text"] == time_case.S_ROUTINE
    assert set(body) <= set(time_case.REAL_SCRIPT) and time_case.S_SUPPORT not in body and time_case.S_PAYOFF not in body
    gain = state["information_gain"]
    assert gain["payoff"]["status"] == "pass"
    assert gain["summary"]["beat_classes"]["redundant"] == 0
    assert not any(issue["code"] == "hook_explains_mechanism" for issue in gain["issues"])


def test_time_example_with_only_the_spent_hook_opens_with_the_observation(monkeypatch, tmp_path):
    state = _time_generation(monkeypatch, tmp_path, [time_case.HOOK])
    blocks = state["script"]["blocks"]
    assert blocks[0]["text"] != time_case.HOOK
    assert time_case.S_EXPLANATION in body_texts(blocks)
    assert all(block["text"] in [*time_case.REAL_SCRIPT, blocks[0]["text"]] for block in blocks)
    assert len(body_texts(blocks)) < len(time_case.REAL_SCRIPT)  # shorter, never padded
    assert blocks[-1]["role"] == "payoff"


def test_the_gate_reports_a_hook_that_spends_the_mechanism():
    blocks = [{"id": "b1", "role": "hook", "text": time_case.HOOK}, *copy.deepcopy(time_case.WRITER_BLOCKS)]
    issues = information_gain_quality_issues(state_for(time_case.QUESTION, time_case.FACTS, blocks))
    assert any(issue["code"] == "hook_explains_mechanism" and issue["severity"] == "warning" for issue in issues)


# ---------------------------------------------------------------------------
# 3. Relevance, progression, plateaus and momentum
# ---------------------------------------------------------------------------

def test_tiktok_drift_is_classified_off_chain():
    report = assess_information_gain(state_for(TIKTOK_Q, TIKTOK_FACTS, TIKTOK_BLOCKS))
    beats = {unit["text"]: unit for unit in report["units"] if unit["category"] != "hook"}
    assert beats[T_ANSWER]["beat_class"] == "useful_gain" and beats[T_ANSWER]["chain_role"] == "answer"
    assert beats[T_PAYOFF]["beat_class"] == "useful_gain" and beats[T_PAYOFF]["chain_role"] == "payoff"
    assert beats[T_CACHE]["beat_class"] == "off_chain" and beats[T_USERS]["beat_class"] == "off_chain"
    codes = [issue["code"] for issue in report["issues"]]
    assert codes.count("off_question_segment") == 2
    assert "information_plateau" in codes  # two tangents in a row: the viewer stalls


def test_tiktok_drift_is_pruned_without_inventing_anything():
    state = state_for(TIKTOK_Q, TIKTOK_FACTS, TIKTOK_BLOCKS)
    pruned, repairs = prune_redundant_information(copy.deepcopy(TIKTOK_BLOCKS), state)
    assert body_texts(pruned) == [T_ANSWER, T_PAYOFF]
    assert [repair["action"] for repair in repairs] == ["remove_off_question", "remove_off_question"]
    assert {fact_id for repair in repairs for fact_id in repair["dropped_fact_ids"]} == {"fact_03", "fact_04"}
    state["script"]["blocks"] = pruned
    report = assess_information_gain(state)
    assert not any(issue["code"] in {"off_question_segment", "information_plateau"} for issue in report["issues"])
    assert report["payoff"]["status"] == "pass"


def test_a_subject_fact_tied_back_to_the_question_survives():
    tied = "TikTok speichert Videos im Cache, deshalb startet die App so schnell, dass wir sie öffnen, bevor wir nachdenken."
    blocks = [*copy.deepcopy(TIKTOK_BLOCKS[:2]), {"id": "b3", "role": "support", "text": tied, "fact_ids": ["fact_03"]}, copy.deepcopy(TIKTOK_BLOCKS[-1])]
    state = state_for(TIKTOK_Q, TIKTOK_FACTS, blocks)
    units = {unit["text"]: unit for unit in assess_information_gain(state)["units"]}
    assert units[tied]["beat_class"] != "off_chain"
    pruned, _repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert tied in body_texts(pruned)


def test_only_tangents_fail_instead_of_padding():
    blocks = [{"id": "b1", "role": "hook", "text": T_HOOK}, {"id": "b2", "role": "support", "text": T_CACHE, "fact_ids": ["fact_03"]},
              {"id": "b3", "role": "support", "text": T_USERS, "fact_ids": ["fact_04"]}]
    supplied = {"units": [{"fact_index": 3, "serves_question": False, "role": "supporting_fact"},
                          {"fact_index": 4, "serves_question": False, "role": "evidence"}], "primary_answer_index": 1}
    issues = information_gain_quality_issues(state_for(TIKTOK_Q, TIKTOK_FACTS, blocks, supplied))
    assert any(issue["code"] == "no_question_relevant_information" and issue["severity"] == "error" for issue in issues)


def test_every_beat_reports_viewer_momentum():
    report = assess_information_gain(state_for(TIKTOK_Q, TIKTOK_FACTS, TIKTOK_BLOCKS[:2] + TIKTOK_BLOCKS[-1:]))
    body = [unit for unit in report["units"] if unit["category"] != "hook"]
    for unit in body:
        assert set(unit["momentum"]) == {"viewer_knows_before", "new_information", "viewer_understands_after", "unresolved_question", "reason_to_continue"}
    first, last = body[0], body[-1]
    assert first["momentum"]["viewer_knows_before"] == [] and first["momentum"]["new_information"]
    assert first["momentum"]["unresolved_question"] == TIKTOK_Q
    assert set(first["momentum"]["new_information"]) <= set(last["momentum"]["viewer_knows_before"])
    assert last["momentum"]["unresolved_question"] == "" and last["momentum"]["reason_to_continue"].startswith("none")
    assert report["question_contract"]["hook_promise"] == T_HOOK
    assert report["summary"]["beat_classes"] == {"useful_gain": 2, "redundant": 0, "off_chain": 0, "unsupported": 0, "weak_value": 0}


def test_the_first_body_beat_adds_value_after_the_hook():
    report = assess_information_gain(state_for(TIKTOK_Q, TIKTOK_FACTS, TIKTOK_BLOCKS))
    assert report["hook_transition"]["status"] == "pass"


def test_mirror_script_has_no_off_chain_or_plateau_beats():
    blocks = [{"id": "b1", "role": "hook", "text": mirror.HOOK}, *copy.deepcopy(mirror.WRITER_BLOCKS)]
    state = state_for(mirror.QUESTION, mirror.FACTS, blocks)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert not any(repair["action"] in {"remove_off_question", "remove_weak_value"} for repair in repairs)
    state["script"]["blocks"] = pruned
    report = assess_information_gain(state)
    assert report["summary"]["beat_classes"]["off_chain"] == 0
    assert not any(issue["code"] in {"off_question_segment", "information_plateau", "no_question_relevant_information"} for issue in report["issues"])


# ---------------------------------------------------------------------------
# 4. Child-simple language
# ---------------------------------------------------------------------------

def test_office_language_in_the_body_is_flagged():
    hard = "Infolgedessen resultiert hinsichtlich der Wahrnehmungsverarbeitung eine Komprimierung der retrospektiven Zeitrepräsentation."
    blocks = [{"id": "b1", "role": "hook", "text": HONEST}, {"id": "b2", "role": "answer", "text": time_case.S_NUANCE, "fact_ids": ["fact_01"]},
              {"id": "b3", "role": "payoff", "text": hard, "fact_ids": ["fact_04"]}]
    report = assess_information_gain(state_for(time_case.QUESTION, time_case.FACTS, blocks))
    flagged = [issue for issue in report["issues"] if issue["code"] == "complex_language"]
    assert [issue["block_id"] for issue in flagged] == ["b3"]


def test_a_technical_term_explained_on_the_spot_is_fine():
    explained = "Das nennt man Mere-Exposure-Effekt: Was wir oft sehen, finden wir schöner."
    blocks = [{"id": "b1", "role": "hook", "text": mirror.HOOK}, {"id": "b2", "role": "answer", "text": explained, "fact_ids": ["fact_04"]}]
    units = assess_information_gain(state_for(mirror.QUESTION, mirror.FACTS, blocks))["units"]
    assert units[1]["language"]["status"] == "clear"


# ---------------------------------------------------------------------------
# 5. Writer and review receive the same contract
# ---------------------------------------------------------------------------

def test_writer_and_review_serve_the_core_question(monkeypatch, tmp_path):
    seen: dict = {}

    class Writer:
        name = "capture"

        def __init__(self, _settings=None):
            pass

        def generate(self, request):
            seen["writer"] = request
            return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[
                ScriptBlockV2(role="answer", text=T_ANSWER, fact_ids=["fact_01"]),
                ScriptBlockV2(role="payoff", text=T_PAYOFF, fact_ids=["fact_02"]),
            ]), "connected")

    class Reviewer:
        name = "capture"

        def __init__(self, _settings=None):
            pass

        def review(self, request):
            seen["review"] = request
            return ScriptReviewResult(ScriptReviewResponse(status="approve"), "approved")

    source = {"label": "Quelle", "url": "https://source.test/tiktok"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in TIKTOK_FACTS]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", Reviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation([
        ai_candidate("A", "curiosity_gap", T_HOOK, "person holding a phone at night", ["phone at night"], action="scrolling", detail="phone"),
    ]))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    state = build_initial_state(TIKTOK_Q, AdvancedOptions(), Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path))

    writer_request, review_request = seen["writer"], seen["review"]
    assert writer_request.story_arc["question_contract"]["core_question"] == TIKTOK_Q
    assert set(writer_request.story_arc["question_contract"]["off_question_ids"]) == {"fact_03", "fact_04"}
    assert set(RETENTION_REQUIREMENTS) <= set(writer_request.writing_requirements)
    assert review_request.model_input()["story_arc"]["question_contract"]["core_question"] == TIKTOK_Q
    assert set(RETENTION_REQUIREMENTS) <= set(review_request.writing_requirements)
    assert body_texts(state["script"]["blocks"]) == [T_ANSWER, T_PAYOFF]


def test_writer_and_review_prompts_carry_the_retention_rules():
    for prompt in (script_writer.SCRIPT_WRITER_V2_INSTRUCTIONS, script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS):
        assert "core_question" in prompt and "same subject" in prompt and "10–12 year old" in prompt
    assert "never pad" in script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS


def test_an_information_plateau_is_flagged_and_broken_safely():
    elaboration = "Push-Benachrichtigungen lösen also einen echten Reflex aus, die App ganz schnell zu öffnen."
    again = "Der Reflex kommt oft schon, bevor wir uns bewusst entscheiden, die App zu öffnen, ganz automatisch."
    blocks = [*copy.deepcopy(TIKTOK_BLOCKS[:2]), {"id": "x", "role": "support", "text": elaboration, "fact_ids": ["fact_01"]},
              {"id": "y", "role": "support", "text": again, "fact_ids": ["fact_01"]}, copy.deepcopy(TIKTOK_BLOCKS[-1])]
    state = state_for(TIKTOK_Q, TIKTOK_FACTS, blocks)
    report = assess_information_gain(state)
    assert [unit["beat_class"] for unit in report["units"][2:4]] == ["weak_value", "weak_value"]
    assert [issue["block_id"] for issue in report["issues"] if issue["code"] == "information_plateau"] == ["x"]
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    assert [repair["action"] for repair in repairs] == ["remove_weak_value"]
    assert len(body_texts(pruned)) == 3 and body_texts(pruned)[0] == T_ANSWER and body_texts(pruned)[-1] == T_PAYOFF
    state["script"]["blocks"] = pruned
    assert not any(issue["code"] == "information_plateau" for issue in assess_information_gain(state)["issues"])
