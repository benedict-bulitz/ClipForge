"""Public LOC contract and denial isolation; no live requests or asset downloads."""

import json

import httpx
import pytest
from test_multi_source_visual_regression import historical_project
from test_staged_media_search import Commons, cand
from test_visual_sources_v2 import adapter, image_bytes, loc_row, payload, routed, search

from clipforge import media, open_media
from clipforge.config import Settings
from clipforge.open_media import LOCProvider
from clipforge.source_router import route_sources
from clipforge.visual_providers import (
    AcquisitionBudget,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderRegistry,
    create_provider_registry,
)
from clipforge.visual_rights import evaluate_rights


def test_loc_enabled_without_any_configured_provider_credentials():
    settings = Settings(
        _env_file=None, pexels_api_key=None, pixabay_api_key=None, europeana_api_key=None
    )
    registry = create_provider_registry(settings)
    try:
        source = registry.get("loc")
        assert isinstance(source, LOCProvider)
        assert source in registry.enabled("photo") and not source.disabled
        assert not any(k.casefold() in {"authorization", "x-api-key"} for k in source.headers())
    finally:
        registry.close()


def test_public_search_contract_encoding_and_normalization():
    requests = []
    query = "historical city & crossing / 1900"

    def handle(request):
        requests.append(request)
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        if request.url.host != "www.loc.gov":
            return httpx.Response(200, content=image_bytes())
        assert request.url.path == "/search/"
        assert dict(request.url.params) == {
            "q": query, "fo": "json", "c": "12", "sp": "1", "fa": "online-format:image"
        }
        assert request.headers["accept"] == "application/json"
        assert request.headers["user-agent"] == "ClipForge (public LOC JSON client)"
        return httpx.Response(200, json=payload(LOCProvider))

    budget = AcquisitionBudget(max_search_requests=1)
    result = search(adapter(LOCProvider, handle), budget, query)
    assert len(result) == 1 and budget.search_requests == 1
    item = result[0]
    assert item.provider == "loc" and item.provider_id == loc_row()["id"]
    assert item.creator == "Archive photographer" and item.origin["date"] == "1900"
    assert item.origin["collection"] == ["Historical photographs"]
    assert item.width == 900 and item.height == 1200
    assert evaluate_rights(item.rights).status == "usable"
    assert len(requests) == 2  # One JSON search, one bounded geometry probe.


@pytest.mark.parametrize("status,category", [
    (401, "provider_error"), (403, "provider_error"),
    (429, "rate_limited"), (503, "provider_error"), (302, "provider_error"),
])
def test_public_http_failures_have_safe_evidence_and_never_credential_semantics(status, category):
    def handle(request):
        return httpx.Response(status, content=b"<html>CAPTCHA secret-body-value</html>", headers={
            "Content-Type": "text/html", "Retry-After": "120", "Server": "edge",
            "Set-Cookie": "secret-cookie", "Authorization": "secret-response-auth",
            "Location": "https://www.loc.gov/search/?fo=json&api_key=secret-query",
        })

    source = adapter(LOCProvider, handle)
    budget = AcquisitionBudget()
    with pytest.raises(media.MediaProviderError) as caught:
        search(source, budget, "historical city")
    error = caught.value
    assert error.category == category
    evidence = error.diagnostics
    assert evidence["http_status"] == status and evidence["body_format"] == "html"
    assert evidence["captcha_indicated"]
    assert evidence["endpoint"] == "https://www.loc.gov/search/"
    assert evidence["parameters"]["q"] == "historical city"
    assert evidence["response_headers"]["retry-after"] == "120"
    assert evidence["redirects_followed"] == 0 and budget.search_requests == 1
    assert evidence["redirect_target"]["parameters"] == {"fo": "json"}
    assert "secret" not in json.dumps(evidence)
    assert source.disabled == (status in {401, 403})
    if source.disabled:
        assert search(source, budget, "different query") == []
        assert budget.search_requests == 1


@pytest.mark.parametrize("body", [b"not JSON", b"<html>CAPTCHA</html>", b"[]"])
def test_malformed_public_json_is_isolated_and_diagnostic(body):
    source = adapter(LOCProvider, lambda request: httpx.Response(200, content=body))
    with pytest.raises(media.MediaProviderError) as caught:
        search(source)
    assert caught.value.category == "malformed_response"
    assert caught.value.diagnostics["http_status"] == 200
    assert not source.disabled


@pytest.mark.parametrize("status", [401, 403, 429])
def test_public_download_denial_is_not_invalid_credentials(status, tmp_path):
    source = adapter(LOCProvider, lambda request: httpx.Response(
        status, stream=httpx.ByteStream(b"denied")
    ))
    item = source.normalize(payload(LOCProvider), "photo")[0]
    destination = tmp_path / "asset.jpg"
    with pytest.raises(media.MediaProviderError) as caught:
        source.download(item, destination, budget=AcquisitionBudget())
    assert caught.value.category == ("rate_limited" if status == 429 else "provider_error")
    assert caught.value.diagnostics["body_format"] == "unread_stream"
    assert not destination.exists() and not destination.with_suffix(".jpg.part").exists()


@pytest.mark.parametrize("failure,category", [
    (403, "provider_error"), (429, "rate_limited"),
    ("timeout", "timeout"), ("malformed", "malformed_response"),
])
@pytest.mark.parametrize("next_provider", ["europeana", "wikimedia", "openverse"])
def test_history_failure_continues_to_other_archival_sources(failure, category, next_provider):
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private request information", request=request)
        if failure == "malformed":
            return httpx.Response(200, content=b"not JSON")
        return httpx.Response(failure, headers={"Retry-After": "120"})

    loc = adapter(LOCProvider, handle)
    state = historical_project()
    scene = state["scenes"][0]
    query = scene["search_queries"][0]
    good = cand("archive", query, "Archival city barrier construction 1959",
                provider=next_provider, kind="photo")
    healthy = Commons(photos={query: [good]})
    healthy.capabilities = ProviderCapabilities(("photo",))
    sources = [loc, ProviderAdapter(next_provider, healthy)]
    empty = Commons()
    empty.capabilities = ProviderCapabilities(("photo",))
    if next_provider != "europeana":
        sources.append(ProviderAdapter("europeana", empty))
    registry = ProviderRegistry(sources)
    groups = route_sources(registry, scene, state, query, "photo")
    assert [s.adapter.provider for s in groups[0]] == ["loc", "europeana"]
    budget = AcquisitionBudget(max_search_requests=5)
    result = routed(state, registry, budget=budget)
    assert result.ranked[0][0].identity == good.identity
    assert len(requests) == 1 and len(healthy.calls) == 1
    assert budget.search_requests == (2 if next_provider == "europeana" else 3)
    stage = result.provenance["stages"][0]
    stats = next(s for s in stage["providers"] if s["provider"] == "loc")
    assert stats["requests"] == 1 and stats["failure"] == category
    if isinstance(failure, int):
        assert stats["failure_evidence"]["http_status"] == failure
    assert "private" not in json.dumps(result.provenance)
    assert bool(stage["widening_reasons"]) == (next_provider != "europeana")
    assert result.provenance["logical_queries_executed"] == 1


def test_public_denial_skips_only_loc_in_following_routes():
    loc = adapter(LOCProvider, lambda request: httpx.Response(403))
    with pytest.raises(media.MediaProviderError):
        search(loc)
    registry = ProviderRegistry([loc, ProviderAdapter("europeana", Commons()),
                                 ProviderAdapter("wikimedia", Commons())])
    state = historical_project()
    sources = route_sources(registry, state["scenes"][0], state, "historical crossing", "photo")
    assert {s.adapter.provider for group in sources for s in group} == {"europeana", "wikimedia"}


def test_loc_client_timeout_and_redirect_policy_remain_bounded(monkeypatch):
    config = {}

    def factory(**kwargs):
        config.update(kwargs)
        return httpx.Client(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "https://outside.test/"})
        ))

    monkeypatch.setattr(open_media, "HTTP_CLIENT_FACTORY", factory)
    source = LOCProvider()
    try:
        assert config["timeout"].connect == 4 and config["timeout"].read == 10
        budget = AcquisitionBudget()
        with pytest.raises(media.MediaProviderError) as caught:
            search(source, budget)
        assert budget.search_requests == 1  # JSON API explicitly does not follow redirects.
        assert caught.value.category == "provider_error"
        assert caught.value.diagnostics["redirect_target"]["endpoint"] == "https://outside.test/"
    finally:
        source.close()
