from __future__ import annotations

"""Retrieval: obtain page contents reliably, politely and in isolation.

Fallback order per URL:

1. extraction cache (same URL, still fresh for the question's time sensitivity);
2. encyclopedia API for Wikipedia article URLs (clean plain text, no scraping);
3. plain HTTP GET (httpx, honest User-Agent, robots.txt respected, bounded
   size/timeout, at most one retry for timeouts / 5xx);
4. only if the page is JS-only *and* browser rendering is enabled *and* the
   dynamic budget allows it: Scrapling's ``DynamicFetcher`` (plain headless
   Chromium, no stealth/fingerprint spoofing).

401/402/403/407/451 are access controls and are never worked around; 429
stops the host for the rest of the run.  Every failure is an outcome record,
never an exception: one bad site cannot break research.
"""

import hashlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .extraction import ExtractedPage, extract_page, extract_plaintext
from .models import FetchOutcome, ResearchBudget
from .quality import host_of

USER_AGENT = "ClipForgeResearch/2.0 (local video research assistant; respects robots.txt)"
ROBOTS_AGENT = "ClipForgeResearch"
MAX_BYTES = 1_500_000
ACCESS_DENIED = {401, 402, 403, 407, 451}
DynamicFetch = Callable[[str, float], str | None]
_UNREACHABLE = object()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class ExtractionCache:
    """Extracted pages (never raw HTML) on disk + in memory, with a max age per read."""

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def get(self, url: str, max_age_hours: float) -> tuple[ExtractedPage, str] | None:
        key = self._key(url)
        with self._lock:
            record = self._memory.get(key)
        if record is None and self._root is not None:
            try:
                record = json.loads((self._root / f"{key}.json").read_text("utf-8"))
            except (OSError, ValueError):
                record = None
        if not isinstance(record, dict):
            return None
        try:
            fetched = datetime.fromisoformat(str(record["fetched_at"]))
            if (datetime.now(UTC) - fetched).total_seconds() > max_age_hours * 3600:
                return None
            return ExtractedPage(**record["page"]), str(record["fetched_at"])
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, url: str, page: ExtractedPage, fetched_at: str) -> None:
        record = {"fetched_at": fetched_at, "page": asdict(page)}
        key = self._key(url)
        with self._lock:
            self._memory[key] = record
        if self._root is None:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / f"{key}.json").write_text(json.dumps(record, ensure_ascii=False), "utf-8")
        except OSError:
            pass  # a read-only cache only costs a refetch


class RobotsPolicy:
    """robots.txt per host (RFC 9309: 4xx = no rules, 5xx/unreachable = disallow)."""

    def __init__(self, client: httpx.Client, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._parsers: dict[str, RobotFileParser | bool | object] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool | None:
        """True / False, or None when the host is unreachable (no robots answer at all)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._parsers.get(origin)
        if cached is None:
            cached = self._load(origin)
            with self._lock:
                self._parsers[origin] = cached
        if cached is _UNREACHABLE:
            return None
        if isinstance(cached, bool):
            return cached
        return cached.can_fetch(ROBOTS_AGENT, url)

    def _load(self, origin: str) -> RobotFileParser | bool | object:
        try:
            response = self._client.get(f"{origin}/robots.txt", timeout=self._timeout)
        except httpx.TransportError:
            return _UNREACHABLE
        except httpx.HTTPError:
            return False
        if 400 <= response.status_code < 500:
            return True
        if response.status_code >= 500:
            return False
        parser = RobotFileParser()
        parser.parse(response.text.splitlines()[:2000])
        return parser


def _scrapling_dynamic(url: str, timeout_seconds: float) -> str | None:
    """Render a JS-only page with Scrapling's plain (non-stealth) browser fetcher."""
    from scrapling.fetchers import (
        DynamicFetcher,  # optional extra: scrapling[fetchers] + playwright
    )

    response = DynamicFetcher.fetch(
        url, headless=True, network_idle=True, disable_resources=True, timeout=int(timeout_seconds * 1000)
    )
    if getattr(response, "status", 200) in ACCESS_DENIED:
        return None
    body = getattr(response, "html_content", None) or getattr(response, "body", None)
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else (str(body) if body else None)


class Retriever:
    def __init__(
        self,
        budget: ResearchBudget,
        *,
        timeout: float = 8.0,
        cache: ExtractionCache | None = None,
        cache_max_age_hours: float = 168.0,
        transport: httpx.BaseTransport | None = None,
        dynamic_fetch: DynamicFetch | None = None,
        dynamic_enabled: bool = False,
        respect_robots: bool = True,
        deadline: float | None = None,
    ) -> None:
        self.budget = budget
        self.timeout = timeout
        self.cache = cache or ExtractionCache(None)
        self.cache_max_age_hours = cache_max_age_hours
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
            transport=transport,
        )
        self.robots = RobotsPolicy(self.client, min(timeout, 4.0))
        self.respect_robots = respect_robots
        self.dynamic_fetch = dynamic_fetch or _scrapling_dynamic
        self.dynamic_enabled = dynamic_enabled
        self.deadline = deadline
        self._failed_hosts: set[str] = set()
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def _host_failed(self, host: str) -> bool:
        with self._lock:
            return host in self._failed_hosts

    def _fail_host(self, host: str) -> None:
        with self._lock:
            self._failed_hosts.add(host)

    def _expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    # -- public ---------------------------------------------------------
    def fetch(self, url: str) -> FetchOutcome:
        started = time.monotonic()
        outcome = self._fetch(url)
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def fetch_many(self, urls: list[str], *, workers: int = 4) -> list[FetchOutcome]:
        """Fetch in parallel; results keep the order of ``urls``."""
        results: dict[str, FetchOutcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1))) as pool:
            futures = {pool.submit(self.fetch, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolation: a crash is one failed URL
                    results[url] = FetchOutcome(url, "network_error", error=f"{type(exc).__name__}: {exc}")
        return [results[url] for url in urls]

    # -- internals --------------------------------------------------------
    def _fetch(self, url: str) -> FetchOutcome:
        cached = self.cache.get(url, self.cache_max_age_hours)
        if cached is not None:
            page, fetched_at = cached
            return FetchOutcome(url, "cached", "cache", page=page, fetched_at=fetched_at)
        host = host_of(url)
        if self._host_failed(host):
            return FetchOutcome(url, "host_skipped", error="host failed or rate-limited earlier in this run")
        if self._expired():
            return FetchOutcome(url, "budget_exhausted", error="research deadline reached")
        if not self.budget.take("documents"):
            return FetchOutcome(url, "budget_exhausted", error="document budget exhausted")
        if host.endswith("wikipedia.org") and urlsplit(url).path.startswith("/wiki/"):
            outcome = self._encyclopedia(url, host)
        else:
            outcome = self._http(url, host)
        if outcome.usable and outcome.page is not None and outcome.fetched_at:
            self.cache.put(url, outcome.page, outcome.fetched_at)
        return outcome

    def _encyclopedia(self, url: str, host: str) -> FetchOutcome:
        title = unquote(urlsplit(url).path.removeprefix("/wiki/")).replace("_", " ")
        try:
            response = self.client.get(
                f"https://{host}/w/api.php",
                params={
                    "action": "query", "prop": "extracts|info", "explaintext": 1, "exsectionformat": "wiki",
                    "titles": title, "redirects": 1, "format": "json",
                },
            )
            response.raise_for_status()
            page = next(iter(response.json()["query"]["pages"].values()))
        except (httpx.HTTPError, KeyError, ValueError, StopIteration) as exc:
            if isinstance(exc, httpx.TransportError):
                self._fail_host(host)
            return FetchOutcome(url, "network_error" if isinstance(exc, httpx.HTTPError) else "extraction_failed",
                                "encyclopedia_api", error=str(exc)[:160], attempts=1)
        fetched_at = now_iso()
        extracted = extract_plaintext(
            str(page.get("extract") or ""), url, title=str(page.get("title") or title),
            language=host.split(".")[0], site_name="Wikipedia",
        )
        extracted.updated_at = str(page.get("touched") or "")[:10] or None
        status = "ok" if extracted.paragraphs else "extraction_failed"
        return FetchOutcome(url, status, "encyclopedia_api", page=extracted, http_status=200, fetched_at=fetched_at, attempts=1)

    def _http(self, url: str, host: str) -> FetchOutcome:
        if self.respect_robots:
            allowed = self.robots.allowed(url)
            if allowed is None:
                self._fail_host(host)
                return FetchOutcome(url, "network_error", "http", error="host unreachable (robots.txt request failed)")
            if not allowed:
                return FetchOutcome(url, "robots_disallowed", "http", error="robots.txt disallows this URL")
        attempts = 0
        while True:
            attempts += 1
            try:
                with self.client.stream("GET", url) as response:
                    status = response.status_code
                    if status in ACCESS_DENIED:
                        return FetchOutcome(url, "access_denied", "http", http_status=status, attempts=attempts,
                                            error="access control (not bypassed)")
                    if status == 429:
                        self._fail_host(host)
                        return FetchOutcome(url, "rate_limited", "http", http_status=status, attempts=attempts)
                    if status in {404, 410}:
                        return FetchOutcome(url, "not_found", "http", http_status=status, attempts=attempts)
                    if status >= 500:
                        raise httpx.HTTPStatusError(f"server error {status}", request=response.request, response=response)
                    if status >= 400:
                        return FetchOutcome(url, "http_error", "http", http_status=status, attempts=attempts)
                    kind = response.headers.get("content-type", "").casefold()
                    if kind and not any(token in kind for token in ("html", "xml", "text/plain")):
                        return FetchOutcome(url, "not_html", "http", http_status=status, attempts=attempts, error=kind[:60])
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            return FetchOutcome(url, "too_large", "http", http_status=status, attempts=attempts)
                    encoding = response.encoding or "utf-8"
                    final_url = str(response.url)
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                retryable = isinstance(exc, (httpx.TimeoutException, httpx.HTTPStatusError))
                if retryable and attempts == 1 and not self._expired() and self.budget.take("retries"):
                    continue
                if isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException):
                    self._fail_host(host)
                status_name = "timeout" if isinstance(exc, httpx.TimeoutException) else (
                    "http_error" if isinstance(exc, httpx.HTTPStatusError) else "network_error"
                )
                return FetchOutcome(url, status_name, "http", attempts=attempts, error=str(exc)[:160])
            break
        fetched_at = now_iso()
        text = bytes(body).decode(encoding, "replace")
        try:
            page = extract_page(text, final_url) if "plain" not in kind else extract_plaintext(text, final_url)
        except Exception as exc:  # noqa: BLE001 - malformed page: recorded, skipped
            return FetchOutcome(url, "extraction_failed", "http", http_status=status, attempts=attempts, error=str(exc)[:160])
        if page.js_only:
            return self._dynamic(url, page, status, attempts)
        if not page.paragraphs:
            return FetchOutcome(url, "extraction_failed", "http", page=page, http_status=status, attempts=attempts,
                                fetched_at=fetched_at, error="no main-content paragraphs")
        return FetchOutcome(url, "ok", "http", page=page, http_status=status, attempts=attempts, fetched_at=fetched_at)

    def _dynamic(self, url: str, shell: ExtractedPage, status: int, attempts: int) -> FetchOutcome:
        if not self.dynamic_enabled:
            return FetchOutcome(url, "js_only", "http", page=shell, http_status=status, attempts=attempts,
                                error="JS-only page; browser rendering disabled")
        if self._expired() or not self.budget.take("dynamic"):
            return FetchOutcome(url, "js_only", "http", page=shell, http_status=status, attempts=attempts,
                                error="JS-only page; dynamic budget exhausted")
        try:
            html = self.dynamic_fetch(url, self.timeout * 2)
        except Exception as exc:  # noqa: BLE001 - missing extra, browser crash, timeout
            return FetchOutcome(url, "js_only", "dynamic", page=shell, http_status=status, attempts=attempts + 1,
                                error=f"dynamic render failed: {type(exc).__name__}: {str(exc)[:120]}")
        if not html:
            return FetchOutcome(url, "access_denied", "dynamic", attempts=attempts + 1, error="dynamic render returned nothing")
        page = extract_page(html, url)
        page.extractor = f"{page.extractor}+dynamic"
        if not page.paragraphs:
            return FetchOutcome(url, "extraction_failed", "dynamic", page=page, attempts=attempts + 1,
                                error="no main-content paragraphs after rendering")
        return FetchOutcome(url, "ok", "dynamic", page=page, http_status=status, attempts=attempts + 1, fetched_at=now_iso())
