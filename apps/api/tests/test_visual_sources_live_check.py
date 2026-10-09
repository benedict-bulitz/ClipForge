"""Offline tests for scripts/visual_sources_live_check.py (no network, no keys)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "visual_sources_live_check.py"
ALLOWED_HOSTS = {"commons.wikimedia.org", "upload.wikimedia.org", "images-api.nasa.gov", "images-assets.nasa.gov"}


@pytest.fixture()
def live():
    spec = importlib.util.spec_from_file_location("visual_sources_live_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (180, 80, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def ext(**values):
    return {key: {"value": value} for key, value in values.items()}


CC_BY = ext(License="cc-by-4.0", LicenseShortName="CC BY 4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/",
            Artist="Ann", AttributionRequired="true")
CC0 = ext(License="cc-zero", LicenseShortName="CC0", LicenseUrl="https://creativecommons.org/publicdomain/zero/1.0/")


def page(pageid, index, title, width, height, metadata):
    return {"pageid": pageid, "ns": 6, "title": f"File:{title}", "index": index, "imageinfo": [{
        "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{title}",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
        "width": width * 3, "height": height * 3, "mime": "image/jpeg",
        "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{title}/1600px-{title}-{width}x{height}.jpg",
        "thumbwidth": width, "thumbheight": height, "extmetadata": metadata,
    }]}


# Keyed by page id (API order is NOT the search rank); index carries the rank.
PAGES = {
    "30": page(30, 3, "Mars_rocks.jpg", 1600, 1200, CC_BY),
    "10": page(10, 1, "PIA24546_Mars_surface.jpg", 1600, 1067, CC0),
    "20": page(20, 2, "Mars_no_license.jpg", 1600, 900, ext(Artist="Bob")),
    "40": page(40, 4, "Mars_trademark.jpg", 1600, 1600, CC_BY | ext(Restrictions="trademarked")),
}


def commons_answer(request: httpx.Request) -> dict:
    search = request.url.params.get("gsrsearch", "")
    if request.url.params.get("gsrlimit") == "bogus":
        return {"error": {"code": "badinteger", "info": "Invalid value"}}
    if " OR " not in search and ("rust colored" in search or "martian red dust" in search):
        return {"batchcomplete": ""}
    return {"batchcomplete": "", "query": {"pages": PAGES}}


def nasa_answer(path: str) -> dict:
    if path == "/search":
        return {"collection": {"items": [{
            "data": [{"nasa_id": nasa_id, "media_type": "image", "title": f"Mars {nasa_id}", "description": "Mars surface",
                      "keywords": ["Mars"], "center": "JPL"}],
            "links": [{"href": f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~thumb.jpg", "rel": "preview", "render": "image"}],
        } for nasa_id in ("PIA24546", "PIA00001", "PIA00002")]}}
    nasa_id = path.rsplit("/", 1)[-1]
    if path.startswith("/metadata/"):
        return {"location": f"https://images-assets.nasa.gov/image/{nasa_id}/metadata.json"}
    return {"collection": {"items": [{"href": f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~orig.jpg"}]}}


def canned(request: httpx.Request) -> httpx.Response:
    """Official response shapes for Commons, NASA search/metadata/asset and image bytes."""
    if (request.extensions.get("timeout", {}).get("connect") or 1) < 0.01:
        raise httpx.ConnectTimeout("simulated", request=request)
    host, path = request.url.host, request.url.path
    assert host in ALLOWED_HOSTS, host
    if host == "commons.wikimedia.org":
        return httpx.Response(200, json=commons_answer(request), request=request)
    if host == "images-api.nasa.gov":
        return httpx.Response(200, json=nasa_answer(path), request=request)
    if path.endswith("metadata.json"):
        nasa_id = path.split("/")[2]
        metadata = {"AVAIL:NASAID": nasa_id, "File:ImageWidth": 2000, "File:ImageHeight": 1500}
        if nasa_id != "PIA00002":  # PIA00002 has no rights statement: must stay uncleared
            metadata["XMP:Marked"] = False
        return httpx.Response(200, json=metadata, request=request)
    if host == "images-assets.nasa.gov":
        return httpx.Response(200, content=jpeg(2000, 1500) if "~orig" in path else jpeg(640, 480), request=request)
    width, height = path.rsplit("-", 1)[-1].removesuffix(".jpg").split("x")
    return httpx.Response(200, content=jpeg(int(width), int(height)), request=request)


def run(live, tmp_path, *args, handler=canned, sleeps=None):
    output = tmp_path / "report.json"
    code = live.main(["--output", str(output), *args],
                     transport_factory=lambda: httpx.MockTransport(handler),
                     sleep=(sleeps.append if sleeps is not None else lambda _seconds: None))
    return code, json.loads(output.read_text())


def statuses(report, status):
    return [c["check"] for c in report["checks"] if c["status"] == status]


def test_full_canned_run_passes_every_check_and_writes_json(live, tmp_path):
    code, report = run(live, tmp_path)
    assert code == 0, statuses(report, "FAIL")
    assert report["summary"]["FAIL"] == 0 and report["summary"]["PASS"] >= 45
    assert set(report["request_totals"]) <= ALLOWED_HOSTS  # no paid or keyed providers
    assert report["limits"]["requests_sent"] <= report["limits"]["max_requests"]
    sections = report["sections"]
    assert sections["commons_zero_hit"]["searches"][1] == "filetype:bitmap rust OR colored OR dust OR lifting OR dry OR ground"
    assert sections["commons_zero_hit_tight_budget"]["api_requests"] == 1
    assert sections["nasa_direct"]["candidates"][2]["license"]["status"] == "unknown"
    mars = sections["routed_mars_scene"]["stages"][0]
    assert [(r["provider"], r["tier"]) for r in mars["routed_providers"]] == [("wikimedia", 0), ("nasa", 1)]
    assert {row["provider"]: row["dedupe_rejects"] for row in mars["providers"]}["nasa"] == 1  # NASA copy of the Commons mirror
    assert "routed_mars_nasa_timeout" in sections and report["code_under_test"].endswith("clipforge")
    assert "Bob" not in json.dumps(report["requests"])  # request log keeps no response bodies


def test_offline_run_reports_failures_and_never_crashes(live, tmp_path):
    def offline(request):
        raise httpx.ConnectError("no route", request=request)

    code, report = run(live, tmp_path, handler=offline)
    assert code == 1
    assert "run: completed without an unexpected exception" not in statuses(report, "FAIL")
    assert "commons_mars: request completed without provider error" in statuses(report, "FAIL")
    assert report["sections"]["commons_mars"]["error"] == "network_error"
    assert {row.get("error") for row in report["requests"]} == {"ConnectError"}
    # No retry storm: every logical request is attempted once.
    assert report["limits"]["requests_sent"] == len(report["requests"]) < 20


def test_request_cap_is_hard_and_reported(live, tmp_path):
    code, report = run(live, tmp_path, "--max-requests", "5")
    assert code == 1 and report["limits"]["requests_sent"] == 5
    skipped = [row for row in report["requests"] if row.get("error") == "skipped_request_cap"]
    assert skipped and all("status" not in row for row in skipped)


def test_host_answering_429_is_not_contacted_again(live, tmp_path):
    seen = []

    def limited(request):
        if request.url.host == "commons.wikimedia.org":
            seen.append(request.url)
            return httpx.Response(429, headers={"retry-after": "120"}, request=request)
        return canned(request)

    code, report = run(live, tmp_path, handler=limited)
    assert code == 1 and len(seen) == 1
    assert report["limits"]["rate_limited_hosts"] == ["commons.wikimedia.org"]
    commons = [row for row in report["requests"] if row["host"] == "commons.wikimedia.org"]
    assert commons[0]["status"] == 429 and commons[0]["retry_after"] == "120"
    assert all(row.get("error") == "skipped_after_429" for row in commons[1:])
    assert report["sections"]["commons_mars"]["error"] == "rate_limited"


def test_pacing_is_per_host_and_has_a_floor(live, tmp_path):
    sleeps: list[float] = []
    _, report = run(live, tmp_path, "--pace", "0", sleeps=sleeps)
    assert report["limits"]["pace_seconds"] == live.MIN_PACE_SECONDS
    assert sleeps and max(sleeps) <= live.MIN_PACE_SECONDS


def test_repeated_runs_are_independent(live, tmp_path):
    first_code, first = run(live, tmp_path)
    second_code, second = run(live, tmp_path)
    assert first_code == second_code == 0
    assert first["summary"] == second["summary"]
    assert len(first["requests"]) == len(second["requests"])  # nothing accumulates across runs
    assert live.default_output() != live.default_output()
    assert str(live.default_output()).startswith("/tmp/clipforge-visual-live-")


def test_empty_usage_restrictions_fail_the_check_instead_of_raising(live, monkeypatch):
    from dataclasses import replace

    from test_staged_media_search import cand

    from clipforge.visual_rights import MediaRights

    original = live.asset_metadata

    def without_restrictions(candidate):
        return original(candidate) | {"usage_restrictions": []}

    monkeypatch.setattr(live, "asset_metadata", without_restrictions)
    run_ = live.LiveRun(transport_factory=lambda: httpx.MockTransport(canned), sleep=lambda _s: None)
    unknown = replace(cand("x", "mars", "Mars", kind="photo", provider="openverse"), rights=MediaRights())
    run_.rights_checks("probe", [unknown])
    check = next(c for c in run_.checks if c["check"] == "probe: uncleared assets carry not_cleared restriction")
    assert check["status"] == "FAIL"


def test_wrong_source_tree_aborts_before_any_request(live, tmp_path, monkeypatch):
    monkeypatch.setattr(live, "API_ROOT", tmp_path)
    calls = []
    output = tmp_path / "never.json"
    code = live.main(["--output", str(output)], transport_factory=lambda: httpx.MockTransport(lambda r: calls.append(r)))
    assert code == 2 and not calls and not output.exists()


def test_keyring_is_forced_to_the_failing_backend(live, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.macOS.Keyring")
    run(live, tmp_path)
    import os

    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"


def test_report_write_is_atomic(live, tmp_path):
    target = tmp_path / "nested" / "report.json"
    live.write_report(target, {"ok": True})
    live.write_report(target, {"ok": False})
    assert json.loads(target.read_text()) == {"ok": False}
    assert [path.name for path in target.parent.iterdir()] == ["report.json"]  # no temp leftovers


# --- delivered-size verdicts, NASA coverage and demotion checks ---------------------------


@pytest.mark.parametrize("reported,actual,verdict", [
    ([1600, 900], [1600, 900], "exact"),
    ([1600, 900], [1600, 899], "rounding"),
    ([1600, 900], [1599, 901], "rounding"),
    ([1600, 1772], [1287, 1425], "overstated"),  # the live defect: 1600px claimed, 1287px delivered
    ([1600, 900], [1600, 897], "overstated"),  # 3px over: beyond harmless rounding
    ([1000, 700], [1280, 900], "understated"),
    ([1000, 2000], [2000, 1000], "swapped"),
    ([0, 0], [1920, 1080], "unknown_reported"),
    ([1600, 900], "probe_http_403", "probe_error"),
    ([1600, 900], "probe_failed:ReadTimeout", "probe_error"),
    ([1600, 900], None, "unreadable"),
])
def test_dimension_classification(live, reported, actual, verdict):
    assert live.classify_dimensions(reported, actual) == verdict


def new_run(live):
    return live.LiveRun(transport_factory=lambda: httpx.MockTransport(canned), sleep=lambda _s: None)


def verdicts(run_, label):
    return {c["check"]: c["status"] for c in run_.checks if c["check"].startswith(label)}


def test_probe_failures_are_reported_separately_and_never_pass_as_matches(live):
    run_ = new_run(live)
    run_.dimension_check("probe", [
        {"identity": "a", "reported": [1600, 900], "actual": "probe_http_403"},
        {"identity": "b", "reported": [1600, 900], "actual": None},
    ])
    status = verdicts(run_, "probe")
    assert status["probe: dimension probe failures (not size results)"] == "INFO"
    # Nothing was measured: neither a mismatch (FAIL) nor a match (PASS).
    assert status["probe: reported size never exceeds delivered size (+-1px)"] == "INFO"
    assert run_.counts()["FAIL"] == 0


def test_overstated_sizes_fail_but_rounding_and_conservative_differences_do_not(live):
    ok = new_run(live)
    ok.dimension_check("ok", [
        {"identity": "a", "reported": [1920, 1080], "actual": [1920, 1080]},
        {"identity": "b", "reported": [1600, 1067], "actual": [1600, 1066]},  # harmless 1px rounding
        {"identity": "c", "reported": [1000, 700], "actual": [1280, 900]},  # conservative
    ])
    assert ok.counts()["FAIL"] == 0
    assert verdicts(ok, "ok")["ok: delivered size differs but is not overstated"] == "INFO"
    bad = new_run(live)
    bad.dimension_check("bad", [{"identity": "a", "reported": [1600, 1772], "actual": [1287, 1425]}])
    assert verdicts(bad, "bad")["bad: reported size never exceeds delivered size (+-1px)"] == "FAIL"
    assert bad.counts()["FAIL"] == 1


def test_run_fails_when_the_delivered_image_is_smaller_than_reported(live, tmp_path):
    def shrunk(request):
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=jpeg(700, 500), request=request)
        return canned(request)

    code, report = run(live, tmp_path, handler=shrunk)
    assert code == 1
    assert any("reported size never exceeds delivered size" in name for name in statuses(report, "FAIL"))
    assert all(c["detail"] for c in report["checks"] if c["status"] == "FAIL")


def test_unreachable_image_host_is_a_probe_note_not_a_size_failure(live, tmp_path):
    def forbidden(request):
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(403, request=request)
        return canned(request)

    code, report = run(live, tmp_path, handler=forbidden)
    assert code == 0, statuses(report, "FAIL")
    notes = [c for c in report["checks"] if c["check"].endswith("dimension probe failures (not size results)")]
    assert notes and all(c["status"] == "INFO" for c in notes)
    assert any(p["actual"] == "probe_http_403" for s in report["sections"].values() for p in s.get("dimension_probes", []))


def unestablished_rights(request):
    """Live behavior: NASA item metadata with only the NASA ID and no rights statement."""
    if request.url.host == "images-assets.nasa.gov" and request.url.path.endswith("metadata.json"):
        return httpx.Response(200, json={"AVAIL:NASAID": request.url.path.split("/")[2]}, request=request)
    return canned(request)


def test_unestablished_nasa_rights_are_reported_as_coverage_not_failure(live, tmp_path):
    code, report = run(live, tmp_path, handler=unestablished_rights)
    assert code == 0, statuses(report, "FAIL")
    coverage = report["sections"]["nasa_direct"]["coverage"]
    assert coverage["rights_status"] == {"unknown": 3} and coverage["metadata_enrichment"] == {"fetched": 3}
    assert coverage["rights_fields_present"] == {"AVAIL:NASAID": 3}
    assert coverage["rights_fields_missing"]["XMP:Marked"] == 3
    info = next(c for c in report["checks"] if c["check"].startswith("nasa_direct: rights-usable coverage"))
    assert info["status"] == "INFO"
    assert "nasa_direct: no uncleared asset admitted or reported reusable" in [c["check"] for c in report["checks"] if c["status"] == "PASS"]
    # Every unclear NASA item stays excluded from the routed pool.
    assert "routed_mars_scene: no uncleared candidate admitted to the pool" in [c["check"] for c in report["checks"] if c["status"] == "PASS"]


def test_missing_nasa_diagnostics_fail_the_run(live, tmp_path, monkeypatch):
    original = live.NASAProvider.search

    def stripped(self, *args, **kwargs):
        from dataclasses import replace

        results = original(self, *args, **kwargs)
        return [replace(r, rights=replace(r.rights, evidence={k: v for k, v in r.rights.evidence.items() if k != "enrichment"})) for r in results]

    monkeypatch.setattr(live.NASAProvider, "search", stripped)
    code, report = run(live, tmp_path)
    assert code == 1 and "nasa_direct: every candidate carries enrichment diagnostics" in statuses(report, "FAIL")


def test_routing_checks_require_nasa_to_trail_wikimedia(live, tmp_path):
    code, report = run(live, tmp_path)
    assert code == 0
    names = statuses(report, "PASS")
    assert "routed_mars_scene: NASA is routed after Wikimedia (fallback tier)" in names
    assert "routed_mars_nasa_timeout: NASA is routed after Wikimedia (fallback tier)" in names
    run_ = new_run(live)
    ahead = [{"routed_providers": [{"provider": "nasa", "tier": 0}, {"provider": "wikimedia", "tier": 0}],
              "providers": [{"provider": "nasa", "requests": 7}], "widening_reasons": []}]
    run_.nasa_demotion_checks("legacy", ahead)
    failed = [c["check"] for c in run_.checks if c["status"] == "FAIL"]
    assert "legacy: NASA is routed after Wikimedia (fallback tier)" in failed
    assert "legacy: no NASA request while the first tier gave strong coverage" in failed


def test_timeout_scenario_forces_the_nasa_fallback_tier(live, tmp_path):
    _, report = run(live, tmp_path)
    stage = report["sections"]["routed_mars_nasa_timeout"]["stages"][0]
    by_provider = {row["provider"]: row for row in stage["providers"]}
    assert by_provider["nasa"]["failure"] == "timeout" and by_provider["wikimedia"]["requests"] == 1
    assert "fallback: NASA timeout recorded and Commons still searched" in statuses(report, "PASS")
