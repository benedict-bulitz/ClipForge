"""Claim-specific evidence from the existing independent script verifier.

This module never calls a provider. Semantic approvals are usable only for the
exact narration, citation mapping and research dossier the verifier inspected.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .narration import split_sentences
from .verbal_hook import _related, proposition_words


class ClaimGrounding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_index: int = Field(ge=1, le=40)
    sentence: str = Field(min_length=1, max_length=600)
    status: Literal["supported", "unsupported", "uncertain", "nonfactual"]
    supporting_fact_ids: list[str] = Field(default_factory=list, max_length=8)
    covers_all_claims: bool
    reasoning: str = Field(max_length=320)


def evidence_key(blocks: list[dict[str, Any]], facts: list[dict[str, Any]]) -> str:
    payload = {
        "blocks": [[block.get("id"), block.get("role"), block.get("text"), block.get("fact_ids") or []]
                   for block in blocks],
        "facts": [{key: fact.get(key) for key in ("id", "claim", "verification", "confidence", "sources")}
                  for fact in facts if isinstance(fact, dict)],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def grounding_request(blocks: list[dict[str, Any]], usable: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "beat_index": index, "role": block.get("role"), "sentences": split_sentences(str(block.get("text") or "")),
        "fact_ids": list(block.get("fact_ids") or []),
        "cited_facts": [usable[item] for item in block.get("fact_ids") or [] if item in usable],
    } for index, block in enumerate(blocks, 1)]


def hook_has_assertion(text: str) -> bool:
    # Pure questions/imperatives can be rhetorical. Their presuppositions are
    # still checked by the independent verifier, never authorized by this test.
    return any(not sentence.rstrip().endswith("?") and not re.match(
        r"(?i)^(?:stell dir vor|denk\w* (?:dir|an)|schau\w*|sieh\w*|imagine|look|watch|consider)\b", sentence,
    ) for sentence in split_sentences(text))


def narrated_fact_ids(block: dict[str, Any], facts: list[dict[str, Any]] | None = None) -> list[str]:
    """Hook citations ground its claims, not necessarily the entire source fact.

    Count a hook as narrating a full fact only when it actually states all of
    that native fact's sentences. Textual spoiler checks remain independent.
    """
    ids = list(block.get("fact_ids") or [])
    if str(block.get("role") or "").casefold() != "hook":
        return ids
    def normalized(value: str) -> str:
        return " ".join(value.casefold().split()).rstrip(".!?")
    said = {normalized(sentence) for sentence in split_sentences(str(block.get("text") or ""))
            if not sentence.rstrip().endswith("?")}
    return [str(fact["id"]) for fact in facts or [] if fact.get("id") in ids
            and split_sentences(str(fact.get("claim") or ""))
            and all(normalized(sentence) in said for sentence in split_sentences(str(fact.get("claim") or "")))]


def contradicts_citation(text: str, claims: list[str]) -> bool:
    """An explicit polarity reversal of the same single proposition is hard.

    Conditional/contrastive negation needs semantic review; it is not safely
    decided by stripping a negator. Other semantic contradictions stay with
    the independent verifier and cannot be overridden by a positive audit.
    """
    negator = re.compile(r"(?i)\b(?:not|nicht)\b")
    conditional = re.compile(r"(?i)\b(?:only|nur|until|bis|unless|wenn|if|aber|but)\b")
    if len(split_sentences(text)) != 1 or conditional.search(text):
        return False
    def words_without_negation(value: str) -> set[str]:
        words = proposition_words(re.sub(r"(?i)\b(?:do|does|did)\b", "", negator.sub("", value)))
        return {word.removesuffix("s") if len(word) > 4 else word for word in words}
    said = words_without_negation(text)
    for claim in claims:
        if len(split_sentences(claim)) != 1 or conditional.search(claim):
            continue
        words = words_without_negation(claim)
        if (said and words and bool(negator.search(text)) != bool(negator.search(claim))
                and len(_related(said, words)) / len(said) >= .9
                and len(_related(words, said)) / len(words) >= .9):
            return True
    return False


def check_claim_grounding(
    entries: list[ClaimGrounding], blocks: list[dict[str, Any]], usable: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Structural/negative results are hard; a generic grounded flag cannot override them."""
    failures: list[dict[str, Any]] = []
    expected = {(index, sentence) for index, block in enumerate(blocks, 1)
                for sentence in split_sentences(str(block.get("text") or ""))}
    seen: set[tuple[int, str]] = set()
    for entry in entries:
        key = (entry.beat_index, entry.sentence)
        block = blocks[entry.beat_index - 1] if entry.beat_index <= len(blocks) else {}
        ids = set(entry.supporting_fact_ids)
        code = None
        if key not in expected or key in seen:
            code = "claim_grounding_mismatch"
        elif ids - set(block.get("fact_ids") or []) or ids - set(usable):
            code = "claim_citation_mismatch"
        elif entry.status in {"unsupported", "uncertain"}:
            code = "claim_unsupported" if entry.status == "unsupported" else "claim_unverifiable"
        elif not entry.covers_all_claims or (entry.status == "supported" and not ids):
            code = "claim_grounding_incomplete"
        elif entry.status == "nonfactual" and ids:
            code = "claim_citation_mismatch"
        seen.add(key)
        if code:
            failures.append({"code": code, "severity": "hard", "beat_index": entry.beat_index,
                             "message": entry.reasoning or "Claim-specific verification does not match the candidate.",
                             "source": "verifier"})
    if entries:
        for index, sentence in sorted(expected - seen):
            failures.append({"code": "claim_grounding_incomplete", "severity": "hard", "beat_index": index,
                             "message": "Independent grounding omitted a sentence: " + sentence[:180], "source": "verifier"})
        for index, block in enumerate(blocks, 1):
            supported = {identifier for entry in entries if entry.beat_index == index
                         for identifier in entry.supporting_fact_ids}
            unused = set(block.get("fact_ids") or []) - supported
            if unused:
                failures.append({"code": "claim_citation_mismatch", "severity": "hard", "beat_index": index,
                                 "message": "Citations supporting none of this beat's verified claims: " + ", ".join(sorted(unused)), "source": "verifier"})
    return failures


def semantic_entries(
    blocks: list[dict[str, Any]], facts: list[dict[str, Any]], audit: dict[str, Any],
) -> dict[tuple[int, str], dict[str, Any]]:
    grounding = audit.get("claim_grounding") or {}
    if (audit.get("source") != "script_story_verifier" or grounding.get("approved") is not True
            or grounding.get("evidence_key") != evidence_key(blocks, facts)):
        return {}
    return {(entry["beat_index"], entry["sentence"]): entry for entry in grounding.get("evaluations") or []
            if entry.get("status") in {"supported", "nonfactual"} and entry.get("covers_all_claims") is True}


def hook_semantically_supported(blocks: list[dict[str, Any]], facts: list[dict[str, Any]], audit: dict[str, Any]) -> bool:
    entries = semantic_entries(blocks, facts, audit)
    hooks = [(index, block) for index, block in enumerate(blocks, 1)
             if str(block.get("role") or "").casefold() == "hook"]
    if len(hooks) != 1:
        return False
    index, hook = hooks[0]
    sentences = split_sentences(str(hook.get("text") or ""))
    reviewed = [entries.get((index, sentence), {}) for sentence in sentences]
    return bool(reviewed) and any(item.get("status") == "supported" for item in reviewed) and all(
        item.get("status") == "nonfactual" or (item.get("status") == "supported" and item.get("supporting_fact_ids"))
        for item in reviewed
    )


def sentence_citations(
    text: str, fact_ids: list[str], facts: list[dict[str, Any]], *, native_only: bool = False,
) -> list[list[str]] | None:
    """Keep one existing citation; split unions only on native sentence matches.

    Retaining a single original citation does not certify support: every child
    claim still needs independent verification. Ambiguous citation unions stay
    intact for claim review; lexical overlap never invents a source mapping.
    """
    by_id = {str(fact.get("id")): str(fact.get("claim") or "") for fact in facts}
    def normalized(value: str) -> str:
        return " ".join(value.casefold().split()).rstrip(".!?")
    mapped = []
    for sentence in split_sentences(text):
        ids = [identifier for identifier in fact_ids if any(
            normalized(sentence) == normalized(claim) for claim in split_sentences(by_id.get(identifier, ""))
        )]
        if not ids:
            if native_only or len(fact_ids) != 1:
                return None
            ids = list(fact_ids)
        mapped.append(ids)
    return mapped
