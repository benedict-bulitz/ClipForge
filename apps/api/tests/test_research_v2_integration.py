"""Research Pipeline V2 inside generation: package, traceability, readiness and the bounded retry."""
from __future__ import annotations

import functools
from types import SimpleNamespace

from clipforge import research as research_module
from clipforge import research_v2
from clipforge.ai import AIFact
from clipforge.pipeline import build_initial_state, research_retry_focus, research_retry_query
from clipforge.readiness import content_readiness
from clipforge.research import ResearchResult, research_topic
from clipforge.schemas import AdvancedOptions
from research_v2_support import research_settings
from test_research_pipeline_v2 import MICRO, _micro_web

KÜCHE = "https://www.uni-delta.de/kueche"


def _wire(monkeypatch, web) -> list[dict]:
    calls: list[dict] = []
    real = functools.partial(research_v2.run_research, transport=web.transport(), llm=None, cache_root=None)

    def recorded(query, language, settings, *, context=None):
        calls.append({"query": query, "context": dict(context or {})})
        return real(query, language, settings, context=context)

    monkeypatch.setattr(research_v2, "run_research", recorded)
    return calls


def _settings(tmp_path, **values):
    return research_settings(brave_search_api_key="test-key", render_root=tmp_path, **values)


def test_generation_carries_the_research_package_and_traceable_facts(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, _micro_web())
    state = build_initial_state(MICRO, AdvancedOptions(), _settings(tmp_path))
    research = state["research"]
    assert research["provider"] == "research_v2" and research["package"]["status"] == "sufficient"
    assert calls[0]["context"]["question"] == MICRO
    facts = {fact["id"]: fact for fact in state["facts"]}
    core = research["package"]["core_answer"]
    # Package refs carry the final fact IDs after the pipeline re-numbered facts.
    assert core["fact_id"] in facts and facts[core["fact_id"]]["claim"].startswith("Mikrowellen dringen")
    assert all(ref["fact_id"] in facts for ref in research["package"]["explanation_spine"]["why_it_happens"])
    for fact in state["facts"]:
        assert fact["sources"] and fact["evidence_ids"] and fact["verification"] in {"supported", "source_attributed"}
    assert research["diagnostics"]["sufficiency"]["status"] == "sufficient"
    assert state["render"]["status"] == "ready_to_render"


def test_insufficient_research_blocks_readiness_and_broadens_once(monkeypatch, tmp_path):
    web = _micro_web()
    web.brave = {"Mikrowelle": [{"url": KÜCHE, "title": "Küche", "description": "Mikrowelle"}], "Essen": []}
    web.page(KÜCHE, "<html><body><main><p>Viele Küchengeräte wurden im zwanzigsten Jahrhundert für den Haushalt entwickelt.</p></main></body></html>")
    calls = _wire(monkeypatch, web)
    state = build_initial_state(MICRO, AdvancedOptions(), _settings(tmp_path))
    assert state["facts"] == [] and state["render"]["status"] == "blocked_by_research"
    readiness = state["script"]["readiness"]
    assert not readiness["ready"] and readiness["research_required"]
    assert any(item["code"] == "research_insufficient" for item in readiness["blocking"])
    # Exactly one bounded retry, aimed at the relation a why-question lacks: the cause.
    assert len(calls) == 2 and calls[1]["context"]["focus"] == "mechanism"
    assert len(state["research"]["attempts"]) == 2 and state["script"]["readiness"].get("retry_exhausted")
    assert all(block["role"] == "status" for block in state["script"]["blocks"])


def test_retry_focus_follows_the_package():
    insufficient = {"intent": {"question": MICRO, "language": "de"}, "research": {"package": {"status": "insufficient"}}}
    assert research_retry_focus(insufficient) == "broaden"
    assert research_retry_query(insufficient) == MICRO
    assert research_retry_focus({"research": {"package": {"status": "missing_mechanism"}}}) == "mechanism"
    why = {"status": "insufficient", "answer_grounding": {"question_type": "why"}}
    can = {"status": "insufficient", "answer_grounding": {"question_type": "can"}}
    assert research_retry_focus({"research": {"package": why}}) == "mechanism"
    assert research_retry_focus({"research": {"package": can}}) == "capability"
    assert research_retry_focus({"research": {}}) == "mechanism"


def test_readiness_blocks_an_insufficient_package():
    state = {"research": {"required": True, "package": {"status": "insufficient"}}, "script": {"blocks": []}}
    readiness = content_readiness(state)
    assert not readiness["ready"] and readiness["status"] == "research_required"


def test_planner_named_source_is_not_research(monkeypatch, tmp_path):
    """A URL the planner names is no evidence unless research retrieved it."""
    monkeypatch.setattr("clipforge.pipeline.research_topic", lambda *_a, **_k: ResearchResult([], [], "unavailable", "research_v2"))
    plan = SimpleNamespace(
        intent=None, facts=[AIFact(claim="Die Mitte bleibt kalt, weil Mikrowellen nur außen wirken.", confidence=0.9,
                                   importance=0.9, source_label="Erfundene Quelle", source_url="https://invented.example/x")],
    )

    def planner(*_a, **_k):
        return SimpleNamespace(plan=plan, status="connected", error=None)

    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", planner)
    monkeypatch.setattr("clipforge.pipeline.ai_plan_to_dict", lambda value: {
        "intent": {"language": "de"}, "facts": [fact.model_dump() for fact in value.facts], "script_blocks": [],
    })
    state = build_initial_state(MICRO, AdvancedOptions(), _settings(tmp_path))
    fact = state["facts"][0]
    assert fact["verification"] == "unverified_model_synthesis" and fact["sources"] == []
    assert fact["model_named_source"]["url"] == "https://invented.example/x"
    assert state["render"]["status"] == "blocked_by_research"


def test_planner_receives_the_research_brief(monkeypatch, tmp_path):
    _wire(monkeypatch, _micro_web())
    seen: dict = {}

    def planner(*_a, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(plan=None, status="provider_error", error=None)

    monkeypatch.setattr("clipforge.pipeline.plan_with_openai", planner)
    build_initial_state(MICRO, AdvancedOptions(), _settings(tmp_path))
    brief = seen["research_brief"]
    assert brief["direct_answer_index"] == 1 and brief["mechanism_indexes"]
    evidence = seen["evidence"]
    assert evidence[brief["direct_answer_index"] - 1].startswith("Mikrowellen dringen")


def test_v2_failure_falls_back_to_v1(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(research_v2, "run_research", broken)
    monkeypatch.setattr(research_module, "_research_topic_v1",
                        lambda *_a: ResearchResult([{"claim": "x"}], [{"label": "w", "url": "https://w.test"}], "verified_sources", "wikipedia"))
    result = research_topic(MICRO, "de", research_settings())
    assert result.provider == "wikipedia" and result.diagnostics["v2_error"].startswith("RuntimeError")


def test_v1_remains_selectable(monkeypatch):
    monkeypatch.setattr(research_module, "_research_topic_v1",
                        lambda *_a: ResearchResult([], [], "unavailable", "wikipedia", "offline"))
    monkeypatch.setattr(research_v2, "run_research", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("V2 must not run")))
    assert research_topic(MICRO, "de", research_settings(research_pipeline="v1")).provider == "wikipedia"


def test_off_question_evidence_blocks_and_retries_the_missing_relation(monkeypatch, tmp_path):
    from research_semantic_fixtures import AI_PAIN

    calls = _wire(monkeypatch, AI_PAIN.web())
    state = build_initial_state(AI_PAIN.question, AdvancedOptions(), _settings(tmp_path))
    assert state["facts"] == [] and state["render"]["status"] == "blocked_by_research"
    assert state["research"]["package"]["answer_grounding"]["core_answer"] == "missing"
    readiness = state["script"]["readiness"]
    assert not readiness["ready"] and any(item["code"] == "research_insufficient" for item in readiness["blocking"])
    # One retry, aimed at the asked capability - never a second one.
    assert len(calls) == 2 and calls[1]["context"]["focus"] == "capability"
