"""Visual Sources V2 phase 1: bounded, read-only live check of Commons and NASA.

Run from apps/api (Python 3.11+, the project venv):

    PYTHONPATH=. python scripts/visual_sources_live_check.py --output /tmp/clipforge-visual-live.json

Exercises the production Commons/NASA adapters, shared acquisition budget,
rights authority, candidate ledger, ``asset_metadata`` and the routed scene
search (the code that writes ``visual-acquisition.json``) against the real
public APIs. No OpenAI, TTS, Pexels/Pixabay/Europeana keys, settings or
keychain access; no media is saved (dimension probes read <= 512 KiB).

Bounded and repeatable: about 35 requests, paced per host, hard-capped
(``--max-requests``), and a host that answers 429 is not contacted again in
the same run. Every run writes a fresh JSON report. Exit codes: 0 all checks
passed, 1 a check failed, 2 the run could not start (wrong tree/Python).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 11)
if tuple(sys.version_info[:2]) < MIN_PYTHON:  # before importing clipforge (3.11+ syntax)
    print(f"ABORT: Python {sys.version.split()[0]} is unsupported; use the project venv (3.11+, e.g. 3.12).")
    sys.exit(2)

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import httpx
from PIL import ImageFile

from clipforge import media
from clipforge.media import (
    COMMONS_USER_AGENT,
    MediaProviderError,
    WikimediaMediaClient,
    asset_metadata,
)
from clipforge.open_media import NASAProvider, clear_search_cache
from clipforge.visual_providers import (
    AcquisitionBudget,
    CandidateLedger,
    ProviderAdapter,
    ProviderRegistry,
)
from clipforge.visual_rights import evaluate_rights

MIN_PACE_SECONDS = 0.3
DEFAULT_PACE_SECONDS = 0.6  # per host; neither API publishes a stricter anonymous limit
DEFAULT_MAX_REQUESTS = 60  # the full run needs ~35, including dimension probes
PROBE_BYTES = 512 * 1024
LOGGED_PARAMS = {"gsrsearch", "q", "gsrlimit", "media_type", "page_size"}
KEYRING_FAIL_BACKEND = "keyring.backends.fail.Keyring"
SKIPPED = {"skipped_after_429", "skipped_request_cap"}


class LiveCheckLimit(httpx.TransportError):
    """Raised by the recorder instead of sending; production maps it to a provider error."""


class Recorder(httpx.BaseTransport):
    """Wraps the real transport: per-host pacing, a hard request cap, no
    further requests to a host after 429, and a sanitized log (no headers or
    bodies are kept; only rank/size fields of Commons search pages)."""

    def __init__(self, run: LiveRun, inner: httpx.BaseTransport, rewrite: Callable | None = None):
        self.run, self.inner, self.rewrite = run, inner, rewrite

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        run, host = self.run, request.url.host
        if self.rewrite:
            request = self.rewrite(request)
        entry: dict[str, Any] = {
            "host": host,
            "path": request.url.path[:160],
            "params": {k: v[:200] for k, v in request.url.params.items() if k in LOGGED_PARAMS},
        }
        if host in run.rate_limited_hosts:
            entry["error"] = "skipped_after_429"
            run.requests.append(entry)
            raise LiveCheckLimit(f"{host} answered 429 earlier in this run", request=request)
        if run.sent >= run.max_requests:
            entry["error"] = "skipped_request_cap"
            run.requests.append(entry)
            raise LiveCheckLimit("live-check request cap reached", request=request)
        wait = run.pace - (run.clock() - run.last_request.get(host, -1e9))
        if wait > 0:
            run.sleep(wait)
        run.sent += 1
        started = run.clock()
        try:
            response = self.inner.handle_request(request)
        except Exception as exc:  # recorded, then re-raised for the production error mapping
            entry.update(error=type(exc).__name__, ms=int((run.clock() - started) * 1000))
            run.requests.append(entry)
            run.last_request[host] = run.clock()
            raise
        run.last_request[host] = run.clock()
        entry.update(status=response.status_code, ms=int((run.clock() - started) * 1000))
        if response.status_code == 429:
            entry["retry_after"] = response.headers.get("retry-after")
            run.rate_limited_hosts.add(host)
        json_like = host in {"commons.wikimedia.org", "images-api.nasa.gov"} or request.url.path.endswith(".json")
        if json_like and response.status_code == 200:
            response.read()
            try:
                data = json.loads(response.content)
            except ValueError:
                data = None
            if host == "commons.wikimedia.org" and isinstance(data, dict):
                if isinstance(data.get("error"), dict):
                    entry["api_error_code"] = data["error"].get("code")
                query = data.get("query") if isinstance(data.get("query"), dict) else {}
                pages = query.get("pages") if isinstance(query.get("pages"), dict) else {}
                entry["commons_pages"] = [_page_summary(page) for page in pages.values() if isinstance(page, dict)]
        run.requests.append(entry)
        return response


def _page_summary(page: dict[str, Any]) -> dict[str, Any]:
    info = next(iter(page.get("imageinfo") or []), None)
    info = info if isinstance(info, dict) else {}
    return {
        "pageid": page.get("pageid"),
        "index": page.get("index"),
        "title": str(page.get("title"))[:120],
        "mime": info.get("mime"),
        "thumb": [info.get("thumbwidth"), info.get("thumbheight")],
        "original": [info.get("width"), info.get("height")],
    }


def _search_rank(page: dict[str, Any]) -> int:
    return page["index"] if isinstance(page.get("index"), int) else 10**6


class LiveRun:
    """One self-contained run: its own report, request log, clients and limits."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        pace: float = DEFAULT_PACE_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.transport_factory = transport_factory or (lambda: httpx.HTTPTransport(retries=0))
        self.pace = max(MIN_PACE_SECONDS, float(pace))
        self.max_requests = max(1, int(max_requests))
        self.sleep, self.clock = sleep, clock
        self.sent = 0
        self.last_request: dict[str, float] = {}
        self.rate_limited_hosts: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.sections: dict[str, Any] = {}

    # -- reporting ---------------------------------------------------------

    def check(self, name: str, ok: bool | None, detail: Any = None) -> None:
        status = "INFO" if ok is None else "PASS" if ok else "FAIL"
        self.checks.append({"check": name, "status": status, "detail": detail})
        print(f"[{status}] {name}" + (f" -- {detail}" if detail is not None else ""))

    def counts(self) -> dict[str, int]:
        return {status: sum(c["status"] == status for c in self.checks) for status in ("PASS", "FAIL", "INFO")}

    # -- clients -----------------------------------------------------------

    def http(self, rewrite: Callable | None = None, **kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=Recorder(self, self.transport_factory(), rewrite), **kwargs)

    def commons_client(self, rewrite: Callable | None = None) -> WikimediaMediaClient:
        return WikimediaMediaClient(client=self.http(
            rewrite, timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=True,
            headers={"User-Agent": COMMONS_USER_AGENT},
        ))

    def nasa_provider(self, timeout: httpx.Timeout | None = None) -> NASAProvider:
        return NASAProvider(client=self.http(timeout=timeout or httpx.Timeout(10, connect=4), follow_redirects=True))

    def probe_dimensions(self, client: httpx.Client, address: str) -> list[int] | str | None:
        """Pixel size from at most PROBE_BYTES of the real file; failures are reported, not raised."""
        parser, consumed = ImageFile.Parser(), 0
        try:
            with client.stream("GET", address) as response:
                if response.status_code != 200:
                    return f"probe_http_{response.status_code}"
                for chunk in response.iter_bytes(16384):
                    consumed += len(chunk)
                    parser.feed(chunk)
                    if parser.image:
                        return list(parser.image.size)
                    if consumed > PROBE_BYTES:
                        return None
        except Exception as exc:  # noqa: BLE001 - diagnostic probe only
            return f"probe_failed:{type(exc).__name__}"
        return None

    # -- checks ------------------------------------------------------------

    @staticmethod
    def summarize(candidate: Any) -> dict[str, Any]:
        meta = asset_metadata(candidate)
        return {
            "identity": candidate.identity, "title": candidate.title[:100], "rank": candidate.rank,
            "dimensions": [candidate.width, candidate.height], "url": candidate.download_url[:200],
            "source_url": candidate.source_url[:200], "search_mode": candidate.origin.get("search_mode"),
            "same_as": candidate.origin.get("same_as"), "license": meta["license"],
            "attribution_required": meta["attribution"]["required"], "usage_restrictions": meta["usage_restrictions"],
        }

    def rights_checks(self, label: str, candidates: list) -> None:
        statuses = [evaluate_rights(c.rights).status for c in candidates]
        self.check(f"{label}: rights status counts", None, {s: statuses.count(s) for s in sorted(set(statuses))})
        metas = [asset_metadata(c) for c in candidates]
        populated = all(m["source"] and m["asset_type"] and m["license"]["status"] and "usage_restrictions" in m for m in metas)
        self.check(f"{label}: asset_metadata populated for every candidate", populated if candidates else None, len(metas))
        uncleared = [c for c in candidates if evaluate_rights(c.rights).status != "usable"]
        ledger = CandidateLedger()
        leaked = [c.identity for c in uncleared if ledger.admit(c) or asset_metadata(c)["license"]["status"] == "usable"]
        self.check(f"{label}: no uncleared asset admitted or reported reusable", not leaked, {"uncleared": len(uncleared), "leaked": leaked})
        # An empty restriction list on an uncleared asset is itself a failure, never an IndexError.
        unlabelled = [
            m["source_url"] for m in metas
            if m["license"]["status"] != "usable"
            and not str((m["usage_restrictions"] or [""])[0]).startswith("not_cleared:")
        ]
        self.check(f"{label}: uncleared assets carry not_cleared restriction", not unlabelled if uncleared else None, unlabelled or None)

    def commons_direct(self, label: str, query: str, *, expect_relaxed: bool = False, budget: AcquisitionBudget | None = None) -> list:
        client, budget = self.commons_client(), budget or AcquisitionBudget()
        first = len(self.requests)
        try:
            results = ProviderAdapter("wikimedia", client).search(query, "photo", portrait=True, scene_duration=4, budget=budget)
            error = None
        except MediaProviderError as exc:
            results, error = [], exc.category
        finally:
            client.close()
        # Requests that reached the network (answered or failed), not ones the run's own limits skipped.
        sent = [
            r for r in self.requests[first:]
            if r["host"] == "commons.wikimedia.org" and r.get("error") not in SKIPPED
        ]
        section = {
            "query": query, "error": error, "api_requests": len(sent), "budget": budget.snapshot(),
            "searches": [c["params"].get("gsrsearch") for c in sent],
            "raw_hits_per_request": [len(c.get("commons_pages") or []) for c in sent],
            "candidates": [self.summarize(c) for c in results],
        }
        self.sections[label] = section
        print(f"\n== {label}: '{query}' -> {len(results)} candidates, raw hits {section['raw_hits_per_request']}, error={error}")
        for row in section["candidates"][:5]:
            print(f"   {row['identity']:<28} {row['dimensions']} {row['license']['status']:<8} {row['license']['id']} :: {row['title']}")
        self.check(f"{label}: request completed without provider error", error is None, error)
        self.check(f"{label}: physical requests == budget search_requests", len(sent) <= budget.search_requests and (error is not None or len(sent) == budget.search_requests), [len(sent), budget.search_requests])
        if sent and sent[-1].get("commons_pages"):
            raw = sent[-1]["commons_pages"]
            expected = [str(p["pageid"]) for p in sorted(raw, key=_search_rank)]
            got = [c.provider_id for c in results]
            # Production adds a +20 orientation bonus (portrait shorts prefer
            # height >= width); the search index must hold inside each class.
            classes = {c.provider_id: c.height >= c.width for c in results}
            in_order = all(
                [pid for pid in got if classes[pid] == cls] == [pid for pid in expected if pid in classes and classes[pid] == cls]
                for cls in (True, False)
            )
            self.check(f"{label}: candidates follow API search index (within orientation class)", in_order,
                       {"api_order": expected, "candidate_order": got, "portrait_class": [pid for pid in got if classes[pid]]})
            by_id = {str(p["pageid"]): p for p in raw}
            mismatched = [c.provider_id for c in results if by_id.get(c.provider_id, {}).get("thumb") not in ([c.width, c.height], [None, None])]
            self.check(f"{label}: candidate dimensions == API thumbwidth/thumbheight", not mismatched, mismatched or None)
        if results:
            probe_client = self.http(timeout=httpx.Timeout(15, connect=5), follow_redirects=True, headers={"User-Agent": COMMONS_USER_AGENT})
            try:
                probes = [
                    {"identity": c.identity, "reported": [c.width, c.height], "actual": self.probe_dimensions(probe_client, c.download_url)}
                    for c in results[:2]
                ]
            finally:
                probe_client.close()
            section["dimension_probes"] = probes
            self.check(f"{label}: downloaded image size == reported size (top 2)", all(p["actual"] == p["reported"] for p in probes), probes)
            self.rights_checks(label, results)
        if expect_relaxed:
            if sent and sent[0].get("commons_pages"):
                self.check(f"{label}: strict query was not zero-hit (relaxation not exercised)", None, len(sent[0]["commons_pages"]))
            elif error is not None:
                self.check(f"{label}: relaxation not exercised (provider error)", None, error)
            else:
                relaxed = len(sent) == 2 and " OR " in (sent[1]["params"].get("gsrsearch") or "")
                self.check(f"{label}: zero-hit strict search issued exactly one any-keyword request", relaxed, section["searches"])
                self.check(f"{label}: any-keyword request returned hits", bool(len(sent) == 2 and sent[1].get("commons_pages")), section["raw_hits_per_request"])
                self.check(f"{label}: relaxed candidates marked search_mode=any_keyword",
                           all(c.origin.get("search_mode") == "any_keyword" for c in results) if results else None)
        return results

    def nasa_direct(self, query: str) -> list:
        provider, budget = self.nasa_provider(), AcquisitionBudget()
        first = len(self.requests)
        try:
            results, error = provider.search(query, "photo", portrait=True, scene_duration=4, budget=budget), None
        except MediaProviderError as exc:
            results, error = [], exc.category
        calls = [r for r in self.requests[first:] if r["host"] in {"images-api.nasa.gov", "images-assets.nasa.gov"}]
        api_calls = [r for r in calls if not r["path"].endswith((".jpg", ".jpeg", ".png")) and r.get("error") not in SKIPPED]
        usable = [c for c in results if evaluate_rights(c.rights).status == "usable"]
        self.sections["nasa_direct"] = {
            "query": query, "error": error, "requests": len(calls), "budget": budget.snapshot(),
            "candidates": [self.summarize(c) | {"rights_evidence": {k: v for k, v in c.rights.evidence.items() if k != "title"}} for c in results],
        }
        print(f"\n== nasa_direct: '{query}' -> {len(results)} candidates ({len(usable)} rights-usable), {len(calls)} requests, error={error}")
        for c in results[:6]:
            print(f"   {c.identity:<36} {[c.width, c.height]} {evaluate_rights(c.rights).status:<8} :: {c.title[:70]}")
        self.check("nasa_direct: request completed without provider error", error is None, error)
        self.check("nasa_direct: physical requests <= budget search_requests (+probes as downloads)",
                   len(api_calls) == budget.search_requests, [len(api_calls), budget.snapshot()])
        bad_pd = [c.identity for c in usable if not (c.rights.evidence.get("XMP:Marked") is False and c.rights.evidence.get("AVAIL:NASAID") == c.provider_id)]
        self.check("nasa_direct: every usable NASA asset has item XMP Marked=false + matching NASA ID", not bad_pd if usable else None, bad_pd or len(usable))
        self.check("nasa_direct: at least one rights-usable NASA asset", bool(usable) if results else None, len(usable))
        if usable:
            probe_client = self.http(timeout=httpx.Timeout(20, connect=5), follow_redirects=True)
            try:
                probes = [
                    {"identity": c.identity, "reported": [c.width, c.height], "actual": self.probe_dimensions(probe_client, c.download_url)}
                    for c in usable[:1]
                ]
            finally:
                probe_client.close()
            self.sections["nasa_direct"]["dimension_probes"] = probes
            self.check("nasa_direct: original image size == reported size",
                       all(p["actual"] == p["reported"] or p["reported"] == [0, 0] for p in probes), probes)
        if results:
            self.rights_checks("nasa_direct", results)
        provider.close()
        return results

    def routed_scene(self, label: str, scene: dict, registry: ProviderRegistry, *, expect_reason: str) -> dict:
        state = {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {}}
        plan = media.build_visual_query_plan(scene, state)
        try:
            result = media.run_staged_scene_search(
                scene["search_queries"], scene, state, plan, pexels=None, wikimedia=registry.get("wikimedia"),
                registry=registry, preferred_kind="photo", portrait=True, scene_duration=4, used=set(),
                verifier=media._METADATA_ONLY_VERIFIER, budget=2, acquisition_budget=AcquisitionBudget(),
            )
        finally:
            registry.close()
        prov = result.provenance
        stages = [{
            "query": stage["query"], "routed_providers": stage["routed_providers"], "providers": stage["providers"],
            "errors": stage["errors"], "coverage": stage["coverage"],
            "candidate_evidence": [
                {k: e.get(k) for k in ("identity", "title", "rights_status", "rights_reason", "license_id",
                                       "usage_restrictions", "dimensions", "metadata_confidence", "metadata_score")}
                for e in stage["candidate_evidence"]
            ],
        } for stage in prov["stages"]]
        self.sections[label] = {
            "stop_reason": prov["stop_reason"], "acquisition_budget": prov["acquisition_budget"],
            "duplicate_count": prov["duplicate_count"], "visual_verification": prov["visual_verification"], "stages": stages,
        }
        print(f"\n== {label}: stop={prov['stop_reason']} budget={prov['acquisition_budget']['used']}")
        for stage in stages:
            print(f"   query '{stage['query']}' routed={[(r['provider'], r['reason'], r['tier']) for r in stage['routed_providers']]}")
            for stats in stage["providers"]:
                print(f"     {stats['provider']:<10} requests={stats['requests']} returned={stats['returned']} rights_rejects={stats['rights_rejects']} "
                      f"dedupe_rejects={stats['dedupe_rejects']} relevance_rejects={stats['relevance_rejects']} failure={stats.get('failure')}")
        used, limits = prov["acquisition_budget"]["used"], prov["acquisition_budget"]["limits"]
        self.check(f"{label}: acquisition budget respected", all(used[k] <= limits[k] for k in used), {"used": used, "limits": limits})
        reasons = {r["reason"] for stage in stages for r in stage["routed_providers"] if r["tier"] == 0}
        self.check(f"{label}: routing reason == {expect_reason}", expect_reason in reasons, sorted(reasons))
        admitted = [c for c in result.candidates if evaluate_rights(c.rights).status != "usable"]
        self.check(f"{label}: no uncleared candidate admitted to the pool", not admitted, [c.identity for c in admitted])
        errors = [s for stage in stages for s in stage["providers"] if s.get("failure")]
        self.check(f"{label}: provider errors are recorded in diagnostics (not hidden)", None, [(s["provider"], s["failure"]) for s in errors] or "none")
        return prov

    def error_envelope(self) -> None:
        def bad_limit(request: httpx.Request) -> httpx.Request:
            return httpx.Request(request.method, request.url.copy_set_param("gsrlimit", "bogus"), headers=request.headers)

        client = self.commons_client(bad_limit)
        first = len(self.requests)
        try:
            client.search_photos("mars", portrait=True)
            envelope = None
        except MediaProviderError as exc:
            envelope = exc.category
        finally:
            client.close()
        last = self.requests[-1] if len(self.requests) > first else {}
        self.check("errors: real MediaWiki HTTP-200 error envelope -> provider_error (not empty)",
                   envelope == "provider_error" and last.get("status") == 200,
                   {"category": envelope, "http_status": last.get("status"), "api_error_code": last.get("api_error_code"),
                    "request_error": last.get("error")})

    # -- scenario ----------------------------------------------------------

    def execute(self) -> None:
        clear_search_cache()  # process-local NASA/open-media cache and cooldowns start empty
        # 1/5/6/7: Mars imagery on Commons (rank, real dimensions, licenses)
        mars = self.commons_direct("commons_mars", "Mars surface rocks")
        # 2: general everyday scene
        self.commons_direct("commons_everyday", "pouring water glass kitchen")
        # 3: zero-hit fallback (long natural query) + budget headroom guard
        self.commons_direct("commons_zero_hit", "rust colored dust lifting dry ground", expect_relaxed=True)
        self.commons_direct("commons_zero_hit_tight_budget", "rust colored dust lifting dry ground",
                            budget=AcquisitionBudget(max_search_requests=4))
        tight = self.sections["commons_zero_hit_tight_budget"]
        if tight["error"] is None and not (tight["raw_hits_per_request"] or [1])[0]:
            self.check("commons_zero_hit_tight_budget: no any-keyword request without 4 free slots", tight["api_requests"] == 1, tight["searches"])
        else:
            self.check("commons_zero_hit_tight_budget: guard not exercised (strict search errored or had hits)", None, tight["error"])
        # 4/7: NASA rights + dimensions, then NASA <-> Commons mirror dedupe
        nasa = self.nasa_direct("Mars surface rover")
        mirrors = [c for c in mars if c.origin.get("same_as")]
        shared = {a["canonical_id"] for c in mirrors for a in c.origin["same_as"]} & {c.provider_id for c in nasa}
        self.check("dedupe: Commons files naming a NASA ID (same_as)", None,
                   [(c.identity, c.origin["same_as"][0]["canonical_id"]) for c in mirrors] or "none in this result page")
        self.check("dedupe: NASA/Commons overlap found in this live sample", None, sorted(shared) or "no overlap in sample")
        # 4: routed production search (the visual-acquisition.json path)
        self.routed_scene("routed_mars_scene", {
            "id": "s1", "start": 0, "end": 4, "preferred_media": "photo",
            "narration": "Iron oxide dust covers the rocky surface of the planet Mars.",
            "visual_goal": "rocky red surface of the planet Mars",
            "search_queries": ["mars surface rocks", "martian red dust planet surface"],
        }, ProviderRegistry([self.nasa_provider(), ProviderAdapter("wikimedia", self.commons_client(), owned=True)]),
            expect_reason="space_or_earth_observation")
        self.routed_scene("routed_everyday_scene", {
            "id": "s2", "start": 0, "end": 4, "preferred_media": "photo",
            "narration": "Pour the water slowly into the glass.", "visual_goal": "person pouring water into a glass in a kitchen",
            "search_queries": ["pouring water glass kitchen"],
        }, ProviderRegistry([ProviderAdapter("wikimedia", self.commons_client(), owned=True)]),
            expect_reason="everyday_action_or_general_photo")
        # 8: real failures are reported and the other source continues
        prov = self.routed_scene("routed_mars_nasa_timeout", {
            "id": "s3", "start": 0, "end": 4, "preferred_media": "photo",
            "narration": "Dust storms sweep across the planet Mars.", "visual_goal": "dust storm on the planet Mars",
            "search_queries": ["mars dust storm"],
        }, ProviderRegistry([self.nasa_provider(timeout=httpx.Timeout(0.001)),
                             ProviderAdapter("wikimedia", self.commons_client(), owned=True)]),
            expect_reason="space_or_earth_observation")
        first = prov["stages"][0]["providers"] if prov["stages"] else []
        nasa_failure = next((s.get("failure") for s in first if s["provider"] == "nasa"), None)
        commons_ran = any(s["provider"] == "wikimedia" and s["requests"] for s in first)
        self.check("fallback: NASA timeout recorded and Commons still searched", bool(nasa_failure) and commons_ran,
                   {"nasa_failure": nasa_failure, "commons_ran": commons_ran})
        self.error_envelope()

    def report(self, source: str) -> dict[str, Any]:
        hosts: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for row in self.requests:
            hosts[row["host"]] = hosts.get(row["host"], 0) + 1
            key = str(row.get("status") or row.get("error"))
            statuses[key] = statuses.get(key, 0) + 1
        return {
            "tool": "visual_sources_live_check", "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": sys.version.split()[0], "code_under_test": source,
            "limits": {"pace_seconds": self.pace, "max_requests": self.max_requests, "requests_sent": self.sent,
                       "rate_limited_hosts": sorted(self.rate_limited_hosts)},
            "summary": self.counts(), "request_totals": hosts, "status_totals": statuses,
            "checks": self.checks, "sections": self.sections, "requests": self.requests,
        }


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Atomic write: a re-run never leaves a half-written or mixed report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, default=str)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def default_output() -> Path:
    # Unique per run, so repeated runs never overwrite each other's evidence.
    return Path("/tmp") / f"clipforge-visual-live-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}.json"


def main(argv: list[str] | None = None, *, transport_factory: Callable[[], httpx.BaseTransport] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=None, help="JSON report path (default: /tmp/clipforge-visual-live-<time>.json)")
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE_SECONDS, help=f"seconds between requests per host (min {MIN_PACE_SECONDS})")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help="hard cap on outgoing requests for this run")
    args = parser.parse_args(argv)
    # Nothing here reads settings or secrets; make any accidental keyring use fail loudly.
    os.environ["PYTHON_KEYRING_BACKEND"] = KEYRING_FAIL_BACKEND
    import clipforge

    source = str(Path(clipforge.__file__).resolve().parent)
    if Path(source) != (API_ROOT / "clipforge").resolve():
        print(f"ABORT: clipforge imported from {source}, expected {API_ROOT / 'clipforge'}")
        return 2
    print(f"code under test: {source}  python {sys.version.split()[0]}")
    run = LiveRun(transport_factory=transport_factory, pace=args.pace, max_requests=args.max_requests, sleep=sleep)
    try:
        run.execute()
    except Exception as exc:  # noqa: BLE001 - still write what was gathered
        run.check("run: completed without an unexpected exception", False, f"{type(exc).__name__}: {exc}"[:300])
    output = args.output or default_output()
    report = run.report(source)
    write_report(output, report)
    counts = report["summary"]
    print(f"\nrequests sent: {run.sent}  by host: {report['request_totals']}  statuses: {report['status_totals']}")
    print(f"checks: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['INFO']} INFO")
    print(f"full report: {output}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
