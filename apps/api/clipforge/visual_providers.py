"""Bounded provider boundary and catalog, shared by acquisition and browsing.

Adapters normalize API payloads into MediaCandidate inside search. Exceptions
are isolated only at this external boundary, never around retrieval logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import httpx

if TYPE_CHECKING:
    from .media import MediaCandidate


@dataclass
class AcquisitionBudget:
    # Enough for the existing 3-query, preferred/alternate + optional + Commons
    # paths, with headroom for download failures and relaxed fallbacks.
    max_search_requests: int = 18
    max_verifications: int = 72
    max_downloads: int = 12
    search_requests: int = 0
    verifications: int = 0
    downloads: int = 0
    verified_identities: set[str] = field(default_factory=set)
    verification_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    exhausted: set[str] = field(default_factory=set)

    def claim(self, work: str, identity: str | None = None) -> bool:
        if work == "verifications" and identity in self.verified_identities:
            return False
        if getattr(self, work) >= getattr(self, f"max_{work}"):
            self.exhausted.add(work)
            return False
        setattr(self, work, getattr(self, work) + 1)
        if work == "verifications" and identity:
            self.verified_identities.add(identity)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": {
                key: getattr(self, f"max_{key}")
                for key in ("search_requests", "verifications", "downloads")
            },
            "used": {
                key: getattr(self, key) for key in ("search_requests", "verifications", "downloads")
            },
            "exhausted": sorted(self.exhausted),
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    kinds: tuple[str, ...]
    page_limit: int = 24
    evidence: str = "provider metadata"
    suitability: tuple[str, ...] = ()  # descriptive catalog data for source routing


class VisualProvider(Protocol):
    provider: str
    capabilities: ProviderCapabilities

    def search(
        self,
        query: str,
        kind: str,
        *,
        portrait: bool,
        scene_duration: float,
        budget: AcquisitionBudget,
    ) -> list[MediaCandidate]: ...
    def download(
        self, candidate: MediaCandidate, destination: Path, *, budget: AcquisitionBudget
    ) -> Path: ...
    def normalize(self, rows: object, kind: str) -> list[MediaCandidate]: ...
    def close(self) -> None: ...


def provider_call(operation: Callable[[], Any]) -> Any:
    from .media import MediaProviderError

    try:
        return operation()
    except MediaProviderError:
        raise
    except httpx.TimeoutException:
        raise MediaProviderError("timeout", "Visual provider timed out.") from None
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        category = (
            "invalid_credentials"
            if code in {401, 403}
            else "rate_limited"
            if code == 429
            else "provider_error"
        )
        raise MediaProviderError(category, "Visual provider request failed.") from None
    except httpx.RequestError:
        raise MediaProviderError("network_error", "Visual provider is unavailable.") from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MediaProviderError(
            "malformed_response", "Visual provider returned malformed data."
        ) from None
    except AssertionError:
        raise  # contract/programming assertions are not provider outages
    except Exception:  # noqa: BLE001 - isolated external adapter boundary
        raise MediaProviderError(
            "unexpected_provider_error", "Visual provider failed unexpectedly."
        ) from None


class ProviderAdapter:
    """Compatibility adapter for the three existing normalizing clients."""

    def __init__(self, provider: str, client: Any, *, owned: bool = False):
        self.provider, self.client, self.owned = provider, client, owned
        self.capabilities = getattr(
            client,
            "capabilities",
            ProviderCapabilities(
                tuple(
                    kind
                    for kind, method in (("video", "search_videos"), ("photo", "search_photos"))
                    if hasattr(client, method)
                )
            ),
        )

    def search(
        self,
        query: str,
        kind: str,
        *,
        portrait: bool,
        scene_duration: float,
        budget: AcquisitionBudget,
    ) -> list[MediaCandidate]:
        from .media import MediaProviderError

        if kind not in self.capabilities.kinds:
            return []
        if not budget.claim("search_requests"):
            raise MediaProviderError(
                "budget_exhausted", "Visual acquisition search budget exhausted."
            )

        def fetch() -> list[MediaCandidate]:
            method = self.client.search_videos if kind == "video" else self.client.search_photos
            kwargs = {"portrait": portrait}
            if getattr(self.client, "request_budget_supported", False):
                kwargs["request_budget"] = budget
            if kind == "video":
                kwargs["scene_duration"] = scene_duration
            return self.normalize(method(query, **kwargs), kind)

        return provider_call(fetch)

    def normalize(self, rows: object, kind: str) -> list[MediaCandidate]:
        from .media import MediaCandidate

        if not isinstance(rows, (list, tuple)):
            raise TypeError("Expected normalized candidates")
        normalized = [
            row
            for row in rows[: self.capabilities.page_limit]
            if isinstance(row, MediaCandidate)
            and row.kind == kind
            and isinstance(row.provider_id, str)
            and row.provider_id not in {"", "None"}
            and isinstance(row.width, (int, float))
            and row.width > 0
            and isinstance(row.height, (int, float))
            and row.height > 0
            and isinstance(row.rank, (int, float))
            and isinstance(row.title, str)
            and isinstance(row.description, str)
            and isinstance(row.tags, (list, tuple))
            and all(isinstance(tag, str) for tag in row.tags)
        ]
        if rows and not normalized:
            raise ValueError("No well-formed normalized results")
        return normalized

    def download(
        self, candidate: MediaCandidate, destination: Path, *, budget: AcquisitionBudget
    ) -> Path:
        from .media import MediaProviderError, is_real_media_allowed

        if not is_real_media_allowed(candidate):
            raise MediaProviderError(
                "ineligible_rights", "Media has insufficient reusable-rights evidence."
            )
        if not budget.claim("downloads"):
            raise MediaProviderError(
                "budget_exhausted", "Visual acquisition download budget exhausted."
            )
        return provider_call(lambda: self.client.download(candidate, destination))

    def close(self) -> None:
        if self.owned:
            provider_call(self.client.close)


def provider_relative_ranks(candidates: list[Any]) -> dict[str, float]:
    """Provider rank is meaningful only inside its own returned result page."""
    groups: dict[str, list[Any]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.provider, []).append(candidate)
    result = {}
    for group in groups.values():
        ranks = sorted({candidate.rank for candidate in group}, reverse=True)
        positions = {rank: 1.0 - i / max(1, len(ranks) - 1) for i, rank in enumerate(ranks)}
        result.update({candidate.identity: positions[candidate.rank] for candidate in group})
    return result


class ProviderRegistry:
    def __init__(self, providers: list[VisualProvider]):
        self._providers = {item.provider: item for item in providers}

    def get(self, provider: str) -> VisualProvider | None:
        return self._providers.get(provider)

    def enabled(self, kind: str | None = None) -> list[VisualProvider]:
        return [
            item
            for item in self._providers.values()
            if kind is None or kind in item.capabilities.kinds
        ]

    def close(self) -> None:
        from .media import MediaProviderError

        for provider in self.enabled():
            try:
                provider_call(provider.close)
            except MediaProviderError:
                pass


def canonical_source(url: str) -> str | None:
    parsed = urlparse(str(url or "").strip())
    path = unquote(parsed.path).strip("/")
    if not parsed.netloc or path.casefold() in {"", "video", "videos", "photo", "photos", "wiki"}:
        return None
    # Preserve case and identity query parameters; tracking/presentation/auth
    # parameters do not identify a different original asset.
    host = parsed.netloc.casefold().removeprefix('www.')
    if host == "commons.wikimedia.org":
        path = path.replace("wiki/Special:FilePath/", "wiki/File:")
        path = path.replace(" ", "_")
    # Commons originals and thumbnails share a filename; dimensions/rehosting
    # must not create independent assets. This is exact origin evidence only.
    if host == "upload.wikimedia.org" and path.startswith("wikipedia/commons/"):
        parts = path.split("/")
        filename = parts[-2] if "thumb" in parts else parts[-1]
        return f"commons.wikimedia.org/wiki/File:{filename.replace(' ', '_')}"
    ignored = {"ref", "download", "width", "height", "w", "h", "size", "token", "signature", "expires", "api_key", "apikey", "wskey", "key"}
    params = [(key, value) for key, value in parse_qsl(parsed.query)
              if key.casefold() not in ignored and not key.casefold().startswith(("utm_", "x-amz-"))]
    suffix = f"?{urlencode(sorted(params))}" if params else ""
    return f"{host}/{path}{suffix}"


def asset_keys(value: Any) -> set[str]:
    data = value if isinstance(value, dict) else vars(value)
    identity = data.get("identity") or (
        getattr(value, "identity", None) if not isinstance(value, dict) else None
    )
    keys = {str(identity)} if identity else set()
    source = canonical_source(str(data.get("source_url") or ""))
    if source:
        keys.add(f"source:{source}")
    canonical = data.get("canonical_asset_key")
    if canonical:
        keys.add(str(canonical))
    origin = data.get("origin") or {}
    for address in (data.get("download_url"), origin.get("media_url"), origin.get("source_url")):
        key = canonical_source(str(address or ""))
        if key:
            keys.add(f"source:{key}")
    if origin.get("canonical_id") and origin.get("provider"):
        keys.add(f"origin:{origin['provider']}:{origin['canonical_id']}")
    return keys


class CandidateLedger:
    def __init__(self, excluded: set[str] | None = None):
        self.keys = set(excluded or ())

    def admit(self, candidate: MediaCandidate) -> bool:
        from .media import is_real_media_allowed

        keys = asset_keys(candidate)
        if not is_real_media_allowed(candidate) or keys & self.keys:
            return False
        self.keys.update(keys)
        return True


def create_provider_registry(
    settings: Any,
    *,
    client: Any = None,
    fallback_client: Any = None,
    extra_clients: list[Any] | None = None,
) -> ProviderRegistry:
    from .media import PexelsMediaClient, WikimediaMediaClient, _optional_real_clients

    primary = client or (
        PexelsMediaClient(settings.pexels_api_key) if settings.pexels_api_key else None
    )
    fallback = fallback_client or WikimediaMediaClient()
    extras = _optional_real_clients(settings) if extra_clients is None else extra_clients
    providers = []
    if primary is not None:
        providers.append(ProviderAdapter("pexels", primary, owned=client is None))
    providers.append(ProviderAdapter("wikimedia", fallback, owned=fallback_client is None))
    providers.extend(
        extra if getattr(extra, "budget_supported", False) else ProviderAdapter(extra.provider, extra, owned=extra_clients is None) for extra in extras
    )
    # Explicit client injection describes a caller-owned provider universe.
    # Production uses the catalog; tests/custom clients never open hidden APIs.
    if client is None and fallback_client is None and extra_clients is None:
        from .open_media import (
            EuropeanaProvider,
            FlickrProvider,
            LOCProvider,
            NASAProvider,
            OpenverseProvider,
        )
        providers.extend([OpenverseProvider(), NASAProvider(), LOCProvider()])
        if getattr(settings, "europeana_api_key", None):
            providers.append(EuropeanaProvider(settings.europeana_api_key))
        if getattr(settings, "flickr_api_key", None):
            providers.append(FlickrProvider(settings.flickr_api_key))
    return ProviderRegistry(providers)
