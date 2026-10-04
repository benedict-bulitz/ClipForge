"""Read-only Topic Intelligence ranking diagnostic; run from apps/api with PYTHONPATH=.

Prints the top candidates with their sources, every score component, penalties,
the mass-audience quality gate, final score and rejection reasons.  Nothing is
written and no external call is made.

    PYTHONPATH=. python scripts/topic_diagnostics.py --status       # which state Home is in, and why
    PYTHONPATH=. python scripts/topic_diagnostics.py --status --live  # + live stage of the running API
    PYTHONPATH=. python scripts/topic_diagnostics.py --curated        # every curated candidate + rejecting gates
    PYTHONPATH=. python scripts/topic_diagnostics.py                # latest pool, top 20
    PYTHONPATH=. python scripts/topic_diagnostics.py --shown 9      # what Home was served, in order
    PYTHONPATH=. python scripts/topic_diagnostics.py --rescore      # + score with the current version
    PYTHONPATH=. python scripts/topic_diagnostics.py --json         # full structured output
    PYTHONPATH=. python scripts/topic_diagnostics.py --evaluate     # V2 real run: top 3 + Full Auto winner

``--evaluate`` is the one exception to "read-only": it runs the REAL Topic Intelligence path
(configured providers, the curator if an OpenAI key is set, provider caches and budgets)
exactly as Home and "Generate automatically" do - it never starts a generation.
"""

import argparse
import json

from sqlalchemy import select

from clipforge.config import get_settings
from clipforge.database import SessionLocal, prepare_schema
from clipforge.models import TopicDiscoveryRun
from clipforge.topic_intelligence import service

COLUMNS = (
    ("curiosity", "cur"), ("payoff", "pay"), ("knowledge_value", "know"), ("short_worthiness", "shrt"),
    ("demand", "dem"), ("trend", "trend"), ("outlier", "outl"), ("opportunity", "opp"), ("novelty", "nov"),
    ("channel_fit", "fit"), ("broad_appeal", "broad"), ("accessibility", "acc"),
    ("visual", "vis"), ("researchability", "res"), ("own_performance", "own"),
)


def _cell(component: dict | None) -> str:
    if not component or component.get("value") is None:
        return "   -"
    return f"{component['value']:4.2f}"


def _print_view(label: str, view: dict) -> None:
    components = view.get("components") or {}
    penalties = view.get("penalties") or {}
    quality = view.get("quality") or {}
    cells = " ".join(_cell(components.get(name)) for name, _short in COLUMNS)
    print(f"    {label:9} {view['final_score']:5.3f} {view['confidence'][:4]:4} {cells}")
    extras = []
    if quality:
        extras.append(f"quality={quality.get('mass_audience')} floor={quality.get('floor')} passed={quality.get('passed')}")
        if quality.get("exceptional_evidence"):
            extras.append(f"exceptional={','.join(quality['exceptional_evidence'])}")
    trend_factor = (components.get("trend") or {}).get("trend_quality_factor")
    if trend_factor is not None:
        extras.append(f"trend_factor={trend_factor}")
    if penalties.get("value"):
        extras.append(f"penalty={penalties.get('value')} {penalties.get('obscurity') or ''} {penalties.get('flags') or ''}".strip())
    sem = (quality or {}).get("semantic") or {}
    if sem:
        dims = " ".join(f"{name}={value}" for name, value in (sem.get("dimensions") or {}).items())
        extras.append(f"semantic={sem.get('status')} {dims} issues={sem.get('issues')} grounded={sem.get('grounded')} v={sem.get('curator_version')}")
    short = (quality or {}).get("short_worthiness") or {}
    if short.get("value") is not None:
        dims = " ".join(f"{name}={value}" for name, value in (short.get("dimensions") or {}).items())
        extras.append(f"short_worthiness={short.get('value')} (floor {short.get('floor')}, {short.get('basis')}) {dims} "
                      f"penalties={short.get('penalties') or {}} shape={short.get('shape_flags') or []}")
    if view.get("rejection_reasons"):
        extras.append(f"REJECTED: {', '.join(view['rejection_reasons'])}")
    if extras:
        print(f"    {'':9} {' | '.join(extras)}")


DIAGNOSES = {
    "discovery_running": "A discovery refresh is running right now; Home shows a loading note and asks again.",
    "warmup_failed": "The startup warm-up failed or timed out before a pool existed (see warmup.error).",
    "discovery_failed": "The last discovery failed (see the 'discovery' source error); the next request retries.",
    "discovery_timed_out": "The last discovery was abandoned at its hard time limit; the next request retries.",
    "no_pool_yet": "No discovery has run yet; the first Home visit starts one.",
    "provider_failure": "Every discovery source failed (see sources); Home says discovery is unavailable.",
    "stale_pool_other_version": "The latest pool was scored by an older score version; the next request re-discovers.",
    "pool_expired_refreshes_on_next_request": "The pool is older than its TTL; the next request refreshes it.",
    "question_transformation_failed": "Discovery worked, but most topics could not become a valid German question.",
    "quality_floor_rejected_all": "Discovery worked, but no candidate cleared the quality floor (see rejection reasons).",
    "prior_knowledge_rejected_all": "Discovery worked, but every question needed prior knowledge (universal 12+ gate).",
    "semantic_rejected_all": "The semantic curator rejected every candidate (see semantic issues/dimensions).",
    "local_strict_rejected_all": "No semantic validation available; the strict local rules accepted nothing.",
    "no_usable_candidates": "Discovery worked, but every candidate was rejected for other reasons.",
    "fewer_than_three_candidates": "Fewer than 3 candidates cleared the floor; Home shows only those plus a note.",
    "ok": "Enough candidates are available.",
}


def print_calibration(report: dict | None) -> None:
    if not report:
        print("no pool yet")
        return
    print(f"curated: {report['curated']} accepted: {report['accepted']} gates per candidate: {report['gates_per_candidate']}")
    print(f"thresholds: {report['thresholds']}")
    print(f"gate counts: {report['gate_counts']}")
    print(f"sole gate (one threshold decides): {report['sole_gate']}")
    for row in report["candidates"]:
        mark = "ACCEPTED" if row["accepted"] else "REJECTED"
        dims = " ".join(f"{name}={value}" for name, value in row["dimensions"].items())
        short = " ".join(f"{name}={value}" for name, value in row["short_dimensions"].items())
        print(f"\n[{mark}] {row['final_score']:.3f} {row['question']}\n    topic: {row['topic']}")
        print(f"    semantic: {dims}")
        print(f"    short: {short} -> short_worthiness={row['short_worthiness']} penalties={row['short_penalties']}")
        print(f"    issues={row['issues']} grounded={row['grounded']} gates={row['gates']}")


def status_report(settings, live: str | None) -> dict:
    """The running API's view (``--live``: flight, stage, warm-up live) or this process's DB-only view."""
    if live:
        import httpx

        return httpx.get(f"{live.rstrip('/')}/api/topic-intelligence/status", timeout=10).json()
    with SessionLocal() as db:
        return service.discovery_status(db, settings)


def print_status(settings, live: str | None = None) -> None:
    report = status_report(settings, live)
    print(f"diagnosis: {report['diagnosis']} - {DIAGNOSES.get(report['diagnosis'], '')}")
    print(f"score version: {report['current_score_version']} | discovery running: {report['discovery_running']}")
    flight = report.get("discovery") or {}
    scope = "running API" if live else "this process only - use --live for the running app"
    print(f"discovery ({scope}): stage={flight.get('discovery_stage')} provider={flight.get('active_provider')} "
          f"owner={flight.get('owner')} elapsed={flight.get('elapsed_seconds')}s stage_elapsed={flight.get('stage_elapsed_seconds')}s "
          f"limit={flight.get('hard_limit_seconds')}s timeouts={flight.get('timeouts_seconds')}")
    for stage in flight.get("stages") or []:
        print(f"  stage {stage['stage']:22} {stage.get('provider') or ''!s:22} {stage.get('seconds')}s {stage.get('status') or 'running'}")
    for event in flight.get("events") or []:
        print(f"  event {event['code']}: {event['message']}")
    if flight.get("last_error"):
        print(f"  last error: {flight['last_error']}")
    warmup = report["warmup"]
    print(f"warm-up ({scope}): {warmup['state']} result={warmup['result']} error={warmup['error']}")
    print(f"config: {report['config']}")
    run = report["run"] or {}
    print(f"latest pool: version={run.get('score_version')} status={run.get('status')} fresh={run.get('fresh')} "
          f"expires={run.get('expires_at')} quota_units={run.get('youtube_quota_units')}")
    for source in run.get("sources") or []:
        print(f"  source {source['name']:28} {source['status']:8} items={source.get('items')} calls={source.get('calls')} "
              f"units={source.get('quota_units')} {source.get('error') or ''}")
    pool = report["pool"] or {}
    print(f"pool: raw_topics={pool.get('raw_topics')} evaluated={pool.get('evaluated')} (budget {pool.get('evaluation_budget')}) "
          f"accepted={pool.get('accepted')} available={pool.get('available')} rejected={pool.get('rejected')} "
          f"transformation_failures={pool.get('transformation_failures')} statuses={pool.get('statuses')}")
    print(f"transformation: {pool.get('transformation')} (current version {pool.get('transformation_version')})")
    evaluation = pool.get("evaluation") or {}
    if evaluation:
        print(f"evaluation: {evaluation.get('evaluated')} evaluated, AI requests {evaluation.get('ai_requests')}/{evaluation.get('ai_request_budget')} "
              f"(combined curation + validation), "
              f"raw groups left {evaluation.get('remaining_raw_groups')}, broadened={evaluation.get('broadened')}, "
              f"prefiltered {evaluation.get('prefiltered', 0)} {evaluation.get('prefiltered_topics') or ''}")
    sem = pool.get("semantic_validation") or {}
    if sem:
        print(f"semantic curation: {sem.get('status')} enabled={sem.get('enabled')} version={sem.get('curator_version')} "
              f"requests={sem.get('requests')} curated={sem.get('curated')} cached={sem.get('cached')} {'; '.join(sem.get('errors') or [])}")
        for index, batch in enumerate(sem.get("batches") or [], 1):
            print(f"  curator request {index}: {batch.get('status')} size={batch.get('size')} {batch.get('seconds')}s "
                  f"tokens in/out/reasoning={batch.get('input_tokens')}/{batch.get('output_tokens')}/{batch.get('reasoning_tokens')} "
                  f"retry={batch.get('retry')} {batch.get('error') or ''}")
    if evaluation.get("unevaluated"):
        print(f"not evaluated (never 'low quality'): {evaluation['unevaluated']} "
              f"{[item['topic'] for item in evaluation.get('unevaluated_topics') or []][:8]}")
    if evaluation.get("curation_order"):
        print(f"curation order (batch size {evaluation.get('curator_batch_size')}):")
        for item in evaluation["curation_order"]:
            print(f"  {item['priority']:6.3f} {item['topic'][:70]} {item.get('penalties') or ''}")
    print(f"rejection reasons: {pool.get('rejection_reasons')}")

    def semantic_line(item: dict) -> str:
        info = item.get("semantic") or {}
        dims = info.get("dimensions") or {}
        short = {"self_contained_clarity": "clear", "clear_factual_payoff": "payoff", "universal_12plus_relevance": "12+",
                 "prior_knowledge_free": "no_prior", "natural_spoken_german": "german", "knowledge_short_fit": "short_fit"}
        cells = " ".join(f"{short[name]}={value}" for name, value in dims.items()) or "-"
        issues = ",".join(info.get("issues") or []) or "-"
        return f"semantic={info.get('status')} [{cells}] issues={issues} {info.get('reason') or ''}".rstrip()

    def line(item: dict) -> str:
        access = item.get("universal_accessibility")
        prior = ",".join(item.get("prior_knowledge") or []) or "-"
        return (f"{item['final_score']:.3f} access={access if access is not None else '-'} prior_knowledge={prior} "
                f"via={item.get('transformation')} src={','.join(item.get('sources') or [])}")

    print("accepted:")
    for item in pool.get("accepted_candidates") or []:
        print(f"  [{item['status']}] {item['question']}\n      {line(item)}\n      {semantic_line(item)}")
    print("rejected:")
    for item in pool.get("rejected_candidates") or []:
        print(f"  {item['question'] or '(no question) ' + item['topic']}  <- {', '.join(item['reasons'])}\n      {line(item)}\n      {semantic_line(item)}")
    print(f"semantic curation cache: {report.get('semantic_cache_entries')} topics")
    print("caches:")
    for cache in report["caches"]:
        print(f"  {cache['provider']:28} fresh={cache['fresh']} fetched={cache['fetched_at']} expires={cache['expires_at']}")


def evaluate(settings) -> dict:
    """The real path: Home's three suggestions, then Full Auto - with the evidence behind each."""
    from clipforge.integrations import get_secret_store
    from clipforge.youtube.routes import get_youtube_provider

    with SessionLocal() as db:
        deps = service.default_deps(db, settings, get_secret_store(), get_youtube_provider())
        chips = service.suggestions(db, settings, deps, count=3)
        auto = service.auto_topic(db, settings, deps)
        status = service.discovery_status(db, settings)
        rows = service.diagnostics(db, settings, limit=60)["candidates"]
    return {"suggestions": chips, "auto": auto, "sources": (status.get("run") or {}).get("sources"), "candidates": rows}


def print_evaluation(report: dict) -> None:
    by_id = {row["candidate_id"]: row for row in report["candidates"]}

    def block(index: int | str, item: dict) -> None:
        row = by_id.get(item["candidate_id"], {})
        v2 = row.get("v2") or {}
        persisted = row.get("persisted") or {}
        evidence = []
        for name in ("demand", "trend", "outlier", "opportunity"):
            component = (persisted.get("components") or {}).get(name) or {}
            if component.get("value") is not None:
                evidence.append(f"{name}={component['value']} ({component.get('confidence')})")
        print(f"{index}.\nQUESTION: {item['question']}\nCATEGORY: {item.get('signal_class') or 'no time/evergreen claim'}"
              f" ({item.get('signal_label') or '-'})\nWHY: {item.get('reason')}\n     ranked: {v2.get('selection_reason')}"
              f"\nEVIDENCE: {', '.join(evidence) or 'none (no live evidence)'}; freshness={v2.get('evidence_freshness') or {}}"
              f"; sources={','.join(row.get('sources') or [])}\nCONFIDENCE: {item.get('confidence')}\n")

    print("SOURCES:")
    for source in report.get("sources") or []:
        print(f"  {source['name']:28} {source['status']:8} items={source.get('items')} calls={source.get('calls')} {source.get('error') or ''}")
    chips = report["suggestions"]
    print(f"\nASSISTED ({chips['status']}):")
    for index, item in enumerate(chips.get("candidates") or [], 1):
        block(index, item)
    auto = report["auto"]
    print(f"AUTO WINNER ({auto['status']}):")
    if auto.get("candidate"):
        block("*", auto["candidate"])
    else:
        print(f"  {auto.get('message')} considered={auto.get('considered')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--shown", type=int, metavar="N", help="the first N candidates served to Home, in order")
    parser.add_argument("--rescore", action="store_true", help="also score with the current scoring version")
    parser.add_argument("--status", action="store_true", help="diagnosis, sources, pool, rejections, caches")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--curated", action="store_true",
                        help="every curated candidate: all v2 dimensions, short-worthiness, issues, rejecting gates")
    parser.add_argument("--evaluate", action="store_true", help="V2 real run: top 3 suggestions + Full Auto winner with evidence")
    parser.add_argument("--live", nargs="?", const="http://localhost:8000", metavar="API_URL",
                        help="with --status: read the running API (live discovery stage, warm-up, lock)")
    args = parser.parse_args()
    prepare_schema()
    settings = get_settings()
    if args.evaluate:
        report = evaluate(settings)
        if args.json:
            print(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        else:
            print_evaluation(report)
        return
    if args.curated:
        with SessionLocal() as db:
            run = db.scalar(select(TopicDiscoveryRun).where(TopicDiscoveryRun.status.not_in(("failed", "running")))
                            .order_by(TopicDiscoveryRun.sequence.desc()).limit(1))
            report = service.curation_calibration(db, run)
        if args.json:
            print(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        else:
            print_calibration(report)
        return
    if args.status:
        if args.json:
            print(json.dumps(status_report(settings, args.live), default=str, ensure_ascii=False, indent=2))
        else:
            print_status(settings, args.live)
        return
    with SessionLocal() as db:
        report = service.diagnostics(
            db, settings, limit=args.shown or args.limit, shown=bool(args.shown), rescore=args.rescore,
        )
    if args.json:
        print(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        return
    run = report["run"] or {}
    print(f"current score version: {report['current_score_version']} | latest pool: {run.get('score_version')} ({run.get('status')})")
    for source in run.get("sources") or []:
        print(f"  source {source['name']}: {source['status']} items={source.get('items')} units={source.get('quota_units')} {source.get('error') or ''}")
    header = " ".join(f"{short:>4}" for _name, short in COLUMNS)
    print(f"\n    {'':9} {'final':>5} conf {header}")
    for index, row in enumerate(report["candidates"], 1):
        print(f"{index:2}. {row['question']}  [{row['status']}]")
        print(f"    topic={row['topic']!r} sources={','.join(row['sources'])} niche={row['niche']} mechanism={row['mechanism']} via={row['transformation']}")
        _print_view("persisted", row["persisted"])
        v2 = row.get("v2") or {}
        print(f"    {'':9} v2: rank={v2.get('rank')} class={v2.get('signal_class')} family={v2.get('topic_family')} "
              f"origin={v2.get('origin')} auto={v2.get('auto')} duplicate_of={(v2.get('duplicate_of') or {}).get('question')}")
        print(f"    {'':9}     {v2.get('selection_reason')}")
        if "rescored" in row:
            _print_view("rescored", row["rescored"])


if __name__ == "__main__":
    main()
