"""Research Pipeline V2 audit; run from apps/api with PYTHONPATH=.

Two read-only modes (nothing is ever written to a project):

  --question "Warum ist der Himmel auf dem Mars rot?" [--compare-v1]
      Run the real research path for one question and print the sources,
      source types, core answer, mechanism, key evidence, confidence and
      every failure/fallback.  --compare-v1 also runs the old snippet path.

  --projects [N] | --project <id>
      For saved projects (default: the 6 most recent), print the OLD research
      package stored in the first revision next to a fresh V2 run for the
      same prompt (network required).  --no-network prints the old packages
      only.

Uses the app's settings (keyring/.env): BRAVE_SEARCH_API_KEY and, with
CLIPFORGE_AI_MODE=openai, the validated LLM decomposition/synthesis.
"""
from __future__ import annotations

import argparse
import json
import sys

from clipforge.config import get_settings
from clipforge.research import _research_topic_v1, research_topic


def _short(text: object, limit: int = 150) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def print_v2(question: str, language: str) -> dict:
    settings = get_settings()
    result = research_topic(question, language, settings, context={"question": question})
    package = result.package or {}
    diagnostics = result.diagnostics or {}
    print(f"QUESTION     {question}")
    print(f"PROVIDER     {result.provider} status={result.status} error={result.error}")
    if diagnostics.get("v2_error"):
        print(f"V2 ERROR     {diagnostics['v2_error']} -> fell back to V1")
        return {}
    route = package.get("route") or {}
    print(f"ROUTE        {route.get('domain')} preferred={route.get('preferred_types')}")
    print(f"SUB-QUESTIONS {[(item['kind'], item['query']) for item in package.get('sub_questions') or []]} "
          f"({diagnostics.get('decomposition')})")
    print("SOURCES USED")
    for source in (package.get("source_summary") or {}).get("sources") or []:
        print(f"  {source['id']} {source['source_type']:<20} {source['authority']:<7} {source.get('published_at') or '-':<10} {source['url']}")
    print(f"SOURCE TYPES {(package.get('source_summary') or {}).get('types')} "
          f"independent={(package.get('source_summary') or {}).get('independent')}")
    core = package.get("core_answer") or {}
    print(f"CORE ANSWER  {_short(core.get('text'), 300)} [{core.get('verification')}, {core.get('independent_sources')} indep., "
          f"{core.get('evidence_ids')}]")
    spine = package.get("explanation_spine") or {}
    print(f"MECHANISM    status={spine.get('status')}")
    for ref in (spine.get("why_it_happens") or []) + (spine.get("how_it_works") or []):
        print(f"  - {_short(ref['text'], 260)} [{ref['verification']}, {ref['evidence_ids']}]")
    for section in ("numbers_dates", "misconceptions", "caveats", "supporting_facts"):
        for ref in package.get(section) or []:
            print(f"{section.upper():<13}{_short(ref['text'], 240)} [{ref['verification']}]")
    print("KEY EVIDENCE")
    for item in (package.get("evidence") or [])[:8]:
        print(f"  {item['id']} {item['source_id']} {item['kind']:<13} {_short(item['text'], 200)}")
    print(f"CONFIDENCE   {package.get('confidence')} status={package.get('status')} gaps={package.get('gaps')}")
    if package.get("contradictions"):
        print(f"CONTRADICTIONS {json.dumps(package['contradictions'], ensure_ascii=False)[:600]}")
    print("FAILURES/FALLBACKS")
    for call in (diagnostics.get("discovery") or {}).get("calls") or []:
        if call.get("status") != "ok":
            print(f"  discovery {call['provider']} {call['status']} {call.get('error') or ''}")
    for item in diagnostics.get("failures") or []:
        print(f"  retrieval {item['status']:<18} {item.get('http_status') or ''} {item['url']} {item.get('error') or ''}")
    for item in (package.get("rejected_claims") or [])[:6]:
        print(f"  rejected  {item.get('reason')}: {_short(item.get('text'), 120)}")
    print(f"BUDGET       {diagnostics.get('budget')} elapsed={diagnostics.get('elapsed_ms')} ms "
          f"extractor={diagnostics.get('extractor')} synthesis={diagnostics.get('synthesis')}")
    return package


def print_v1(question: str, language: str) -> None:
    result = _research_topic_v1(question, language, get_settings())
    print(f"V1           provider={result.provider} status={result.status} error={result.error}")
    for fact in result.facts:
        print(f"  {fact['id']} {_short(fact['claim'], 200)} <- {[source.get('url') for source in fact.get('sources') or []]}")


def print_old_package(state: dict) -> None:
    research = state.get("research") or {}
    print(f"OLD          provider={research.get('provider')} status={research.get('status')} sources={len(research.get('sources') or [])}")
    for fact in state.get("facts") or []:
        print(f"  {fact.get('id')} {fact.get('verification')} {_short(fact.get('claim'), 200)}")
        if fact.get("raw_claim") and fact.get("raw_claim") != fact.get("claim"):
            print(f"      raw: {_short(fact['raw_claim'], 200)}")
        for source in fact.get("sources") or []:
            print(f"      <- {source.get('url')}")
    spine = ((state.get("story_arc") or {}).get("question_contract") or {}).get("explanation_spine") or {}
    print(f"OLD SPINE    status={spine.get('status')} mechanism={spine.get('mechanism')}")
    readiness = (state.get("script") or {}).get("readiness") or {}
    print(f"OLD READY    {readiness.get('status')} attempts={len(research.get('attempts') or [])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", action="append", default=[])
    parser.add_argument("--language", default="de")
    parser.add_argument("--compare-v1", action="store_true")
    parser.add_argument("--projects", type=int, nargs="?", const=6)
    parser.add_argument("--project")
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args()
    for question in args.question:
        print("=" * 100)
        print_v2(question, args.language)
        if args.compare_v1:
            print_v1(question, args.language)
    if args.projects or args.project:
        from sqlalchemy import select

        from clipforge.database import SessionLocal
        from clipforge.models import Project, ProjectRevision

        with SessionLocal() as session:
            query = select(Project).order_by(Project.created_at.desc()).limit(args.projects or 1)
            if args.project:
                query = select(Project).where(Project.id == args.project)
            for project in session.scalars(query).all():
                revision = session.scalars(
                    select(ProjectRevision).where(ProjectRevision.project_id == project.id).order_by(ProjectRevision.number)
                ).first()
                state = dict(revision.state) if revision is not None else {}
                language = str((state.get("intent") or {}).get("language") or args.language)
                print("=" * 100)
                print(f"PROJECT      {project.id} {project.original_prompt}")
                print_old_package(state)
                if not args.no_network:
                    print("-" * 40 + " V2")
                    print_v2(project.original_prompt, language)
            session.rollback()  # read-only
    if not (args.question or args.projects or args.project):
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
