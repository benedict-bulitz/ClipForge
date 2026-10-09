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
    mars = sections["routed_mars_scene"]["stages"][0]["providers"]
    assert {row["provider"]: row["dedupe_rejects"] for row in mars}["wikimedia"] == 1  # NASA mirror
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
