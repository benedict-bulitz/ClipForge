"""Audit regressions through real normalization, schemas and bounded orchestration."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.lib._parsing._completions import type_to_response_format_param
from pydantic import ValidationError
from test_question_answer_contract import (
    PipelineEditor,
    contract,
    coverage,
    evaluated,
    qac_context,
)
from test_script_story_rewrite import (
    DRAFT_EN,
    EN_FACTS,
    EN_QUESTION,
    REWRITE_EN,
    FakeEditor,
    rewrite,
)

from clipforge import pipeline, question_answer_contract
from clipforge.config import Settings
from clipforge.question_answer_contract import answer_obligations, checked_coverage
from clipforge.question_answer_research import merge_results, normalize_facts, run_contract_research
from clipforge.readiness import content_readiness
from clipforge.research import ResearchResult
from clipforge.research_v2.discovery import encyclopedia_keywords
from clipforge.research_v2.synthesis import deterministic_sub_questions
from clipforge.schemas import AdvancedOptions
from clipforge.script_story_rewrite import (
    ContractObligationEvaluation,
    OpenAIScriptStoryProvider,
    _checked_verdict,
    _contract_obligations,
    _verifier_findings,
    run_script_story_quality,
)

PARSE = OpenAIScriptStoryProvider._parse


@pytest.mark.parametrize("status", ["missing", "partially_satisfied"])
def test_primary_model_flag_cannot_make_primary_optional(status):
    model = contract()
    # Bypass model validators to exercise every consumer's deterministic invariant.
    model.primary_answer_obligation.is_required = False
    assert answer_obligations(model)[0].is_required
    assert not checked_coverage(model, coverage([], status=status), EN_FACTS).is_sufficient
    context = qac_context()
    context["question_answer_contract"] = model.model_dump()
    assert _contract_obligations(context)[0]["is_required"]
    checked = _checked_verdict(evaluated(status, required=False), context)
    assert checked.contract_evaluations[0].is_required
    assert "primary_answer_missing" in {item["code"] for item in _verifier_findings(checked)}
    _, research_context = pipeline.research_request(EN_QUESTION, {"language": "en", "content_type": "factual_explainer", "question_intent": {}}, model)
    assert model.primary_answer_obligation.description in research_context["search_intents"]


@pytest.mark.parametrize("question,description,language,term", [
    ("Warum ist der Mars rot?", "Erkläre die rote Farbe durch Eisenoxid und Oxidation im Staub.", "de", "Eisenoxid"),
    ("Why is Mars red?", "Explain the reddish color through iron oxide and oxidation in the dust.", "en", "oxide"),
    ("Warum wurde die Berliner Mauer gebaut?", "Erkläre das unmittelbare Motiv der Verhinderung von Flucht und Abwanderung.", "de", "Flucht"),
])
def test_clean_search_and_wikipedia_queries_keep_semantic_target(question, description, language, term):
    model = contract(question, description)
    intent = {"language": language, "content_type": "factual_explainer", "question_intent": {}}
    query, context = pipeline.research_request(question, intent, model)
    for diagnostic in [context["focus"], "Missing required answer: " + description]:
        subs = deterministic_sub_questions(question, query, language, focus=diagnostic, search_intents=context["search_intents"])
        assert len(subs[0].query) <= 160 and " | " not in subs[0].query
        assert term.casefold() in " ".join(sub.query for sub in subs[1:]).casefold()
        for sub in subs:
            searches = [sub.query, *encyclopedia_keywords(sub.query, language)]
            assert all(not any(label in search.lower() for label in ("primary goal", "required detail", "missing required answer")) for search in searches)
            assert all(len(search) <= 160 for search in searches)


def test_german_query_never_injects_foreign_contract_prose():
    subs = deterministic_sub_questions("Warum ist der Mars rot?", "Mars rot", "de", search_intents=["Explain why the material is red through supported iron oxidation"])
    assert all("Explain" not in sub.query and "supported" not in sub.query for sub in subs)


@pytest.mark.parametrize("stage", ["critique", "rewrite", "verify"])
def test_active_contract_provider_outage_blocks_ready_draft(stage):
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)] * 3, fail_on=stage)
    final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert not report["gate"]["ready"]
    readiness = content_readiness({**qac_context(), "contract": qac_context()["question_answer_contract"], "contract_coverage": qac_context()["research_coverage"], "script": {"blocks": final, "script_story_quality_v1": report}})
    assert not readiness["ready"]


def test_contradictory_flag_cannot_pass():
    assert "contract_insufficient" in {item["code"] for item in _verifier_findings(evaluated(contract_sufficient=False))}


def test_stale_failures_clear_when_current_contract_passes_but_quality_fails():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)] * 3, verdicts=[evaluated("missing"), evaluated(grounded=False), evaluated()])
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert report["gate"]["ready"]
    assert report["holistic"]["attempts"][1]["failed_obligations"] == []
    fresh = [brief for stage, brief in editor.calls if stage == "rewrite"][2]
    assert fresh["failed_obligations"] == []


def test_late_contract_failure_gets_full_data_in_third_fresh_request():
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)] * 3, verdicts=[evaluated(grounded=False), evaluated("partially_satisfied"), evaluated()])
    _final, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert report["gate"]["ready"]
    requests = [brief for stage, brief in editor.calls if stage == "rewrite"]
    assert [request["mode"] for request in requests] == ["normal_rewrite", "quality_repair", "fresh_regeneration"]
    assert requests[2]["failed_obligations"][0]["supporting_fact_ids"] == ["lift_02", "lift_03", "lift_04"]
    assert "previous_rewrite" not in requests[2] and "draft" not in requests[2]


def test_provider_schema_has_explicit_required_contract_fields(monkeypatch):
    schema = type_to_response_format_param(type(evaluated()))["json_schema"]
    assert schema["strict"]
    assert {"contract_sufficient", "contract_evaluations"} <= set(schema["schema"]["required"])
    evaluation = schema["schema"]["$defs"]["ContractObligationEvaluation"]
    assert {"is_primary", "is_required"} <= set(evaluation["required"])
    assert "default" not in evaluation["properties"]["is_required"]
    with pytest.raises(ValidationError):
        ContractObligationEvaluation(id="bad", status="satisfied", reasoning="No flags")
    monkeypatch.setattr(OpenAIScriptStoryProvider, "_parse", PARSE)
    provider = OpenAIScriptStoryProvider(Settings(openai_api_key="test-key"))
    parse = Mock(return_value=SimpleNamespace(output_parsed=evaluated()))
    provider._client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    assert provider.verify(qac_context()).contract_sufficient
    assert parse.call_args.kwargs["text_format"] is type(evaluated())
    assert json.loads(parse.call_args.kwargs["input"])["question_answer_contract"]


def test_normalization_is_idempotent_deduplicates_and_keeps_raw_provenance():
    facts = [
        {**EN_FACTS[1], "claim": "  " + EN_FACTS[1]["claim"] + "  ", "research_key": "first"},
        {**EN_FACTS[1], "research_key": "retry", "sources": [{"label": "other", "url": "https://other.test"}]},
        EN_FACTS[2],
    ]
    first = normalize_facts(facts)
    second = normalize_facts(first)
    assert first == second and len(first) == 2
    assert first[0]["raw_claim"].startswith("  ")
    assert len(first[0]["sources"]) == 2 and first[0]["research_keys"] == ["first", "retry"]
    assert [item["id"] for item in second] == ["fact_01", "fact_02"]


def native_split_results():
    results = []
    for index, text in enumerate([
        "Der Mars erscheint rot, weil Eisenoxidstaub seine Oberfläche bedeckt.",
        "Auf dem Mars bilden sich rötliche Eisenoxide, weil Eisen im Staub mit Sauerstoff reagiert.",
    ]):
        source = {"id": "src_01", "title": "Mars und Eisenoxide", "url": f"https://source{index}.test/mars", "domain": f"source{index}.test", "authority": "high", "source_type": "science", "basis": "full_text"}
        evidence = [{"id": "ev_01", "text": text, "excerpt": text, "source_id": "src_01", "sub_question": "q_core", "kinds": ["mechanism"], "matched": ["mars"], "relevance": 1.0, "basis": "full_text"}]
        results.append(ResearchResult([{"id": "fact_01", "claim": text, "confidence": 0.9, "research_key": "claim_01", "evidence_ids": ["ev_01"], "verification": "supported", "sources": [{"source_id": "src_01", "label": source["title"], "url": source["url"]}]}], [{"label": source["title"], "url": source["url"]}], "verified_sources", "fixture", None,
                                      {"status": "missing_mechanism", "core_answer": None}, {"sentinel": index},
                                      {"sources": [source], "evidence": evidence}))
    return results


def test_merge_rebuilds_combined_native_package_and_isolates_id_collisions():
    first, retry = native_split_results()
    combined = merge_results(first, retry, "Warum ist der Mars rot?", "de")
    package = combined.package
    assert package["status"] not in {"insufficient", "missing_mechanism"}
    assert package["core_answer"] and package["explanation_spine"]["status"] == "complete"
    assert {item["source_id"].split("_")[0] for item in package["evidence"]} == {"run1", "run2"}
    assert len({item["id"] for item in combined.evidence_bundle["sources"]}) == 2
    assert [run["diagnostics"]["sentinel"] for run in combined.diagnostics["runs"]] == [0, 1]
    assert len({item["research_key"] for item in combined.facts}) == len(combined.facts)
    assert package["core_answer"]["fact_id"] in {item["id"] for item in combined.facts}


@pytest.mark.parametrize("scenario", ["missing", "weak", "mechanism", "first_outage", "retry_outage"])
def test_one_research_budget_owner_for_all_decisions(scenario):
    research = Mock(side_effect=[ResearchResult(copy.deepcopy(EN_FACTS), [], "verified_sources", "fixture", package={
        "status": "missing_mechanism" if scenario == "mechanism" else "sufficient",
        "core_answer": {"basis": "snippet", "authority_tier": 3} if scenario == "weak" else None,
    }), ResearchResult(copy.deepcopy(EN_FACTS), [], "verified_sources", "fixture")])
    ids = [item["id"].replace("lift_", "fact_") for item in EN_FACTS[1:4]]
    missing = coverage([], status="missing")
    sufficient = coverage(ids)
    evaluate = Mock(side_effect=[RuntimeError("coverage transport down")] if scenario == "first_outage" else [missing if scenario in {"missing", "retry_outage"} else sufficient, RuntimeError("retry coverage down") if scenario == "retry_outage" else sufficient])
    result, report, retry, diagnostics = run_contract_research(
        EN_QUESTION, "en", Settings(), context={"question": EN_QUESTION}, contract=contract(), research=research,
        evaluate=evaluate, request=lambda query, context, focus, descriptions, reason: (query, {**context, "focus": focus, "search_intents": descriptions}),
    )
    expected = 1 if scenario == "first_outage" else 2
    assert research.call_count == expected <= 2
    assert len(diagnostics["research_calls"]) == expected
    assert diagnostics["retry_budget"]["used"] == expected - 1
    if "outage" in scenario:
        assert report is None and diagnostics["failure_type"] == "COVERAGE_UNAVAILABLE"
        assert diagnostics["coverage_status"] != "RESEARCH_MISSING"
    else:
        assert report.is_sufficient and result.facts
    if retry:
        assert contract().primary_answer_obligation.description in research.call_args.kwargs["context"]["focus"]


@pytest.mark.parametrize("failure", ["generation", "coverage_first", "coverage_retry"])
def test_pipeline_provider_failure_has_friendly_block_and_exact_accounting(monkeypatch, failure):
    monkeypatch.setattr(pipeline, "generate_contract", Mock(side_effect=RuntimeError("planner down")) if failure == "generation" else lambda *_a: contract())
    monkeypatch.setattr(pipeline, "plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="local", error=None))
    monkeypatch.setattr(pipeline, "_generate_body_with_v2_or_fallback", lambda *_a, **_k: (_a[5], {"status": "legacy_fallback"}))
    calls = []
    def research(*_a, **_k):
        calls.append(_k)
        return ResearchResult(copy.deepcopy(EN_FACTS), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture")
    monkeypatch.setattr(pipeline, "research_topic", research)
    monkeypatch.setattr(pipeline, "evaluate_research_coverage", Mock(side_effect=[coverage([], status="missing"), RuntimeError("coverage down")] if failure == "coverage_retry" else RuntimeError("coverage down")))
    state = pipeline.build_initial_state(EN_QUESTION, AdvancedOptions(language="en", research="on"), Settings(clipforge_ai_mode="local", openai_api_key="test-key"), script_quality_provider=PipelineEditor())
    assert not state["script"]["readiness"]["ready"]
    assert len(calls) == (2 if failure == "coverage_retry" else 1)
    assert len(state["research"]["attempts"]) == len(calls)
    assert state["script"]["readiness"]["message"] == "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."
    assert state["research"]["diagnostics"]["failure_type"] == ("CONTRACT_UNAVAILABLE" if failure == "generation" else "COVERAGE_UNAVAILABLE")


def test_fiction_never_calls_contract_or_research(monkeypatch):
    planner, researcher = Mock(side_effect=AssertionError("not applicable")), Mock(side_effect=AssertionError("not applicable"))
    monkeypatch.setattr(pipeline, "generate_contract", planner)
    monkeypatch.setattr(pipeline, "research_topic", researcher)
    state = pipeline.build_initial_state("Write a short story about a lighthouse", AdvancedOptions(content_type="fictional_story", research_enabled=False), Settings(clipforge_ai_mode="local", openai_api_key=None))
    assert state["contract"] is None and not state["research"]["required"]
    planner.assert_not_called()
    researcher.assert_not_called()


def test_combined_native_evidence_unblocks_full_pipeline_after_narrow_retry(monkeypatch):
    question = "Warum ist der Mars rot?"
    model = contract(question, "Erkläre die Entstehung der roten Eisenoxide durch Oxidation.")
    first, retry = native_split_results()
    researcher = Mock(side_effect=[first, retry])
    monkeypatch.setattr(pipeline, "generate_contract", lambda *_a: model)
    monkeypatch.setattr(pipeline, "research_topic", researcher)
    monkeypatch.setattr(pipeline, "plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="local", error=None))
    monkeypatch.setattr(pipeline, "_generate_body_with_v2_or_fallback", lambda *_a, **_k: (_a[5], {"status": "legacy_fallback"}))

    def evaluate(_model, facts, _settings):
        ids = [fact["id"] for fact in facts if "Sauerstoff" in fact["claim"]]
        return coverage(ids, status="satisfied" if ids else "missing")
    monkeypatch.setattr(pipeline, "evaluate_research_coverage", evaluate)

    class Editor(FakeEditor):
        def beats(self, brief):
            facts = brief["research"]["facts"]
            mechanism = next(fact for fact in facts if "Sauerstoff" in fact["claim"])
            surface = next(fact for fact in facts if "Oberfläche" in fact["claim"])
            return [("hook", "Die rote Farbe des Mars beginnt mit einer chemischen Veränderung.", []),
                    ("explanation", mechanism["claim"], [mechanism["id"]]),
                    ("payoff", surface["claim"], [surface["id"]])]

        def rewrite(self, brief):
            self._call("rewrite", brief)
            return rewrite(self.beats(brief))

        def verify(self, brief):
            self._call("verify", brief)
            return evaluated(beats=self.beats(brief))

    state = pipeline.build_initial_state(question, AdvancedOptions(language="de", research="on"),
                                         Settings(clipforge_ai_mode="local", openai_api_key="test-key"), script_quality_provider=Editor())
    assert researcher.call_count == 2
    assert state["script"]["readiness"]["ready"], json.dumps({"readiness": state["script"]["readiness"], "quality": state["script"]["script_story_quality_v1"]["holistic"]}, ensure_ascii=False)
    assert state["research"]["package"]["status"] != "missing_mechanism"
    assert state["research"]["package"]["synthesis"] == "combined_evidence"
    assert [run["diagnostics"]["sentinel"] for run in state["research"]["diagnostics"]["runs"]] == [0, 1]


def test_pipeline_runs_actual_mocked_planner_and_coverage_parser(monkeypatch):
    from test_contract_minimality import approved_audit

    model = contract()
    parse = Mock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=model))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=approved_audit(model)))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=coverage(["fact_02", "fact_03", "fact_04"])))]),
    ])
    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))
    monkeypatch.setattr(question_answer_contract, "OpenAI", lambda **_k: client)
    monkeypatch.setattr(pipeline, "plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="local", error=None))
    monkeypatch.setattr(pipeline, "_generate_body_with_v2_or_fallback", lambda *_a, **_k: (_a[5], {"status": "legacy_fallback"}))
    researcher = Mock(return_value=ResearchResult(copy.deepcopy(EN_FACTS), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"))
    monkeypatch.setattr(pipeline, "research_topic", researcher)
    state = pipeline.build_initial_state(EN_QUESTION, AdvancedOptions(language="en", research="on"),
                                         Settings(clipforge_ai_mode="local", openai_api_key="test-key"), script_quality_provider=PipelineEditor())
    assert parse.call_count == 3 and researcher.call_count == 1
    assert state["contract"] and state["script"]["readiness"]["ready"]
    assert state["research"]["contract_minimality"]["repaired"] is False
    assert parse.call_args_list[0].kwargs["response_format"] is type(model)


@pytest.mark.parametrize("stage", ["critique", "rewrite", "verify"])
def test_provider_outage_stops_before_tts_render_gate(monkeypatch, stage):
    monkeypatch.setattr(pipeline, "generate_contract", lambda *_a: contract())
    monkeypatch.setattr(pipeline, "evaluate_research_coverage", lambda *_a: coverage(["fact_02", "fact_03", "fact_04"]))
    monkeypatch.setattr(pipeline, "plan_with_openai", lambda *_a, **_k: SimpleNamespace(plan=None, status="local", error=None))
    monkeypatch.setattr(pipeline, "_generate_body_with_v2_or_fallback", lambda *_a, **_k: (_a[5], {"status": "legacy_fallback"}))
    researcher = Mock(return_value=ResearchResult(copy.deepcopy(EN_FACTS), [{"label": "s", "url": "https://s.test"}], "verified_sources", "fixture"))
    monkeypatch.setattr(pipeline, "research_topic", researcher)
    editor = PipelineEditor()
    editor.fail_on = stage
    state = pipeline.build_initial_state(EN_QUESTION, AdvancedOptions(language="en", research="on"), Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
                                         script_quality_provider=editor)
    from clipforge.readiness import ScriptNotReady
    from clipforge.services import _require_ready
    with pytest.raises(ScriptNotReady):
        _require_ready(state)
    assert researcher.call_count == 1
    assert stage in editor.stages()
    assert not state["script"]["readiness"]["ready"]


@pytest.mark.parametrize("weak_status", ["low_confidence", "unverified"] )
def test_duplicate_claim_uses_stronger_retry_without_losing_first_source(weak_status):
    weak = {**EN_FACTS[1], "confidence": 0.2} if weak_status == "low_confidence" else {**EN_FACTS[1], "confidence": 0.99, "verification": "unverified_model_synthesis"}
    strong = {**EN_FACTS[1], "confidence": 0.9, "sources": [{"label": "strong", "url": "https://strong.test"}]}
    facts = normalize_facts([weak, strong])
    assert len(facts) == 1 and facts[0]["confidence"] == 0.9 and len(facts[0]["sources"]) == 2


@pytest.mark.parametrize("language,question,description", [
    ("de", "Warum ist der Mars rot?", "Erkläre Eisenoxid und Oxidation im Staub."),
    ("en", "Why is Mars red?", "Explain iron oxide and oxidation in the dust."),
])
def test_v1_fallback_also_uses_clean_semantic_search_intent(monkeypatch, language, question, description):
    from clipforge import research
    provider = Mock(return_value=ResearchResult([], [], "unavailable", "fixture"))
    monkeypatch.setattr(research, "_research_topic_v1", provider)
    research.research_topic(question, language, Settings(research_pipeline="v1"), context={
        "question": question, "focus": "Missing required answer: " + description,
        "search_intents": [description],
    })
    query = provider.call_args.args[0]
    assert "oxid" in query.lower() and len(query) <= 160
    assert "Missing required answer" not in query and "Explain" not in query
