"""An insufficient answer stops production; it is never narrated.

Second round of real Mac outputs: the narration below is each run verbatim
(hook + writer blocks; the pipeline splits blocks into the sentences the run
showed).  Research facts were not kept with the runs and are reconstructed
from what each script says.  Topics are fixtures only.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker
from test_time_perception_redundancy import OfflineReviewer
from test_triple_hook import Judge, ai_candidate, generation

from clipforge import script_review, script_writer
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
from clipforge.readiness import ScriptNotReady, content_readiness
from clipforge.renderer import RenderResult
from clipforge.research import ResearchResult
from clipforge.review import pre_render_quality_gate
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state
from clipforge.story_arc import build_story_arc
from clipforge.triple_hook import state_context
from clipforge.verbal_hook import assess_verbal, narrates_failure, ungrounded_cause


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


def intent(question: str) -> dict:
    return {"topic": question, "question": question, "content_type": "factual_explainer", "language": "de", "research_required": True}


# --- REAL MAC RESULT 1: photo / mirror ----------------------------------------

MIRROR_Q = "Warum sehe ich auf Fotos schlechter aus als im Spiegel?"
MIRROR_FACTS = [
    fact(1, "Fotos zeigen das Gesicht anders, als das Gehirn es aus dem Spiegel erwartet; deshalb wirken sie zuerst fremd.", 0.9),
    fact(2, "Ein Spiegel zeigt das Gesicht seitenverkehrt; ein Foto zeigt es ungespiegelt, so wie andere Menschen es sehen."),
    fact(3, "Auch die eigene Stimme klingt auf Aufnahmen fremd, weil man sie sonst anders hört; mit der Zeit gewöhnt man sich daran.", 0.5),
    fact(4, "Was das Gehirn öfter sieht, kennt es besser und empfindet es als angenehmer (Mere-Exposure-Effekt)."),
]
MIRROR_HOOK = "Deine Stimme auf einer Aufnahme klingt dir fremd – bei Fotos passiert etwas Ähnliches."
M_ANSWER = "Ein Foto zeigt dich nicht schlechter."
M_DIFFERENT = "Es zeigt dein Gesicht nur anders, als dein Gehirn es erwartet."
M_STRANGE = "Deshalb wirkt es zuerst oft fremd."
M_REVERSED = "Im Spiegel siehst du dein Gesicht seitenverkehrt."
M_UNMIRRORED = "Auf einem Foto ist diese Ansicht nicht gespiegelt."
M_OTHERS = "Darum kann dir das Bild schlechter vorkommen, obwohl andere dich so sehen."
M_VOICE = "Das ist wie bei deiner Stimme: Auf einer Aufnahme klingt sie zuerst fremd, weil du sie sonst anders hörst."
M_VOICE_MORE = "Wenn du sie öfter hörst, gewöhnst du dich daran."
M_PAYOFF = "Darum wirkt das Foto schlechter: Dein Gehirn kennt dein Spiegelbild viel besser als diese ungewohnte Ansicht."
MIRROR_SCRIPT = [M_ANSWER, M_DIFFERENT, M_STRANGE, M_REVERSED, M_UNMIRRORED, M_OTHERS, M_VOICE, M_VOICE_MORE, M_PAYOFF]
MIRROR_BLOCKS = [
    {"role": "answer", "text": f"{M_ANSWER} {M_DIFFERENT} {M_STRANGE}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{M_REVERSED} {M_UNMIRRORED}", "fact_ids": ["fact_02"]},
    {"role": "explanation", "text": M_OTHERS, "fact_ids": ["fact_01", "fact_02"]},
    {"role": "support", "text": f"{M_VOICE} {M_VOICE_MORE}", "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": M_PAYOFF, "fact_ids": ["fact_04"]},
]

# --- REAL MAC RESULT 2: TikTok ------------------------------------------------

TIKTOK_Q = "Warum öffnen wir TikTok, obwohl wir es gar nicht wollten?"
TIKTOK_FACTS = [
    fact(1, "Wird ein TikTok-Link im Browser geöffnet, kann ein Fenster mit „TikTok öffnen“ und „Anmelden“ erscheinen.", 0.9),
    fact(2, "Über „Anmelden“ gelangt man zum Video im Browser zurück, ohne sich anzumelden."),
]
TIKTOK_HOOK = "TikTok öffnet sich nicht immer, weil du es wolltest – manchmal steckt ein Fehler in der App dahinter."
T_ANSWER = "Wenn du einen TikTok-Link im Browser öffnest, kann ein Fenster mit „TikTok öffnen“ und „Anmelden“ erscheinen."
T_BACK = "Über „Anmelden“ kommst du zum Video im Browser zurück, ohne dich anzumelden."
T_META = "Diese Fakten erklären aber nicht, warum TikTok überhaupt geöffnet wird, obwohl du es nicht wolltest."
TIKTOK_BLOCKS = [
    {"role": "answer", "text": f"{T_ANSWER} {T_BACK}", "fact_ids": ["fact_01", "fact_02"]},
    {"role": "payoff", "text": T_META, "fact_ids": []},
]

# --- REAL MAC RESULT 3: time ----------------------------------------------------

TIME_Q = "Warum vergeht die Zeit schneller, je älter man wird?"
TIME_FACTS = [
    fact(1, "Ältere Erwachsene haben im Durchschnitt eher das Gefühl, dass die letzten zehn Jahre schnell vergangen sind; "
            "der Unterschied zu jüngeren Erwachsenen ist klein.", 0.9),
    fact(2, "Beim Warten denken wir oft über Sorgen nach und achten weniger auf den Moment; dann vergeht Zeit unbemerkt."),
    fact(3, "Neue, fordernde Aufgaben und starke Erinnerungen können die Aufmerksamkeit stärker ins Jetzt holen."),
]
TIME_HOOK = "Warum fühlen sich zehn Jahre später oft viel kürzer an?"
Z_ANSWER = "Ältere Erwachsene haben im Durchschnitt eher das Gefühl, dass die letzten zehn Jahre schnell vergangen sind."
Z_SMALL = "Der Unterschied zu jüngeren Erwachsenen ist aber klein."
Z_WAITING = "Beim Warten denken wir oft über Sorgen nach."
Z_MOMENT = "Dann achten wir weniger auf den Moment, und die Zeit kann unbemerkt vergehen."
Z_TASKS = "Neue, fordernde Aufgaben und starke Erinnerungen können uns dagegen wieder stärker ins Jetzt holen."
Z_FEELS = "Zeit kann sich also schneller anfühlen, wenn wir sie weniger bewusst erleben."
Z_META = "Warum das im Alter häufiger so ist, erklären diese Befunde aber noch nicht vollständig."
TIME_BLOCKS = [
    {"role": "answer", "text": f"{Z_ANSWER} {Z_SMALL}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{Z_WAITING} {Z_MOMENT} {Z_TASKS} {Z_FEELS}", "fact_ids": ["fact_02", "fact_03"]},
    {"role": "payoff", "text": Z_META, "fact_ids": []},
]

CASES = {
    "mirror": (MIRROR_Q, MIRROR_FACTS, MIRROR_HOOK, MIRROR_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 4}),
    "tiktok": (TIKTOK_Q, TIKTOK_FACTS, TIKTOK_HOOK, TIKTOK_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 2}),
    "time": (TIME_Q, TIME_FACTS, TIME_HOOK, TIME_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 2}),
}


def real(name: str) -> tuple[list[dict], dict]:
    question, facts, hook, writer, planner = CASES[name]
    blocks = _normalise_blocks([{"role": "hook", "text": hook}, *copy.deepcopy(writer)], 90)
    novelty = build_novelty_plan(intent(question), facts)
    arc = build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty, supplied=planner)
    return blocks, {"intent": intent(question), "facts": copy.deepcopy(facts), "novelty_plan": novelty, "story_arc": arc, "script": {"blocks": blocks}}


def repaired(name: str) -> tuple[list[dict], list[dict], dict]:
    blocks, state = real(name)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return pruned, repairs, state


def body(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


# ---------------------------------------------------------------------------
# PHOTO: valid, duplicated analogy compressed
# ---------------------------------------------------------------------------

def test_photo_drops_spent_analogy_and_unverified_paraphrase():
    pruned, repairs, state = repaired("mirror")
    assert [repair["action"] for repair in repairs] == ["remove_hook_analogy_reuse", "remove_weak_tail", "remove_hook_analogy_reuse"]
    assert {repair["text"] for repair in repairs} == {M_VOICE, M_VOICE_MORE, M_OTHERS}
    # The reconstructed facts do not lexically establish this paraphrase.
    # No independent claim assessment exists, so it cannot borrow support
    # from question wording or another beat instead of its actual citations.
    _, original_state = real("mirror")
    disputed = next(unit for unit in assess_information_gain(original_state)["units"] if unit["text"] == M_OTHERS)
    assert disputed["evidence"]["kind"] == "lexical_mismatch"
    assert body(pruned) == [M_ANSWER, M_DIFFERENT, M_STRANGE, M_REVERSED, M_UNMIRRORED, M_PAYOFF]
    assert set(body(pruned)) <= set(MIRROR_SCRIPT)  # nothing invented or rewritten
    readiness = content_readiness(state)
    assert readiness["ready"] and readiness["status"] == "ready"
    report = assess_information_gain(state)
    assert report["answer_sufficiency"]["status"] == "pass" and report["payoff"]["status"] == "pass"


def test_an_analogy_callback_that_deepens_the_explanation_stays():
    blocks, state = real("mirror")
    deeper = "Bei deiner Stimme passiert dasselbe: Dein Gehirn kennt die vertraute Version öfter und findet sie deshalb angenehmer."
    blocks[7] = {**blocks[7], "text": deeper}
    state["script"]["blocks"] = blocks
    units = {unit["text"]: unit for unit in assess_information_gain(state)["units"]}
    assert not units[deeper]["hook_spent"]


def test_analogies_are_not_banned_without_a_hook_that_spent_them():
    blocks, state = real("mirror")
    blocks[0] = {**blocks[0], "text": "Auf Fotos gefällst du dir oft weniger als im Spiegel – obwohl es dasselbe Gesicht ist."}
    state["script"]["blocks"] = blocks
    assert not any(unit.get("hook_spent") for unit in assess_information_gain(state)["units"])


# ---------------------------------------------------------------------------
# TIKTOK: research required, meta payoff never narrated, invented hook cause
# ---------------------------------------------------------------------------

def test_tiktok_is_research_required_and_never_ready():
    pruned, repairs, state = repaired("tiktok")
    assert "remove_narrated_failure" in [repair["action"] for repair in repairs]
    assert T_META not in body(pruned)
    readiness = content_readiness(state)
    assert not readiness["ready"] and readiness["status"] == "research_required"
    assert "information_gain_answer_insufficient" in [item["code"] for item in readiness["blocking"]]
    assert "hook_unsupported_claim" in [item["code"] for item in readiness["blocking"]]


def test_the_invented_app_error_hook_is_rejected():
    _blocks, state = real("tiktok")
    context = state_context(state)
    assert ungrounded_cause(TIKTOK_HOOK, context) == ["app", "fehler"]
    result = assess_verbal(TIKTOK_HOOK, "counterintuitive_insight", context)
    assert "unsupported_cause" in result["hard_fail"]
    # A cause the research states is fine; so is an open question.
    assert ungrounded_cause("TikTok öffnet sich, weil im Browser ein Fenster erscheint.", context) == []
    assert ungrounded_cause("Steckt wirklich ein Fehler dahinter?", context) == []


def test_the_meta_payoff_fails_before_any_repair():
    _blocks, state = real("tiktok")
    report = assess_information_gain(state)
    assert any(issue["code"] == "narrated_failure" and issue["severity"] == "error" for issue in report["issues"])
    assert report["payoff"]["result"] == "weak_resolution"


# ---------------------------------------------------------------------------
# TIME: generic attention facts cannot pass for the age explanation
# ---------------------------------------------------------------------------

def test_time_is_research_required_without_an_age_mechanism():
    pruned, repairs, state = repaired("time")
    assert Z_META not in body(pruned) and "remove_narrated_failure" in [repair["action"] for repair in repairs]
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["condition_terms"] == ["age"]
    assert "condition_not_explained" in sufficiency["reasons"]  # waiting/attention never reaches "älter"
    readiness = content_readiness(state)
    assert not readiness["ready"] and readiness["status"] == "research_required"


def test_time_passes_once_research_holds_an_age_specific_mechanism():
    facts = [*TIME_FACTS, fact(4, "Mit dem Alter gibt es weniger neue Erlebnisse, deshalb bleiben weniger Erinnerungen hängen und Jahre wirken kürzer.")]
    blocks = [
        {"id": "b1", "role": "hook", "text": TIME_HOOK},
        {"id": "b2", "role": "answer", "text": Z_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "explanation", "text": "Mit dem Alter gibt es weniger neue Erlebnisse.", "fact_ids": ["fact_04"]},
        {"id": "b4", "role": "payoff", "text": "Deshalb bleiben weniger Erinnerungen hängen, und die Jahre wirken im Rückblick kürzer.", "fact_ids": ["fact_04"]},
    ]
    novelty = build_novelty_plan(intent(TIME_Q), facts)
    arc = build_story_arc(intent(TIME_Q), facts, plan_format(intent(TIME_Q), facts, [], novelty), novelty,
                          supplied={"primary_answer_index": 1, "final_payoff_index": 4})
    state = {"intent": intent(TIME_Q), "facts": facts, "novelty_plan": novelty, "story_arc": arc, "script": {"blocks": blocks}}
    assert content_readiness(state)["ready"]


# ---------------------------------------------------------------------------
# Internal failure is never narration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    T_META, Z_META,
    "Die Recherche reicht dafür nicht aus.",
    "Das lässt sich mit diesen Quellen nicht beantworten.",
    "These findings do not fully explain why this happens.",
    "Our sources cannot answer that yet.",
])
def test_reports_about_the_evidence_are_diagnostics(sentence):
    assert narrates_failure(sentence)


@pytest.mark.parametrize("sentence", [
    "Studien zeigen keinen Zusammenhang zwischen Kaffee und Krebs.",
    "Die Studie erklärt nicht nur das Leuchten, sondern auch die Farbe.",
    "Die Daten zeigen nicht, dass Ältere schlechter schlafen.",
    M_OTHERS,
])
def test_real_findings_are_not_mistaken_for_diagnostics(sentence):
    assert not narrates_failure(sentence)


def test_a_diagnostic_can_never_be_the_hook():
    _blocks, state = real("time")
    assert "narrates_failure" in assess_verbal(Z_META, "curiosity_gap", state_context(state))["hard_fail"]


def test_prompts_forbid_narrating_the_evidence():
    assert "never write a sentence about" in script_writer.SCRIPT_WRITER_V2_INSTRUCTIONS
    assert "internal diagnostic" in script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS


# ---------------------------------------------------------------------------
# End to end: generation -> gate -> no TTS, no render
# ---------------------------------------------------------------------------

class Calls:
    def __init__(self):
        self.render = 0
        self.media = 0
        self.voice = 0
        self.queries: list[str] = []


def _wire(monkeypatch, name: str, calls: Calls, *, retry_facts: list[dict] | None = None, writer_for=None) -> None:
    _question, facts, hook, writer, _planner = CASES[name]
    source = {"label": "Quelle", "url": "https://source.test/a"}

    def research(query, *_a, **_k):
        calls.queries.append(query)
        chosen = retry_facts if (retry_facts is not None and len(calls.queries) > 1) else facts
        return ResearchResult([{**{k: v for k, v in item.items() if k != "id"}, "sources": [source]} for item in chosen], [source], "verified_sources", "fixture")

    class Writer:
        name = "fixture"

        def __init__(self, _settings=None):
            pass

        def generate(self, request):
            blocks = writer_for(request) if writer_for else writer
            return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[ScriptBlockV2(**block) for block in blocks]), "connected")

    monkeypatch.setattr("clipforge.pipeline.research_topic", research)
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation([
        ai_candidate("A", "counterintuitive_insight", hook, "person holding a phone", ["person holding phone"], action="holding", detail="phone"),
    ]))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())

    def render(*_a, **_k):
        calls.render += 1
        return RenderResult("/media/t.mp4", 12.0, "openai", 100)

    def media(*_a, **_k):
        calls.media += 1

    def voice(*_a, **_k):
        calls.voice += 1

    monkeypatch.setattr("clipforge.services.render_video", render)
    monkeypatch.setattr("clipforge.services.prepare_project_media", media)
    monkeypatch.setattr("clipforge.renderer._create_voice", voice)
    monkeypatch.setattr("clipforge.services.run_ai_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._ensure_music_recommendations", lambda *_a, **_k: None)


SETTINGS = {"clipforge_ai_mode": "openai", "openai_api_key": "test-key"}


@pytest.mark.parametrize("name", ["tiktok", "time"])
@pytest.mark.usefixtures("legacy_without_qac")
def test_generation_job_stops_before_tts_and_render(db, monkeypatch, tmp_path, name):
    calls = Calls()
    _wire(monkeypatch, name, calls)
    job, _ = create_generation_job(db, ProjectCreate(prompt=CASES[name][0], options=AdvancedOptions(language="de")))
    assert claim_next_generation_job(db) is not None
    run_generation_job(job.id, Settings(render_root=tmp_path, **SETTINGS), session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=True))
    db.expire_all()

    failed = db.get(GenerationJob, job.id)
    project = db.get(Project, job.project_id)
    assert failed.status == "failed" and failed.failure_category == "research_required"
    assert failed.failure_message == "ClipForge konnte für diese Frage noch keine ausreichend belegte Antwort erstellen. Bitte versuche es erneut."
    # No TTS, no media, no render: only the blocked initial revision exists.
    assert calls.render == calls.media == calls.voice == 0
    assert project.status == "needs_attention" and project.current_revision == 1
    state = project.revisions[0].state
    assert state["script"]["readiness"]["status"] == "research_required"
    assert next(stage for stage in state["pipeline"] if stage["id"] == "script")["status"] == "blocked"
    # One bounded research retry aimed at the missing mechanism, then a clean stop.
    assert len(calls.queries) == 2 and "Mechanismus" in calls.queries[1]
    assert state["script"]["readiness"].get("retry_exhausted") is True
    narration = state["script"]["text"]
    assert T_META not in narration and Z_META not in narration
    assert "Fehler in der App" not in narration


def test_render_state_refuses_a_blocked_script(monkeypatch, tmp_path):
    calls = Calls()
    _wire(monkeypatch, "time", calls)
    state = build_initial_state(TIME_Q, AdvancedOptions(language="de"), Settings(render_root=tmp_path, **SETTINGS))
    with pytest.raises(ScriptNotReady) as refused:
        _render_state(state, "project-time", 2, Settings(render_root=tmp_path, **SETTINGS))
    assert refused.value.category == "research_required"
    assert calls.render == calls.media == calls.voice == 0
    gate = pre_render_quality_gate(copy.deepcopy(state))
    assert gate["ready"] is False and "information_gain_answer_insufficient" in gate["blocking_issues"]


@pytest.mark.usefixtures("legacy_without_qac")
def test_a_successful_research_retry_produces_a_ready_script(monkeypatch, tmp_path):
    calls = Calls()
    mechanism = fact(4, "Mit dem Alter gibt es weniger neue Erlebnisse, deshalb bleiben weniger Erinnerungen hängen und Jahre wirken kürzer.")
    good = [
        {"role": "answer", "text": Z_ANSWER, "fact_ids": ["fact_01"]},
        {"role": "explanation", "text": "Mit dem Alter gibt es weniger neue Erlebnisse.", "fact_ids": ["fact_02"]},
        {"role": "payoff", "text": "Deshalb bleiben weniger Erinnerungen hängen, und die Jahre wirken im Rückblick kürzer.", "fact_ids": ["fact_02"]},
    ]
    retry_facts = [TIME_FACTS[0], mechanism]

    def writer_for(request):
        return good if any("Erlebnisse" in item.claim for item in request.facts) else TIME_BLOCKS

    _wire(monkeypatch, "time", calls, retry_facts=retry_facts, writer_for=writer_for)
    state = build_initial_state(TIME_Q, AdvancedOptions(language="de"), Settings(render_root=tmp_path, **SETTINGS))
    assert len(calls.queries) == 2
    assert state["script"]["readiness"]["ready"] and [item["readiness"] for item in state["research"]["attempts"]] == ["research_required", "ready"]
    assert Z_META not in state["script"]["text"]
    rendered = _render_state(state, "project-time", 2, Settings(render_root=tmp_path, **SETTINGS))
    assert calls.render == 1 and rendered["render"]["status"] == "complete"
