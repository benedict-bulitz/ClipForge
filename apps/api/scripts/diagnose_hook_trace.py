"""Read-only verbal-hook trace per revision; run from apps/api with PYTHONPATH=.

Prints, for a project (default: the most recently created one), the format
decision, raw vs sanitized research, the Story Arc reveal contract with its
answer consistency, the hook generation (retry) status, every Triple Hook
candidate with its assessment, the fallback reason, and the opening at each
persisted revision: hook block, next block, narration, TTS input and
captions.  Nothing is written.
"""

import argparse
import re

from sqlalchemy import select

from clipforge.database import SessionLocal
from clipforge.models import Project, ProjectRevision
from clipforge.narration import clean_narration_text, clean_research_claim
from clipforge.story_arc import comparison_verdicts


def _first_sentences(text: object, count: int = 2) -> str:
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", str(text or "").strip()) if part]
    return " ".join(sentences[:count])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", nargs="?")
    args = parser.parse_args()
    with SessionLocal() as session:
        query = select(Project).order_by(Project.created_at.desc())
        if args.project_id:
            query = select(Project).where(Project.id == args.project_id)
        project = session.scalars(query).first()
        if project is None:
            parser.error("Project not found")
        revisions = session.scalars(
            select(ProjectRevision).where(ProjectRevision.project_id == project.id).order_by(ProjectRevision.number)
        ).all()
        print(f"project {project.id}\nprompt  {project.original_prompt}")
        first = revisions[0].state if revisions else {}
        arc = first.get("story_arc") or {}
        fmt = first.get("format_plan") or {}
        print(f"format  {fmt.get('selected_format')} reason={fmt.get('selection_reason')} "
              f"research_mentions_ranking={fmt.get('research_mentions_ranking')}")
        facts = first.get("facts") or []
        for fact in facts:
            raw = fact.get("raw_claim")
            print(f"fact    {fact.get('id')}: {fact.get('claim')}")
            if raw and raw != fact.get("claim"):
                print(f"  raw   {raw}")
            elif raw is None and clean_research_claim(fact.get("claim")) != fact.get("claim"):
                print(f"  now   {clean_research_claim(fact.get('claim'))!r} (current sanitizer)")
        primary_id = arc.get("primary_answer_id")
        primary = next((unit.get("claim") for unit in arc.get("units") or [] if unit.get("id") == primary_id), None)
        question = str(arc.get("primary_question") or "")
        _winner, verdicts = comparison_verdicts(facts, question)
        print(f"arc     primary_question={question!r}")
        print(f"        primary_answer_id={primary_id} final_payoff_id={arc.get('final_payoff_id')} "
              f"withhold={(arc.get('curiosity_gap') or {}).get('withhold_answer')} protected={(arc.get('hook') or {}).get('protected_ids')}")
        print(f"        primary_answer={primary!r}")
        print(f"        answer_subject={arc.get('answer_subject')} hook_must_not_reveal="
              f"{(first.get('payoff_plan') or {}).get('hook_must_not_reveal')!r} repairs={arc.get('repairs')}")
        if verdicts:
            consistent = primary_id in verdicts
            print(f"        answer consistency: facts stating the winner={sorted(verdicts)} primary states it={consistent}")
        plan = (first.get("script") or {}).get("triple_hook") or {}
        selection = plan.get("selection") or {}
        generation = (first.get("script") or {}).get("hook_generation") or {}
        print(f"hook generation status={generation.get('status')} attempts={generation.get('attempts', 1)} "
              f"retry={generation.get('retry_reason')} error={str(generation.get('error') or '')[:160]!r}")
        print(f"judge   {selection.get('judge')}")
        for item in selection.get("candidates") or []:
            print(f"cand    {item.get('id')} origin={item.get('origin')} {item.get('strategy')} score={item.get('score')} "
                  f"eligible={item.get('eligible')} hard={item.get('hard_fail')} codes={item.get('reason_codes')} "
                  f"facts={item.get('supported_by_fact_ids')} | {item.get('verbal_hook')}")
        fallback = plan.get("status") == "fallback"
        print(f"selected {plan.get('verbal_hook')!r} strategy={plan.get('selected_strategy')} source={plan.get('source')} "
              f"verbal_origin={plan.get('verbal_origin')}"
              + (f" fallback_reason={'no_ai_candidates' if not selection.get('candidates') else 'no_complete_triple_passed'}" if fallback else ""))
        print("\nREV | KIND | HOOK BLOCK | NEXT BLOCK | PLAN VERBAL HOOK | NARRATION (2) | TTS (2) | CAPTIONS (2)")
        for revision in revisions:
            state = revision.state or {}
            script = state.get("script") or {}
            blocks = script.get("blocks") or []
            hook = next((block.get("text") for block in blocks if str(block.get("role")).casefold() == "hook"), None)
            following = f"{blocks[1].get('role')}:{blocks[1].get('text')}" if len(blocks) > 1 else None
            captions = " ".join(str(item.get("text") or "") for item in (state.get("captions") or {}).get("items") or [])
            print(" | ".join(str(value) for value in (
                revision.number, revision.kind, hook, following, (script.get("triple_hook") or {}).get("verbal_hook"),
                _first_sentences(script.get("text")), _first_sentences(clean_narration_text(str(script.get("text") or ""))),
                _first_sentences(captions),
            )))


if __name__ == "__main__":
    main()
