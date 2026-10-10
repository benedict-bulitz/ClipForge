"""Causal spine / explanatory value: three real Mac outputs.

The narration below is each run's script verbatim (hook + writer blocks;
the pipeline splits blocks into the sentences the run showed).  Research
facts were not kept with the runs: they are reconstructed from what each
script says, and the planner's primary answer / final payoff are supplied
as the AI planner would.  Topics are fixtures only; production code has no
topic vocabulary.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from test_triple_hook import Judge, ai_candidate, generation

from clipforge import script_review, script_writer
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.novelty import (
    EXPLANATORY_DELTAS,
    assess_information_gain,
    build_novelty_plan,
    information_gain_quality_issues,
    prune_redundant_information,
)
from clipforge.pipeline import _normalise_blocks, build_initial_state
from clipforge.research import ResearchResult
from clipforge.review import pre_render_quality_gate
from clipforge.schemas import AdvancedOptions
from clipforge.script_review import (
    ScriptReviewResponse,
    ScriptReviewResult,
    ScriptReviewSentence,
    ScriptReviewSufficiency,
)
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.story_arc import build_story_arc, is_explanatory_question, story_brief


def fact(index: int, claim: str, importance: float = 0.8) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim, "importance": importance, "confidence": 0.9,
        "verification": "source_attributed", "priority": "MUST_KNOW",
        "sources": [{"label": f"s{index}", "url": f"https://s{index}.test/a"}],
    }


def intent(question: str) -> dict:
    return {"topic": question, "question": question, "content_type": "factual_explainer", "language": "de", "research_required": True}


# --- REAL OUTPUT 1: photo / mirror -------------------------------------------

MIRROR_Q = "Warum sehe ich auf Fotos schlechter aus als im Spiegel?"
MIRROR_FACTS = [
    fact(1, "Fotos wirken oft ungewohnt, weil sie nicht dem Spiegelbild entsprechen, das das Gehirn erwartet.", 0.9),
    fact(2, "Ein Spiegel zeigt das eigene Gesicht seitenverkehrt; ein Foto zeigt es so, wie andere Menschen es sehen."),
    fact(3, "Durch den Mere-Exposure-Effekt wirkt, was wir oft sehen, vertrauter und angenehmer; deshalb gefällt uns das gewohnte Spiegelbild besser."),
    fact(4, "Auch die eigene Stimme auf Aufnahmen wirkt vielen Menschen ungewohnt.", 0.5),
]
MIRROR_HOOK = "Das Foto kann gut aussehen und dir trotzdem schlechter gefallen als dein Spiegelbild."
M_ANSWER = "Auf einem Foto siehst du nicht unbedingt schlechter aus."
M_DIFFERENT = "Es sieht nur anders aus, als dein Gehirn erwartet."
M_LIKES_LESS = "Deshalb gefällt es dir oft weniger."
M_REVERSED = "Im Spiegel siehst du dein Gesicht seitenverkehrt."
M_OTHERS = "Ein Foto zeigt es meist so, wie andere Menschen dich sehen."
M_UNFAMILIAR = "Diese ungewohnte Ansicht kann dir schlechter vorkommen."
M_FAMILIARITY = "Dazu kommt ein Gewöhnungseffekt: Was wir oft sehen, wirkt vertrauter und angenehmer."
M_VOICE = "Das gilt auch für deine Stimme auf Aufnahmen."
M_LABEL = "Dieser Effekt heißt Mere-Exposure-Effekt."
M_VANITY = "Es liegt also nicht einfach an Eitelkeit."
M_PAYOFF = (
    "Darum wirkt dein Spiegelbild oft besser: Du siehst dich selbst viel öfter im Spiegel und bist an dieses "
    "seitenverkehrte Bild gewöhnt als an dein Gesicht auf Fotos."
)
MIRROR_SCRIPT = [M_ANSWER, M_DIFFERENT, M_LIKES_LESS, M_REVERSED, M_OTHERS, M_UNFAMILIAR, M_FAMILIARITY, M_VOICE, M_LABEL, M_VANITY, M_PAYOFF]
MIRROR_BLOCKS = [
    {"role": "answer", "text": f"{M_ANSWER} {M_DIFFERENT}", "fact_ids": ["fact_01"]},
    {"role": "support", "text": M_LIKES_LESS, "fact_ids": ["fact_03"]},
    {"role": "explanation", "text": f"{M_REVERSED} {M_OTHERS} {M_UNFAMILIAR}", "fact_ids": ["fact_02"]},
    {"role": "support", "text": f"{M_FAMILIARITY} {M_VOICE} {M_LABEL} {M_VANITY}", "fact_ids": ["fact_03", "fact_04"]},
    {"role": "payoff", "text": M_PAYOFF, "fact_ids": ["fact_03"]},
]

# --- REAL OUTPUT 2: TikTok ---------------------------------------------------

TIKTOK_Q = "Warum öffnen wir TikTok, obwohl wir es gar nicht wollten?"
TIKTOK_FACTS = [
    fact(1, "Wird ein TikTok-Video im Browser geöffnet, kann ein Fenster mit „TikTok öffnen“ oder „Anmelden“ erscheinen.", 0.9),
    fact(2, "Schließt man dieses Fenster, kehrt man zum Video zurück und es wird weiter abgespielt."),
]
TIKTOK_HOOK = "Manchmal öffnest du TikTok nicht neu – du landest nur wieder beim Video."
T_ANSWER = "Wenn du ein TikTok-Video im Browser öffnest, kann ein Fenster mit „TikTok öffnen“ oder „Anmelden“ erscheinen."
T_CLOSE = "Schließt du es, kommst du zurück zum Video, und es läuft weiter."
T_SEEMS = "Deshalb wirkt es so, als hättest du TikTok geöffnet, obwohl du nur zum Video zurückwolltest."
T_AGAIN = "Das Fenster führt dich also nicht einfach weg: Nach dem Schließen landest du wieder beim laufenden TikTok-Video."
T_PAYOFF = "Genau dadurch sieht es aus, als hättest du die App absichtlich geöffnet."
TIKTOK_SCRIPT = [T_ANSWER, T_CLOSE, T_SEEMS, T_AGAIN, T_PAYOFF]
TIKTOK_BLOCKS = [
    {"role": "answer", "text": T_ANSWER, "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{T_CLOSE} {T_SEEMS} {T_AGAIN}", "fact_ids": ["fact_02"]},
    {"role": "payoff", "text": T_PAYOFF, "fact_ids": ["fact_02"]},
]

# --- REAL OUTPUT 3: time perception -------------------------------------------

TIME_Q = "Warum vergeht die Zeit schneller, je älter man wird?"
TIME_FACTS = [
    fact(1, "Ältere Erwachsene haben im Durchschnitt eher das Gefühl, dass die letzten zehn Jahre schneller vergangen sind "
            "als jüngere Erwachsene; der Unterschied ist klein.", 0.9),
    fact(2, "Wir nehmen Zeit oft nicht bewusst wahr, etwa wenn wir beim Warten über Probleme nachdenken statt auf den Moment zu achten."),
    fact(3, "Neue Herausforderungen und besondere Erlebnisse können helfen, Zeit bewusster zu erleben."),
]
TIME_HOOK = "Im Alter läuft die Zeit nicht schneller – sie fühlt sich nur oft so an."
Z_ANSWER = (
    "Ältere Erwachsene haben im Schnitt eher das Gefühl, dass die letzten zehn Jahre schneller vergangen sind als bei "
    "jüngeren Erwachsenen."
)
Z_SMALL = "Der Unterschied ist aber klein: Nicht die echte Zeit läuft schneller, sondern unser Gefühl für sie verändert sich."
Z_REASON = "Ein Grund dafür ist, dass wir die Zeit oft nicht bewusst wahrnehmen."
Z_WAITING = "Beim Warten denken wir zum Beispiel über Probleme nach, statt auf den Moment zu achten."
Z_MINUTES = "Dann vergehen die Minuten, ohne dass wir es merken."
Z_ADVICE = "Darum können neue Herausforderungen und besondere Erlebnisse helfen, Zeit wieder bewusster zu erleben."
Z_ATTENTION = "Wenn unsere Aufmerksamkeit stärker im Jetzt ist, vergeht die Zeit nicht so unbemerkt."
Z_PAYOFF = "Das ist ein Teil der Erklärung, warum sie sich im Alter oft schneller anfühlt."
TIME_SCRIPT = [Z_ANSWER, Z_SMALL, Z_REASON, Z_WAITING, Z_MINUTES, Z_ADVICE, Z_ATTENTION, Z_PAYOFF]
TIME_BLOCKS = [
    {"role": "answer", "text": f"{Z_ANSWER} {Z_SMALL}", "fact_ids": ["fact_01"]},
    {"role": "explanation", "text": f"{Z_REASON} {Z_WAITING} {Z_MINUTES}", "fact_ids": ["fact_02"]},
    {"role": "support", "text": f"{Z_ADVICE} {Z_ATTENTION}", "fact_ids": ["fact_03"]},
    {"role": "payoff", "text": Z_PAYOFF, "fact_ids": []},
]

CASES = {
    "mirror": (MIRROR_Q, MIRROR_FACTS, MIRROR_HOOK, MIRROR_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 3}),
    "tiktok": (TIKTOK_Q, TIKTOK_FACTS, TIKTOK_HOOK, TIKTOK_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 2}),
    "time": (TIME_Q, TIME_FACTS, TIME_HOOK, TIME_BLOCKS, {"primary_answer_index": 1, "final_payoff_index": 2}),
}


def real(name: str, audit: dict | None = None) -> tuple[list[dict], dict]:
    """The run's narration units and the state they were assessed in."""
    question, facts, hook, writer, planner = CASES[name]
    blocks = _normalise_blocks([{"role": "hook", "text": hook}, *copy.deepcopy(writer)], 90)
    novelty = build_novelty_plan(intent(question), facts)
    arc = build_story_arc(intent(question), facts, plan_format(intent(question), facts, [], novelty), novelty, supplied=planner)
    state = {
        "intent": intent(question), "facts": copy.deepcopy(facts), "novelty_plan": novelty, "story_arc": arc,
        "script": {"blocks": blocks}, "explanation_audit": audit,
    }
    return blocks, state


def body(blocks: list[dict]) -> list[str]:
    return [block["text"] for block in blocks if block["role"] != "hook"]


def units_by_text(report: dict) -> dict[str, dict]:
    return {unit["text"]: unit for unit in report["units"] if unit["category"] != "hook"}


def repaired(name: str, audit: dict | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    blocks, state = real(name, audit)
    pruned, repairs = prune_redundant_information(copy.deepcopy(blocks), state)
    state["script"]["blocks"] = pruned
    return pruned, repairs, state, assess_information_gain(state)


def sentence(text: str, delta: str, needed: bool) -> dict:
    return {"sentence": text, "delta": delta, "needed": needed}


# The review's judgement of the mirror run (what the AI layer returns).
MIRROR_AUDIT = {
    "sentences": [
        sentence(M_ANSWER, "advances_explanation", True),
        sentence(M_DIFFERENT, "advances_explanation", True),
        sentence(M_LIKES_LESS, "restatement", False),
        sentence(M_REVERSED, "advances_explanation", True),
        sentence(M_OTHERS, "advances_explanation", True),
        sentence(M_UNFAMILIAR, "restatement", False),
        sentence(M_FAMILIARITY, "advances_explanation", True),
        sentence(M_VOICE, "useful_example", False),
        sentence(M_LABEL, "context_only", False),
        sentence(M_VANITY, "context_only", False),
        sentence(M_PAYOFF, "advances_explanation", True),
    ],
    "answer_sufficiency": {
        "verdict": "answered",
        "one_sentence_answer": "Weil du dein seitenverkehrtes Spiegelbild viel öfter siehst und es dir deshalb vertrauter ist.",
        "missing": "",
    },
}


# ---------------------------------------------------------------------------
# The spine
# ---------------------------------------------------------------------------

def test_why_questions_get_an_explanation_spine():
    _blocks, state = real("mirror")
    spine = state["story_arc"]["question_contract"]["explanation_spine"]
    assert spine["explanatory_question"] and spine["status"] == "complete"
    assert spine["main_answer"] == "fact_01" and spine["final_resolution"] == "fact_03"
    assert "fact_03" in spine["mechanism"] and not spine["research_required"]
    assert story_brief(state["story_arc"])["question_contract"]["explanation_spine"] == spine


def test_research_without_any_mechanism_is_marked_before_writing():
    for name in ("tiktok", "time"):
        spine = real(name)[1]["story_arc"]["question_contract"]["explanation_spine"]
        assert spine["status"] == "missing_mechanism" and spine["research_required"], name


def test_only_why_and_how_questions_owe_a_mechanism():
    assert is_explanatory_question(MIRROR_Q) and is_explanatory_question("How do fireflies glow?")
    assert not is_explanatory_question("Welches Land hat mehr Inseln – Schweden oder Indonesien?")


# ---------------------------------------------------------------------------
# Explanatory delta
# ---------------------------------------------------------------------------

def test_every_beat_gets_an_explanatory_delta():
    report = assess_information_gain(real("mirror")[1])
    deltas = {text: unit["explanatory_delta"] for text, unit in units_by_text(report).items()}
    assert set(deltas.values()) <= set(EXPLANATORY_DELTAS)
    assert deltas[M_REVERSED] == deltas[M_FAMILIARITY] == deltas[M_PAYOFF] == "advances_explanation"
    assert deltas[M_VOICE] == "useful_example"
    assert deltas[M_LABEL] == "context_only"
    assert sum(report["summary"]["explanatory_deltas"].values()) == len(MIRROR_SCRIPT)


def test_explanatory_delta_is_stricter_than_information_gain():
    units = units_by_text(assess_information_gain(real("mirror")[1]))
    # New, on-topic and supported - yet it lets the viewer explain nothing more.
    for text in (M_VOICE, M_LABEL):
        assert units[text]["beat_class"] == "useful_gain" and units[text]["counts_as_gain"]
        assert units[text]["explanatory_delta"] != "advances_explanation"
    tiktok = units_by_text(assess_information_gain(real("tiktok")[1]))
    assert tiktok[T_SEEMS]["beat_class"] == "useful_gain" and tiktok[T_SEEMS]["explanatory_delta"] == "context_only"


def test_the_reviews_judgement_drives_the_delta_but_never_the_evidence():
    units = units_by_text(assess_information_gain(real("mirror", MIRROR_AUDIT)[1]))
    assert units[M_LIKES_LESS]["explanatory_delta"] == "restatement" and units[M_LIKES_LESS]["delta_source"] == "ai"
    # Unsupported by the research: the review may say "not needed", never "explains".
    audit = copy.deepcopy(MIRROR_AUDIT)
    audit["sentences"] = [sentence(M_VANITY, "advances_explanation", True)]
    vanity = units_by_text(assess_information_gain(real("mirror", audit)[1]))[M_VANITY]
    assert vanity["beat_class"] == "unsupported" and vanity["explanatory_delta"] == "weak_value"


# ---------------------------------------------------------------------------
# PHOTO / MIRROR: compresses without losing the explanation
# ---------------------------------------------------------------------------

def test_mirror_weak_tail_is_removed_deterministically():
    before, _state = real("mirror")
    pruned, repairs, _state, report = repaired("mirror")
    # Analogy, technical label and the vanity aside come after the mechanism.
    assert {repair["text"] for repair in repairs} == {M_VOICE, M_LABEL, M_VANITY}
    assert {repair["action"] for repair in repairs} == {"remove_weak_tail"}
    assert len(body(pruned)) == len(body(before)) - 3
    assert body(pruned)[-1] == M_PAYOFF and M_FAMILIARITY in body(pruned) and M_REVERSED in body(pruned)
    assert report["answer_sufficiency"]["status"] == "pass" and report["payoff"]["status"] == "pass"


def test_mirror_with_the_reviews_judgement_compresses_to_its_causal_spine():
    pruned, repairs, _state, report = repaired("mirror", MIRROR_AUDIT)
    assert body(pruned) == [M_ANSWER, M_DIFFERENT, M_REVERSED, M_OTHERS, M_FAMILIARITY, M_PAYOFF]
    assert {repair["action"] for repair in repairs} <= {"remove_low_explanation", "remove_weak_tail"}
    assert set(body(pruned)) <= set(MIRROR_SCRIPT)  # nothing invented, nothing rewritten
    assert report["answer_sufficiency"]["status"] == "pass"
    assert report["answer_sufficiency"]["one_sentence_answer"].startswith("Weil")
    assert not any(issue["code"] in {"weak_tail", "low_explanatory_value", "answer_insufficient"} for issue in report["issues"])


def test_an_analogy_or_label_survives_when_the_review_needs_it():
    audit = {"sentences": [sentence(M_LABEL, "context_only", True)]}
    pruned, _repairs, _state, _report = repaired("mirror", audit)
    assert M_LABEL in body(pruned) and M_VOICE not in body(pruned)


def test_the_mirror_payoff_completes_the_causal_path():
    report = assess_information_gain(real("mirror")[1])
    assert report["payoff"]["status"] == "pass" and not units_by_text(report)[M_PAYOFF]["weak_resolution"]


# ---------------------------------------------------------------------------
# TIKTOK: on topic, but no mechanism -> fail, research required
# ---------------------------------------------------------------------------

def test_tiktok_real_script_fails_without_a_supported_mechanism():
    report = assess_information_gain(real("tiktok")[1])
    sufficiency = report["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and sufficiency["research_required"]
    assert {"no_mechanism_linked_to_question", "payoff_does_not_resolve", "research_has_no_mechanism"} <= set(sufficiency["reasons"])
    assert report["payoff"]["result"] == "weak_resolution"  # "sieht es aus, als hättest du ..." describes, not explains
    errors = [issue for issue in report["issues"] if issue["severity"] == "error"]
    assert [issue["code"] for issue in errors] == ["answer_insufficient"]
    assert "research" in errors[0]["message"]


def test_tiktok_repairs_never_turn_the_modal_loop_into_an_answer():
    pruned, _repairs, _state, report = repaired("tiktok")
    assert set(body(pruned)) <= set(TIKTOK_SCRIPT)  # nothing invented
    assert len(body(pruned)) < len(TIKTOK_SCRIPT)
    assert report["answer_sufficiency"]["status"] == "fail"  # shorter, still not an answer


def test_the_reviews_unanswered_verdict_fails_on_its_own():
    audit = {"sentences": [], "answer_sufficiency": {"verdict": "unanswered", "one_sentence_answer": "", "missing": "why the app opens"}}
    _blocks, state = real("mirror", audit)
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and "review_unanswered" in sufficiency["reasons"]
    assert sufficiency["missing"] == "why the app opens"


# ---------------------------------------------------------------------------
# TIME: generic attention facts, no age-specific chain -> fail
# ---------------------------------------------------------------------------

def test_time_real_script_fails_without_an_age_specific_causal_chain():
    report = assess_information_gain(real("time")[1])
    sufficiency = report["answer_sufficiency"]
    assert sufficiency["status"] == "fail" and sufficiency["research_required"]
    assert "no_mechanism_linked_to_question" in sufficiency["reasons"]
    assert sufficiency["mechanism_block_ids"] == []  # attention/waiting never reaches "älter"
    assert units_by_text(report)[Z_WAITING]["explanatory_delta"] == "useful_example"


def test_part_of_the_explanation_is_not_a_payoff():
    report = assess_information_gain(real("time")[1])
    assert report["payoff"]["status"] == "fail" and report["payoff"]["result"] == "weak_resolution"
    assert any(issue["code"] == "payoff_weak_resolution" for issue in report["issues"])


def test_weak_summary_endings_are_rejected():
    _blocks, state = real("time")
    for ending in ("Das ist ein Teil der Erklärung.", "Deshalb passiert dieser Effekt.", "Und mit zunehmendem Alter wird dieser Effekt stärker."):
        blocks = copy.deepcopy(state["script"]["blocks"])
        blocks[-1] = {**blocks[-1], "text": ending}
        state["script"]["blocks"] = blocks
        payoff = assess_information_gain(state)["payoff"]
        assert payoff["status"] == "fail" and payoff["result"] in {"weak_resolution", "repeats_earlier", "generic"}, ending


def test_time_repairs_do_not_turn_advice_into_an_answer():
    pruned, _repairs, _state, report = repaired("time")
    assert set(body(pruned)) <= set(TIME_SCRIPT)
    assert Z_PAYOFF not in body(pruned)  # the meta ending hands over, nothing is written in its place
    assert report["answer_sufficiency"]["status"] == "fail"


def test_an_age_specific_mechanism_passes():
    good = "Ein Grund: Mit dem Alter gibt es oft weniger neue Erlebnisse, die einen starken Eindruck hinterlassen."
    facts = [*TIME_FACTS, fact(4, "Mit dem Alter gibt es weniger neue Erlebnisse, deshalb bleiben weniger Erinnerungen hängen.")]
    blocks = [
        {"id": "b1", "role": "hook", "text": TIME_HOOK},
        {"id": "b2", "role": "answer", "text": Z_ANSWER, "fact_ids": ["fact_01"]},
        {"id": "b3", "role": "payoff", "text": good, "fact_ids": ["fact_04"]},
    ]
    novelty = build_novelty_plan(intent(TIME_Q), facts)
    arc = build_story_arc(intent(TIME_Q), facts, plan_format(intent(TIME_Q), facts, [], novelty), novelty,
                          supplied={"primary_answer_index": 1, "final_payoff_index": 4})
    state = {"intent": intent(TIME_Q), "facts": facts, "novelty_plan": novelty, "story_arc": arc, "script": {"blocks": blocks}}
    sufficiency = assess_information_gain(state)["answer_sufficiency"]
    assert sufficiency["status"] == "pass" and sufficiency["mechanism_block_ids"] == ["b3"]


# ---------------------------------------------------------------------------
# Generation: the review's audit reaches pruning and the gate
# ---------------------------------------------------------------------------

def _generate(monkeypatch, tmp_path, name: str, response: ScriptReviewResponse) -> dict:
    question, facts, hook, writer, _planner = CASES[name]
    source = {"label": "Quelle", "url": "https://source.test/a"}
    research = [{**{key: value for key, value in item.items() if key != "id"}, "sources": [source]} for item in facts]

    class Writer:
        name = "fixture"

        def __init__(self, _settings=None):
            pass

        def generate(self, _request):
            return ScriptWriterResult(ScriptDraftV2(language="de", blocks=[ScriptBlockV2(**block) for block in writer]), "connected")

    class Reviewer:
        name = "fixture"

        def __init__(self, _settings=None):
            pass

        def review(self, _request):
            return ScriptReviewResult(response, "approved")

    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, [source], "verified_sources", "fixture"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", Reviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation([
        ai_candidate("A", "curiosity_gap", hook, "person holding a phone", ["person holding phone"], action="holding", detail="phone"),
    ]))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", Judge())
    return build_initial_state(question, AdvancedOptions(), Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path))


def test_generation_fails_the_tiktok_script_at_the_gate(monkeypatch, tmp_path):
    response = ScriptReviewResponse(
        status="approve",
        explanation_audit=[ScriptReviewSentence(sentence=T_SEEMS, delta="restatement", needed=False),
                           ScriptReviewSentence(sentence=T_AGAIN, delta="restatement", needed=False)],
        answer_sufficiency=ScriptReviewSufficiency(verdict="unanswered", missing="why TikTok opens without the viewer wanting it"),
    )
    state = _generate(monkeypatch, tmp_path, "tiktok", response)
    assert state["explanation_audit"]["answer_sufficiency"]["verdict"] == "unanswered"
    assert T_AGAIN not in body(state["script"]["blocks"])  # the modal loop is not repeated
    assert set(body(state["script"]["blocks"])) <= set(TIKTOK_SCRIPT)
    assert any(issue["code"] == "answer_insufficient" and issue["severity"] == "error" for issue in information_gain_quality_issues(state))
    gate = pre_render_quality_gate(state)
    assert gate["status"] == "fallback" and "information_gain_answer_insufficient" in gate["severe_issues"]


def test_review_and_writer_prompts_carry_the_causal_spine():
    for prompt in (script_writer.SCRIPT_WRITER_V2_INSTRUCTIONS, script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS):
        assert "explanation_spine" in prompt and "part of the explanation" in prompt
        assert "Du bist dieses Bild einfach weniger gewohnt." in prompt
    assert "answer_sufficiency" in script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS
    assert "ORIGINAL question" in script_review.SCRIPT_REVIEW_V2_INSTRUCTIONS
