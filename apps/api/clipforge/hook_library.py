"""Validated, cached access to the editable hook strategy library."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


class HookLibraryError(ValueError):
    """Raised when the configured hook library cannot be used safely."""


@dataclass(frozen=True)
class HookOpportunityProfile:
    strongest_fact: str | None
    surprising_fact: str | None
    counterintuitive_angle: str | None
    misconception: str | None
    viewer_consequence: str | None
    practical_benefit: str | None
    practical_risk: str | None
    supported_statistic: str | None
    supported_trend: str | None
    contrast: str | None
    challengeable_assumption: str | None
    social_proof_evidence: str | None
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
        }


def _library_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "hook_library.json"


def _required_text(record: dict[str, Any], field: str, context: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HookLibraryError(f"{context}.{field} must be a non-empty string")
    return value.strip()


def _validate_library(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HookLibraryError("Hook library root must be an object")
    version = data.get("schema_version")
    if not isinstance(version, str) or not version.strip():
        raise HookLibraryError("Hook library schema_version must be a non-empty string")
    families = data.get("strategy_families")
    templates = data.get("fallback_templates")
    if not isinstance(families, list) or not families:
        raise HookLibraryError("Hook library must define strategy_families")
    if not isinstance(templates, list):
        raise HookLibraryError("Hook library fallback_templates must be a list")

    family_ids: set[str] = set()
    for index, family in enumerate(families):
        if not isinstance(family, dict):
            raise HookLibraryError(f"strategy_families[{index}] must be an object")
        family_id = _required_text(family, "id", f"strategy_families[{index}]")
        if family_id in family_ids:
            raise HookLibraryError(f"Duplicate hook strategy id: {family_id}")
        family_ids.add(family_id)
        _required_text(family, "primary_pattern", f"strategy_families[{index}]")
        for field in ("use_when", "avoid_when"):
            values = family.get(field)
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise HookLibraryError(
                    f"strategy_families[{index}].{field} must be a list of strings"
                )
        _required_text(family, "evidence_policy", f"strategy_families[{index}]")

    template_ids: set[str] = set()
    for index, template in enumerate(templates):
        if not isinstance(template, dict):
            raise HookLibraryError(f"fallback_templates[{index}] must be an object")
        template_id = _required_text(template, "id", f"fallback_templates[{index}]")
        if template_id in template_ids:
            raise HookLibraryError(f"Duplicate hook template id: {template_id}")
        template_ids.add(template_id)
        family = _required_text(template, "family", f"fallback_templates[{index}]")
        if family not in family_ids:
            raise HookLibraryError(
                f"fallback_templates[{index}].family references unknown strategy {family}"
            )
        _required_text(template, "source_text_de", f"fallback_templates[{index}]")
        _required_text(template, "evidence_gate", f"fallback_templates[{index}]")

    return data


@lru_cache(maxsize=1)
def load_hook_library() -> dict[str, Any]:
    path = _library_path()
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise HookLibraryError(f"Hook library not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HookLibraryError(f"Hook library is invalid JSON: {exc.msg}") from exc
    return _validate_library(data)


def strategy_families() -> list[dict[str, Any]]:
    return list(load_hook_library()["strategy_families"])


def fallback_templates(family: str | None = None) -> list[dict[str, Any]]:
    templates = load_hook_library()["fallback_templates"]
    if family is None:
        return list(templates)
    return [item for item in templates if item.get("family") == family]


def _claims(facts: list[dict[str, Any]]) -> list[str]:
    import re

    from .narration import clean_research_claim

    claims: list[str] = []
    for fact in facts:
        if (
            not fact.get("claim")
            or not fact.get("sources")
            or fact.get("verification") not in {"source_attributed", "source_snippet"}
        ):
            continue
        cleaned = clean_research_claim(fact.get("claim"))
        claims.extend(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
            if len(sentence.split()) >= 4
        )
    return claims


def analyze_hook_opportunities(facts: list[dict[str, Any]]) -> HookOpportunityProfile:
    """Extract only evidence-backed rhetorical opportunities; absent signals stay null."""
    import re

    claims = _claims(facts)
    def first(pattern: str) -> str | None:
        return next((claim for claim in claims if re.search(pattern, claim, re.IGNORECASE)), None)

    statistic = first(r"(?<!\w)\d+(?:[.,]\d+)?\s*%?")
    trend = first(
        r"\b(?:currently|lately|increasingly|adopted|adoption|trend|trending|"
        r"aktuell|zunehmend|im trend|immer mehr)\b"
    )
    misconception = first(
        r"\b(?:not damaged|not broken|not a defect|no damage|"
        r"nicht beschädigt|kein schaden|kein defekt|nicht kaputt|"
        r"misconception|misunderstanding|mistake|error|wrong|"
        r"irrtum|missverständnis|fehler|falsch)\b"
    )
    contrast = first(
        r"\b(?:not|rather than|instead of|although|despite|nicht|sondern|"
        r"statt|obwohl|entgegen|faster than|slower than|more .* than|"
        r"less .* than|kälter als|wärmer als)\b"
    )
    consequence = first(
        r"(?:\b(?:helps?|protects?|reduces?|prevents?|handles?|improves?|"
        r"equaliz|distribut|costs?|saves?|avoids?|hilft|schützt|senkt|"
        r"verhindert|kostet|spart|vermeidet)\b|"
        r"\w*(?:regulier|ausgleich|verteil)\w*)"
    )
    strongest = next(
        (
            claim
            for claim in claims
            if re.search(
                r"(?:\b(?:helps?|protects?|reduces?|prevents?|equaliz|"
                r"distribut|hilft|schützt|senkt|verhindert)\b|"
                r"\w*(?:ausgleich|regulier|verteil)\w*)",
                claim,
                re.IGNORECASE,
            )
        ),
        claims[0] if claims else None,
    )
    surprising = misconception or contrast or consequence
    return HookOpportunityProfile(
        strongest_fact=strongest,
        surprising_fact=surprising,
        counterintuitive_angle=contrast,
        misconception=misconception,
        viewer_consequence=consequence,
        practical_benefit=consequence,
        practical_risk=None,
        supported_statistic=statistic,
        supported_trend=trend,
        contrast=contrast,
        challengeable_assumption=misconception or contrast,
        social_proof_evidence=trend,
        confidence=round(min(1.0, 0.4 + len(claims) * 0.1), 2),
    )


def eligible_strategy_ids(profile: HookOpportunityProfile) -> list[str]:
    """Return a small, evidence-driven subset of library families."""
    eligible: list[str] = []
    if profile.contrast or profile.challengeable_assumption:
        eligible.extend(["direct_confrontation", "direct_reframe", "hot_take"])
    if profile.misconception:
        eligible.append("ego_challenge")
    if profile.supported_statistic:
        eligible.append("shock_number")
    if profile.supported_trend:
        eligible.append("social_proof")
    if profile.viewer_consequence:
        eligible.extend(["direct_confrontation", "high_stakes_consequence"])
    result: list[str] = []
    for strategy in eligible:
        if strategy not in result:
            result.append(strategy)
    return result[:5]


def strategy_guidance(profile: HookOpportunityProfile) -> list[dict[str, Any]]:
    eligible = set(eligible_strategy_ids(profile))
    return [
        {
            "id": family["id"],
            "primary_pattern": family["primary_pattern"],
            "use_when": family["use_when"],
            "avoid_when": family["avoid_when"],
            "evidence_policy": family["evidence_policy"],
        }
        for family in strategy_families()
        if family["id"] in eligible
    ]
