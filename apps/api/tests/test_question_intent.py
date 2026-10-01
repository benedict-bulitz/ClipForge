"""Question intent: answer what the user means, not one literal reading of the words.

The accepted TikTok script explains how a tapped link hands over to the app.
It is factually fine - and the wrong answer to "Warum öffnen wir TikTok,
obwohl wir es gar nicht wollten?", which asks about human behaviour.
Script narration is the real Mac run verbatim; research facts are
reconstructed from it.  Topics are fixtures only.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker
from test_time_perception_redundancy import OfflineReviewer
from test_triple_hook import Judge, ai_candidate, generation

from clipforge import ai, script_review, script_writer
from clipforge.ai import AIPlanResult
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.generation import (
    claim_next_generation_job,
    create_generation_job,
    run_generation_job,
)
from clipforge.models import GenerationJob, Project
from clipforge.novelty import (
    assess_information_gain,
    build_novelty_plan,
    prune_redundant_information,
)
from clipforge.pipeline import _normalise_blocks, build_initial_state
from clipforge.question_intent import (
    domain_alignment,
    interpret_question,
    merge_planner_intent,
    research_query,
)
from clipforge.readiness import content_readiness
from clipforge.renderer import RenderResult
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import build_story_arc, story_brief

A = "Warum öffnen wir TikTok, obwohl wir es gar nicht wollten?"
B = "Warum öffnet sich TikTok, obwohl ich nichts angeklickt habe?"
C = "Warum greifen wir ständig zum Handy, obwohl wir eigentlich nichts machen wollten?"
D = "Warum öffnen wir den Kühlschrank, obwohl wir gar keinen Hunger haben?"
E = "Warum essen wir weiter, obwohl wir schon satt sind?"
F = "Warum startet mein Handy eine App, obwohl ich nur auf einen Link klicke?"


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


def intent(question: str) -> dict:
    return {
        "topic": question, "question": question, "content_type": "factual_explainer", "language": "de",
        "research_required": True, "question_intent": interpret_question(question, "de"),
    }


# The real accepted TikTok script (deep-link interpretation).
DEEPLINK_FACTS = [
    fact(1, "Tippt man auf einen TikTok-Link, erkennt der Link das Gerät und öffnet TikTok direkt in der App, auf iPhones und Android-Geräten.", 0.9),
    fact(2, "Ist die App nicht installiert, öffnet sich die mobile TikTok-Seite und fordert zur Installation auf."),
    fact(3, "Verlangt TikTok zuerst eine Anmeldung, kann das Video weiterlaufen, wenn man den Anmeldebildschirm verlässt."),
]
DEEPLINK_HOOK = "TikTok öffnet sich nicht von allein – du hast meist einen Link angetippt."
DEEPLINK_BLOCKS = [
    {"role": "answer", "text": "Wenn du auf einen TikTok-Link tippst, erkennt der Link dein Handy und öffnet TikTok direkt in der App – auf iPhones und Android-Handys.", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": "Ist die App nicht installiert, öffnet sich stattdessen die mobile TikTok-Seite und bittet dich um die Installation.", "fact_ids": ["fact_02"]},
    {"role": "explanation", "text": "Selbst wenn TikTok zuerst „Anmelden“ verlangt, kann das Video danach trotzdem laufen, wenn du den Anmeldebildschirm wieder verlässt.", "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": "Deshalb öffnet sich TikTok manchmal, obwohl du die App gar nicht selbst starten wolltest: Der Link übernimmt das Öffnen für dich.", "fact_ids": ["fact_01"]},
]
DEEPLINK_PLANNER = {"primary_answer_index": 1, "final_payoff_index": 1, "answers_why": True}

# An evidence-grounded behavioural answer (fixture research).
HABIT_FACTS = [
    fact(1, "Oft geöffnete Apps werden zur Gewohnheit: Ein Auslöser wie Langeweile startet die Handlung automatisch, bevor man bewusst entscheidet.", 0.9),
    fact(2, "Unvorhersehbare Belohnungen im Feed verstärken diese Gewohnheit, deshalb greift man immer wieder zur App."),
]
HABIT_HOOK = "Du wolltest nur kurz aufs Handy schauen – und schon ist TikTok wieder offen."
HABIT_BLOCKS = [
    {"role": "answer", "text": "Wenn du TikTok oft öffnest, wird das zur Gewohnheit: Langeweile löst die Handlung automatisch aus, bevor du bewusst entscheidest.", "fact_ids": ["fact_01"]},
    {"role": "payoff", "text": "Deshalb öffnest du TikTok, obwohl du es gar nicht wolltest: Unvorhersehbare Belohnungen machen die Gewohnheit schneller als deine Entscheidung.", "fact_ids": ["fact_02"]},
]
HABIT_PLANNER = {"primary_answer_index": 1, "final_payoff_index": 2, "answers_why": True}


def assessed(question: str, facts: list[dict], hook: str, writer: list[dict], planner: dict | None) -> dict:
    blocks = _normalise_blocks([{"role": "hook", "text": hook}, *copy.deepcopy(writer)], 90)
    novelty = build_novelty_plan(intent(question), facts)
    arc = build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty, supplied=planner)
    state = {"intent": intent(question), "facts": copy.deepcopy(facts), "novelty_plan": novelty, "story_arc": arc, "script": {"blocks": blocks}}
    pruned, _repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return state


# ---------------------------------------------------------------------------
# 1. Interpretation: agency, intention, contrast
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("question", "expected", "actor"), [
    (A, "behavioral_why", "person"),
    (B, "technical_why", "object"),
    (C, "behavioral_why", "person"),
    (D, "behavioral_why", "person"),
    (E, "behavioral_why", "person"),
    (F, "technical_why", "object"),
])
def test_adversarial_questions_resolve_to_their_intent(question, expected, actor):
    contract = interpret_question(question, "de")
    assert contract["question_type"] == expected and contract["actor"] == actor
    assert contract["confidence"] == "high" and not contract["ambiguous"]


def test_we_open_tiktok_and_tiktok_opens_itself_are_different_questions():
    behavioral, technical = interpret_question(A, "de"), interpret_question(B, "de")
    assert behavioral["question_type"] != technical["question_type"]
    assert behavioral["key_contrast_or_condition"] == "wir es gar nicht wollten"
    assert technical["key_contrast_or_condition"] == "ich nichts angeklickt habe"


def test_the_contract_keeps_the_original_and_adds_the_meaning():
    contract = interpret_question(A, "de")
    assert contract["original_question"] == A
    assert contract["intended_question"] != A and contract["intended_question"].startswith(A.rstrip("?"))
    assert "Gewohnheiten" in contract["expected_explanation_domain"]
    assert any("technisch" in item for item in contract["explicitly_excluded_interpretations"])
    assert [item["question_type"] for item in contract["candidates"]] == ["behavioral_why", "technical_why"]


def test_questions_without_agency_signals_keep_their_current_behaviour():
    for question in ("Warum vergeht die Zeit schneller, je älter man wird?", "Warum sehe ich auf Fotos schlechter aus als im Spiegel?"):
        contract = interpret_question(question, "de")
        assert contract["question_type"] == "causal_why" and research_query(contract, "de") is None
        assert contract["explicitly_excluded_interpretations"] == []


def test_genuine_ambiguity_is_kept_ranked_not_forced():
    contract = interpret_question("Warum öffnen wir TikTok, obwohl ich nichts angeklickt habe?", "de")
    assert contract["ambiguous"] and contract["confidence"] == "low"
    assert len(contract["candidates"]) == 2 and contract["candidates"][0]["score"] >= contract["candidates"][1]["score"]


# ---------------------------------------------------------------------------
# 2. Intent drives research, facts, Story Arc and the planner
# ---------------------------------------------------------------------------

def test_research_queries_follow_the_interpretation():
    assert "Gewohnheit" in research_query(interpret_question(A, "de"), "de")
    assert "technisch" in research_query(interpret_question(F, "de"), "de")
    assert "Gewohnheit" not in research_query(interpret_question(B, "de"), "de")


def test_deeplink_facts_do_not_join_the_story_arc_of_the_behavioural_question():
    arc = assessed(A, DEEPLINK_FACTS, DEEPLINK_HOOK, DEEPLINK_BLOCKS, None)["story_arc"]
    primary = arc["primary_answer_id"]
    links = {unit["id"]: unit["question_link"] for unit in arc["units"] if unit["id"] != primary}
    # Every deep-link fact except the arc's own answer is recognised as answering the
    # excluded reading (one the closing beat requires stays on the chain: with
    # nothing else researched, the script then fails sufficiency instead).
    assert set(links.values()) == {"excluded_interpretation"}
    assert arc["question_contract"]["off_question_ids"]
    # The same facts serve the technical question.
    technical = assessed(F, DEEPLINK_FACTS, DEEPLINK_HOOK, DEEPLINK_BLOCKS, None)["story_arc"]
    assert not technical["question_contract"]["off_question_ids"]


def test_the_story_arc_carries_the_interpreted_question_for_every_consumer():
    arc = assessed(A, HABIT_FACTS, HABIT_HOOK, HABIT_BLOCKS, HABIT_PLANNER)["story_arc"]
    contract = arc["question_contract"]
    assert contract["core_question"] == A  # the user's words are never replaced
    assert contract["question_type"] == "behavioral_why" and contract["intended_question"] != A
    brief = story_brief(arc)["question_contract"]  # what writer, review and hook receive
    assert brief["intended_question"] == contract["intended_question"] and brief["excluded_interpretations"]


def test_the_planner_confirms_or_corrects_the_reading():
    grammar = interpret_question(A, "de")
    corrected = merge_planner_intent(grammar, {"question_type": "technical_why", "intended_question": "Warum öffnet ein Link TikTok?", "confidence": "medium"})
    assert corrected["reinterpreted"] and corrected["grammar_question_type"] == "behavioral_why"
    assert corrected["original_question"] == A and corrected["source"] == "planner"
    assert merge_planner_intent(grammar, None) is grammar


def test_prompts_carry_the_intent():
    assert "question_intent" in ai.DIRECTOR_INSTRUCTIONS and "grammatical agency" in ai.DIRECTOR_INSTRUCTIONS
    assert "question_intent" in ai.AIProjectPlan.model_fields
    assert "intended_question" in script_writer.SCRIPT_WRITER_V2_INSTRUCTIONS
    assert "excluded_interpretations" in script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS
    assert "intended_question" in ai.HOOK_GENERATION_INSTRUCTIONS


# ---------------------------------------------------------------------------
# 3. Answer sufficiency against the intent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("planner", [DEEPLINK_PLANNER, None], ids=["planner", "deterministic"])
def test_the_deeplink_script_fails_the_behavioural_question(planner):
    state = assessed(A, DEEPLINK_FACTS, DEEPLINK_HOOK, DEEPLINK_BLOCKS, planner)
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and "answers_excluded_interpretation" in sufficiency["reasons"]
    assert content_readiness(state)["status"] == "research_required"


def test_the_deeplink_script_answers_the_technical_question():
    state = assessed(F, DEEPLINK_FACTS, DEEPLINK_HOOK, DEEPLINK_BLOCKS, DEEPLINK_PLANNER)
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "pass" and sufficiency["intent_alignment"]["status"] == "aligned"
    assert content_readiness(state)["ready"]


def test_a_habit_explanation_answers_the_behavioural_question():
    state = assessed(A, HABIT_FACTS, HABIT_HOOK, HABIT_BLOCKS, HABIT_PLANNER)
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "pass" and sufficiency["intent_alignment"]["status"] == "aligned"
    assert content_readiness(state)["ready"]


def test_a_habit_explanation_fails_the_technical_question():
    state = assessed(B, HABIT_FACTS, HABIT_HOOK, HABIT_BLOCKS, HABIT_PLANNER)
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert "answers_excluded_interpretation" in sufficiency["reasons"] and sufficiency["status"] == "fail"


@pytest.mark.parametrize(("question", "wrong"), [
    (D, "Die Kühlschranktür schließt durch eine Dichtung und eine leichte Neigung des Geräts, die Einstellung regelt die Software."),
    (E, "Der Magen dehnt sich, die Verdauung dauert Stunden; das Programm der Waage zeigt Daten per Link."),
])
def test_alignment_backstop_flags_a_technical_answer_to_a_behavioural_question(question, wrong):
    contract = interpret_question(question, "de")
    assert domain_alignment(contract, [wrong])["status"] in {"mismatch", "unclear"}
    assert domain_alignment(contract, ["Ein Auslöser wie Langeweile startet die Gewohnheit automatisch."])["status"] == "aligned"


# ---------------------------------------------------------------------------
# 4. Live pipeline
# ---------------------------------------------------------------------------

class Calls:
    def __init__(self):
        self.render = self.media = self.voice = 0
        self.queries: list[str] = []


def _wire(monkeypatch, calls: Calls, *, research_for, writer_for, hook: str, planner=None) -> None:
    source = {"label": "Quelle", "url": "https://source.test/a"}

    def research(query, *_a, **_k):
        calls.queries.append(query)
        facts = research_for(query)
        return ResearchResult([{**{k: v for k, v in item.items() if k != "id"}, "sources": [source]} for item in facts], [source], "verified_sources", "fixture")

    class Writer:
        name = "fixture"

        def __init__(self, _settings=None):
            pass

        def generate(self, request):
            blocks = writer_for(request)
            return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[ScriptBlockV2(**block) for block in blocks]), "connected")

    def render(*_a, **_k):
        calls.render += 1
        return RenderResult("/media/t.mp4", 12.0, "openai", 100)

    def media(*_a, **_k):
        calls.media += 1

    def voice(*_a, **_k):
        calls.voice += 1

    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", planner or (lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None)))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation([
        ai_candidate("A", "curiosity_gap", hook, "person holding a phone", ["person holding phone"], action="holding", detail="phone"),
    ]))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    monkeypatch.setattr("clipforge.services.render_video", render)
    monkeypatch.setattr("clipforge.services.prepare_project_media", media)
    monkeypatch.setattr("clipforge.renderer._create_voice", voice)
    monkeypatch.setattr("clipforge.services.run_ai_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._ensure_music_recommendations", lambda *_a, **_k: None)


SETTINGS = {"clipforge_ai_mode": "openai", "openai_api_key": "test-key"}


def _run_job(db, tmp_path, question: str) -> tuple[GenerationJob, Project]:
    job, _ = create_generation_job(db, ProjectCreate(prompt=question, options=AdvancedOptions(language="de")))
    assert claim_next_generation_job(db) is not None
    run_generation_job(job.id, Settings(render_root=tmp_path, **SETTINGS), session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=True))
    db.expire_all()
    return db.get(GenerationJob, job.id), db.get(Project, job.project_id)


def _deeplink_writer(_request):
    return DEEPLINK_BLOCKS


def test_behavioural_tiktok_with_only_deeplink_evidence_is_research_required(db, monkeypatch, tmp_path):
    calls = Calls()
    _wire(monkeypatch, calls, research_for=lambda _query: DEEPLINK_FACTS, writer_for=_deeplink_writer, hook=DEEPLINK_HOOK)
    job, project = _run_job(db, tmp_path, A)
    # The research searched the behavioural meaning - first and on the retry.
    assert len(calls.queries) == 2 and all("Gewohnheit" in query for query in calls.queries)
    state = project.revisions[0].state
    assert state["intent"]["question_intent"]["question_type"] == "behavioral_why"
    assert state["intent"]["question"] == A  # the user's words are kept
    arc = state["story_arc"]
    links = {unit["question_link"] for unit in arc["units"] if unit["id"] != arc["primary_answer_id"]}
    assert links == {"excluded_interpretation"}  # deep links are recognised as the wrong story
    assert job.status == "failed" and job.failure_category == "research_required"
    assert project.status == "needs_attention" and calls.render == calls.voice == calls.media == 0


def test_behavioural_tiktok_with_behavioural_evidence_renders(db, monkeypatch, tmp_path):
    calls = Calls()

    def research(query):
        return HABIT_FACTS if "Gewohnheit" in query else DEEPLINK_FACTS

    def writer(request):
        return HABIT_BLOCKS if any("Gewohnheit" in item.claim for item in request.facts) else DEEPLINK_BLOCKS

    _wire(monkeypatch, calls, research_for=research, writer_for=writer, hook=HABIT_HOOK)
    job, project = _run_job(db, tmp_path, A)
    assert job.status == "completed" and calls.render == 1 and len(calls.queries) == 1
    state = project.revisions[-1].state
    assert all("Gewohnheit" in fact["claim"] or "Belohnungen" in fact["claim"] for fact in state["facts"])
    assert state["script"]["readiness"]["ready"]


def test_technical_question_with_deeplink_evidence_renders(db, monkeypatch, tmp_path):
    calls = Calls()
    _wire(monkeypatch, calls, research_for=lambda _query: DEEPLINK_FACTS, writer_for=_deeplink_writer, hook=DEEPLINK_HOOK)
    job, project = _run_job(db, tmp_path, F)
    assert "technisch" in calls.queries[0] and "Gewohnheit" not in calls.queries[0]
    assert job.status == "completed" and calls.render == 1
    assert project.revisions[0].state["intent"]["question_intent"]["question_type"] == "technical_why"


def test_the_existing_planner_call_can_correct_the_reading(monkeypatch, tmp_path):
    calls = Calls()

    class Plan:
        def model_dump(self, mode="json"):
            return {
                "intent": {"topic": A, "intent": "explain", "question": A, "language": "de", "content_type": "factual_explainer",
                           "tone": "fast_documentary", "research_required": True, "visual_style": "documentary_graphics", "shortform": True},
                "research_questions": [], "facts": [], "answer_skeleton": ["ANSWER"], "script_blocks": [], "music_mood": "documentary",
                "question_intent": {"intended_question": "Warum öffnet ein Link die TikTok-App?", "question_type": "technical_why",
                                    "actor": "Link", "explicitly_excluded_interpretations": ["Gewohnheit"], "confidence": "medium"},
            }

    seen: dict = {}

    def planner(*_a, **kwargs):
        seen.update(kwargs)
        return AIPlanResult(Plan(), "connected")

    _wire(monkeypatch, calls, research_for=lambda _query: DEEPLINK_FACTS, writer_for=_deeplink_writer, hook=DEEPLINK_HOOK, planner=planner)
    state = build_initial_state(A, AdvancedOptions(language="de"), Settings(render_root=tmp_path, **SETTINGS))
    assert seen["question_intent"]["question_type"] == "behavioral_why"  # the planner sees the grammatical reading
    merged = state["intent"]["question_intent"]
    assert merged["source"] == "planner" and merged["reinterpreted"] and merged["question_type"] == "technical_why"
    assert merged["original_question"] == A
