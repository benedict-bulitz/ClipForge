"""Open-media adapters. Verified contracts and limits: docs/visual-sources-v2.md.

Only still images are exposed: these APIs' videos/documents do not all provide
the bounded file/duration contract needed by ClipForge. No provider-name grant.
"""

from __future__ import annotations

import copy
import html
import re
import time
from collections import OrderedDict, deque
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from PIL import ImageFile

from .visual_providers import AcquisitionBudget, ProviderCapabilities, provider_call
from .visual_rights import MediaRights, commons_rights, evaluate_rights

HTTP_CLIENT_FACTORY = httpx.Client
_LOCK = RLock()
_CACHE: OrderedDict[tuple, tuple[float, list]] = OrderedDict()
_REQUESTS: dict[str, deque[float]] = {}
_COOLDOWN: dict[str, float] = {}
CACHE_TTL = 300.0
CACHE_SIZE = 128


def clear_search_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _REQUESTS.clear()
        _COOLDOWN.clear()


def plain(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(plain(v) for v in value[:32])
    if not isinstance(value, str):
        return ""  # Malformed creator/credit metadata must not become permission.
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()[
        :6000
    ]


def url(value: Any) -> str:
    value = str(value or "")
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""


def integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def uri_rights(
    uri: str, *, source: str, creator: str, page: str, title: str = "", attribution: str = ""
) -> MediaRights:
    # Reuse the existing exact-host/CC-deed normalizer and policy. NC, ND,
    # share-alike, RightsStatements "NoC-*", and unknown URIs do not pass.
    fields = {"LicenseUrl": {"value": uri}}
    if attribution:
        fields["Attribution"] = {"value": attribution}
    rights = commons_rights(fields, source_url=page, creator=creator)
    return replace(rights, rights_source=source, evidence={"rights_uri": uri, "title": title})


class OpenMediaProvider:
    budget_supported = True
    provider: str
    endpoint: str
    capabilities = ProviderCapabilities(("photo",), page_limit=20)
    per_minute: int | None = None
    rate_window = 60.0
    denial_category = "invalid_credentials"

    def __init__(self, *, client: httpx.Client | None = None):
        self.client = client or HTTP_CLIENT_FACTORY(
            timeout=httpx.Timeout(10, connect=4), follow_redirects=True
        )
        self.owned = client is None
        self.disabled = False

    def close(self) -> None:
        if self.owned:
            self.client.close()

    def params(self, query: str) -> dict:
        raise NotImplementedError

    def headers(self) -> dict:
        return {"Accept": "application/json"}

    def _check_response(self, response: httpx.Response) -> None:
        response.raise_for_status()

    def _response_json(self, response: httpx.Response) -> dict:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Expected provider object")
        return value

    def _json(self, address: str, budget: AcquisitionBudget, *, params: dict | None = None) -> dict:
        from .media import MediaProviderError

        if self.disabled:
            raise MediaProviderError(self.denial_category, "Provider path is disabled.")
        now = time.monotonic()
        with _LOCK:
            recent = _REQUESTS.setdefault(self.provider, deque())
            while recent and now - recent[0] >= self.rate_window:
                recent.popleft()
            if (
                now < _COOLDOWN.get(self.provider, 0)
                or self.per_minute is not None
                and len(recent) >= self.per_minute
            ):
                raise MediaProviderError(
                    "rate_limited", "Provider request quota is temporarily exhausted."
                )
            if not budget.claim("search_requests"):
                raise MediaProviderError("budget_exhausted", "Visual search budget exhausted.")
            if self.per_minute is not None:
                recent.append(now)
        # Never forward a credential header through an API redirect.
        response = self.client.get(
            address, params=params, headers=self.headers(), follow_redirects=False
        )
        if response.status_code in {401, 403}:
            self.disabled = True
        if response.status_code == 429:
            # No blocking retry; widening/fallback can continue immediately.
            try:
                delay = min(3600, max(60, float(response.headers.get("retry-after", "60"))))
            except ValueError:
                delay = 60
            with _LOCK:
                _COOLDOWN[self.provider] = now + delay
        self._check_response(response)
        return self._response_json(response)

    def search(
        self,
        query: str,
        kind: str,
        *,
        portrait: bool,
        scene_duration: float,
        budget: AcquisitionBudget,
    ) -> list:
        if kind not in self.capabilities.kinds or self.disabled:
            return []
        params = self.params(query)
        # Headers/keys are intentionally absent. Only normalized reusable
        # candidate metadata is cached, not raw API envelopes (Europeana echoes keys).
        key = (
            self.provider,
            " ".join(query.casefold().split()),
            kind,
            tuple(sorted((k, v) for k, v in params.items() if k not in {"q", "query"})),
        )
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_TTL:
                _CACHE.move_to_end(key)
                return [
                    replace(c, query=query)
                    for c in copy.deepcopy(cached[1])
                    if evaluate_rights(c.rights).status == "usable"
                ]
            _CACHE.pop(key, None)

        def fetch() -> list:
            rows = self._search(query, budget)
            result = []
            probes = 0
            for candidate in rows[: self.capabilities.page_limit]:
                if (
                    (candidate.width == 0 or candidate.height == 0)
                    and evaluate_rights(candidate.rights).status == "usable"
                    and probes < 3
                ):
                    probes += 1
                    try:
                        dimensions = provider_call(
                            lambda address=candidate.download_url: self._dimensions(address, budget)
                        )
                        if dimensions:
                            candidate = replace(
                                candidate, width=dimensions[0], height=dimensions[1]
                            )
                    except Exception as exc:
                        from .media import MediaProviderError

                        if not isinstance(exc, MediaProviderError):
                            raise
                result.append(candidate)
            # Never cache a response whose metadata cannot establish rights.
            safe = [c for c in result if evaluate_rights(c.rights).status == "usable"]
            with _LOCK:
                _CACHE[key] = (time.monotonic(), copy.deepcopy(safe))
                while len(_CACHE) > CACHE_SIZE:
                    _CACHE.popitem(last=False)
            return result

        return provider_call(fetch)

    def _search(self, query: str, budget: AcquisitionBudget) -> list:
        return self.normalize(
            self._json(self.endpoint, budget, params=self.params(query)), "photo", query=query
        )

    def _dimensions(self, address: str, budget: AcquisitionBudget) -> tuple[int, int] | None:
        if not url(address) or not budget.claim("downloads"):
            return None
        parser = ImageFile.Parser()
        consumed = 0
        with self.client.stream("GET", address) as response:
            self._check_response(response)
            for chunk in response.iter_bytes(16384):
                consumed += len(chunk)
                if consumed > 512 * 1024:
                    break
                parser.feed(chunk)
                if parser.image:
                    return parser.image.size
        return None

    def download(self, candidate: Any, destination: Path, *, budget: AcquisitionBudget) -> Path:
        from .media import MAX_PHOTO_BYTES, MediaProviderError, is_real_media_allowed

        if not is_real_media_allowed(candidate) or not url(candidate.download_url):
            raise MediaProviderError("ineligible_rights", "Selected media is not eligible.")
        if not budget.claim("downloads"):
            raise MediaProviderError("budget_exhausted", "Visual download budget exhausted.")

        def fetch() -> Path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".part")
            try:
                with self.client.stream("GET", candidate.download_url) as response:
                    self._check_response(response)
                    total = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(65536):
                            total += len(chunk)
                            if total > MAX_PHOTO_BYTES:
                                raise MediaProviderError(
                                    "provider_error", "Media exceeds download limit."
                                )
                            handle.write(chunk)
                if not total:
                    raise MediaProviderError("malformed_response", "Empty media response.")
                partial.replace(destination)
                return destination
            finally:
                partial.unlink(missing_ok=True)

        return provider_call(fetch)


def candidate(
    provider: str,
    identifier: str,
    query: str,
    *,
    page: str,
    image: str,
    creator: str = "",
    creator_url: str | None = None,
    title: str = "",
    description: str = "",
    tags: tuple = (),
    preview: str = "",
    width: int = 0,
    height: int = 0,
    rights: MediaRights | None = None,
    origin: dict | None = None,
    rank: float = 0,
) -> Any:
    from .media import MediaCandidate

    # IDs from external services never become filesystem paths.
    return MediaCandidate(
        provider_id=identifier,
        provider=provider,
        kind="photo",
        download_url=image,
        source_url=page,
        creator=creator,
        creator_url=creator_url,
        title=title,
        description=description,
        tags=tags,
        preview_url=preview or image,
        verification_url=preview or image,
        width=width,
        height=height,
        duration=None,
        query=query,
        rank=rank,
        rights=rights or MediaRights(),
        origin=origin or {},
    )


class OpenverseProvider(OpenMediaProvider):
    provider = "openverse"
    endpoint = "https://api.openverse.org/v1/images/"
    capabilities = ProviderCapabilities(
        ("photo",), page_limit=20, suitability=("general_photo", "entity")
    )
    # Anonymous access is supported. Server limits remain authoritative.
    per_minute = 5
    rate_window = 3600.0  # Official repository anonymous burst default: 5/hour.

    def params(self, query: str) -> dict:
        return {
            "q": query[:200],
            "page": 1,
            "page_size": 20,
            "license": "cc0,pdm,by",
            "excluded_source": "flickr",
        }

    def normalize(self, rows: object, kind: str, *, query: str = "") -> list:
        if not isinstance(rows, dict) or not isinstance(rows.get("results"), list):
            raise TypeError("Expected Openverse results")
        result = []
        for row in rows["results"][:20]:
            if not isinstance(row, dict):
                continue
            page, image = url(row.get("foreign_landing_url")), url(row.get("url"))
            if not row.get("id") or not page or not image or row.get("source") == "flickr":
                continue
            creator, title = plain(row.get("creator")), plain(row.get("title"))
            uri = url(row.get("license_url"))
            rights = uri_rights(
                uri,
                source=f"openverse_api:{row['id']}:license_url",
                creator=creator,
                page=page,
                title=title,
                attribution=plain(row.get("attribution")),
            )
            identifier = row.get("license")
            expected = {"cc0": "publicdomain/zero/1.0", "pdm": "publicdomain/mark/1.0"}.get(
                identifier
            )
            if identifier == "by":
                expected = f"licenses/by/{row.get('license_version')}"
            if not expected or urlparse(uri).path.strip("/") != expected:
                rights = replace(
                    rights, evidence=rights.evidence | {"conflicting_license_metadata": True}
                )
            tags = tuple(
                plain(t["name"])
                for t in (row.get("tags") or [])
                if isinstance(t, dict) and t.get("name")
            )
            result.append(
                candidate(
                    self.provider,
                    str(row["id"]),
                    query,
                    page=page,
                    image=image,
                    creator=creator,
                    creator_url=url(row.get("creator_url")) or None,
                    title=title,
                    tags=tags,
                    preview=url(row.get("thumbnail")),
                    width=integer(row.get("width")),
                    height=integer(row.get("height")),
                    rights=rights,
                    origin={
                        "provider": plain(row.get("source")),
                        "aggregator_provider": plain(row.get("provider")),
                        "source_url": page,
                        "media_url": image,
                        "item_id": str(row["id"]),
                    },
                    rank=20 - len(result),
                )
            )
        return result


class NASAProvider(OpenMediaProvider):
    provider = "nasa"
    endpoint = "https://images-api.nasa.gov/search"
    capabilities = ProviderCapabilities(("photo",), page_limit=12, suitability=("space",))

    def params(self, query: str) -> dict:
        return {"q": query, "media_type": "image", "page": 1, "page_size": 12}

    def _search(self, query: str, budget: AcquisitionBudget) -> list:
        payload = self._json(self.endpoint, budget, params=self.params(query))
        rows = payload.get("collection", {}).get("items")
        if not isinstance(rows, list):
            raise TypeError("Expected NASA collection")
        # Metadata work is bounded and leaves at least two search slots for
        # secondary providers. The API search does NOT establish permissions.
        for row in rows[:3]:
            if not isinstance(row, dict) or budget.max_search_requests - budget.search_requests < 3:
                continue
            data = (row.get("data") or [{}])[0]
            identifier = data.get("nasa_id") if isinstance(data, dict) else None
            if not identifier:
                continue
            try:
                location = self._json(
                    f"https://images-api.nasa.gov/metadata/{quote(str(identifier), safe='')}",
                    budget,
                ).get("location")
                if url(location) and urlparse(location).hostname == "images-assets.nasa.gov":
                    row["_metadata_url"] = location
                    row["_metadata"] = self._json(location, budget)
                    if (
                        row["_metadata"].get("XMP:Marked") is False
                        and budget.max_search_requests - budget.search_requests >= 3
                    ):
                        manifest = self._json(
                            f"https://images-api.nasa.gov/asset/{quote(str(identifier), safe='')}",
                            budget,
                        )
                        assets = manifest.get("collection", {}).get("items", [])
                        row["_image_url"] = next(
                            (
                                url(a.get("href"))
                                for a in assets
                                if isinstance(a, dict)
                                and "~orig" in str(a.get("href"))
                                and urlparse(url(a.get("href")))
                                .path.casefold()
                                .endswith((".jpg", ".jpeg", ".png"))
                            ),
                            "",
                        )
            except Exception as exc:
                from .media import MediaProviderError

                if not isinstance(
                    exc, (MediaProviderError, httpx.HTTPError, ValueError, TypeError)
                ):
                    raise
        return self.normalize(payload, "photo", query=query)

    def normalize(self, rows: object, kind: str, *, query: str = "") -> list:
        if (
            not isinstance(rows, dict)
            or not isinstance(rows.get("collection"), dict)
            or not isinstance(rows["collection"].get("items"), list)
        ):
            raise TypeError("Expected NASA collection")
        result = []
        for row in rows["collection"]["items"][:12]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("data"), list)
                or not row["data"]
                or not isinstance(row["data"][0], dict)
            ):
                continue
            data = row["data"][0]
            if data.get("media_type") != "image" or not data.get("nasa_id"):
                continue
            metadata = row.get("_metadata") or {}
            preview = next(
                (
                    url(link.get("href"))
                    for link in row.get("links", [])
                    if isinstance(link, dict)
                    and link.get("rel") == "preview"
                    and link.get("render") == "image"
                ),
                "",
            )
            image = row.get("_image_url") or preview
            if not image:
                continue
            identifier = str(data["nasa_id"])
            page = f"https://images.nasa.gov/details/{quote(identifier, safe='')}"
            creator = plain(
                data.get("photographer")
                or data.get("secondary_creator")
                or metadata.get("AVAIL:Photographer")
                or metadata.get("XMP:Creator")
            )
            evidence = {
                k: metadata[k]
                for k in (
                    "XMP:Marked",
                    "XMP:Rights",
                    "XMP:UsageTerms",
                    "XMP:WebStatement",
                    "IPTC:CopyrightNotice",
                    "AVAIL:NASAID",
                )
                if k in metadata
            }
            # XMP Marked=false explicitly means public domain (Adobe spec).
            # Missing, string 'false', or unrelated file's XMP is not permission.
            established = (
                metadata.get("XMP:Marked") is False and metadata.get("AVAIL:NASAID") == identifier
            )
            if (
                any(
                    metadata.get(k)
                    for k in (
                        "XMP:Rights",
                        "XMP:UsageTerms",
                        "XMP:WebStatement",
                        "IPTC:CopyrightNotice",
                    )
                )
                or metadata.get("Photoshop:CopyrightFlag") is True
            ):
                evidence["unresolved_restrictions"] = True
            rights = MediaRights(
                license_id="public-domain-xmp" if established else None,
                license_name="Public domain (item XMP)" if established else None,
                public_domain=True if established else None,
                attribution_required=True if established else None,
                attribution_text=f"NASA; {creator}; {page}; cropped/transformed for video"
                if established
                else None,
                rights_source="nasa_item_metadata:"
                + str(
                    row.get("_metadata_url")
                    or f"https://images-api.nasa.gov/metadata/{quote(identifier, safe='')}"
                ),
                evidence=evidence,
            )
            result.append(
                candidate(
                    self.provider,
                    identifier,
                    query,
                    page=page,
                    image=image,
                    creator=creator,
                    title=plain(data.get("title")),
                    description=plain(data.get("description")),
                    tags=tuple(plain(t) for t in data.get("keywords", [])[:32]),
                    preview=preview,
                    width=integer(metadata.get("File:ImageWidth")) if row.get("_image_url") else 0,
                    height=integer(metadata.get("File:ImageHeight"))
                    if row.get("_image_url")
                    else 0,
                    rights=rights,
                    origin={
                        "provider": "NASA",
                        "item_id": identifier,
                        "source_url": page,
                        "media_url": image,
                        "metadata_url": f"https://images-api.nasa.gov/metadata/{quote(identifier, safe='')}",
                        "date": plain(data.get("date_created")),
                        "institution": plain(data.get("center")),
                    },
                    rank=12 - len(result),
                )
            )
        return result


class EuropeanaProvider(OpenMediaProvider):
    provider = "europeana"
    endpoint = "https://api.europeana.eu/record/v2/search.json"
    capabilities = ProviderCapabilities(("photo",), page_limit=12, suitability=("archival",))

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        super().__init__(client=client)
        self._api_key = api_key

    def headers(self) -> dict:
        return super().headers() | {"X-Api-Key": self._api_key}

    def params(self, query: str) -> dict:
        return {
            "query": query,
            "qf": "TYPE:IMAGE",
            "rows": 12,
            "start": 1,
            "profile": "standard",
            "media": "true",
            "reusability": "open",
        }

    def normalize(self, rows: object, kind: str, *, query: str = "") -> list:
        from .media import MediaProviderError

        if (
            not isinstance(rows, dict)
            or rows.get("success") is not True
            or not isinstance(rows.get("items"), list)
        ):
            # An HTTP 200 success=false is not an empty search.
            if isinstance(rows, dict) and "api key" in str(rows.get("error", "")).casefold():
                self.disabled = True
                raise MediaProviderError("invalid_credentials", "Europeana rejected credentials.")
            raise TypeError("Expected Europeana items")
        result = []
        for row in rows["items"][:12]:
            if (
                not isinstance(row, dict)
                or row.get("type") != "IMAGE"
                or row.get("previewNoDistribute") is True
            ):
                continue
            pages, images = row.get("edmIsShownAt") or [], row.get("edmIsShownBy") or []
            if (
                not row.get("id")
                or not isinstance(images, list)
                or len(images) != 1
                or not isinstance(pages, list)
            ):
                continue
            page, image = url(pages[0] if pages else ""), url(images[0])
            if not page or not image:
                continue
            uris = row.get("rights") or []
            uri = uris[0] if isinstance(uris, list) and len(set(uris)) == 1 else ""
            creator, title = plain(row.get("dcCreator")), plain(row.get("title"))
            rights = uri_rights(
                str(uri),
                source=f"europeana_api:{row['id']}:rights",
                creator=creator,
                page=page,
                title=title,
            )
            preview = row.get("edmPreview") or []
            result.append(
                candidate(
                    self.provider,
                    str(row["id"]),
                    query,
                    page=page,
                    image=image,
                    creator=creator,
                    title=title,
                    description=plain(row.get("dcDescription")),
                    preview=url(preview[0]) if isinstance(preview, list) and preview else "",
                    rights=rights,
                    origin={
                        "provider": plain(row.get("dataProvider")),
                        "aggregator_provider": plain(row.get("provider")),
                        "item_id": str(row["id"]),
                        "aggregator_url": url(row.get("guid")),
                        "source_url": page,
                        "media_url": image,
                        "date": plain(row.get("year")),
                        "rights": uris,
                    },
                    rank=12 - len(result),
                )
            )
        return result


class LOCProvider(OpenMediaProvider):
    provider = "loc"
    endpoint = "https://www.loc.gov/search/"
    capabilities = ProviderCapabilities(("photo",), page_limit=12, suitability=("archival",))
    per_minute = 20  # Official JSON/YAML limit; no blocking sleeps.
    # https://www.loc.gov/apis/json-and-yaml/: public, no key/authentication.
    # Access denial is not an invalid configured credential. Preserve the
    # existing credential category for providers that actually use credentials.
    denial_category = "provider_error"

    def headers(self) -> dict:
        return super().headers() | {"User-Agent": "ClipForge (public LOC JSON client)"}

    @staticmethod
    def _http_evidence(response: httpx.Response) -> dict:
        def public_address(address: httpx.URL) -> dict:
            result = {"endpoint": f"{address.scheme}://{address.host}{address.path}"[:300]}
            if address.host == "www.loc.gov":
                result["parameters"] = {
                    k: v[:240] for k, v in address.params.items()
                    if k in {"q", "fo", "c", "sp", "fa", "at"}
                }
            return result

        evidence = {
            **public_address(response.request.url),
            "http_status": response.status_code,
            "user_agent": response.request.headers.get("user-agent", "")[:120],
            "response_headers": {
                key: response.headers[key][:160]
                for key in ("content-type", "retry-after", "server", "x-cache", "cf-mitigated")
                if key in response.headers
            },
            "redirects_followed": len(response.history),
        }
        if response.headers.get("location"):
            evidence["redirect_target"] = public_address(httpx.URL(urljoin(
                str(response.request.url), response.headers["location"]
            )))
        # Never persist raw bodies (which can echo headers/tokens). API bodies
        # are already loaded; failed streamed media must not trigger a read.
        if response.is_stream_consumed:
            prefix = response.content[:4096].lstrip().lower()
            evidence["body_format"] = (
                "html" if prefix.startswith((b"<!doctype html", b"<html"))
                else "json" if prefix.startswith((b"{", b"[")) else "text"
            )
            evidence["captcha_indicated"] = b"captcha" in prefix
        else:
            evidence["body_format"] = "unread_stream"
        return evidence

    def _check_response(self, response: httpx.Response) -> None:
        from .media import MediaProviderError

        if not 200 <= response.status_code < 300:
            raise MediaProviderError(
                "rate_limited" if response.status_code == 429 else "provider_error",
                f"Public LOC request failed (HTTP {response.status_code}).",
                diagnostics=self._http_evidence(response),
            )

    def _response_json(self, response: httpx.Response) -> dict:
        from .media import MediaProviderError

        try:
            return super()._response_json(response)
        except (ValueError, TypeError):
            raise MediaProviderError(
                "malformed_response", "Public LOC request returned malformed JSON.",
                diagnostics=self._http_evidence(response),
            ) from None

    def params(self, query: str) -> dict:
        return {"q": query, "fo": "json", "c": 12, "sp": 1, "fa": "online-format:image"}

    def _search(self, query: str, budget: AcquisitionBudget) -> list:
        payload = self._json(self.endpoint, budget, params=self.params(query))
        if not isinstance(payload.get("results"), list):
            raise TypeError("Expected LOC results")
        for row in payload["results"][:3]:
            if not isinstance(row, dict) or budget.max_search_requests - budget.search_requests < 3:
                continue
            page = url(row.get("url") or row.get("id"))
            if urlparse(page).hostname != "www.loc.gov" or not urlparse(page).path.startswith(
                "/item/"
            ):
                continue
            try:
                detail = self._json(page, budget, params={"fo": "json", "at": "item,resources"})
                if isinstance(detail.get("item"), dict):
                    row.update(detail["item"])
                    row["_resources"] = detail.get("resources") or []
            except Exception as exc:
                from .media import MediaProviderError

                if not isinstance(
                    exc, (MediaProviderError, httpx.HTTPError, ValueError, TypeError)
                ):
                    raise
        return self.normalize(payload, "photo", query=query)

    def normalize(self, rows: object, kind: str, *, query: str = "") -> list:
        if not isinstance(rows, dict) or not isinstance(rows.get("results"), list):
            raise TypeError("Expected LOC results")
        result = []
        for row in rows["results"][:12]:
            if (
                not isinstance(row, dict)
                or row.get("access_restricted") is True
                or "image" not in (row.get("online_format") or [])
            ):
                continue
            page = url(row.get("url") or row.get("id"))
            images = row.get("image_url") or []
            image = (
                next((url(v) for v in reversed(images) if url(v)), "")
                if isinstance(images, list)
                else ""
            )
            if not page or not image or "/static/" in image:
                continue
            nested = row.get("item") if isinstance(row.get("item"), dict) else {}
            raw = {
                k: row.get(k) or nested.get(k)
                for k in ("rights", "rights_advisory", "rights_information", "access_advisory")
                if row.get(k) or nested.get(k)
            }
            statements = [plain(v) for v in raw.values()]
            # Only an affirmative whole statement, not 'may be public domain',
            # 'No known restrictions', or collection identity establishes PD.
            pd = bool(statements) and all(
                re.fullmatch(
                    r"(?:public domain|this (?:image|item|work) is in the public domain)[.!]?",
                    s,
                    re.IGNORECASE,
                )
                for s in statements
            )
            creator = plain(
                row.get("contributor_names")
                or row.get("contributor")
                or nested.get("contributor_names")
            )
            rights = MediaRights(
                license_id="public-domain" if pd else None,
                license_name="Public domain" if pd else None,
                public_domain=True if pd else None,
                attribution_required=False if pd else None,
                rights_source=f"loc_item:{page}:rights",
                evidence=raw,
            )
            result.append(
                candidate(
                    self.provider,
                    page,
                    query,
                    page=page,
                    image=image,
                    creator=creator,
                    title=plain(row.get("title")),
                    description=plain(row.get("description") or row.get("summary")),
                    tags=tuple(
                        plain(v) for v in (row.get("subject") or row.get("subjects") or [])[:32]
                    ),
                    rights=rights,
                    origin={
                        "provider": "Library of Congress",
                        "item_id": page,
                        "source_url": page,
                        "media_url": image,
                        "date": plain(row.get("date")),
                        "collection": row.get("partof") or [],
                        "rights": raw,
                    },
                    rank=12 - len(result),
                )
            )
        return result
