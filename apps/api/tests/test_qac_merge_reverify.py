"""QAC union and exact-script recovery regressions; no live providers."""
import copy
import json
from unittest.mock import Mock

import pytest
from test_question_answer_contract import (
    contract,
    coverage,
    evaluated,
    install_pipeline,
    qac_context,
)
from test_question_answer_contract_edges import native_split_results
from test_script_story_rewrite import DRAFT_EN, REWRITE_EN, FakeEditor, rewrite

from clipforge import pipeline
from clipforge.ai import AIEditDirective
from clipforge.config import Settings
from clipforge.novelty import fact_is_supported
from clipforge.question_answer_research import merge_results, normalize_facts, run_contract_research
from clipforge.readiness import content_readiness
from clipforge.research import ResearchResult
from clipforge.script_story_quality import script_quality_signature
from clipforge.script_story_rewrite import run_script_story_quality


@pytest.mark.parametrize("native_first", [False, True])
def test_mixed_merge_retains_every_supported_original(native_first):
    native, _ = native_split_results()
    summary = {**copy.deepcopy(native.facts[0]), "claim": "Unique supported summary of iron oxidation.",
               "research_key": "summary", "research_keys": ["summary", "support"], "raw_claim": "Raw validated summary"}
    legacy = ResearchResult([summary], copy.deepcopy(native.sources), "verified_sources", "v1")
    first, retry = (native, legacy) if native_first else (legacy, native)
    snapshot = copy.deepcopy((first, retry))
    merged = merge_results(first, retry, "Warum ist der Mars rot?", "de")
    for result in (first, retry):
        for original in result.facts:
            retained = next(fact for fact in merged.facts if fact["claim"] == original["claim"])
            assert fact_is_supported(retained) and retained["confidence"] >= original["confidence"]
    assert (first, retry) == snapshot
    retained = next(fact for fact in merged.facts if fact["claim"] == summary["claim"])
    prefix = "run2_" if native_first else "run1_"
    assert retained["research_keys"] == [prefix + "summary", prefix + "support"]
    assert "Raw validated summary" in retained["raw_claims"]
    assert len({fact["id"] for fact in merged.facts}) == len(merged.facts)


def test_duplicate_merge_preserves_all_provenance_and_namespaces():
    first, retry = native_split_results()
    retry.facts[0] = copy.deepcopy(first.facts[0])
    retry.facts[0]["sources"][0]["url"] = retry.sources[0]["url"]
    retry.facts[0]["raw_claim"] = "Retry raw claim"
    merged = merge_results(first, retry, "Warum ist der Mars rot?", "de")
    matches = [fact for fact in merged.facts if fact["claim"] == first.facts[0]["claim"]]
    assert len(matches) == 1
    fact = matches[0]
    assert {"run1_claim_01", "run2_claim_01"} <= set(fact["research_keys"])
    assert {"run1_ev_01", "run2_ev_01"} <= set(fact["evidence_ids"])
    assert {"run1_src_01", "run2_src_01"} <= {source["source_id"] for source in fact["sources"]}
    assert {first.facts[0]["claim"], "Retry raw claim"} <= set(fact["raw_claims"])
    assert fact_is_supported(fact) and merged.package["core_answer"]["fact_id"]


def test_supported_duplicate_without_confidence():
    first, _ = native_split_results()
    weak = {**first.facts[0], "verification": "unverified_model_synthesis", "confidence": 0.9}
    supported = copy.deepcopy(first.facts[0])
    supported.pop("confidence")
    facts = normalize_facts([weak, supported])
    assert len(facts) == 1 and fact_is_supported(facts[0]) and facts[0]["confidence"] == 0.5
    assert normalize_facts(facts) == facts


def run_research(first, retry, evaluate):
    return run_contract_research("Mars", "de", Settings(), context={"question": "Mars"}, contract=contract(),
                                 research=Mock(side_effect=[first, retry]), evaluate=evaluate,
                                 request=lambda query, context, *_args: (query, context))


def test_quality_retry_preserves_first_pass_coverage_facts():
    first, retry = native_split_results()
    summary = {**copy.deepcopy(first.facts[0]), "claim": "Unique supported summary used by first-pass coverage."}
    first.facts.append(summary)
    observed = []

    def evaluate(_contract, facts, _settings):
        supported = [fact for fact in facts if fact_is_supported(fact)]
        observed.append({fact["claim"] for fact in supported})
        ids = [fact["id"] for fact in supported if fact["claim"] == summary["claim"]]
        return coverage(ids, status="satisfied" if ids else "missing")

    _, report, retry_report, diagnostic = run_research(first, retry, evaluate)
    assert retry_report["failure_type"] == "RESEARCH_QUALITY"
    assert report.is_sufficient and diagnostic["coverage_status"] == "SUFFICIENT"
    assert observed[0] <= observed[1]


def test_coverage_downgrade_is_merge_regression():
    first, retry = native_split_results()
    _, report, _, diagnostic = run_research(first, retry, Mock(side_effect=[coverage(["fact_01"]), coverage([], status="missing")]))
    assert not report.is_sufficient
    assert diagnostic["failure_type"] == diagnostic["coverage_status"] == "MERGE_REGRESSION"


class CurrentVerifier(FakeEditor):
    def __init__(self, status="satisfied", fail=False):
        super().__init__(fail_on="verify" if fail else None)
        self.status = status

    def verify(self, brief):
        self._call("verify", brief)
        beats = [(item["role"], item["text"], item["fact_ids"]) for item in brief["candidate"]["beats"]]
        return evaluated(self.status, beats=beats)


def assert_current_verified(state):
    assert content_readiness(state)["ready"], content_readiness(state)
    report = state["script"]["script_story_quality_v1"]
    assert report["rewrite"]["verified_script_signature"] == script_quality_signature(state["script"]["blocks"])
    assert report["rewrite"]["verified_by"] == "ai_verifier"


@pytest.mark.parametrize("result", ["pass", "missing", "outage", "unavailable"])
def test_edit_reverifies_current_blocks_once_without_rewriting(monkeypatch, result):
    state, _, _ = install_pipeline(monkeypatch)
    assert_current_verified(state)
    old_signature = state["script"]["script_story_quality_v1"]["rewrite"]["verified_script_signature"]
    state["script"]["blocks"][1]["text"] = state["script"]["blocks"][1]["text"].replace("push", "drive")
    edited = copy.deepcopy(state["script"]["blocks"])
    assert not content_readiness(state)["ready"]
    assert "contract_verification_stale" in {item["code"] for item in content_readiness(state)["blocking"]}
    editor = CurrentVerifier("missing" if result == "missing" else "satisfied", fail=result == "outage")
    pipeline._refresh_script_derivatives(state, settings=Settings(openai_api_key=None),
                                        script_quality_provider=None if result == "unavailable" else editor)
    assert state["script"]["blocks"] == edited
    assert old_signature != script_quality_signature(edited)
    report = state["script"]["script_story_quality_v1"]
    if result == "unavailable":
        assert editor.stages() == [] and report["rewrite"]["status"] == "CONTRACT_VERIFICATION_STALE"
    else:
        assert editor.stages() == ["verify"]
        assert editor.calls[0][1]["candidate"]["beats"] == [
            {"index": index, "role": block["role"], "text": block["text"], "fact_ids": block["fact_ids"]} for index, block in enumerate(edited, 1)]
    if result == "pass":
        assert_current_verified(state)
        assert report["rewrite"]["contract_evaluations"] and report["holistic"]["explanation_audit"]
    else:
        assert not content_readiness(state)["ready"]
        assert state["script"]["readiness"]["message"] == "ClipForge couldn't create a sufficiently supported answer for this question yet. Please try again."
        if result == "outage":
            assert "verify offline" in report["verification_diagnostics"]["error"]


def test_no_contract_edit_never_verifies(monkeypatch):
    state, _, _ = install_pipeline(monkeypatch)
    state["contract"] = None
    state["script"]["blocks"][1]["text"] = state["script"]["blocks"][1]["text"].replace("push", "drive")
    editor = CurrentVerifier(fail=True)
    pipeline._refresh_script_derivatives(state, settings=Settings(openai_api_key=None), script_quality_provider=editor)
    assert editor.stages() == []
    assert not any(item["code"].startswith("contract_") for item in content_readiness(state)["blocking"])


def test_unchanged_verified_script_never_reverifies(monkeypatch):
    state, _, _ = install_pipeline(monkeypatch)
    editor = CurrentVerifier(fail=True)
    pipeline._refresh_script_derivatives(state, settings=Settings(openai_api_key=None), script_quality_provider=editor)
    assert editor.stages() == []
    assert_current_verified(state)


@pytest.mark.parametrize("success", [True, False])
def test_automatic_post_verification_mutation_gets_final_verifier(monkeypatch, success):
    original = pipeline.run_script_story_quality
    signatures = []

    def mutate(blocks, context, provider):
        blocks, report = original(blocks, context, provider)
        signatures.append(report["rewrite"]["verified_script_signature"])
        blocks[0]["text"] += " Watch the wings."
        if not success:
            provider.fail_on = "verify"
        return blocks, report

    monkeypatch.setattr(pipeline, "run_script_story_quality", mutate)
    state, _, editor = install_pipeline(monkeypatch)
    assert editor.stages().count("rewrite") == 1 and editor.stages().count("verify") == 2
    assert script_quality_signature(state["script"]["blocks"]) != signatures[0]
    assert [beat["text"] for beat in editor.calls[-1][1]["candidate"]["beats"]] == [block["text"] for block in state["script"]["blocks"]]
    if success:
        assert_current_verified(state)
    else:
        assert not content_readiness(state)["ready"]


def test_fresh_regeneration_strips_verifier_failed_wording():
    bad = evaluated("missing")
    bad.contract_evaluations[0].reasoning = "FAILED WORDING quoted from rejected narration"
    editor = FakeEditor(rewrites=[rewrite(REWRITE_EN)] * 3, verdicts=[bad, bad, evaluated()])
    _, report = run_script_story_quality(copy.deepcopy(DRAFT_EN), qac_context(), editor)
    assert report["gate"]["ready"]
    fresh = [brief for stage, brief in editor.calls if stage == "rewrite"][2]
    assert "FAILED WORDING" not in json.dumps(fresh) and "verifier_reason" not in json.dumps(fresh)
    assert fresh["failed_obligations"][0]["supporting_facts"]


@pytest.mark.parametrize("status", ["satisfied", "missing"])
def test_apply_edit_uses_verifier_only_on_resulting_script(monkeypatch, status):
    state, _, _ = install_pipeline(monkeypatch)
    editor = CurrentVerifier(status)
    monkeypatch.setattr(pipeline, "interpret_edit", lambda *_a: AIEditDirective(components=["script"]))
    monkeypatch.setattr(pipeline, "OpenAIScriptStoryProvider", lambda _settings: editor)
    edited, _ = pipeline.apply_edit(state, "Remove the script sentence about engines.",
                                    Settings(clipforge_ai_mode="local", openai_api_key="test-key"))
    assert editor.stages() == ["verify"]
    assert "The engines push" not in edited["script"]["text"]
    assert [beat["text"] for beat in editor.calls[0][1]["candidate"]["beats"]] == [block["text"] for block in edited["script"]["blocks"]]
    assert content_readiness(edited)["ready"] == (status == "satisfied")
    assert state["script"]["blocks"] != edited["script"]["blocks"]


@pytest.mark.parametrize("success", [True, False])
def test_render_hook_reselection_reverifies_before_render(monkeypatch, success):
    from clipforge import services
    from clipforge.readiness import ScriptNotReady

    state, _, _ = install_pipeline(monkeypatch)
    state.setdefault("music", {}).setdefault("selection", {})["mode"] = "all_music"
    editor = CurrentVerifier(fail=not success)
    monkeypatch.setattr(pipeline, "OpenAIScriptStoryProvider", lambda _settings: editor)
    monkeypatch.setattr(services, "prepare_project_media", lambda *_a, **_k: None)
    monkeypatch.setattr(services, "run_ai_review", lambda *_a, **_k: None)

    def reselect(current):
        current["script"]["blocks"][0]["text"] += " Watch the wings."
        assert not content_readiness(current)["ready"]

    monkeypatch.setattr(services, "enforce_selected_hook", reselect)
    reached_render = []

    def render(current, *_a, **_k):
        assert_current_verified(current)
        reached_render.append(current)
        raise RuntimeError("mock render boundary reached")

    monkeypatch.setattr(services, "render_video", render)
    with pytest.raises(RuntimeError if success else ScriptNotReady, match="mock render boundary" if success else "sufficiently supported"):
        services._render_state(state, "fixture", 2, Settings(openai_api_key="test-key"))
    assert editor.stages() == ["verify"]
    assert bool(reached_render) == success


@pytest.mark.parametrize("success", [True, False])
def test_editor_tool_replacement_keeps_user_words_and_uses_active_settings(monkeypatch, success):
    from clipforge import editor_tools

    state, _, _ = install_pipeline(monkeypatch)
    editor = CurrentVerifier("satisfied" if success else "missing")
    monkeypatch.setattr(pipeline, "OpenAIScriptStoryProvider", lambda settings: editor if settings.openai_api_key == "tool-key" else None)
    monkeypatch.setattr(editor_tools, "effective_revision_state", lambda _project: state)
    monkeypatch.setattr(editor_tools, "mutate_project_state", lambda *_a, **kwargs: kwargs["mutate"](state))
    monkeypatch.setattr(editor_tools.EditorToolbox, "_mutation_result", lambda *_a: None)
    toolbox = editor_tools.EditorToolbox(None, None, Settings(openai_api_key="tool-key"), auto_render=False)
    replacement = state["script"]["blocks"][1]["text"].replace("push", "drive")
    toolbox._tool_edit_scene(editor_tools.EditSceneArguments(scene_number=2, narration_text=replacement), False)
    assert editor.stages() == ["verify"]
    assert replacement in state["script"]["text"]
    assert editor.calls[0][1]["candidate"]["beats"][1]["text"] == replacement
    assert content_readiness(state)["ready"] == success
