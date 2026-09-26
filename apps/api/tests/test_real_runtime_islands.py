"""Regression from the real runtime trace of "Welches Land hat mehr Inseln – Schweden oder Indonesien?".

The local project showed: truncated provider JSON (no AI hook candidates),
format "ranking" from research vocabulary, a Story Arc protecting a fact that
does not state the answer, a clause fragment as the spoken hook ("ist es der
größte Inselstaat der Welt.") and scraped page chrome inside the research.
The research below is the persisted research of that run, verbatim; every
provider is a fake, the pipeline and the provider call handling are real.
"""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

import clipforge.services  # noqa: F401 - registers ORM models
from clipforge.ai import (
    AIHookGenerationResponse,
    AITripleHookCandidate,
    generate_hook_candidates_with_openai,
)
from clipforge.config import Settings
from clipforge.format_intelligence import plan_format
from clipforge.hooks import CANONICAL_STRATEGIES, _grounded_insight, standalone_issue
from clipforge.models import Project
from clipforge.narration import clean_narration_text, clean_research_claim
from clipforge.pipeline import build_initial_state
from clipforge.renderer import RenderResult, _create_voice
from clipforge.research import ResearchResult
from clipforge.schemas import AdvancedOptions, ProjectCreate
from clipforge.script_writer import ScriptBlockV2, ScriptDraftV2, ScriptWriterResult
from clipforge.services import _render_state, create_project
from clipforge.story_arc import build_story_arc, comparison_verdicts
from clipforge.triple_hook import state_context
from clipforge.verbal_hook import rank_verbal, select_verbal

QUESTION = "Welches Land hat mehr Inseln – Schweden oder Indonesien?"
FRAGMENT = "ist es der größte Inselstaat der Welt."
SOURCES = [{"label": f"Quelle {index}", "url": f"https://source{index}.test/inseln"} for index in range(1, 6)]
RAW_RESEARCH = [
    (
        "Schockierenderweise schaffen es Indonesien mit etwa 17.000 Inseln und die Philippinen mit rund 7.600 nicht einmal "
        "in die Top fünf. Beitrag archiviert. Es können weder neue Kommentare hinzugefügt noch Up- oder Downvotes vergeben werden."
    ),
    (
        "Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein. Die nördlichsten Länder Norwegen, Schweden, Kanada "
        "und Finnland bestechen mit einer einzigartig zerklüfteten Landschaft und haben weltweit die meisten Inseln - mit Abstand."
    ),
    (
        "Obwohl Indonesien nur auf Platz sechs liegt, ist es der größte Inselstaat der Welt, weil sein gesamtes Staatsgebiet "
        "ausschließlich Inseln umfasst. Im Gegensatz dazu hat Schweden zwar mehr Inseln, besteht aber überwiegend aus Festland."
    ),
    (
        "Schweden ist das Land mit den meisten Inseln weltweit – stolze 267.570 zählt man hier. Trotz dieser imposanten Zahl "
        "sind weniger als 1000 Inseln dauerhaft bewohnt. Dank des Jedermannsrechts können Besucherinnen und Besucher dennoch rund."
    ),
    (
        "Das wird deutlich, wenn man das. in dem Top-5-Ranking der Länder mit den meisten Inseln vor: Schweden führt die Liste "
        "der Länder mit den meisten Inseln an Foto: Getty Images."
    ),
]
PAGE_CHROME = ("Beitrag archiviert", "Kommentare", "Downvotes", "Foto:", "Getty", "dennoch rund", "wenn man das")
# The Script Writer's body of that run: the answer statement said before the reveal.
WRITER_BODY = [
    ScriptBlockV2(role="support", text="In Schweden werden 267.570 Inseln gezählt. Weniger als 1000 davon sind dauerhaft bewohnt.", fact_ids=["fact_04"]),
    ScriptBlockV2(role="explanation", text="Indonesien ist trotzdem der größte Inselstaat der Welt, weil sein gesamtes Staatsgebiet aus Inseln besteht.", fact_ids=["fact_03"]),
    ScriptBlockV2(role="answer", text="Schweden hat mehr Inseln als Indonesien.", fact_ids=["fact_04"]),
    ScriptBlockV2(role="payoff", text="Indonesien kommt auf etwa 17.000 Inseln und schafft es nicht einmal in die Top fünf.", fact_ids=["fact_01"]),
]
# The provider's answer, cut off mid-string as in the real run.
TRUNCATED = (
    '{"strategy_plan":[{"strategy":"verified_statistic","fact_ids":["fact_04"],"reason_codes":["strong_sourced_number"]}],'
    '"triple_hook_candidates":[{"id":"A","strategy":"verified_statistic","verbal_hook":"Rund 270.000 gegen etwa 17.000 Inseln'
)


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


class Provider:
    """The OpenAI Responses API as seen by ``responses.parse``: the SDK validates the returned text."""

    def __init__(self, texts: list[str]):
        self.texts = texts
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        text = self.texts[min(len(self.calls), len(self.texts)) - 1]
        return SimpleNamespace(output_parsed=kwargs["text_format"].model_validate_json(text))


def settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(clipforge_ai_mode="openai", openai_api_key="test-key", render_root=tmp_path)


def production(monkeypatch, provider: Provider) -> None:
    research = [
        {"claim": claim, "importance": 0.8, "confidence": 0.78, "verification": "source_snippet", "sources": [source]}
        for claim, source in zip(RAW_RESEARCH, SOURCES)
    ]
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult(research, SOURCES, "verified_sources", "brave"))
    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="provider_error", error=None))
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptWriterProvider", Writer)
    monkeypatch.setattr("clipforge.pipeline.OpenAIScriptReviewProvider", OfflineReviewer)
    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_k: SimpleNamespace(responses=provider))


def spoken_before_reveal(state: dict) -> list[dict]:
    """Script blocks before the first one that carries the primary answer."""
    primary = state["story_arc"]["primary_answer_id"]
    blocks = state["script"]["blocks"]
    reveal = next(index for index, block in enumerate(blocks) if primary in (block.get("fact_ids") or []))
    return blocks[:reveal]


def assert_hook_everywhere(state: dict, hook: str) -> None:
    blocks = state["script"]["blocks"]
    assert [block["role"] for block in blocks].count("hook") == 1 and blocks[0]["role"] == "hook"
    assert blocks[0]["text"] == hook == state["script"]["selected_hook"] == state["script"]["triple_hook"]["verbal_hook"]
    assert state["script"]["text"].startswith(hook)
    assert " ".join(item["text"] for item in state["captions"]["items"]).startswith(hook)


# ---------------------------------------------------------------------------
# End to end: the real research, a truncated provider answer, the fallback
# ---------------------------------------------------------------------------

def test_real_runtime_trace_end_to_end(monkeypatch, tmp_path):
    provider = Provider([TRUNCATED])
    production(monkeypatch, provider)
    state = build_initial_state(QUESTION, AdvancedOptions(), settings(tmp_path))
    arc, plan = state["story_arc"], state["script"]["triple_hook"]
    facts = {fact["id"]: fact for fact in state["facts"]}

    # A. The user asked an A-or-B question: comparison, whatever the research says.
    assert state["format_plan"]["selected_format"] == "comparison"
    assert state["format_plan"]["selection_reason"] == "question_names_two_alternatives"
    # B/C. The primary answer is a fact that states the winner.
    assert arc["answer_subject"] == ["schweden"]
    primary = facts[arc["primary_answer_id"]]["claim"]
    assert "Schweden" in primary and "meisten Inseln" in primary
    winner, verdicts = comparison_verdicts(state["facts"], QUESTION)
    assert winner == 0 and arc["primary_answer_id"] in verdicts
    # D. The protected reveal is the winner: every fact stating it is protected.
    assert set(verdicts) <= set(arc["hook"]["protected_ids"])
    assert state["payoff_plan"]["hook_must_not_reveal"].rstrip(".") == "Schweden"
    # E. Page chrome and cut sentences never reach the facts that feed story and hook.
    for fact in state["facts"]:
        assert not any(marker in fact["claim"] for marker in PAGE_CHROME), fact["claim"]
        assert fact["sources"] and fact["sources"][0]["url"].startswith("https://source")
    assert facts["fact_05"]["claim"] == "Schweden führt die Liste der Länder mit den meisten Inseln an."
    assert facts["fact_05"]["sources"] == [SOURCES[4]]
    assert "Beitrag archiviert" in facts["fact_01"]["raw_claim"]

    # The provider answer was cut off: exactly one bounded retry, recorded.
    assert len(provider.calls) == 2
    assert provider.calls[1]["max_output_tokens"] > provider.calls[0]["max_output_tokens"]
    generation = state["script"]["hook_generation"]
    assert generation["status"] == "provider_error" and generation["attempts"] == 2
    assert generation["retry_reason"] == "truncated_structured_output" and "json_invalid" in generation["first_error"]
    assert plan["selection"]["generation"]["attempts"] == 2

    # F/G/H/I. A complete, standalone, documented hook - never the fragment.
    hook = plan["verbal_hook"]
    assert hook != FRAGMENT and not hook.casefold().startswith("ist es")
    assert standalone_issue(hook) is None
    assert plan["selected_strategy"] in CANONICAL_STRATEGIES
    assert "schweden" not in hook.casefold().split("welche")[0]  # the winner is only an open option, if named at all
    # J. The winner is withheld until the reveal.
    assert arc["curiosity_gap"]["withhold_answer"] and plan["reveal_contract"]["withhold_answer"] is True
    for block in spoken_before_reveal(state)[1:]:
        assert "schweden" not in block["text"].casefold(), block
    assert state["script"]["blocks"][1]["role"] != "answer"

    # K. The same hook through review/enforcement/refit, narration, TTS and captions.
    assert_hook_everywhere(state, hook)
    monkeypatch.setattr("clipforge.services.prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services._final_quality_review", lambda *_a, **_k: None)
    monkeypatch.setattr("clipforge.services.render_video", lambda *_a, **_k: RenderResult("/media/t.mp4", 12.0, "openai", 100))
    rendered = _render_state(state, "project-islands", 2, settings(tmp_path))
    assert_hook_everywhere(rendered, hook)
    captured: dict[str, object] = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=b"w" * 5000)

    monkeypatch.setattr("clipforge.renderer.OpenAI", lambda **_k: SimpleNamespace(audio=SimpleNamespace(speech=Speech())))
    rendered["voice"].update(provider="openai", voice_id="marin", model="gpt-4o-mini-tts")
    _create_voice(rendered, tmp_path, settings(tmp_path))
    assert str(captured["input"]) == clean_narration_text(rendered["script"]["text"])
    assert str(captured["input"]).startswith(hook)


def test_real_runtime_project_persists_the_same_opening(monkeypatch, tmp_path, db):
    production(monkeypatch, Provider([TRUNCATED]))
    project = create_project(db, ProjectCreate(prompt=QUESTION), settings(tmp_path))
    project_id = project.id
    db.expire_all()
    state = db.get(Project, project_id).revisions[0].state
    hook = state["script"]["triple_hook"]["verbal_hook"]
    assert state["format_plan"]["selected_format"] == "comparison"
    assert state["story_arc"]["answer_subject"] == ["schweden"]
    assert standalone_issue(hook) is None and hook != FRAGMENT
    assert_hook_everywhere(state, hook)


# ---------------------------------------------------------------------------
# 1. Provider: one bounded retry, only for a cut-off structured answer
# ---------------------------------------------------------------------------

def _complete_answer() -> str:
    candidate = AITripleHookCandidate(
        id="A", strategy="verified_statistic", verbal_hook="Rund 270.000 gegen etwa 17.000 Inseln – welche Zahl gehört zu welchem Land?",
        visual={"subject": "rocky islands in a cold sea", "media_queries": ["archipelago aerial"]},
    )
    return AIHookGenerationResponse(triple_hook_candidates=[candidate]).model_dump_json()


def _generate(monkeypatch, texts: list[str]):
    provider = Provider(texts)
    monkeypatch.setattr("clipforge.ai.OpenAI", lambda **_k: SimpleNamespace(responses=provider))
    intent = {"question": QUESTION, "topic": QUESTION, "language": "de"}
    result = generate_hook_candidates_with_openai(QUESTION, intent, [{"id": "fact_01", "claim": "Indonesien hat etwa 17.000 Inseln."}], "Body.", Settings(openai_api_key="k"))
    return result, provider


def test_truncated_answer_is_retried_once_and_recovers(monkeypatch):
    result, provider = _generate(monkeypatch, [TRUNCATED, _complete_answer()])
    assert result.status == "connected" and len(result.triple_candidates) == 1
    assert result.attempts == 2 and result.retry_reason == "truncated_structured_output"
    assert len(provider.calls) == 2 and "retry" in json.loads(provider.calls[1]["input"])
    # Same documented strategy system on the retry.
    assert provider.calls[1]["instructions"] == provider.calls[0]["instructions"]
    assert json.loads(provider.calls[1]["input"])["document_strategies"] == json.loads(provider.calls[0]["input"])["document_strategies"]


def test_retry_is_bounded_and_other_failures_are_not_retried(monkeypatch):
    result, provider = _generate(monkeypatch, [TRUNCATED, TRUNCATED, TRUNCATED])
    assert result.status == "provider_error" and result.attempts == 2 and len(provider.calls) == 2
    # Complete JSON that breaks the schema is not a truncation: no retry.
    result, provider = _generate(monkeypatch, ['{"triple_hook_candidates": [{"id": "A"}]}'])
    assert result.status == "provider_error" and result.attempts == 1 and len(provider.calls) == 1
    # The first answer, when complete, is the only call.
    result, provider = _generate(monkeypatch, [_complete_answer()])
    assert result.status == "connected" and result.attempts == 1 and len(provider.calls) == 1


def test_reasoning_budget_is_bounded_for_structured_hook_writing(monkeypatch):
    _result, provider = _generate(monkeypatch, [_complete_answer()])
    call = provider.calls[0]
    assert call["max_output_tokens"] <= 3200 and call["reasoning"] == {"effort": "low"}
    assert "rationale" not in AITripleHookCandidate.model_fields


# ---------------------------------------------------------------------------
# 2. Fallback sentences are complete and standalone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    FRAGMENT, "hat es die meisten Inseln.", "Deshalb hat Schweden so viele Inseln.", "Aber nicht jede Insel ist bewohnt.",
    "Jedoch liegt Indonesien nur auf Platz sechs.", "because its whole territory is islands.", "But not every island is inhabited.",
    "However, Indonesia ranks sixth.", "Es ist der größte Inselstaat der Welt.", "Indonesien ist trotzdem der größte Inselstaat der Welt.",
])
def test_incomplete_or_context_dependent_sentences_are_not_standalone(text):
    assert standalone_issue(text) is not None


@pytest.mark.parametrize("text", [
    "Indonesien besteht komplett aus Inseln – und hat trotzdem nicht die meisten.",
    "Obwohl Indonesien nur auf Platz sechs liegt, ist es der größte Inselstaat der Welt.",
    "Es gibt auf der Welt Länder mit über 200.000 Inseln.", "Diese Falten sind kein Wasserschaden.",
    "Welches Land hat mehr Inseln – Schweden oder Indonesien?", "Why does blue light win in the sky?",
])
def test_complete_standalone_sentences_pass(text):
    assert standalone_issue(text) is None


def test_research_extraction_never_returns_a_clause_fragment():
    claim = clean_research_claim(RAW_RESEARCH[2])
    assert _grounded_insight(claim, {"question": QUESTION}) != FRAGMENT
    assert not _grounded_insight(claim, {"question": QUESTION})[:1].islower()


def test_the_real_fragment_is_rejected_by_the_verbal_authority(monkeypatch, tmp_path):
    production(monkeypatch, Provider([TRUNCATED]))
    state = build_initial_state(QUESTION, AdvancedOptions(), settings(tmp_path))
    context = state_context(state)
    assessed = rank_verbal(context, [{"strategy": "evidence_insight", "text": FRAGMENT, "origin": "ai"}])[0]
    assert not assessed["eligible"] and "clause_fragment" in assessed["hard_fail"]
    # Even with every researched candidate excluded, a complete hook is still produced.
    chosen = select_verbal(context, exclude={state["script"]["selected_hook"]})
    assert chosen is not None and standalone_issue(chosen["text"]) is None and chosen["strategy"] in CANONICAL_STRATEGIES


# ---------------------------------------------------------------------------
# 3/4. Story Arc answer authority and Format Intelligence (German and English)
# ---------------------------------------------------------------------------

EN_QUESTION = "Which country has more islands – Sweden or Indonesia?"
EN_FACTS = [
    "Surprisingly, Indonesia with about 17,000 islands does not even make the top five. This post is archived. New comments cannot be posted and votes cannot be cast.",
    "Although Indonesia ranks only sixth, it is the largest island nation in the world. Sweden, by contrast, has more islands but consists mostly of mainland.",
    "Sweden is the country with the most islands in the world – 267,570 are counted there. Photo: Getty Images",
    "The top-5 ranking of countries with the most islands is led by Sweden.",
]


def _facts(claims: list[str]) -> list[dict]:
    return [
        {"id": f"fact_{index:02d}", "claim": clean_research_claim(claim), "importance": 0.8, "verification": "source_attributed",
         "sources": [{"label": "s", "url": f"https://s{index}.test"}]}
        for index, claim in enumerate(claims, 1)
    ]


@pytest.mark.parametrize(("question", "claims", "winner_word"), [
    (QUESTION, RAW_RESEARCH, "Schweden"),
    (EN_QUESTION, EN_FACTS, "Sweden"),
])
def test_primary_answer_states_the_researched_winner(question, claims, winner_word):
    facts = [fact for fact in _facts(claims) if fact["claim"]]
    intent = {"question": question, "topic": question, "language": "de" if winner_word == "Schweden" else "en"}
    format_plan = plan_format(intent, facts)
    assert format_plan["selected_format"] == "comparison"
    arc = build_story_arc(intent, facts, format_plan, None, protected=True)
    primary = next(unit["claim"] for unit in arc["units"] if unit["id"] == arc["primary_answer_id"])
    assert primary.startswith(winner_word)  # the fact whose leading statement names the winner
    assert arc["answer_subject"] == [winner_word.casefold()]
    assert arc["primary_answer_id"] in arc["hook"]["protected_ids"]


@pytest.mark.parametrize(("question", "research"), [
    (QUESTION, "Indonesien schafft es nicht einmal in die Top fünf; das Top-5-Ranking der Länder mit den meisten Inseln."),
    (EN_QUESTION, "Indonesia is not even in the top 5; the ranking of the largest island counts."),
    ("Ist der Nil oder der Amazonas länger?", "Platz 1 im Ranking der längsten Flüsse."),
    ("Nile vs Amazon: which river is longer?", "The top 10 ranking of the longest rivers."),
])
def test_incidental_research_ranking_never_overrides_a_two_option_question(question, research):
    plan = plan_format({"question": question, "topic": question}, [{"id": "fact_01", "claim": research}])
    assert plan["selected_format"] == "comparison" and plan["research_mentions_ranking"]
    assert plan["selection_reason"] in {"question_names_two_alternatives", "question_compares"}


@pytest.mark.parametrize("question", ["Top 5 der längsten Flüsse der Welt", "Top 5 fastest animals"])
def test_a_requested_ranking_is_still_a_ranking(question):
    assert plan_format({"question": question, "topic": question}, [])["selected_format"] == "ranking"


# ---------------------------------------------------------------------------
# 5. Research hygiene at the research boundary
# ---------------------------------------------------------------------------

def test_research_boundary_strips_chrome_and_cut_sentences_but_keeps_facts():
    cleaned = [clean_research_claim(claim) for claim in RAW_RESEARCH]
    assert cleaned[0] == "Schockierenderweise schaffen es Indonesien mit etwa 17.000 Inseln und die Philippinen mit rund 7.600 nicht einmal in die Top fünf."
    assert cleaned[3] == "Schweden ist das Land mit den meisten Inseln weltweit – stolze 267.570 zählt man hier. Trotz dieser imposanten Zahl sind weniger als 1000 Inseln dauerhaft bewohnt."
    assert cleaned[4] == "Schweden führt die Liste der Länder mit den meisten Inseln an."
    # Unusual but valid sentences (verb particles, a source-page opener) stay.
    assert cleaned[1].startswith("Aber bei weitem nicht jede Insel lädt auch wirklich zum Baden ein.")
    assert clean_research_claim("Das Loch gleicht den Druck aus … weiterlesen") == ""
    assert clean_research_claim("Plants make sugar. Photosynthesis needs light … read more") == "Plants make sugar."
    assert clean_research_claim("Viele wissen das nicht.") == "Viele wissen das nicht."


def test_research_snippets_with_a_source_support_numbers_like_every_other_layer():
    from clipforge.hooks import hook_issues

    snippet = [{"claim": "Indonesien hat etwa 17.000 Inseln.", "verification": "source_snippet", "sources": [SOURCES[0]]}]
    assert "unsupported_statistic" not in hook_issues("Etwa 17.000 Inseln – reicht das?", snippet, research_scoped=True)
    unsourced = [{**snippet[0], "sources": []}]
    assert "unsupported_statistic" in hook_issues("Etwa 17.000 Inseln – reicht das?", unsourced, research_scoped=True)


def test_the_plans_emergency_hook_survives_later_enforcement(monkeypatch, tmp_path):
    from clipforge.pipeline import _apply_selected_hook, enforce_selected_hook

    production(monkeypatch, Provider([TRUNCATED]))
    state = build_initial_state(QUESTION, AdvancedOptions(), settings(tmp_path))
    plan = state["script"]["triple_hook"]
    # The last resort of the chain: the user's real question.
    plan.update(verbal_hook=QUESTION, verbal_origin="emergency", selected_strategy="curiosity_gap")
    state["script"]["blocks"] = _apply_selected_hook(state["script"]["blocks"], QUESTION)
    state["script"]["selected_hook"] = QUESTION
    assert enforce_selected_hook(state) == "kept"
    assert state["script"]["blocks"][0]["text"] == QUESTION == plan["verbal_hook"]
