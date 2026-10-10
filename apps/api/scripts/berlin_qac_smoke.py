"""Local-only Berlin V3 smoke: preparation is the default; --live authorizes API calls.

Uses the production contract entrypoint exactly once and existing facts only.
The Berlin scope screen is conservative smoke instrumentation, not product logic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

from clipforge import question_answer_contract as qac
from clipforge.config import resolve_settings
from clipforge.contract_diagnostics import sanitized
from clipforge.novelty import fact_is_supported

QUESTION = 'Warum wurde die Berliner Mauer gebaut?'
LANGUAGE = 'de'
EXPECTED_HEAD = '7f7a30c'
BRANCH = 'cloud/question-answer-contract-v1-repair'
FIXTURE = API_ROOT / 'tests/fixtures/berlin_qac_v2_necessity.json'


def load_evidence(path):
    payload = json.loads(path.read_text())
    state = payload['revision']['state'] if 'revision' in payload else payload
    facts = state['facts']
    if state.get('prompt') != QUESTION or not facts or not all(isinstance(f, dict) and f.get('id') for f in facts):
        raise ValueError('Saved Berlin evidence has the wrong question or invalid facts.')
    if len({f['id'] for f in facts}) != len(facts):
        raise ValueError('Saved fact IDs are not unique.')
    return facts, {
        'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'fact_count': len(facts), 'usable_fact_ids': [f['id'] for f in facts if fact_is_supported(f)],
        'previous_contract': state.get('contract'), 'previous_coverage': state.get('contract_coverage'),
        'previous_minimality': (state.get('research') or {}).get('contract_minimality') or state.get('minimality'),
    }


def acceptance(contract, coverage):
    """Fail conservatively on known Berlin overconstraints, never edit the contract.

    Semantic evidence remains the real necessity audit and coverage verdict.
    These lexical checks can reject valid wording and cannot prove semantics.
    Always inspect the persisted full contract/audit, even after this screen.
    """
    review = contract._minimality_review
    audit = review.get('audit') or {}
    minimum = review.get('question_minimum') or {}
    repair = review.get('repair_check') or {}
    obligations = [o for o in qac.answer_obligations(contract) if o.is_required]
    mandatory = [o.description for o in obligations]
    mandatory += [contract.historical_motive or '', *contract.required_mechanism_concepts]
    mandatory += [s.description for s in contract.causal_mechanistic_chain]
    mandatory += [c.get('description', '') for c in minimum.get('necessary_components', [])]
    mandatory += [c.get('description', '') for c in audit.get('components', []) if c.get('strictly_necessary')]
    text = ' '.join(mandatory)
    # Smoke-specific screening of all mandatory fields, including hidden ones.
    regime = bool(re.search(r'(?i)\b(?:herrschaft|regime|fortbestand|überleben|machterhalt|machtsicherung)\w*', text))
    route = bool(re.search(
        r'(?i)\b(?:entscheidend|einzig|verblieben|letzt|wichtigst|haupt)\w*[^.;]{0,90}(?:fluchtweg|fluchtroute|west.?berlin)|'
        r'(?:fluchtweg|fluchtroute|west.?berlin)[^.;]{0,90}\b(?:entscheidend|einzig|verblieben|letzt|wichtigst)\w*', text))
    route = route or any(re.search(r'(?i)west.?berlin|fluchtweg|fluchtroute|geograf', o.description)
                             for o in obligations if not o.is_primary)
    primary = contract.primary_answer_obligation
    immediate = bool(re.search(r'(?i)flucht|flüchtling|abwander|ausreise|emigra', primary.description)
                     and re.search(r'(?i)stopp|verhinder|unterbind|eindämm|beend', primary.description))
    audit_ok = bool(minimum and audit and audit.get('preserves_question_semantics')
                    and audit.get('preserves_explicit_constraints')
                    and minimum.get('preserves_question_semantics') and minimum.get('preserves_explicit_constraints'))
    if review.get('repaired'):
        audit_ok = audit_ok and all(repair.get(flag) is True for flag in (
            'minimally_sufficient', 'preserves_question_semantics', 'preserves_explicit_constraints',
            'preserves_primary_cause_or_motive', 'preserves_necessary_chain'))
        audit_ok = audit_ok and bool(repair.get('components')) and all(
            item.get('correctly_required_or_optional') is True for item in repair['components'])
    else:
        audit_ok = audit_ok and audit.get('minimally_sufficient') is True and audit.get('correction_required') is False
    checks = {
        'immediate_motive_required': primary.is_required and primary.is_primary and immediate,
        'independent_necessity_passed': audit_ok,
        'no_required_regime_preservation_detected': not regime,
        'no_required_escape_route_specifics_detected': not route,
        'production_coverage_passed': bool(coverage and coverage.is_sufficient),
        'no_missing_required_evidence': bool(coverage and not coverage.missing_obligations),
        'question_preserved': contract.core_question == QUESTION,
    }
    return checks


def run_live(settings, facts, report, persist):
    """Observe and forward real SDK parse calls; never substitute AI responses."""
    original_factory = qac.OpenAI
    secrets = (settings.openai_api_key,)
    report['model'] = settings.openai_director_model

    def error_details(exc):
        body = getattr(exc, 'body', None)
        detail = body.get('error', body) if isinstance(body, dict) else {}
        return sanitized({'exception_type': type(exc).__name__, 'message': str(exc),
                          'request_parameter': getattr(exc, 'param', None) or detail.get('param'),
                          'provider_code': detail.get('code'), 'diagnostics': getattr(exc, 'diagnostics', None)}, secrets)

    def factory(**kwargs):
        actual = original_factory(**kwargs, max_retries=0, timeout=90.0)

        def observed_parse(**request):
            schema = request['response_format'].__name__
            stage = {'QuestionMinimum': 'question_minimum', 'NecessityAudit': 'necessity_audit',
                     'RepairCheck': 'repair_check', 'ResearchCoverageReport': 'research_coverage'}.get(schema)
            if schema == 'QuestionAnswerContract':
                stage = 'contract_repair' if any(c['stage'] == 'contract_planner' for c in report['calls']) else 'contract_planner'
            if stage is None or len(report['calls']) >= 6 or any(c['stage'] == stage for c in report['calls']):
                raise ValueError('Unexpected or repeated production stage; no further API call allowed.')
            record = {'stage': stage, 'schema': schema, 'model': request['model'],
                      'messages': sanitized(request['messages'], secrets), 'status': 'started'}
            report['calls'].append(record)
            report['openai_calls'] = len(report['calls'])
            persist()
            try:
                result = actual.beta.chat.completions.parse(**request)
                message = result.choices[0].message
                parsed = message.parsed
                record.update(status='returned', response=parsed.model_dump(mode='json') if parsed is not None else None,
                              refusal=getattr(message, 'refusal', None))
                return result
            except Exception as exc:
                record.update(status='failed', error=error_details(exc))
                raise
            finally:
                persist()

        return SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=observed_parse))))

    log_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # SDK debug logging must not leak credentials
    qac.OpenAI = factory
    try:
        contract = qac.generate_contract(QUESTION, LANGUAGE, settings)  # includes guard exactly once
        report.update(contract=contract.model_dump(mode='json'), minimality=contract._minimality_review,
                      required_obligations=[o.model_dump() for o in qac.answer_obligations(contract) if o.is_required],
                      optional_obligations=[o.model_dump() for o in qac.answer_obligations(contract) if not o.is_required],
                      optional_context=contract.optional_context, contract_pipeline='PASS')
        preliminary = acceptance(contract, None)
        report['contract_minimality'] = 'PASS' if all(value for key, value in preliminary.items()
                                                    if key not in {'production_coverage_passed', 'no_missing_required_evidence'}) else 'FAIL'
        persist()
        coverage = qac.evaluate_research_coverage(contract, facts, settings)
        report['coverage'] = coverage.model_dump(mode='json')
        report['research_coverage'] = 'PASS' if coverage.is_sufficient and not coverage.missing_obligations else 'FAIL'
        checks = acceptance(contract, coverage)
        report['acceptance_checks'] = checks
        report['contract_minimality'] = 'PASS' if all(value for key, value in checks.items()
                                                    if key not in {'production_coverage_passed', 'no_missing_required_evidence'}) else 'FAIL'
        report['status'] = 'PASS' if all(checks.values()) else 'FAIL'
        report['blockers'] = [key for key, passed in checks.items() if not passed]
    except Exception as exc:  # noqa: BLE001 - persist sanitized live failures, fail closed
        report.update(status='FAIL', error=error_details(exc))
        if report['contract_pipeline'] != 'PASS':
            report['contract_pipeline'] = report['contract_minimality'] = 'FAIL'
        else:
            report['research_coverage'] = 'FAIL'
    finally:
        qac.OpenAI = original_factory
        logging.disable(log_threshold)
        persist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-head', default=EXPECTED_HEAD, help='Exact expected revision or unique SHA prefix.')
    parser.add_argument('--live', action='store_true', help='Authorize real API calls locally; otherwise prepare only.')
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    os.chdir(API_ROOT)  # production .env resolution
    output = args.output or Path('/tmp/berlin-qac-v3-smoke.json' if args.live else '/tmp/berlin-qac-v3-preflight.json')
    report = {'question': QUESTION, 'language': LANGUAGE, 'status': 'PREPARED', 'live_executed': False,
              'openai_calls': 0, 'calls': [], 'contract_pipeline': 'NOT_RUN', 'contract_minimality': 'NOT_RUN',
              'research_coverage': 'NOT_RUN', 'scope_screen_note': 'Conservative smoke checks; inspect full contract and audit for semantic minimality.'}
    secrets = ()

    def persist():
        # Never serialize Settings, SDK clients, credentials or authentication headers.
        output.write_text(json.dumps(sanitized(report, secrets), ensure_ascii=False, indent=2) + '\n')
        output.chmod(0o600)

    try:
        head = subprocess.check_output(['git', '-C', str(REPO_ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        branch = subprocess.check_output(['git', '-C', str(REPO_ROOT), 'branch', '--show-current'], text=True).strip()
        report.update(runtime_head=head, branch=branch)
        if not head.startswith(args.expected_head) or branch != BRANCH:
            raise ValueError(f'Requires {BRANCH} @ {args.expected_head}; refusing to run on another revision.')
        evidence = args.evidence or (Path('/tmp/berlin-qac-live-v2.json') if Path('/tmp/berlin-qac-live-v2.json').exists() else FIXTURE)
        facts, metadata = load_evidence(evidence)
        report['evidence'] = metadata
        persist()
        if args.live:
            settings = resolve_settings()  # Keychain access ONLY after explicit local --live
            secrets = (settings.openai_api_key,)
            if not settings.openai_api_key:
                raise ValueError('Configured OpenAI API key unavailable.')
            report['live_executed'] = True
            run_live(settings, facts, report, persist)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - fail closed with sanitized report
        report.update(status='FAIL', error=sanitized({'exception_type': type(exc).__name__, 'message': str(exc)}, secrets))
    finally:
        persist()
    print(json.dumps(sanitized(report, secrets), ensure_ascii=False, indent=2))
    print(f'\nSanitized results: {output}')
    return 0 if report['status'] in {'PASS', 'PREPARED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
