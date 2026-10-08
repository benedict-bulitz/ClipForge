"""Script & Story Quality V2: critic -> holistic creative rewrite -> verifier.

Every AI response is mocked; no test reaches a live API.  Topics are fixtures
only: the production logic has no airplane (or any topic) vocabulary.
"""
from __future__ import annotations

import copy

import pytest
from test_script_story_quality_v1 import FACTS as VISION_FACTS
from test_script_story_quality_v1 import context as vision_context
from test_script_story_quality_v1 import strong_blocks as vision_blocks

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge import pipeline
from clipforge.config import Settings
from clipforge.novelty import VERIFIED_AUDIT_SOURCE, assess_information_gain, verified_script_key
from clipforge.pipeline import (
    _refresh_script_derivatives,
    build_initial_state,
    enforce_selected_hook,
)
from clipforge.readiness import content_readiness
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions
from clipforge.script_review import ScriptReviewSentence, ScriptReviewSufficiency
from clipforge.script_story_quality import run_script_story_quality_v1
from clipforge.script_story_rewrite import (
    MAX_REWRITE_ATTEMPTS,
    CriticResponse,
    EditorFinding,
    OpenAIScriptStoryProvider,
    RewriteBeat,
    RewriteResponse,
    ScriptStoryProviderError,
    VerifierResponse,
    answer_payoff_duplicate,
    run_script_story_quality,
)
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult


def fact(identifier: str, claim: str) -> dict:
    return {
        "id": identifier,
        "claim": claim,
        "confidence": 0.95,
        "importance": 0.9,
        "verification": "source_attributed",
        "sources": [{"label": identifier, "url": f"https://{identifier}.test/source"}],
    }


DE_QUESTION = "Warum können Flugzeuge überhaupt fliegen, obwohl sie so schwer sind?"
EN_QUESTION = "Why can airplanes fly at all even though they are so heavy?"

DE_FACTS = [
    fact("lift_01", "Beim Fliegen wirken vier Kräfte auf ein Flugzeug: Auftrieb, Gewichtskraft, Schub und Luftwiderstand."),
    fact("lift_02", "Die Tragflächen sind gewölbt und leicht schräg angestellt, sodass sie die vorbeiströmende Luft nach unten ablenken."),
    fact("lift_03", "Über der Tragfläche ist der Luftdruck dabei geringer als darunter."),
    fact("lift_04", "Der Druckunterschied und die nach unten gelenkte Luft drücken die Tragfläche nach oben; diese Kraft heißt Auftrieb."),
    fact("lift_05", "Die Triebwerke erzeugen Schub, damit die Luft schnell genug an den Tragflächen vorbeiströmt."),
    fact("lift_06", "Sobald der Auftrieb so groß ist wie die Gewichtskraft, bleibt das Flugzeug trotz seines Gewichts in der Luft."),
]
EN_FACTS = [
    fact("lift_01", "Four forces act on a plane in flight: lift, weight, thrust and drag."),
    fact("lift_02", "The wings are curved and tilted slightly upward, so they deflect the passing air downward."),
    fact("lift_03", "Air pressure above the wing is lower than below it."),
    fact("lift_04", "The pressure difference and the downward-deflected air push the wing upward; this force is called lift."),
    fact("lift_05", "The engines produce thrust so that air flows over the wings fast enough."),
    fact("lift_06", "Once lift is as large as the weight, the plane stays in the air despite how heavy it is."),
]


def lift_arc(question: str) -> dict:
    return {
        "primary_question": question,
        "primary_answer_id": "lift_06",
        "final_payoff_id": "lift_06",
        "order": ["lift_05", "lift_02", "lift_03", "lift_04", "lift_06"],
        "curiosity_gap": {"withhold_answer": False},
        "hook": {"protected_ids": [], "allowed_ids": ["lift_01", "lift_05"]},
        "units": [
            {"id": "lift_01", "role": "supporting_fact", "depends_on": [], "may_be_omitted": True},
            {"id": "lift_05", "role": "explanation", "depends_on": [], "may_be_omitted": False},
            {"id": "lift_02", "role": "explanation", "depends_on": [], "may_be_omitted": False},
            {"id": "lift_03", "role": "explanation", "depends_on": ["lift_02"], "may_be_omitted": False},
            {"id": "lift_04", "role": "explanation", "depends_on": ["lift_02", "lift_03"], "may_be_omitted": False},
            {"id": "lift_06", "role": "primary_answer", "depends_on": ["lift_04"], "may_be_omitted": False},
        ],
        "question_contract": {
            "core_question": question,
            "essential_explanation_chain": ["lift_05", "lift_02", "lift_03", "lift_04", "lift_06"],
            "explanation_spine": {"status": "complete", "mechanism": ["lift_02", "lift_03", "lift_04"]},
        },
    }


def block(index: int, role: str, text: str, fact_ids: list[str] | None = None) -> dict:
    return {"id": f"voice_block_{index:02d}", "role": role, "text": text, "fact_ids": list(fact_ids or [])}


# The real, technically valid but mediocre German draft (regression example).
DRAFT_DE = [
    block(1, "hook", "Ein schweres Flugzeug steigt, obwohl die Schwerkraft es nach unten zieht."),
    block(2, "answer", "Ein Flugzeug bleibt trotz seines Gewichts oben, weil der Auftrieb nach oben der Schwerkraft nach unten entgegenwirkt.", ["lift_06"]),
    block(3, "explanation", "Dafür müssen beim Fliegen vier Kräfte im richtigen Verhältnis zusammenwirken.", ["lift_01"]),
    block(4, "support", "Die Tragflächen haben dafür eine passende Form im Querschnitt.", ["lift_02"]),
    block(5, "detail", "Der Vortrieb lässt Luft an den Tragflächen vorbeiströmen.", ["lift_05"]),
    block(6, "payoff", "Dadurch kann der nötige Auftrieb entstehen und das Flugzeug trotz seines Gewichts oben bleiben.", ["lift_04", "lift_06"]),
]
DRAFT_EN = [
    block(1, "hook", "A heavy airplane climbs even though gravity pulls it down."),
    block(2, "answer", "A plane stays up despite its weight because lift pushing up counters gravity pulling down.", ["lift_06"]),
    block(3, "explanation", "For that, four forces have to work together in the right ratio.", ["lift_01"]),
    block(4, "support", "The wings have a suitable cross-section for this.", ["lift_02"]),
    block(5, "detail", "Thrust makes air flow past the wings.", ["lift_05"]),
    block(6, "payoff", "That is how the necessary lift can form and the plane can stay up despite its weight.", ["lift_04", "lift_06"]),
]

# A strong holistic rewrite: new hook wording, fewer beats, new order, the
# omitted pressure-difference fact (lift_03) and a real mechanism.
REWRITE_DE = [
    ("hook", "Ein Flugzeug ist richtig schwer – und trotzdem trägt es nur die Luft.", []),
    ("explanation", "Die Triebwerke schieben das Flugzeug nach vorn, damit Luft schnell genug an den Tragflächen vorbeiströmt.", ["lift_05"]),
    ("explanation", "Die Tragflächen sind gewölbt und leicht schräg gestellt: Sie lenken diese Luft nach unten ab, und über ihnen sinkt der Luftdruck unter den Druck darunter.", ["lift_02", "lift_03"]),
    ("answer", "Beides zusammen drückt die Tragfläche nach oben – das ist der Auftrieb.", ["lift_04"]),
    ("payoff", "Sobald dieser Auftrieb so groß ist wie die Gewichtskraft, trägt die Luft das ganze Flugzeug.", ["lift_06"]),
]
REWRITE_EN = [
    ("hook", "A plane is seriously heavy, and yet only air holds it up.", []),
    ("explanation", "The engines push the plane forward so air rushes over the wings fast enough.", ["lift_05"]),
    ("explanation", "The wings are curved and tilted slightly up: they bend that air downward, and the pressure above the wing drops below the pressure underneath.", ["lift_02", "lift_03"]),
    ("answer", "Together, that pushes the wing upward. That push is lift.", ["lift_04"]),
    ("payoff", "Once lift grows as large as the plane's weight, the air carries the whole aircraft.", ["lift_06"]),
]


def lift_context(language: str = "de") -> dict:
    question = DE_QUESTION if language == "de" else EN_QUESTION
    draft = DRAFT_DE if language == "de" else DRAFT_EN
    return {
        "prompt": question,
        "intent": {
            "question": question, "topic": "airplane lift", "language": language,
            "research_required": True, "content_type": "factual_explainer",
        },
        "facts": copy.deepcopy(DE_FACTS if language == "de" else EN_FACTS),
        "story_arc": lift_arc(question),
        "novelty_plan": {"explanatory_gain": ["lift_02", "lift_03", "lift_04"]},
        "payoff_plan": {"primary_answer_id": "lift_06", "final_payoff_id": "lift_06"},
        "word_budget": 120,
        "script": {"triple_hook": {"verbal_hook": draft[0]["text"]}, "selected_hook": draft[0]["text"]},
        "reaction_plan": {"planned_arc": {"hook_reaction": "curiosity", "payoff_reaction": "insight"}},
    }


def rewrite(beats, **extra) -> RewriteResponse:
    return RewriteResponse(
        status="rewritten",
        beats=[RewriteBeat(role=role, text=text, fact_ids=list(ids)) for role, text, ids in beats],
        hook_intent_preserved=True,
        reveal_beat_index=extra.pop("reveal_beat_index", len(beats)),
        payoff_beat_index=extra.pop("payoff_beat_index", len(beats)),
        rationale=extra.pop("rationale", "Explain the mechanism step by step and end on the balance of forces."),
        **extra,
    )


def critic(verdict: str = "rewrite", findings: list[EditorFinding] | None = None, missed: list[str] | None = None) -> CriticResponse:
    return CriticResponse(
        verdict=verdict,
        findings=findings if findings is not None else [
            EditorFinding(code="missing_mechanism", severity="major", beat_index=2, message="Names lift but never explains why it occurs."),
            EditorFinding(code="answer_payoff_duplicate", severity="major", beat_index=6, message="The payoff restates the answer."),
        ],
        missed_fact_ids=missed if missed is not None else ["lift_03"],
    )


def verdict(beats=None, **overrides) -> VerifierResponse:
    values = {
        "grounded": True, "answers_question": True, "payoff_fulfilled": True, "premature_reveal": False,
        "hook_promise_kept": True, "answer_payoff_duplicate": False, "better_than_draft": True, "findings": [],
        "answer_sufficiency": ScriptReviewSufficiency(verdict="answered", one_sentence_answer="Wings push air down and lift balances weight."),
        "explanation_audit": [
            ScriptReviewSentence(sentence=text, delta="advances_explanation", needed=True)
            for role, text, _ids in (beats or []) if role != "hook"
        ],
    }
    values.update(overrides)
    return VerifierResponse(**values)


class FakeEditor:
    """Critic / rewriter / verifier double with scripted responses and recorded briefs."""

    name = "fake-editor"

    def __init__(self, *, critic_response=None, rewrites=(), verdicts=(), fail_on: str | None = None):
        self.critic_response = critic_response or critic()
        self.rewrites = list(rewrites)
        self.verdicts = list(verdicts)
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []

    def _call(self, stage: str, brief: dict):
        self.calls.append((stage, copy.deepcopy(brief)))
        if self.fail_on == stage:
            raise ScriptStoryProviderError(f"{stage} offline")

    def critique(self, brief):
        self._call("critique", brief)
        return self.critic_response

    def rewrite(self, brief):
        self._call("rewrite", brief)
        return self.rewrites.pop(0)

    def verify(self, brief):
        self._call("verify", brief)
        item = self.verdicts.pop(0) if self.verdicts else verdict()
        return item(brief) if callable(item) else item

    def stages(self) -> list[str]:
        return [stage for stage, _brief in self.calls]


def texts(blocks: list[dict]) -> list[str]:
    return [item["text"] for item in blocks]


# 1, 3, 4, 9, 15 -------------------------------------------------------------------

def test_entire_german_script_can_be_structurally_rewritten():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    assert editor.stages() == ["critique", "rewrite", "verify"]
    assert report["mode"] == "holistic_ai" and report["holistic"]["status"] == "rewritten"
    assert texts(final) == [text for _role, text, _ids in REWRITE_DE]
    assert not set(texts(final)) & set(texts(DRAFT_DE))  # every sentence rewritten
    assert [item["role"] for item in final] == ["hook", "explanation", "explanation", "answer", "payoff"]
    assert report["gate"]["ready"] is True
    assert report["rewrite"]["verified_by"] == "ai_verifier"


def test_beat_count_may_change():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, _ = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    assert len(DRAFT_DE) == 6 and len(final) == 5


def test_strong_research_fact_omitted_by_the_draft_may_be_introduced():
    draft_ids = {fact_id for item in DRAFT_DE for fact_id in item["fact_ids"]}
    assert "lift_03" not in draft_ids
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, _ = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    assert "lift_03" in {fact_id for item in final for fact_id in item["fact_ids"]}
    # The rewriter saw the whole research dossier, not only the draft's facts.
    brief = editor.calls[1][1]
    assert {item["id"] for item in brief["research"]["facts"]} == {item["id"] for item in DE_FACTS}
    assert brief["critic"]["missed_fact_ids"] == ["lift_03"]


def test_weak_mechanism_explanation_is_replaced_with_supported_research():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    mechanism = {"lift_02", "lift_03", "lift_04"}
    assert mechanism <= {fact_id for item in final for fact_id in item["fact_ids"]}
    assert "weil der Auftrieb nach oben der Schwerkraft nach unten entgegenwirkt" not in " ".join(texts(final))
    # The critic's mechanism finding reached the rewriter as editorial input.
    assert any(item["code"] == "missing_mechanism" for item in editor.calls[1][1]["critic"]["findings"])
    # A verified mechanism without lexical "because" is not blocked by the
    # lexical sufficiency heuristic.
    assert report["gate"]["ready"] is True


def test_german_regression_script_answer_and_payoff_are_semantic_duplicates():
    duplicate = answer_payoff_duplicate(DRAFT_DE)
    assert duplicate is not None and duplicate["new_share"] < 0.35
    assert answer_payoff_duplicate([block(index, role, text, ids) for index, (role, text, ids) in enumerate(REWRITE_DE, 1)]) is None


# 2 -----------------------------------------------------------------------------

def test_hook_wording_may_change_while_hook_intent_remains():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    assert final[0]["role"] == "hook" and final[0]["text"] != DRAFT_DE[0]["text"]
    assert report["rewrite"]["hook_changed"] is True and report["rewrite"]["hook_restored"] is False
    assert report["rewrite"]["hook_text"] == REWRITE_DE[0][1]
    # The rewriter was given the hook intent, not a byte-for-byte constraint.
    assert editor.calls[1][1]["hook_intent"]["selected_hook"] == DRAFT_DE[0]["text"]


def test_hook_that_breaks_the_hook_intent_falls_back_to_the_selected_hook():
    context = vision_context()
    beats = [
        ("hook", VISION_FACTS[2]["claim"], []),  # spends the protected answer
        ("explanation", VISION_FACTS[0]["claim"], ["fact_01"]),
        ("explanation", VISION_FACTS[1]["claim"], ["fact_02"]),
        ("payoff", "So for a moment the brain gets less oxygen and your vision can go dark.", ["fact_03"]),
    ]
    editor = FakeEditor(rewrites=[rewrite(beats)], verdicts=[verdict(beats)])
    context["script"]["selected_hook"] = vision_blocks()[0]["text"]
    final, report = run_script_story_quality(vision_blocks(), context, editor)
    assert final[0]["text"] == vision_blocks()[0]["text"]
    assert report["rewrite"]["hook_restored"] is True


# 5, 6 --------------------------------------------------------------------------

def test_unsupported_new_fact_is_rejected_and_repaired():
    invented = list(REWRITE_EN)
    invented[2] = ("explanation", "The wing works like a sail that catches the wind from below.", [])
    editor = FakeEditor(rewrites=[rewrite(invented), rewrite(REWRITE_EN)], verdicts=[verdict(REWRITE_EN)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    first = report["holistic"]["attempts"][0]
    assert {item["code"] for item in first["hard"]} >= {"uncited_beat"}
    # The hard failure never reached the AI verifier; the repair did.
    assert editor.stages() == ["critique", "rewrite", "rewrite", "verify"]
    assert texts(final)[2] == REWRITE_EN[2][1]
    assert report["holistic"]["selected_attempt"] == 2


def test_fabricated_fact_id_and_verifier_ungrounded_claim_are_hard_failures():
    fabricated = list(REWRITE_EN)
    fabricated[1] = ("explanation", REWRITE_EN[1][1], ["lift_99"])
    paraphrased = list(REWRITE_EN)
    paraphrased[2] = ("explanation", "The wings suck the plane upward like a vacuum cleaner.", ["lift_02"])
    editor = FakeEditor(
        critic_response=critic(findings=[EditorFinding(code="unsupported_statement", severity="hard", message="The answer overstates gravity.")]),
        rewrites=[rewrite(fabricated), rewrite(paraphrased)],
        verdicts=[verdict(paraphrased, grounded=False, findings=[EditorFinding(code="contradicts_research", severity="hard", beat_index=3, message="Lift is not suction.")])],
    )
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    first, second = report["holistic"]["attempts"]
    assert "fabricated_fact_id" in {item["code"] for item in first["hard"]}
    assert {"ungrounded", "contradicts_research"} <= {item["code"] for item in second["hard"]}


def test_unsupported_number_is_rejected():
    numbered = list(REWRITE_EN)
    numbered[1] = ("explanation", "The engines push the plane to 250 km/h so air rushes over the wings fast enough.", ["lift_05"])
    editor = FakeEditor(rewrites=[rewrite(numbered), rewrite(REWRITE_EN)], verdicts=[verdict(REWRITE_EN)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    hard = report["holistic"]["attempts"][0]["hard"]
    assert any(item["code"] == "unsupported_number" and "250" in item["message"] for item in hard)
    assert "250" not in " ".join(texts(final))


# 7 -----------------------------------------------------------------------------

def _vision_rewrite(order: list[str]) -> list[tuple]:
    beats = {
        "f1": ("explanation", "Standing up lets gravity pull blood from your upper body down toward your legs.", ["fact_01"]),
        "f2": ("explanation", "Pressure sensors called baroreceptors react by tightening vessels and speeding up your heart.", ["fact_02"]),
        "f3": ("payoff", "Until that catches up, your brain briefly runs short of oxygen, so your vision can fade to black.", ["fact_03"]),
    }
    return [("hook", "Stand up too fast and your body has a split second to react.", []), *(beats[key] for key in order)]


def test_protected_reveal_may_move_later_but_never_earlier():
    early = _vision_rewrite(["f3", "f1", "f2"])
    early[1] = ("answer", early[1][1], early[1][2])
    early[3] = ("payoff", "That is the moment your circulation fixes it again.", ["fact_02"])
    late = _vision_rewrite(["f1", "f2", "f3"])
    editor = FakeEditor(rewrites=[rewrite(early), rewrite(late)], verdicts=[verdict(late)])
    final, report = run_script_story_quality(vision_blocks(), vision_context(), editor)
    assert "premature_reveal" in {item["code"] for item in report["holistic"]["attempts"][0]["hard"]}
    assert final[-1]["fact_ids"] == ["fact_03"]
    assert report["holistic"]["selected_attempt"] == 2 and report["gate"]["ready"] is True


def test_verifier_premature_reveal_flag_is_a_hard_failure():
    late = _vision_rewrite(["f1", "f2", "f3"])
    editor = FakeEditor(
        critic_response=critic(findings=[EditorFinding(code="unsupported_statement", severity="hard", message="Draft overstates the claim.")]),
        rewrites=[rewrite(late), rewrite(late)],
        verdicts=[verdict(late, premature_reveal=True), verdict(late, premature_reveal=True)],
    )
    _final, report = run_script_story_quality(vision_blocks(), vision_context(), editor)
    assert report["gate"]["ready"] is False and "rewrite_hard_failure" in report["gate"]["blocking"]


# 8 -----------------------------------------------------------------------------

def test_answer_payoff_semantic_duplication_is_rejected():
    duplicate = list(REWRITE_DE)
    duplicate[3] = ("answer", DRAFT_DE[1]["text"], ["lift_04"])
    duplicate[4] = ("payoff", DRAFT_DE[5]["text"], ["lift_06"])
    editor = FakeEditor(rewrites=[rewrite(duplicate), rewrite(REWRITE_DE)], verdicts=[verdict(duplicate), verdict(REWRITE_DE)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    first = report["holistic"]["attempts"][0]
    assert "answer_payoff_duplicate" in {item["code"] for item in first["major"]}
    assert texts(final) == [text for _role, text, _ids in REWRITE_DE]
    repair = editor.calls[3][1]
    assert repair["attempt"] == 2 and any(item["code"] == "answer_payoff_duplicate" for item in repair["verifier_findings"])


def test_verifier_answer_payoff_duplicate_flag_triggers_the_repair():
    editor = FakeEditor(
        rewrites=[rewrite(REWRITE_EN), rewrite(REWRITE_EN)],
        verdicts=[verdict(REWRITE_EN, answer_payoff_duplicate=True), verdict(REWRITE_EN)],
    )
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert editor.stages().count("rewrite") == 2 and report["holistic"]["selected_attempt"] == 2


# 10 ----------------------------------------------------------------------------

def test_strong_initial_script_is_not_rewritten():
    editor = FakeEditor(critic_response=critic(verdict="strong", findings=[], missed=[]))
    final, report = run_script_story_quality(vision_blocks(), vision_context(), editor)
    assert final == vision_blocks()
    assert editor.stages() == ["critique"]
    assert report["holistic"]["status"] == "skipped_already_strong" and report["gate"]["ready"] is True


def test_rewrite_that_is_not_better_than_a_clean_draft_keeps_the_draft():
    late = _vision_rewrite(["f1", "f2", "f3"])
    editor = FakeEditor(
        critic_response=critic(findings=[EditorFinding(code="weak_hook", severity="minor", message="Could be punchier.")]),
        rewrites=[rewrite(late)],
        verdicts=[verdict(late, better_than_draft=False)],
    )
    final, report = run_script_story_quality(vision_blocks(), vision_context(), editor)
    assert final == vision_blocks()
    assert report["holistic"]["status"] == "kept_draft_rewrite_not_better"


# 11, 12, 13 --------------------------------------------------------------------

def test_first_rewrite_failing_the_verifier_gets_one_bounded_retry():
    editor = FakeEditor(
        rewrites=[rewrite(REWRITE_EN), rewrite(REWRITE_EN)],
        verdicts=[
            verdict(REWRITE_EN, answers_question=False, findings=[EditorFinding(code="shallow", severity="hard", message="Never explains why.")]),
            verdict(REWRITE_EN),
        ],
    )
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert editor.stages() == ["critique", "rewrite", "verify", "rewrite", "verify"]
    repair = editor.calls[3][1]
    assert repair["previous_rewrite"] and {"question_unanswered", "shallow"} <= {item["code"] for item in repair["verifier_findings"]}
    assert report["holistic"]["status"] == "rewritten" and report["holistic"]["selected_attempt"] == 2


def test_second_hard_failure_blocks_production():
    editor = FakeEditor(
        critic_response=critic(findings=[EditorFinding(code="unsupported_statement", severity="hard", message="The draft claims more than the research.")]),
        rewrites=[rewrite(REWRITE_EN), rewrite(REWRITE_EN)],
        verdicts=[verdict(REWRITE_EN, grounded=False), verdict(REWRITE_EN, grounded=False)],
    )
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert editor.stages().count("rewrite") == MAX_REWRITE_ATTEMPTS == 2
    assert report["holistic"]["status"] == "needs_fix"
    assert report["gate"]["ready"] is False and "rewrite_hard_failure" in report["gate"]["blocking"]
    state = {**lift_context("en"), "script": {"blocks": final, "script_story_quality_v1": report}}
    readiness = content_readiness(state)
    assert not readiness["ready"]
    assert "script_story_quality_rewrite_hard_failure" in {item["code"] for item in readiness["blocking"]}


def test_hard_failures_with_a_clean_draft_keep_the_grounded_draft():
    late = _vision_rewrite(["f1", "f2", "f3"])
    editor = FakeEditor(
        critic_response=critic(findings=[EditorFinding(code="weak_hook", severity="major", message="Generic opening.")]),
        rewrites=[rewrite(late), rewrite(late)],
        verdicts=[verdict(late, grounded=False), verdict(late, grounded=False)],
    )
    final, report = run_script_story_quality(vision_blocks(), vision_context(), editor)
    assert final == run_script_story_quality_v1(vision_blocks(), vision_context())[0]
    assert report["holistic"]["status"] == "rewrite_rejected_kept_draft" and report["gate"]["ready"] is True


def test_minor_style_warning_does_not_block():
    editor = FakeEditor(
        rewrites=[rewrite(REWRITE_EN)],
        verdicts=[verdict(REWRITE_EN, findings=[EditorFinding(code="rhythm", severity="minor", beat_index=3, message="Slightly long sentence.")])],
    )
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert editor.stages().count("rewrite") == 1
    assert report["gate"]["ready"] is True and report["gate"]["status"] == "passed_with_warnings"
    assert any(item["issue_type"] == "rhythm" and item["severity"] == "info" for item in report["issues"])


def test_needs_research_from_the_rewriter_blocks_instead_of_guessing():
    editor = FakeEditor(rewrites=[RewriteResponse(status="needs_research", research_insufficiency="No source explains the pressure difference.")])
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert report["holistic"]["status"] == "needs_research"
    assert report["research_insufficient"] is True and "needs_research" in report["gate"]["blocking"]


# 14, 17 ------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["critique", "rewrite"])
def test_ai_unavailable_falls_back_to_the_deterministic_pass(stage):
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)], fail_on=stage)
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    expected, deterministic = run_script_story_quality_v1(copy.deepcopy(DRAFT_EN), lift_context("en"))
    assert final == expected
    assert report["mode"] == "deterministic" and report["holistic"]["status"] == "provider_unavailable"
    assert report["gate"] == deterministic["gate"]


def test_verifier_unavailable_lets_the_deterministic_gate_govern():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_DE)], fail_on="verify")
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    attempt = report["holistic"]["attempts"][0]
    assert attempt["status"] == "deterministic_only" and "verify offline" in attempt["verifier_error"]


def test_openai_editor_is_offline_in_tests_and_falls_back():
    provider = OpenAIScriptStoryProvider(Settings(openai_api_key="test-key"))
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), provider)
    assert report["holistic"]["status"] == "provider_unavailable"
    assert final == run_script_story_quality_v1(copy.deepcopy(DRAFT_EN), lift_context("en"))[0]


def test_without_provider_the_deterministic_pass_is_unchanged():
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"))
    expected, deterministic = run_script_story_quality_v1(copy.deepcopy(DRAFT_DE), lift_context("de"))
    assert final == expected and report["gate"] == deterministic["gate"]
    assert report["holistic"]["status"] == "not_requested"


# 16 ----------------------------------------------------------------------------

def test_english_script_is_rewritten_and_verified():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)], verdicts=[verdict(REWRITE_EN)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), lift_context("en"), editor)
    assert texts(final) == [text for _role, text, _ids in REWRITE_EN]
    assert report["mode"] == "holistic_ai" and report["gate"]["ready"] is True
    assert editor.calls[0][1]["language"] == "en"


def test_rewrite_in_the_wrong_language_is_a_hard_failure():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN), rewrite(REWRITE_DE)], verdicts=[verdict(REWRITE_DE)])
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_DE), lift_context("de"), editor)
    assert "language_mismatch" in {item["code"] for item in report["holistic"]["attempts"][0]["hard"]}
    assert texts(final)[0] == REWRITE_DE[0][1]


def test_rewrite_over_the_duration_budget_is_a_hard_failure():
    context = lift_context("en")
    context["word_budget"] = 40
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN), rewrite(REWRITE_EN)], verdicts=[verdict(REWRITE_EN)])
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), context, editor)
    assert "over_duration" in {item["code"] for item in report["holistic"]["attempts"][0]["hard"]}


# Pipeline integration -------------------------------------------------------------

class Writer:
    name = "fixture-writer"

    def __init__(self, blocks: list[ScriptBlockV2]):
        self.result = ScriptWriterResult(ScriptDraftV2(language="de", blocks=blocks), "connected")

    def generate(self, _request):
        return self.result


class BriefEditor:
    """Rewrites from whatever fact IDs the pipeline assigned (by claim)."""

    name = "brief-editor"

    def __init__(self):
        self.stages: list[str] = []

    def _ids(self, brief) -> dict[str, str]:
        return {item["claim"]: item["id"] for item in brief["research"]["facts"]}

    def beats(self, brief):
        ids = self._ids(brief)
        cite = {DE_FACTS[index]["id"]: ids[DE_FACTS[index]["claim"]] for index in range(len(DE_FACTS))}
        return [(role, text, [cite[item] for item in fact_ids]) for role, text, fact_ids in REWRITE_DE]

    def critique(self, brief):
        self.stages.append("critique")
        return critic(missed=[])

    def rewrite(self, brief):
        self.stages.append("rewrite")
        return rewrite(self.beats(brief))

    def verify(self, brief):
        self.stages.append("verify")
        return verdict(self.beats(brief))


def test_pipeline_adopts_the_verified_rewrite_and_its_hook(monkeypatch):
    research = [{key: value for key, value in item.items() if key != "id"} for item in DE_FACTS]
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult(copy.deepcopy(research), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )
    writer = Writer([
        ScriptBlockV2(role="answer", text=DRAFT_DE[1]["text"], fact_ids=["fact_06"]),
        ScriptBlockV2(role="explanation", text=DRAFT_DE[2]["text"], fact_ids=["fact_01"]),
        ScriptBlockV2(role="support", text=DRAFT_DE[3]["text"], fact_ids=["fact_02"]),
        ScriptBlockV2(role="payoff", text=DRAFT_DE[5]["text"], fact_ids=["fact_04", "fact_06"]),
    ])
    editor = BriefEditor()
    state = build_initial_state(
        DE_QUESTION, AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        script_writer_provider=writer, script_quality_provider=editor,
    )
    script = state["script"]
    quality = script["script_story_quality_v1"]
    assert quality["mode"] == "holistic_ai", quality["holistic"]
    hook = REWRITE_DE[0][1]
    assert script["blocks"][0]["text"] == hook == script["selected_hook"] == script["triple_hook"]["verbal_hook"]
    assert REWRITE_DE[-1][1] in script["text"]
    assert state["explanation_audit"]["source"] == "script_story_verifier"
    assert quality["final_signature"] == quality["signature"]
    # Later production stages keep the verified hook and the verified report.
    assert enforce_selected_hook(state) == "kept"
    _refresh_script_derivatives(state, old_scenes=state.get("scenes", []))
    assert state["script"]["script_story_quality_v1"]["mode"] == "holistic_ai"
    assert content_readiness(state)["ready"], content_readiness(state)


def test_pipeline_uses_the_openai_editor_only_when_configured(monkeypatch):
    built: list[Settings] = []

    class Recorder:
        name = "recorder"

        def __init__(self, settings):
            built.append(settings)

        def critique(self, _brief):
            raise ScriptStoryProviderError("offline")

    class OfflineWriter:
        name = "offline-writer"

        def __init__(self, _settings):
            pass

        def generate(self, _request):
            return ScriptWriterResult(None, "provider_error", "offline")

    monkeypatch.setattr(pipeline, "OpenAIScriptStoryProvider", Recorder)
    monkeypatch.setattr(pipeline, "OpenAIScriptWriterProvider", OfflineWriter)
    research = [{key: value for key, value in item.items() if key != "id"} for item in EN_FACTS]
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult(copy.deepcopy(research), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"),
    )

    def build(**settings):
        return build_initial_state(EN_QUESTION, AdvancedOptions(language="en"), Settings(clipforge_ai_mode="local", **settings))

    build(openai_api_key=None)
    build(openai_api_key="test-key", script_holistic_rewrite_enabled=False)
    assert built == []
    state = build(openai_api_key="test-key")
    assert built and state["script"]["script_story_quality_v1"]["holistic"]["status"] == "provider_unavailable"


def test_verified_answer_verdict_applies_only_to_the_verified_words():
    beats = [block(index, role, text, ids) for index, (role, text, ids) in enumerate(REWRITE_DE, 1)]
    audit = {
        "sentences": [],
        "answer_sufficiency": {"verdict": "answered", "one_sentence_answer": "Luft trägt das Flugzeug.", "missing": ""},
        "source": VERIFIED_AUDIT_SOURCE,
        "verified_script": verified_script_key([item["text"] for item in beats]),
    }

    def sufficiency(blocks: list[dict]) -> str:
        state = {**lift_context("de"), "explanation_audit": audit, "script": {"blocks": blocks}}
        return assess_information_gain(state)["answer_sufficiency"]["status"]

    assert sufficiency(beats) != "fail"
    edited = copy.deepcopy(beats)
    edited[3]["text"] = "Das nennt man Auftrieb."
    # A later edit is no longer covered by the verifier: the lexical gate governs again.
    assert sufficiency(edited) == "fail"
