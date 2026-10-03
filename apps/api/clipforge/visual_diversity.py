"""Cheap, bounded tie preferences; never an admission or semantic authority.

Only captions/structured direction describe a concept. Provider identity,
search provenance and result rank are deliberately excluded.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

MAX_NOVELTY_PENALTY = 0.008
COMPARABLE_SCENE_MARGIN = 0.01
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "and",
        "with",
        "for",
        "from",
        "is",
        "are",
        "by",
        "photo",
        "photograph",
        "image",
        "view",
        "showing",
        "shows",
        "this",
        "that",
        "scene",
        "close",
        "up",
        "documentary",
        "generated",
        "ai",
        "realistic",
    ]
)


def concept(media: Any) -> dict[str, Any]:
    def value(key: str) -> Any:
        return media.get(key) if isinstance(media, dict) else getattr(media, key, None)

    # Generated titles already contain the accepted visual description. Do not
    # use full prompts (style boilerplate), topic queries or provider tags.
    caption = str(value("title") or value("description") or "")[:320]
    structured = value("visual_concept") or {}
    evidence = (
        " ".join(
            str(structured.get(key) or "")[:140]
            for key in ("subject", "action", "environment", "shot")
        )
        if structured
        else caption
    )
    tokens = list(
        dict.fromkeys(w for w in re.findall(r"[^\W\d_]{3,}", evidence.casefold()) if w not in _STOP)
    )[:32]
    return {
        "summary": caption[:160],
        "terms": tokens,
        "subject": structured.get("subject") or caption[:120],
        "action": structured.get("action"),
        "environment": structured.get("environment"),
        "shot": structured.get("shot"),
        "explanatory_role": value("story_role"),
    }


def previous_visuals(scene: dict, state: dict) -> list[dict]:
    scenes = state.get("scenes") or []
    index = next(
        (i for i, s in enumerate(scenes) if s is scene or s.get("id") == scene.get("id")), 0
    )
    return [s for s in scenes[max(0, index - 2) : index] if isinstance(s.get("media"), dict)]


def novelty_evidence(media: Any, scene: dict, state: dict) -> dict:
    profile = concept(media)
    previous = previous_visuals(scene, state)
    penalty, exempt = 0.0, False
    identity = media.get("identity") if isinstance(media, dict) else media.identity
    for prior in previous:
        old = prior["media"]
        # Splits of one script statement deliberately keep the base visual.
        if scene.get("block_id") and prior.get("block_id") == scene["block_id"]:
            exempt = True
            continue
        terms, old_terms = set(profile["terms"]), set(concept(old)["terms"])
        overlap = len(terms & old_terms) / max(1, len(terms | old_terms))
        if identity and identity == old.get("identity"):
            penalty = max(penalty, MAX_NOVELTY_PENALTY)
        elif len(terms & old_terms) >= 3 and overlap >= 0.5:
            penalty = max(penalty, round(MAX_NOVELTY_PENALTY * overlap, 6))
    if exempt:
        penalty = 0.0
    return {
        "previous_concept": concept(previous[-1]["media"])["summary"] if previous else None,
        "candidate_concept": profile,
        "penalty": penalty,
        "continuity_exemption": exempt,
    }


def prefer_useful_novelty(
    rows: list, scene: dict, state: dict, *, coverage: Callable[[Any], Any] | None = None
) -> list:
    """Reorder already ranked/admitted rows only within close semantic peers.

    Same tier, same coverage, <= .01 CLIP gap and <= 4 metadata points.
    The <= .008 penalty cannot reorder a materially stronger candidate.
    No new requests, embeddings, admissions or threshold changes.
    """
    pending = list(rows)
    result = []
    for candidate, relevance in pending:
        relevance["diversity"] = novelty_evidence(candidate, scene, state)
    while pending:
        leader = pending[0]
        visual = leader[1].get("visual") or {}
        score = visual.get("scene_score")

        def comparable(row: Any, leader: Any = leader, score: Any = score) -> bool:
            r = row[1]
            other = (r.get("visual") or {}).get("scene_score")
            return (
                r.get("selection_tier") == leader[1].get("selection_tier")
                and row[0].kind == leader[0].kind
                and (coverage is None or coverage(row) == coverage(leader))
                and abs(float(r.get("score") or 0) - float(leader[1].get("score") or 0)) <= 4
                and (
                    (score is None and other is None)
                    or (
                        score is not None
                        and other is not None
                        and abs(float(score) - float(other)) <= COMPARABLE_SCENE_MARGIN
                    )
                )
            )

        peers = [row for row in pending if comparable(row)]
        # Metadata-only ties also remain bounded to the same metadata score.
        winner = max(
            peers,
            key=lambda row: (
                float((row[1].get("visual") or {}).get("scene_score") or 0)
                - row[1]["diversity"]["penalty"]
                if score is not None
                else float(row[1].get("score") or 0) - row[1]["diversity"]["penalty"]
            ),
        )
        winner[1]["diversity"]["selection_reason"] = (
            "useful_novelty_among_semantic_peers"
            if winner is not leader
            else "semantic_rank_preserved"
        )
        result.append(winner)
        pending.remove(winner)
    return result


def translation_context(scene: dict, state: dict) -> dict:
    """Bounded preceding thought for the existing translator, not a new planner."""
    previous = previous_visuals(scene, state)
    prior = next(
        (s for s in reversed(previous) if s.get("block_id") != scene.get("block_id")), None
    )
    return {
        "previous_visual": concept(prior["media"])["summary"] if prior else "",
        "previous_statement": str(prior.get("narration") or "")[:300] if prior else "",
        "same_statement_continuity": bool(
            previous and scene.get("block_id") and previous[-1].get("block_id") == scene["block_id"]
        ),
    }
