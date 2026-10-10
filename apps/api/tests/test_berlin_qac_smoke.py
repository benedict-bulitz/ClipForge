"""Preflight and rejection instrumentation only; never simulate a successful API response."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from clipforge.config import Settings
from clipforge.question_answer_contract import QuestionAnswerContract, ResearchCoverageReport

SPEC = importlib.util.spec_from_file_location('berlin_smoke', Path(__file__).parents[1] / 'scripts/berlin_qac_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def forbid(*_args, **_kwargs):
    raise AssertionError('Preparation must not access Keychain or OpenAI')


def test_preparation_has_zero_keychain_and_api_calls(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(smoke, 'resolve_settings', forbid)
    monkeypatch.setattr(smoke.qac, 'OpenAI', forbid)
    revision = 'fixture-revision-for-v4'
    monkeypatch.setattr(smoke.subprocess, 'check_output',
                        lambda command, **_kwargs: revision if 'rev-parse' in command else smoke.BRANCH)
    output = tmp_path / 'prepared.json'
    assert smoke.main(['--expected-head', revision, '--evidence', str(smoke.FIXTURE), '--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['status'] == 'PREPARED' and not report['live_executed']
    assert report['openai_calls'] == 0 and report['calls'] == []
    assert report['runtime_head'] == revision
    assert report['evidence']['fact_count'] == 17
    assert report['contract_pipeline'] == report['research_coverage'] == 'NOT_RUN'
    assert output.stat().st_mode & 0o777 == 0o600
    capsys.readouterr()


def test_wrong_head_cannot_start_live_calls(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(smoke, 'resolve_settings', forbid)
    monkeypatch.setattr(smoke.qac, 'OpenAI', forbid)
    monkeypatch.setattr(smoke.subprocess, 'check_output', lambda *_args, **_kwargs: 'wrong-revision')
    output = tmp_path / 'rejected.json'
    assert smoke.main(['--live', '--output', str(output)]) == 1
    report = json.loads(output.read_text())
    assert report['status'] == 'FAIL' and report['openai_calls'] == 0
    capsys.readouterr()


def test_fixture_evidence_is_not_modified():
    original = json.loads(smoke.FIXTURE.read_text())
    facts, metadata = smoke.load_evidence(smoke.FIXTURE)
    assert facts == original['facts'] and metadata['previous_coverage'] == original['contract_coverage']
    assert metadata['previous_coverage']['missing_obligations'] == ['primary_motive', 'escape_route_context']
    assert len(metadata['sha256']) == 64


def test_saved_overconstrained_contract_remains_fail():
    original = json.loads(smoke.FIXTURE.read_text())
    contract = QuestionAnswerContract.model_validate(original['contract'])
    contract._minimality_review = original['minimality']
    report = ResearchCoverageReport.model_validate(original['contract_coverage'])
    checks = smoke.acceptance(contract, report)
    assert not checks['no_required_regime_preservation_detected']
    assert not checks['no_required_escape_route_specifics_detected']
    assert not checks['production_coverage_passed']
    assert not checks['independent_necessity_passed']  # V2 lacks the independent question minimum


def test_rejected_provider_stops_once_preserves_diagnostics_and_redacts(monkeypatch):
    class Rejected(ValueError):
        param = 'response_format'

    seen = []
    def rejected(**request):
        seen.append(request)
        raise Rejected('API key=my-private-key; Authorization: Bearer private-token')

    def failing_client(**kwargs):
        assert kwargs['max_retries'] == 0 and kwargs['timeout'] == 90.0
        return SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=rejected))))

    monkeypatch.setattr(smoke.qac, 'OpenAI', failing_client)
    report = {'calls': [], 'openai_calls': 0, 'contract_pipeline': 'NOT_RUN', 'research_coverage': 'NOT_RUN'}
    snapshots = []
    smoke.run_live(Settings(openai_api_key='my-private-key'), [], report,
                   lambda: snapshots.append(json.loads(json.dumps(smoke.sanitized(report, ('my-private-key',))))))
    assert report['status'] == report['contract_pipeline'] == 'FAIL'
    assert report['openai_calls'] == 1 and len(seen) == 1
    assert report['calls'][0]['error']['request_parameter'] == 'response_format'
    assert report['error']['diagnostics']['failure_stage'] == 'contract_planner'
    assert 'my-private-key' not in json.dumps(snapshots) and 'private-token' not in json.dumps(snapshots)
    assert smoke.qac.OpenAI is failing_client
    assert 'temperature' not in seen[0]
