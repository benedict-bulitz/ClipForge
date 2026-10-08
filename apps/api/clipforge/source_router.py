"""Source Router V2: domain suitability, never a relevance or rights authority.

The Search Planner's domain classification decides which providers are worth
asking at all and in which order. Providers whose catalogue cannot contain a
fitting asset for that domain are skipped (and the skip is recorded), so NASA
is never asked for a kitchen cupboard and a stock library is only widening
for an archival event. Each routed source also receives the phrasing its
search engine understands (catalogue-style archives get entity + period).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .visual_providers import ProviderRegistry, VisualProvider
from .visual_search_planner import (
    DOMAIN_REASONS,
    classify_domain,
    query_for_source,
    routing_text,
    source_family,
)

# Provider order per domain. Providers absent from a domain's order are not
# suitable for it and are not queried (an explicit suitability declaration on
# an injected adapter can still opt it in).
DOMAIN_ORDER: dict[str, tuple[str, ...]] = {
    "space": ("nasa", "wikimedia", "openverse", "flickr", "pexels", "pixabay"),
    "historical": ("loc", "europeana", "wikimedia", "openverse", "flickr", "pexels", "pixabay"),
    "art_culture": ("europeana", "wikimedia", "openverse", "loc", "flickr", "pexels", "pixabay"),
    "anatomy": ("wikimedia", "openverse", "pexels", "pixabay"),
    "diagram": ("wikimedia", "openverse", "pexels", "pixabay"),
    "entity_place": ("wikimedia", "openverse", "flickr", "pexels", "pixabay"),
    "nature": ("pexels", "wikimedia", "pixabay", "openverse", "flickr"),
    "general": ("pexels", "pixabay", "openverse", "wikimedia", "flickr"),
}


@dataclass(frozen=True)
class RoutedSource:
    adapter: VisualProvider
    kind: str
    reason: str
    # Provider-family phrasing for this stage; None = the logical query.
    query: str | None = None


def scene_domain(scene: dict[str, Any], state: dict[str, Any], query: str = "") -> tuple[str, str]:
    # Context informs suitability only. Search results still need independent
    # destination-scene evidence at the existing acceptance authorities.
    intent = scene.get("visual_intent") if isinstance(scene.get("visual_intent"), dict) else {}
    plan = scene.get("visual_query_plan") if isinstance(scene.get("visual_query_plan"), dict) else {}
    return classify_domain(routing_text(scene, state, query), intent, plan)


def routing_decision(
    registry: ProviderRegistry, scene: dict[str, Any], state: dict[str, Any], query: str
) -> dict[str, Any]:
    """Diagnostics: the domain, the provider order and every skipped provider with its reason."""
    domain, reason = scene_domain(scene, state, query)
    order = DOMAIN_ORDER[domain]
    skipped = []
    for provider in registry.enabled():
        if provider.provider in order:
            if getattr(provider, "disabled", False):
                skipped.append({"provider": provider.provider, "reason": "disabled_or_unauthorized"})
            continue
        if reason in (provider.capabilities.suitability or ()):
            continue
        skipped.append({"provider": provider.provider, "reason": f"unsuitable_for_domain:{domain}"})
    return {
        "domain": domain,
        "reason": reason,
        "order": [name for name in order if registry.get(name) is not None],
        "skipped": skipped,
    }


def route_sources(
    registry: ProviderRegistry,
    scene: dict[str, Any],
    state: dict[str, Any],
    query: str,
    preferred_kind: str,
    *,
    stage_index: int | None = None,
) -> list[list[RoutedSource]]:
    domain, reason = scene_domain(scene, state, query)
    order = DOMAIN_ORDER.get(domain, DOMAIN_ORDER["general"])
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
            phrased = (
                query_for_source(scene, adapter.provider, query, stage_index)
                if stage_index is not None and source_family(adapter.provider) != "stock"
                else None
            )
            selected.append(RoutedSource(adapter, kind, reason, phrased if phrased and phrased != query else None))
    groups = []
    for i in range(0, len(selected), 2):
        group = selected[i : i + 2]
        groups.append(group)
        # Alternate kinds remain available at that provider's priority, never
        # promote a secondary stock provider ahead of primary domain sources.
        alternates = [
            RoutedSource(
                s.adapter, "photo" if s.kind == "video" else "video", "alternate_media_kind", s.query
            )
            for s in group
            if {"photo", "video"}.issubset(s.adapter.capabilities.kinds)
        ]
        if alternates:
            groups.append(alternates)
    return groups


__all__ = ["DOMAIN_ORDER", "DOMAIN_REASONS", "RoutedSource", "route_sources", "routing_decision", "scene_domain"]
