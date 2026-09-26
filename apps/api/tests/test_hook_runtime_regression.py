"""Regression from a real run: "Welches Land hat mehr Inseln – Schweden oder Indonesien?".

The production output opened with a research sentence lifted out of context
("Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein."), and
the Script Writer's own answer statement followed it immediately, spending
the protected reveal.  The research, the writer body and the provider hooks
below mirror that run; every provider is a fake, the pipeline is the real one.
"""
from __future__ import annotations

import pytest
from test_triple_hook import Judge, ai_candidate, generation

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge.config import Settings
from clipforge.models import Project
from clipforge.narration import clean_narration_text
from clipforge.pipeline import build_initial_state
from clipforge.renderer import RenderResult, _create_voice
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state, create_project
from clipforge.triple_hook import state_context
from clipforge.verbal_hook import (
    assess_verbal,
    deterministic_candidates,
    hook_context,
    rank_verbal,
    select_verbal,
)

QUESTION = "Welches Land hat mehr Inseln – Schweden oder Indonesien?"
BATHING = "Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein."
SOURCE = {"label": "Quelle", "url": "https://source.test/inseln"}


def research_fact(claim: str, importance: float = 0.8) -> dict:
    return {"claim": claim, "importance": importance, "confidence": 0.9, "verification": "source_attributed", "sources": [SOURCE]}


RESEARCH = [
    research_fact("Schweden zählt etwa 267.570 Inseln – mehr als jedes andere Land der Welt.", 0.95),
    research_fact("Indonesien kommt auf etwa 17.000 Inseln und liegt damit nur auf Platz sechs."),
    # A travel-guide paragraph: the bathing sentence is true, but not this story.
    research_fact(f"Weniger als 1.000 der schwedischen Inseln sind dauerhaft bewohnt. {BATHING}", 0.6),
    research_fact("Indonesien ist trotzdem der größte Inselstaat der Welt, weil sein gesamtes Staatsgebiet aus Inseln besteht.", 0.7),
    research_fact("Zu den vielen schwedischen Inseln tragen die stark zerklüfteten Landschaften im Norden bei.", 0.6),
]
# The writer's body as produced: a synthesized answer statement first, citing no fact.
WRITER_BODY = [
    ScriptBlockV2(role="answer", text="Schweden hat mehr Inseln als Indonesien."),
    ScriptBlockV2(role="support", text="Schweden zählt etwa 267.570 Inseln. Weniger als 1.000 davon sind dauerhaft bewohnt.", fact_ids=["fact_01", "fact_03"]),
    ScriptBlockV2(role="explanation", text="Indonesien ist trotzdem der größte Inselstaat der Welt, weil sein gesamtes Staatsgebiet aus Inseln besteht.", fact_ids=["fact_04"]),
    ScriptBlockV2(role="explanation", text="Zu den vielen schwedischen Inseln tragen die stark zerklüfteten Landschaften im Norden bei.", fact_ids=["fact_05"]),
    ScriptBlockV2(role="payoff", text="Indonesien kommt auf etwa 17.000 Inseln und liegt damit nur auf Platz sechs.", fact_ids=["fact_02"]),
]


class Writer:
    name = "fixture-v2"

    def __init__(self, _settings=None):
        pass

    def generate(self, _request):
        return ScriptWriterResult(ScriptDraftV2(language="de", blocks=WRITER_BODY), "connected")


class OfflineReviewer:
    name = "fixture-review"

    def __init__(self, _settings=None):
        pass

    def review(self, _request):
        raise RuntimeError("offline")


def provider_candidates() -> list[dict]:
    """The provider copied the research sentence; the judge liked its picture best."""
    return [
        ai_candidate("A", "counterintuitive_insight", BATHING, "empty rocky islet in a cold sea", ["rocky islet baltic sea"],
                     action="waves hit bare granite", detail="no beach at all", payoff_fact="fact_01"),
        ai_candidate("B", "curiosity_gap", "Tausende Inseln – aber welches Land hat wirklich die meisten?", "island chain from above",
                     ["island archipelago aerial"], payoff_fact="fact_02"),
        ai_candidate("C", "verified_statistic", "Rund 270.000 gegen etwa 17.000 Inseln – welche Zahl gehört zu welchem Land?",
                     "map with many islands", ["archipelago aerial map"], payoff_fact="fact_01"),
    ]


def production(monkeypatch, candidates: list[dict], judge: Judge | None = None) -> None:
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_a, **_k: ResearchResult([dict(item) for item in RESEARCH], [SOURCE], "verified_sources", "fixture"),
    )
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: type("Plan", (), {"plan": None, "status": "provider_error", "error": None})())
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    monkeypatch.setattr("clipforge.pipeline.generate_hook_candidates_with_openai", lambda *_a, **_k: generation(candidates))
    monkeypatch.setattr("clipforge.pipeline.judge_triple_hooks_with_openai", judge or Judge())


def settings(tmp_path) -> Settings:
    return Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)


def opening(state: dict) -> str:
    return state["script"]["blocks"][0]["text"]


def assert_one_hook_everywhere(state: dict, hook: str) -> None:
    blocks = state["script"]["blocks"]
    assert [block["role"] for block in blocks].count("hook") == 1 and blocks[0]["role"] == "hook"
    assert blocks[0]["text"] == hook == state["script"]["selected_hook"] == state["script"]["triple_hook"]["verbal_hook"]
    assert state["script"]["text"].startswith(hook)
    assert " ".join(item["text"] for item in state["captions"]["items"]).startswith(hook)


def assert_reveal_not_immediate(state: dict) -> None:
    arc = state["story_arc"]
    assert arc["curiosity_gap"]["withhold_answer"]
    after_hook = state["script"]["blocks"][1]
    assert after_hook["role"] != "answer"
    assert arc["primary_answer_id"] not in (after_hook.get("fact_ids") or [])
    assert after_hook["text"] != "Schweden hat mehr Inseln als Indonesien."


@pytest.mark.parametrize(
    ("judge", "candidates"),
    [
        # The real run: the provider's copy of the research sentence, rated best by the judge.
        (Judge({"hook_a": {"curiosity": 9, "attention_value": 9, "visual_intrigue": 9}}), provider_candidates()),
        # No provider hooks: the research sentence as a deterministic candidate.
        (Judge(), []),
    ],
    ids=["provider_copy", "deterministic_research_clause"],
)
def test_off_axis_research_sentence_never_opens_and_the_answer_waits(monkeypatch, tmp_path, judge, candidates):
    production(monkeypatch, candidates, judge)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings(tmp_path))
    plan = state["script"]["triple_hook"]
    hook = plan["verbal_hook"]

    assert state["script"]["script_writer_v2"]["status"] == "v2_success"
    assert hook != BATHING and "baden" not in hook.casefold()
    assert plan["selected_strategy"] in {"curiosity_gap", "counterintuitive_insight", "direct_reframe", "evidence_insight",
                                         "common_mistake", "verified_statistic", "social_proof_or_trend",
                                         "high_stakes_consequence", "ego_challenge"}
    bathing = [item for item in plan["selection"]["candidates"] if item["verbal_hook"] == BATHING]
    for item in bathing:
        assert not item["eligible"] and {"context_dependent_opener", "off_story_axis"} <= set(item["hard_fail"])
    # The research authority itself lifts the sentence out of the hook-safe
    # travel paragraph; it is rejected there too, even when the comparison
    # hook is not available.
    context = state_context(state)
    lifted = [item for item in rank_verbal(context, deterministic_candidates(context)) if item["text"] == BATHING]
    assert lifted and not lifted[0]["eligible"]
    assert select_verbal(context, exclude={hook})["text"] != BATHING
    assert_one_hook_everywhere(state, hook)
    assert_reveal_not_immediate(state)

    # Review, hook enforcement and duration refit (the render stage) keep it.
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_a, **_k: None)
    rendered_text: dict[str, str] = {}
    monkeypatch.setattr(
        "clipforge.services.render_video",
        lambda render_state, *_a, **_k: rendered_text.update(text=render_state["script"]["text"]) or RenderResult("/media/t.mp4", 12.0, "openai", 100),
    )
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    rendered = _render_state(state, "project-islands", 2, settings(tmp_path))
    assert_one_hook_everywhere(rendered, hook)
    assert_reveal_not_immediate(rendered)
    assert rendered_text["text"].startswith(hook)

    # TTS receives the same opening as narration and captions.
    captured: dict[str, object] = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"w" * 5000})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", lambda **_k: type("Client", (), {"audio": type("Audio", (), {"speech": Speech()})()})())
    rendered["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(rendered, tmp_path, settings(tmp_path))
    assert str(captured["input"]) == clean_narration_text(rendered["script"]["text"])
    assert str(captured["input"]).startswith(hook)


def test_persisted_project_keeps_the_authoritative_hook(monkeypatch, tmp_path, db):
    production(monkeypatch, provider_candidates(), Judge({"hook_a": {"curiosity": 9, "attention_value": 9, "visual_intrigue": 9}}))
    project = create_project(db, ProjectCreate(prompt=QUESTION), settings(tmp_path))
    project_id = project.id
    db.expire_all()
    state = db.get(Project, project_id).revisions[0].state
    hook = state["script"]["triple_hook"]["verbal_hook"]
    assert hook != BATHING
    assert_one_hook_everywhere(state, hook)
    assert_reveal_not_immediate(state)


def test_context_dependent_or_off_axis_statements_fail_but_the_research_itself_is_untouched():
    state_facts = [{**item, "id": f"fact_{index:02d}"} for index, item in enumerate(RESEARCH, 1)]
    intent = {"question": QUESTION, "topic": QUESTION, "language": "de", "content_type": "factual_explainer", "tone": "documentary"}
    body = [{"role": block.role, "text": block.text, "fact_ids": block.fact_ids} for block in WRITER_BODY]
    context = hook_context(intent, state_facts, story_arc=None, payoff_plan=None, format_plan=None, novelty_plan=None, body_blocks=body)
    # "Aber ..." answers a sentence the viewer never heard.
    assert "context_dependent_opener" in assess_verbal(BATHING, "counterintuitive_insight", context)["hard_fail"]
    # Without the connective it is still about something the story never tells.
    assert "off_story_axis" in assess_verbal("Nicht jede Insel lädt auch wirklich zum Baden ein.", "counterintuitive_insight", context)["hard_fail"]
    # A statement the story does tell, and a question that only opens the gap, stay eligible.
    assert not assess_verbal("Indonesien besteht komplett aus Inseln – und hat trotzdem nicht die meisten.", "counterintuitive_insight", context)["hard_fail"]
    assert "off_story_axis" not in assess_verbal("Tausende Inseln – aber welches Land hat wirklich die meisten?", "curiosity_gap", context)["hard_fail"]
