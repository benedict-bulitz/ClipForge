"""Read-only Topic Intelligence ranking diagnostic; run from apps/api with PYTHONPATH=.

Prints the top candidates with their sources, every score component, penalties,
the mass-audience quality gate, final score and rejection reasons.  Nothing is
written and no external call is made.

    PYTHONPATH=. python scripts/topic_diagnostics.py                # latest pool, top 20
    PYTHONPATH=. python scripts/topic_diagnostics.py --shown 9      # what Home was served, in order
    PYTHONPATH=. python scripts/topic_diagnostics.py --rescore      # + score with the current version
    PYTHONPATH=. python scripts/topic_diagnostics.py --json         # full structured output
"""

import argparse
import json

from clipforge.config import get_settings
from clipforge.database import SessionLocal, prepare_schema
from clipforge.topic_intelligence import service

COLUMNS = (
    ("trend", "trend"), ("outlier", "outl"), ("competition", "comp"), ("novelty", "nov"),
    ("channel_fit", "fit"), ("suitability", "suit"), ("broad_appeal", "broad"), ("accessibility", "acc"),
    ("question_form", "form"), ("visual", "vis"), ("researchability", "res"), ("own_performance", "own"),
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
    if view.get("rejection_reasons"):
        extras.append(f"REJECTED: {', '.join(view['rejection_reasons'])}")
    if extras:
        print(f"    {'':9} {' | '.join(extras)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--shown", type=int, metavar="N", help="the first N candidates served to Home, in order")
    parser.add_argument("--rescore", action="store_true", help="also score with the current scoring version")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    prepare_schema()
    settings = get_settings()
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
        if "rescored" in row:
            _print_view("rescored", row["rescored"])


if __name__ == "__main__":
    main()
