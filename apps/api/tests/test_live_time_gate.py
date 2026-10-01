"""Final live-gate regression: the real time script that escaped, and the two that pass.

Narration is each Mac run verbatim; research facts were not kept with the
runs and are reconstructed from what each script says.  The real time run
ended on "warum es mit dem Alter häufiger wird, bleibt offen" and was still
produced.  Topics are fixtures only.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker
from test_time_perception_redundancy import OfflineReviewer
from test_triple_hook import Judge, ai_candidate, generation

from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.generation import claim_next_generation_job, create_generation_job, run_generation_job
from clipforge.models import GenerationJob, Project
from clipforge.novelty import assess_information_gain, build_novelty_plan, prune_redundant_information
from clipforge.pipeline import _normalise_blocks, build_initial_state
from clipforge.readiness import ScriptNotReady, content_readiness
from clipforge.renderer import RenderResult
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.script_review import SCRIPT_REVIEW_V2_INSTRUCTIONS
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state
from clipforge.story_arc import build_story_arc
from clipforge.verbal_hook import states_open_question


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


def intent(question: str) -> dict:
    return {"topic": question, "question": question, "content_type": "factual_explainer", "language": "de", "research_required": True}


# --- FAIL: time (the escaped live run) ------------------------------------------

TIME_Q = "Warum vergeht die Zeit schneller, je älter man wird?"
TIME_FACTS = [
    fact(1, "Ältere Erwachsene haben im Durchschnitt eher das Gefühl, dass die letzten zehn Jahre schneller vergangen sind; "
            "der Unterschied zu jüngeren Erwachsenen ist klein.", 0.9),
    fact(2, "Wer beim Warten an Sorgen denkt, achtet weniger auf den Moment und bemerkt kaum, wie die Zeit vergeht; "
            "unbemerkte Zeit kann schneller wirken."),
]
TIME_HOOK = "Warum fühlen sich zehn Jahre für manche Menschen plötzlich so kurz an?"
Z_ANSWER = "Bei älteren Erwachsenen fühlt es sich im Durchschnitt eher so an, als seien die letzten zehn Jahre schneller vergangen."
Z_SMALL = "Der Unterschied zu jüngeren Erwachsenen ist allerdings klein."
Z_WAITING = (
    "Ein ähnliches Gefühl kann entstehen, wenn du wartest und dabei an Sorgen denkst: Du achtest weniger auf den Moment "
    "und bemerkst kaum, wie die Zeit vergeht."
)
Z_UNNOTICED = "Darum kann unbemerkte Zeit schneller wirken."
Z_OPEN = "Bei älteren Menschen tritt dieses Gefühl im Durchschnitt eher auf; warum es mit dem Alter häufiger wird, bleibt offen."
TIME_BLOCKS = [
    {"role": "answer", "text": f"{Z_ANSWER} {Z_SMALL}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{Z_WAITING} {Z_UNNOTICED}", "fact_ids": ["fact_02"]},
    {"role": "payoff", "text": Z_OPEN, "fact_ids": ["fact_01"]},
]

# --- PASS: TikTok ------------------------------------------------------------------

TIKTOK_Q = "Warum öffnen wir TikTok, obwohl wir es gar nicht wollten?"
TIKTOK_FACTS = [
    fact(1, "Tippt man auf einen TikTok-Link, erkennt der Link das Gerät und öffnet TikTok direkt in der App, auf iPhones und Android-Geräten.", 0.9),
    fact(2, "Ist die App nicht installiert, öffnet sich die mobile TikTok-Seite und fordert zur Installation auf."),
    fact(3, "Verlangt TikTok zuerst eine Anmeldung, kann das Video weiterlaufen, wenn man den Anmeldebildschirm verlässt."),
]
TIKTOK_HOOK = "TikTok öffnet sich nicht von allein – du hast meist einen Link angetippt."
T_ANSWER = "Wenn du auf einen TikTok-Link tippst, erkennt der Link dein Handy und öffnet TikTok direkt in der App – auf iPhones und Android-Handys."
T_INSTALL = "Ist die App nicht installiert, öffnet sich stattdessen die mobile TikTok-Seite und bittet dich um die Installation."
T_LOGIN = "Selbst wenn TikTok zuerst „Anmelden“ verlangt, kann das Video danach trotzdem laufen, wenn du den Anmeldebildschirm wieder verlässt."
T_PAYOFF = "Deshalb öffnet sich TikTok manchmal, obwohl du die App gar nicht selbst starten wolltest: Der Link übernimmt das Öffnen für dich."
TIKTOK_BLOCKS = [
    {"role": "answer", "text": T_ANSWER, "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": T_INSTALL, "fact_ids": ["fact_02"]},
    {"role": "explanation", "text": T_LOGIN, "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": T_PAYOFF, "fact_ids": ["fact_01"]},
]

# --- PASS: photo / mirror -----------------------------------------------------------

PHOTO_Q = "Warum sehe ich auf Fotos schlechter aus als im Spiegel?"
PHOTO_FACTS = [
    fact(1, "Das Gehirn erwartet meist das gewohnte Spiegelbild; ein Foto wirkt deshalb ungewohnt.", 0.9),
    fact(2, "Ein Foto zeigt das Gesicht seitenverkehrt zum Spiegelbild, so wie andere Menschen es sehen."),
    fact(3, "Menschen sehen sich viel öfter im Spiegel als auf Fotos; das Spiegelbild wird dadurch vertrauter."),
]
PHOTO_HOOK = "Ein Foto zeigt nicht dein schlechteres Gesicht, sondern eine ungewohnte Seite davon."
P_ANSWER = "Auf Fotos siehst du nicht unbedingt schlechter aus."
P_EXPECTS = "Dein Gesicht wirkt nur ungewohnt, weil dein Gehirn meistens dein Spiegelbild erwartet."
P_OTHERS = "Ein Foto zeigt es anders herum – so, wie andere dich sehen."
P_MORE = "Du siehst dich selbst viel öfter im Spiegel als auf Fotos."
P_FAMILIAR = "Deshalb ist dein Spiegelbild für dich vertrauter."
P_PAYOFF = "Darum wirkt das Foto für dich schlechter – nicht weil dein Gesicht schlechter ist, sondern weil das Bild von deiner Erwartung abweicht."
PHOTO_SCRIPT = [P_ANSWER, P_EXPECTS, P_OTHERS, P_MORE, P_FAMILIAR, P_PAYOFF]
PHOTO_BLOCKS = [
    {"role": "answer", "text": f"{P_ANSWER} {P_EXPECTS} {P_OTHERS}", "fact_ids": ["fact_01", "fact_02"]},
    {"role": "support", "text": f"{P_MORE} {P_FAMILIAR}", "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": P_PAYOFF, "fact_ids": ["fact_01"]},
]

# What a live AI planner supplies (primary / final, relevance, does the research explain why).
PLANNERS = {
    "time": {"primary_answer_index": 1, "final_payoff_index": 1},
    "tiktok": {"primary_answer_index": 1, "final_payoff_index": 1, "answers_why": True,
               "units": [{"fact_index": index, "role": role, "serves_question": True}
                         for index, role in ((1, "primary_answer"), (2, "supporting_fact"), (3, "supporting_fact"))]},
    "photo": {"primary_answer_index": 1, "final_payoff_index": 3, "answers_why": True},
}
CASES = {
    "time": (TIME_Q, TIME_FACTS, TIME_HOOK, TIME_BLOCKS),
    "tiktok": (TIKTOK_Q, TIKTOK_FACTS, TIKTOK_HOOK, TIKTOK_BLOCKS),
    "photo": (PHOTO_Q, PHOTO_FACTS, PHOTO_HOOK, PHOTO_BLOCKS),
}


def final_state(name: str, planner: dict | None) -> tuple[list[dict], dict]:
    """The script as it would be narrated: normalised, then repaired."""
    question, facts, hook, writer = CASES[name]
    blocks = _normalise_blocks([{"role": "hook", "text": hook}, *copy.deepcopy(writer)], 90)
    novelty = build_novelty_plan(intent(question), facts)
    arc = build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty, supplied=planner)
    state = {"intent": intent(question), "facts": copy.deepcopy(facts), "novelty_plan": novelty, "story_arc": arc, "script": {"blocks": blocks}}
    pruned, _repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return blocks, state


def body(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


# ---------------------------------------------------------------------------
# TIME: why the live run passed, and that every path now blocks it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("planner", [
    None,  # deterministic arc (the answer fact was mis-assigned: the age observation counted as mechanism)
    PLANNERS["time"],
    {**PLANNERS["time"], "answers_why": True},  # a planner that wrongly vouches for the research
], ids=["deterministic", "planner", "planner_answers_why"])
def test_the_escaped_time_script_is_research_required(planner):
    _blocks, state = final_state("time", planner)
    report = assess_information_gain(state)
    sufficiency = report["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and sufficiency["research_required"]
    assert {"condition_not_explained", "question_left_open"} <= set(sufficiency["reasons"])
    assert sufficiency["condition_terms"] == ["age"] and sufficiency["mechanism_block_ids"] == []
    readiness = content_readiness(state)
    assert not readiness["ready"] and readiness["status"] == "research_required"


def test_bleibt_offen_can_never_be_a_resolving_payoff():
    _blocks, state = final_state("time", PLANNERS["time"])
    report = assess_information_gain(state)
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "weak_resolution"
    assert "still unanswered" in report["payoff"]["reason"]
    # The admission stays visible (it blocks production), it is not hidden behind an earlier beat.
    assert body(state["script"]["blocks"])[-1] == Z_OPEN


def test_generic_time_perception_is_not_the_age_explanation():
    _blocks, state = final_state("time", {**PLANNERS["time"], "answers_why": True})
    blocks = [block for block in state["script"]["blocks"] if block["text"] != Z_OPEN]
    blocks[-1] = {**blocks[-1], "role": "payoff"}
    state["script"]["blocks"] = blocks  # even without the admission, "je älter" stays unexplained
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and "condition_not_explained" in sufficiency["reasons"]


@pytest.mark.parametrize("sentence", [
    Z_OPEN,
    "Warum das so ist, ist noch unklar.",
    "Wie genau das funktioniert, wissen wir nicht genau.",
    "Das lässt sich damit nicht erklären.",
    "Warum es im Alter häufiger passiert, ist nicht geklärt.",
    "Forscher wissen noch nicht genau, warum das passiert.",
    "The reason remains unclear.",
    "We still don't know why.",
])
def test_open_question_endings_are_recognised(sentence):
    assert states_open_question(sentence)


@pytest.mark.parametrize("sentence", [T_PAYOFF, P_PAYOFF, P_FAMILIAR, T_INSTALL, Z_SMALL, "Die Tür bleibt offen, damit Luft zirkulieren kann."])
def test_resolving_sentences_are_not_open_questions(sentence):
    assert not states_open_question(sentence)


def test_review_prompt_treats_an_open_ending_as_unanswered():
    assert "bleibt offen" in SCRIPT_REVIEW_V2_INSTRUCTIONS and "unanswered" in SCRIPT_REVIEW_V2_INSTRUCTIONS


# ---------------------------------------------------------------------------
# TIKTOK and PHOTO: still pass, unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["tiktok", "photo"])
def test_the_passing_live_scripts_stay_ready_and_unchanged(name):
    before, state = final_state(name, PLANNERS[name])
    assert body(state["script"]["blocks"]) == body(before)
    report = assess_information_gain(state)
    assert report["answer_sufficiency"]["status"] == "pass" and report["payoff"]["status"] == "pass"
    assert content_readiness(state)["ready"]


@pytest.mark.parametrize("name", ["tiktok", "photo"])
def test_the_passing_live_scripts_stay_ready_without_a_planner(name):
    _before, state = final_state(name, None)
    assert content_readiness(state)["ready"]


# ---------------------------------------------------------------------------
# End to end: generation job, final-script recheck, no TTS/render
# ---------------------------------------------------------------------------

class Calls:
    def __init__(self):
        self.render = self.media = self.voice = 0
        self.queries: list[str] = []


def _wire(monkeypatch, name: str, calls: Calls) -> None:
    question, facts, hook, writer = CASES[name]
    source = {"label": "Quelle", "url": "https://source.test/a"}

    def research(query, *_a, **_k):
        calls.queries.append(query)
        return ResearchResult([{**{k: v for k, v in item.items() if k != "id"}, "sources": [source]} for item in facts], [source], "verified_sources", "fixture")

    class Writer:
        name = "fixture"

        def __init__(self, _settings=None):
            pass

        def generate(self, _request):
            return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[ScriptBlockV2(**block) for block in writer]), "connected")

    def render(*_a, **_k):
        calls.render += 1
        return RenderResult("/media/t.mp4", 12.0, "openai", 100)

    def media(*_a, **_k):
        calls.media += 1

    def voice(*_a, **_k):
        calls.voice += 1

    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
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


def test_the_live_time_run_fails_research_required_with_no_tts_or_render(db, monkeypatch, tmp_path):
    calls = Calls()
    _wire(monkeypatch, "time", calls)
    job, project = _run_job(db, tmp_path, TIME_Q)
    assert job.status == "failed" and job.failure_category == "research_required"
    assert project.status == "needs_attention" and project.current_revision == 1
    assert calls.voice == calls.render == calls.media == 0
    state = project.revisions[0].state
    assert state["script"]["readiness"]["status"] == "research_required"
    assert next(stage for stage in state["pipeline"] if stage["id"] == "script")["status"] == "blocked"
    assert len(calls.queries) == 2  # one bounded research retry, then a clean stop


@pytest.mark.parametrize("name", ["tiktok", "photo"])
def test_the_passing_live_scripts_render(db, monkeypatch, tmp_path, name):
    calls = Calls()
    _wire(monkeypatch, name, calls)
    job, project = _run_job(db, tmp_path, CASES[name][0])
    assert job.status == "completed" and project.current_revision == 2
    assert calls.render == 1 and len(calls.queries) == 1  # no retry needed


def test_the_render_gate_rechecks_the_final_live_script(monkeypatch, tmp_path):
    calls = Calls()
    _wire(monkeypatch, "photo", calls)
    state = build_initial_state(PHOTO_Q, AdvancedOptions(language="de"), Settings(render_root=tmp_path, **SETTINGS))
    assert state["script"]["readiness"]["ready"]
    # A later change to the narrated script (an edit, a repair) is judged as it is now.
    blocks = state["script"]["blocks"]
    blocks[-1] = {**blocks[-1], "text": "Warum das so ist, bleibt aber offen."}
    with pytest.raises(ScriptNotReady):
        _render_state(state, "project-photo", 2, Settings(render_root=tmp_path, **SETTINGS))
    assert calls.render == calls.media == calls.voice == 0
