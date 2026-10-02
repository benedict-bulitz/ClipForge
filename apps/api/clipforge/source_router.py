"""Broad source suitability, never a relevance or rights authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .visual_context import HISTORICAL_WORDS, positive_intent, scene_story_context
from .visual_providers import ProviderRegistry, VisualProvider


@dataclass(frozen=True)
class RoutedSource:
    adapter: VisualProvider
    kind: str
    reason: str


def route_sources(
    registry: ProviderRegistry,
    scene: dict[str, Any],
    state: dict[str, Any],
    query: str,
    preferred_kind: str,
) -> list[list[RoutedSource]]:
    # Context informs suitability only. Search results still need independent
    # destination-scene evidence at the existing acceptance authorities.
    intent = scene.get("visual_intent") or {}
    plan = scene.get("visual_query_plan") or {}
    text = " ".join((
        query, str(scene.get("narration") or ""), str(scene.get("visual_goal") or ""),
        positive_intent(intent), scene_story_context(scene, state, routing=True),
        " ".join(plan.get("primary_subjects") or []),
    )).casefold()
    words = set(re.findall(r"[\w]+", text))
    objects = intent.get("objects") or [] if isinstance(intent, dict) else []
    named_object = any(
        re.fullmatch(r"[A-Z][\w'-]+(?: [A-Z][\w'-]+)+", str(obj).strip()) for obj in objects
    )
    atmospheric = bool(words & {"atmosphere", "atmospheric", "atmosphäre"}) and bool(
        words & {"dust", "particles", "climate", "weather", "meteorology", "staub", "staubteilchen", "partikel"}
    )
    if atmospheric or words & {
        "space",
        "astronomy",
        "planet",
        "planets",
        "spacecraft",
        "astronaut",
        "galaxy",
        "lunar",
        "orbit",
        "satellite",
        "cosmos",
        "weltraum",
        "astronomie",
        "planetary",
    }:
        order = ("nasa", "wikimedia", "openverse", "pexels", "pixabay")
        reason = "space_or_earth_observation"
    elif words & HISTORICAL_WORDS or re.search(r"\b1\d{3}\b", text):
        order = ("loc", "europeana", "wikimedia", "openverse", "pexels", "pixabay")
        reason = "historical_or_archival"
    elif words & {
        "anatomy",
        "anatomical",
        "medical",
        "organ",
        "tissue",
        "neuron",
        "anatomie",
        "physiology",
    }:
        order = ("wikimedia", "openverse", "pexels", "pixabay")
        reason = "factual_anatomical"
    elif (
        words & {"landmark", "monument", "city", "named_entity"}
        or named_object
        or plan.get("protected_entities")
        or intent
        and isinstance(intent, dict)
        and intent.get("named_entities")
    ):
        order = ("wikimedia", "openverse", "pexels", "pixabay")
        reason = "concrete_entity_or_place"
    else:
        order = ("pexels", "pixabay", "openverse", "wikimedia")
        reason = "everyday_action_or_general_photo"
    ranked = [registry.get(name) for name in order]
    # Explicit injected adapters also work without expanding the taxonomy.
    ranked.extend(
        p
        for p in registry.enabled()
        if p.provider not in order
        and p.capabilities.suitability
        and reason in p.capabilities.suitability
    )
    selected = []
    for adapter in ranked:
        if adapter is None or getattr(adapter, "disabled", False):
            continue
        kinds = adapter.capabilities.kinds
        kind = preferred_kind if preferred_kind in kinds else "photo" if "photo" in kinds else None
        if kind:
            selected.append(RoutedSource(adapter, kind, reason))
    groups = []
    for i in range(0, len(selected), 2):
        group = selected[i : i + 2]
        groups.append(group)
        # Alternate kinds remain available at that provider's priority, never
        # promote a secondary stock provider ahead of primary domain sources.
        alternates = [
            RoutedSource(
                s.adapter, "photo" if s.kind == "video" else "video", "alternate_media_kind"
            )
            for s in group
            if {"photo", "video"}.issubset(s.adapter.capabilities.kinds)
        ]
        if alternates:
            groups.append(alternates)
    return groups
