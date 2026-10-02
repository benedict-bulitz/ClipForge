"""Official-contract fixtures; no live APIs, model downloads, or paid calls."""

import copy
import io
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from test_staged_media_search import Commons, Provider, Verifier, cand, project, settings_for
from test_visual_director import FakeGenerator
from test_visual_selection_regression import incidental_candidate, material_project

from clipforge import media, open_media
from clipforge.config import Settings, resolve_settings
from clipforge.integrations import ProviderValidator
from clipforge.media_candidates import discover_scene_media_candidates
from clipforge.open_media import EuropeanaProvider, LOCProvider, NASAProvider, OpenverseProvider
from clipforge.schemas import SceneMediaCandidateRead
from clipforge.security.secrets import SecretStore
from clipforge.source_router import route_sources
from clipforge.visual_providers import (
    AcquisitionBudget,
    CandidateLedger,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderRegistry,
    asset_keys,
    create_provider_registry,
)
from clipforge.visual_rights import evaluate_rights

CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
BY = "https://creativecommons.org/licenses/by/4.0/"


def ov_row(**overrides):
    return {
        "id": "uuid-1",
        "title": "Lunar spacecraft orbit",
        "creator": "Photographer",
        "creator_url": "https://author.test/profile",
        "foreign_landing_url": "https://source.test/item/1",
        "url": "https://source.test/image/1.jpg",
        "thumbnail": "https://source.test/preview/1.jpg",
        "width": 1200,
        "height": 1600,
        "license": "by",
        "license_version": "4.0",
        "license_url": BY,
        "provider": "wikimedia",
        "source": "wikimedia",
        "tags": [{"name": "spacecraft"}],
        "attribution": "Photographer, CC BY 4.0",
        **overrides,
    }


def nasa_row(metadata=None, **overrides):
    return {
        "href": "https://images-assets.nasa.gov/image/space-1/collection.json",
        "data": [
            {
                "nasa_id": "space-1",
                "media_type": "image",
                "title": "Lunar spacecraft orbit",
                "description": "Spacecraft orbiting the moon",
                "keywords": ["spacecraft", "lunar"],
                "photographer": "NASA photographer",
                "center": "JSC",
                "date_created": "1969-01-01",
                **overrides,
            }
        ],
        "links": [
            {
                "href": "https://images-assets.nasa.gov/image/space-1/preview.jpg",
                "rel": "preview",
                "render": "image",
            }
        ],
        "_metadata": metadata or {},
    }


def eu_row(**overrides):
    return {
        "id": "/collection/item",
        "guid": "https://www.europeana.eu/item/collection/item",
        "type": "IMAGE",
        "title": ["Historical street photograph"],
        "dcCreator": ["Archive photographer"],
        "dcDescription": ["City street in 1900"],
        "dataProvider": ["Original Museum"],
        "provider": ["National Aggregator"],
        "year": ["1900"],
        "rights": [BY],
        "edmIsShownAt": ["https://museum.test/item/1"],
        "edmIsShownBy": ["https://museum.test/image.jpg"],
        "edmPreview": ["https://museum.test/thumb.jpg"],
        **overrides,
    }


def loc_row(**overrides):
    return {
        "id": "https://www.loc.gov/item/item-1/",
        "url": "https://www.loc.gov/item/item-1/",
        "title": "Historical street photograph",
        "description": ["City street in 1900"],
        "date": "1900",
        "contributor": ["Archive photographer"],
        "partof": ["Historical photographs"],
        "subject": ["historical", "street"],
        "online_format": ["image"],
        "image_url": ["https://cdn.loc.gov/small.jpg", "https://cdn.loc.gov/large.jpg"],
        "rights_advisory": ["Public domain."],
        **overrides,
    }


def adapter(kind, handler=None):
    client = httpx.Client(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json={}, request=request))
        )
    )
    return (
        EuropeanaProvider("test-secret-key", client=client)
        if kind is EuropeanaProvider
        else kind(client=client)
    )


def payload(kind, row=None):
    if kind is OpenverseProvider:
        return {"results": [row or ov_row()]}
    if kind is NASAProvider:
        return {"collection": {"items": [row or nasa_row()]}}
    if kind is EuropeanaProvider:
        return {"success": True, "items": [row or eu_row()]}
    return {"results": [row or loc_row()]}


def search(source, budget=None, query="lunar spacecraft"):
    return source.search(
        query, "photo", portrait=True, scene_duration=4, budget=budget or AcquisitionBudget()
    )


def image_bytes():
    target = io.BytesIO()
    Image.new("RGB", (900, 1200)).save(target, format="JPEG")
    return target.getvalue()


def test_openverse_normalizes_item_license_and_original_provenance():
    item = adapter(OpenverseProvider).normalize(
        payload(OpenverseProvider), "photo", query="lunar spacecraft"
    )[0]
    assert item.provider == "openverse" and item.provider_id == "uuid-1"
    assert item.width == 1200 and item.tags == ("spacecraft",)
    assert item.creator_url == "https://author.test/profile"
    assert item.origin["provider"] == "wikimedia"
    assert item.origin["source_url"] == item.source_url
    assert evaluate_rights(item.rights).status == "usable"
    assert item.rights.attribution_required
    assert "cropped/transformed" in item.rights.attribution_text
    assert item.rights.rights_source.startswith("openverse_api:")


@pytest.mark.parametrize(
    "license_id,uri",
    [
        (None, ""),
        ("by-nc", "https://creativecommons.org/licenses/by-nc/4.0/"),
        ("by-nd", "https://creativecommons.org/licenses/by-nd/4.0/"),
        ("by-sa", "https://creativecommons.org/licenses/by-sa/4.0/"),
        ("cc0", BY),
        ("by", "https://evil.test/licenses/by/4.0/"),
    ],
)
def test_openverse_unknown_incompatible_and_conflicting_license_fail_closed(license_id, uri):
    row = ov_row(license=license_id, license_url=uri)
    item = adapter(OpenverseProvider).normalize(payload(OpenverseProvider, row), "photo")[0]
    assert evaluate_rights(item.rights).status != "usable"
    assert not media.is_real_media_allowed(item)


def test_openverse_excludes_flickr_without_adding_flickr_provider():
    source = adapter(OpenverseProvider)
    assert source.params("query")["excluded_source"] == "flickr"
    assert source.normalize(payload(OpenverseProvider, ov_row(source="flickr")), "photo") == []


@pytest.mark.parametrize("flag", [None, True, "false", 0])
def test_nasa_identity_or_missing_boolean_never_implies_rights(flag):
    metadata = {"AVAIL:NASAID": "space-1"}
    if flag is not None:
        metadata["XMP:Marked"] = flag
    item = adapter(NASAProvider).normalize(payload(NASAProvider, nasa_row(metadata)), "photo")[0]
    assert item.rights.public_domain is None
    assert evaluate_rights(item.rights).status != "usable"


def test_nasa_item_xmp_public_domain_requires_matching_asset_and_no_conflicts():
    source = adapter(NASAProvider)
    for metadata, accepted in [
        ({"XMP:Marked": False, "AVAIL:NASAID": "space-1"}, True),
        ({"XMP:Marked": False, "AVAIL:NASAID": "other"}, False),
        (
            {"XMP:Marked": False, "AVAIL:NASAID": "space-1", "IPTC:CopyrightNotice": "Third party"},
            False,
        ),
    ]:
        item = source.normalize(payload(NASAProvider, nasa_row(metadata)), "photo")[0]
        assert (evaluate_rights(item.rights).status == "usable") is accepted
        assert item.origin["date"] == "1969-01-01" and item.origin["institution"] == "JSC"


def test_nasa_enrichment_and_original_manifest_consume_actual_request_budget():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/search":
            assert request.url.params["media_type"] == "image"
            body = payload(NASAProvider)
        elif request.url.path.startswith("/metadata/"):
            body = {"location": "https://images-assets.nasa.gov/image/space-1/metadata.json"}
        elif request.url.path.endswith("metadata.json"):
            body = {
                "XMP:Marked": False,
                "AVAIL:NASAID": "space-1",
                "File:ImageWidth": 1600,
                "File:ImageHeight": 1200,
            }
        else:
            body = {
                "collection": {
                    "items": [
                        {"href": "https://images-assets.nasa.gov/image/space-1/space-1~orig.jpg"}
                    ]
                }
            }
        return httpx.Response(200, json=body, request=request)

    budget = AcquisitionBudget()
    item = search(adapter(NASAProvider, handle), budget)[0]
    assert len(requests) == budget.search_requests == 4
    assert item.download_url.endswith("~orig.jpg")
    assert item.width == 1600
    assert evaluate_rights(item.rights).status == "usable"


@pytest.mark.parametrize("media_type", ["video", "audio", "unknown"])
def test_nasa_unsupported_kinds_are_not_returned(media_type):
    source = adapter(NASAProvider)
    assert source.normalize(payload(NASAProvider, nasa_row(media_type=media_type)), "photo") == []
    assert (
        source.search(
            "query", "video", portrait=False, scene_duration=5, budget=AcquisitionBudget()
        )
        == []
    )


def test_europeana_normalization_preserves_institution_identity_and_rights():
    item = adapter(EuropeanaProvider).normalize(payload(EuropeanaProvider), "photo")[0]
    assert item.provider_id == "/collection/item"  # Canonical ID, not a filesystem path.
    assert item.origin["provider"] == "Original Museum"
    assert item.origin["aggregator_provider"] == "National Aggregator"
    assert item.origin["date"] == "1900"
    assert evaluate_rights(item.rights).status == "usable"
    assert item.width == 0 and item.height == 0  # Unknown, until a bounded header probe.


@pytest.mark.parametrize(
    "rights",
    [
        [],
        ["http://rightsstatements.org/vocab/InC/1.0/"],
        ["http://rightsstatements.org/vocab/NoC-US/1.0/"],
        [CC0, BY],
        ["https://creativecommons.org/licenses/by-nc/4.0/"],
    ],
)
def test_europeana_unknown_ambiguous_and_noncommercial_rights_reject(rights):
    item = adapter(EuropeanaProvider).normalize(
        payload(EuropeanaProvider, eu_row(rights=rights)), "photo"
    )[0]
    assert not media.is_real_media_allowed(item)


def test_europeana_header_auth_never_enters_cache_or_diagnostics():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "api.europeana.eu":
            assert request.headers["X-Api-Key"] == "test-secret-key"
            assert "wskey" not in request.url.params
            return httpx.Response(
                200,
                json=payload(EuropeanaProvider) | {"apikey": "test-secret-key"},
                request=request,
            )
        return httpx.Response(200, content=image_bytes(), request=request)

    item = search(adapter(EuropeanaProvider, handle))[0]
    assert item.width == 900 and item.height == 1200
    assert "test-secret-key" not in repr(open_media._CACHE)
    assert "test-secret-key" not in repr(media.candidate_evidence(item))


def test_loc_normalizes_archival_context_and_item_rights():
    item = adapter(LOCProvider).normalize(payload(LOCProvider), "photo")[0]
    assert item.origin["collection"] == ["Historical photographs"]
    assert item.creator == "Archive photographer" and item.origin["date"] == "1900"
    assert item.download_url == "https://cdn.loc.gov/large.jpg"
    assert evaluate_rights(item.rights).status == "usable"


@pytest.mark.parametrize(
    "rights",
    [
        [],
        ["No known restrictions on publication."],
        ["This item may be in the public domain."],
        ["Copyright holder unknown."],
    ],
)
def test_loc_ambiguous_item_rights_fail_closed(rights):
    item = adapter(LOCProvider).normalize(
        payload(LOCProvider, loc_row(rights_advisory=rights)), "photo"
    )[0]
    assert not media.is_real_media_allowed(item)


@pytest.mark.parametrize(
    "overrides",
    [
        {"image_url": []},
        {"online_format": ["pdf"]},
        {"access_restricted": True},
        {"image_url": ["https://www.loc.gov/static/icon.jpg"]},
    ],
)
def test_loc_missing_restricted_or_icon_media_not_candidates(overrides):
    assert adapter(LOCProvider).normalize(payload(LOCProvider, loc_row(**overrides)), "photo") == []


KINDS = [OpenverseProvider, NASAProvider, EuropeanaProvider, LOCProvider]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "error,category",
    [
        ("timeout", "timeout"),
        ("malformed", "malformed_response"),
        (401, "invalid_credentials"),
        (429, "rate_limited"),
        ("unexpected", "unexpected_provider_error"),
    ],
)
def test_each_provider_failure_is_isolated_and_other_route_continues(kind, error, category):
    if kind is LOCProvider and error == 401:
        category = "provider_error"  # Public API has no configured credentials.

    def handle(request):
        if error == "timeout":
            raise httpx.ReadTimeout("private endpoint detail", request=request)
        if error == "unexpected":
            raise RuntimeError("adapter failure with secret")
        return httpx.Response(error if isinstance(error, int) else 200, json=[], request=request)

    source = adapter(kind, handle)
    with pytest.raises(media.MediaProviderError) as caught:
        search(source)
    assert caught.value.category == category and "secret" not in str(caught.value)
    # Put the failed source in the actual routed path, with healthy coverage
    # from a later source; provider failure must not abort that continuation.
    open_media.clear_search_cache()
    source = adapter(kind, handle)
    query = (
        "lunar spacecraft"
        if kind is NASAProvider
        else "historical street"
        if kind in {LOCProvider, EuropeanaProvider}
        else "everyday running action"
    )
    healthy = ProviderAdapter(
        "wikimedia",
        Commons(photos={query: [cand("good", query, query, provider="wikimedia", kind="photo")]}),
    )
    registry = ProviderRegistry([source, healthy])
    state = project({"visual_goal": query, "preferred_media": "photo", "search_queries": [query]})
    result = routed(state, registry)
    assert result.ranked and result.ranked[0][0].provider == "wikimedia"
    stats = [row for stage in result.provenance["stages"] for row in stage["providers"]]
    assert any(
        row["provider"] == source.provider and row.get("failure") == category for row in stats
    )


@pytest.mark.parametrize("kind", KINDS)
def test_zero_results_are_normal(kind):
    body = {"results": [], "collection": {"items": []}, "items": [], "success": True}
    assert (
        search(adapter(kind, lambda request: httpx.Response(200, json=body, request=request))) == []
    )


def registry_all():
    return ProviderRegistry(
        [
            ProviderAdapter(
                name, type("Client", (), {"capabilities": ProviderCapabilities(kinds)})()
            )
            for name, kinds in [
                ("pexels", ("video", "photo")),
                ("pixabay", ("video", "photo")),
                ("wikimedia", ("photo",)),
                ("openverse", ("photo",)),
                ("nasa", ("photo",)),
                ("europeana", ("photo",)),
                ("loc", ("photo",)),
            ]
        ]
    )


@pytest.mark.parametrize(
    "query,expected",
    [
        ("astronomy spacecraft orbit", ["nasa", "wikimedia"]),
        ("historical archival event 1910", ["loc", "europeana"]),
        ("person running everyday action", ["pexels", "pixabay"]),
        ("running everyday action", ["pexels", "pixabay"]),
        ("famous landmark city", ["wikimedia", "openverse"]),
        ("abstract psychological mechanism", ["pexels", "pixabay"]),
        ("medical anatomical tissue", ["wikimedia", "openverse"]),
    ],
)
def test_router_representative_scene_classes(query, expected):
    routes = route_sources(registry_all(), {"visual_goal": query}, {}, query, "photo")
    assert [source.adapter.provider for source in routes[0]] == expected
    assert len(routes[0]) <= 2


def test_router_disabled_and_capability_constraints():
    registry = ProviderRegistry([ProviderAdapter("pexels", Provider()), adapter(NASAProvider)])
    routes = route_sources(registry, {}, {}, "spacecraft orbit", "video")
    assert routes[0][0].adapter.provider == "nasa" and routes[0][0].kind == "photo"
    assert all(source.adapter.provider != "europeana" for group in routes for source in group)


def test_router_pools_primary_results_before_selection_and_skips_widening():
    query = "lunar spacecraft"
    weak = replace(
        cand("weak", query, "Lunar spacecraft orbit", provider="nasa", kind="photo"), rank=1000
    )
    good = cand("good", query, "Lunar spacecraft orbit", provider="wikimedia", kind="photo")
    nasa, commons, secondary = (
        Commons(photos={query: [weak]}),
        Commons(photos={query: [good]}),
        Commons(),
    )
    registry = ProviderRegistry(
        [
            ProviderAdapter("nasa", nasa),
            ProviderAdapter("wikimedia", commons),
            ProviderAdapter("openverse", secondary),
        ]
    )
    state = project(
        {
            "visual_goal": query,
            "search_queries": [query, "spacecraft moon", "lunar orbit"],
            "preferred_media": "photo",
        }
    )
    result = routed(
        state, registry, verifier=Verifier(scores={"weak": (0.245, 0.245), "good": (0.4, 0.4)})
    )
    assert len(nasa.calls) == len(commons.calls) == 1 and not secondary.calls
    assert result.ranked[0][0].provider == "wikimedia"
    assert result.provenance["logical_queries_executed"] == 1
    assert len(result.provenance["stages"][0]["providers"]) == 2


def routed(state, registry, *, budget=None, verifier=None):
    scene = state["scenes"][0]
    plan = media.build_visual_query_plan(scene, state)
    return media.run_staged_scene_search(
        scene["search_queries"],
        scene,
        state,
        plan,
        pexels=registry.get("pexels"),
        wikimedia=registry.get("wikimedia"),
        registry=registry,
        preferred_kind="photo",
        portrait=True,
        scene_duration=4,
        used=set(),
        verifier=verifier or Verifier(),
        acquisition_budget=budget,
    )


def test_widening_preserves_three_queries_and_total_work_bounds():
    state = project(
        {
            "visual_goal": "spacecraft",
            "search_queries": ["spacecraft orbit", "lunar craft", "space mission", "fourth"],
            "preferred_media": "photo",
        }
    )
    clients = [Commons() for _ in range(4)]
    registry = ProviderRegistry(
        [
            ProviderAdapter(name, client)
            for name, client in zip(["nasa", "wikimedia", "openverse", "pexels"], clients)
        ]
    )
    budget = AcquisitionBudget(max_search_requests=5)
    result = routed(state, registry, budget=budget)
    assert sum(len(client.calls) for client in clients) == budget.search_requests == 5
    assert result.provenance["logical_queries_executed"] <= 3
    assert len(result.provenance["planned_queries"]) == 3
    assert result.provenance["stages"][0]["widening_reasons"]
    assert result.provenance["stop_reason"] == "acquisition_budget_exhausted"


def test_verification_admissions_remain_bounded_across_sources():
    query = "lunar spacecraft"
    registry = ProviderRegistry(
        [
            ProviderAdapter(
                name,
                Commons(
                    photos={
                        query: [
                            cand(
                                f"{name}-{i}",
                                query,
                                "Lunar spacecraft orbit",
                                provider=name,
                                kind="photo",
                            )
                            for i in range(15)
                        ]
                    }
                ),
            )
            for name in ["nasa", "wikimedia", "openverse"]
        ]
    )
    state = project({"visual_goal": query, "search_queries": [query], "preferred_media": "photo"})
    budget, verifier = AcquisitionBudget(max_verifications=2), Verifier()
    result = routed(state, registry, budget=budget, verifier=verifier)
    assert budget.verifications == len(verifier.calls) == 2
    assert sum(stage["verification_admissions"] for stage in result.provenance["stages"]) == 2


def test_more_sources_cannot_rescue_irrelevant_stock_and_director_falls_back(tmp_path, monkeypatch):
    state = material_project()
    bad = replace(incidental_candidate(), provider="openverse")
    commons, extra = Commons(), Commons(photos={bad.query: [bad]})
    extra.provider = "openverse"
    generator = FakeGenerator()
    verifier = Verifier()
    media.prepare_project_media(
        state,
        "project",
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=commons,
        extra_clients=[extra],
        visual_verifier=verifier,
        image_generator=generator,
    )
    scene = state["scenes"][0]
    assert scene["media"]["source"] == "generated_openai"
    assert len(generator.prompts) == 1 and "incidental" not in verifier.calls
    assert scene["media_search"]["routed"]
    assert any(
        stats["relevance_rejects"]
        for stage in scene["media_search"]["stages"]
        for stats in stage["providers"]
    )
    assert "generated_card_count" not in state["assets"]


def test_canonical_origin_dedupe_commons_openverse_and_media_url():
    commons = cand(
        "page-1",
        "moon",
        "Moon",
        provider="wikimedia",
        kind="photo",
        source_url="https://commons.wikimedia.org/wiki/File:Moon_photo.jpg",
    )
    aggregated = replace(
        commons,
        provider="openverse",
        provider_id="uuid",
        source_url="https://another.test/item/1",
        download_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Moon_photo.jpg/800px-Moon_photo.jpg",
    )
    ledger = CandidateLedger()
    assert ledger.admit(commons) and not ledger.admit(aggregated)
    rehost = replace(
        commons, provider="loc", provider_id="other", source_url="https://loc.test/item/1"
    )
    assert asset_keys(commons) & asset_keys(rehost)
    assert not ledger.admit(rehost)


def test_cache_key_normalization_ttl_size_copy_and_rights_recheck(monkeypatch):
    requests = []
    clock = [100.0]
    monkeypatch.setattr(open_media.time, "monotonic", lambda: clock[0])
    source = adapter(
        OpenverseProvider,
        lambda request: (
            requests.append(request)
            or httpx.Response(200, json=payload(OpenverseProvider), request=request)
        ),
    )
    first = search(source, query="Lunar  Spacecraft")
    first[0].origin["provider"] = "mutated"
    assert search(source, query="lunar spacecraft")[0].origin["provider"] == "wikimedia"
    assert len(requests) == 1
    key = next(iter(open_media._CACHE))
    stamp, cached = open_media._CACHE[key]
    open_media._CACHE[key] = (
        stamp,
        [replace(cached[0], rights=replace(cached[0].rights, commercial_use_allowed=False))],
    )
    assert search(source) == []
    clock[0] += open_media.CACHE_TTL + 1
    search(source)
    assert len(requests) == 2
    monkeypatch.setattr(open_media, "CACHE_SIZE", 2)
    search(source, query="moon")
    search(source, query="planet")
    assert len(open_media._CACHE) == 2


def test_unknown_rights_are_not_cached_and_cache_hit_uses_no_requests():
    source = adapter(
        OpenverseProvider,
        lambda request: httpx.Response(
            200, json=payload(OpenverseProvider, ov_row(license_url="")), request=request
        ),
    )
    search(source)
    assert all(not rows for _stamp, rows in open_media._CACHE.values())
    open_media.clear_search_cache()
    source = adapter(
        OpenverseProvider,
        lambda request: httpx.Response(200, json=payload(OpenverseProvider), request=request),
    )
    search(source)
    budget = AcquisitionBudget(max_search_requests=0)
    assert search(source, budget)
    assert budget.search_requests == 0


def test_nonblocking_rate_limit_and_auth_disable_are_local():
    count = []
    source = adapter(
        OpenverseProvider,
        lambda request: (
            count.append(request) or httpx.Response(200, json={"results": []}, request=request)
        ),
    )
    for i in range(5):
        search(source, query=f"query {i}")
    with pytest.raises(media.MediaProviderError, match="quota"):
        search(source, query="sixth")
    assert len(count) == 5
    auth = adapter(
        EuropeanaProvider,
        lambda request: httpx.Response(
            200, json={"success": False, "error": "Invalid API key"}, request=request
        ),
    )
    with pytest.raises(media.MediaProviderError) as caught:
        search(auth)
    assert caught.value.category == "invalid_credentials" and auth.disabled


def test_preview_work_and_downloads_are_bounded(tmp_path):
    row = ov_row(width=None, height=None)
    requests = []

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(200, json=payload(OpenverseProvider, row), request=request)
            if request.url.host == "api.openverse.org"
            else httpx.Response(200, content=image_bytes(), request=request)
        )

    source, budget = adapter(OpenverseProvider, handle), AcquisitionBudget(max_downloads=1)
    item = search(source, budget)[0]
    assert item.width == 900 and budget.downloads == 1
    with pytest.raises(media.MediaProviderError) as caught:
        source.download(item, tmp_path / "asset.jpg", budget=budget)
    assert caught.value.category == "budget_exhausted"
    assert len(requests) == 2


def test_new_source_evidence_survives_manual_discovery_schema_and_manifest(tmp_path):
    state = project(
        {
            "visual_goal": "lunar spacecraft",
            "search_queries": ["lunar spacecraft"],
            "preferred_media": "photo",
        }
    )
    source = adapter(
        OpenverseProvider,
        lambda request: httpx.Response(200, json=payload(OpenverseProvider), request=request),
    )
    _token, found = discover_scene_media_candidates(
        state,
        "project",
        1,
        1,
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=Commons(),
        extra_clients=[source],
        visual_verifier=Verifier(),
    )
    assert len(found) == 1
    item = SceneMediaCandidateRead.model_validate(found[0]).model_dump()
    assert item["origin"]["provider"] == "wikimedia"
    assert item["rights"]["rights_policy_version"]
    reopened = copy.deepcopy(item)
    assert media.destination_asset_allowed(reopened, state["scenes"][0], state)


def test_europeana_optional_key_uses_existing_secure_settings(test_keyring):
    store = SecretStore(test_keyring)
    store.set_secret("EUROPEANA_API_KEY", "secure-test-key")
    settings = resolve_settings(
        Settings(_env_file=None, europeana_api_key="environment-key"), store
    )
    assert settings.europeana_api_key == "secure-test-key"
    registry = create_provider_registry(settings)
    assert registry.get("europeana")
    registry.close()
    registry = create_provider_registry(Settings(_env_file=None, europeana_api_key=None))
    assert registry.get("europeana") is None
    registry.close()


def test_europeana_key_validation_uses_header_and_success_field():
    def handle(request):
        assert request.headers["X-Api-Key"] == "key"
        assert "key" not in str(request.url)
        return httpx.Response(
            200, json={"success": False, "error": "Invalid API key"}, request=request
        )

    result = ProviderValidator(httpx.Client(transport=httpx.MockTransport(handle))).validate(
        "europeana", "key"
    )
    assert result.status == "invalid_credentials"


def test_download_failure_widens_without_repeating_primary_or_new_queries(tmp_path):
    query = "lunar spacecraft"
    row = ov_row(title="Lunar spacecraft orbit")
    source = adapter(
        OpenverseProvider,
        lambda request: (
            httpx.Response(200, json={"results": [row]}, request=request)
            if request.url.host == "api.openverse.org"
            else httpx.Response(200, content=image_bytes(), request=request)
        ),
    )
    bad_download = Commons(
        photos={
            query: [
                cand("unavailable", query, "Lunar spacecraft orbit", provider="nasa", kind="photo")
            ]
        }
    )
    bad_download.provider = "nasa"
    bad_download.capabilities = ProviderCapabilities(("photo",))

    def broken_download(_candidate, _destination):
        raise media.MediaProviderError("network_error", "File unavailable")

    bad_download.download = broken_download
    commons = Commons()
    state = project(
        {
            "narration": query,
            "visual_goal": query,
            "search_queries": [query],
            "preferred_media": "photo",
        }
    )
    media.prepare_project_media(
        state,
        "project",
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=commons,
        extra_clients=[bad_download, source],
        visual_verifier=Verifier(),
    )
    scene = state["scenes"][0]
    assert scene["media"]["provider"] == "openverse"
    assert scene["media_search"]["winning_source"] == "routed_download_recovery"
    assert len(bad_download.calls) == 1
    assert scene["media_search"]["logical_queries_executed"] == 1
    assert len(scene["media_search"]["executed_queries"]) == 1
    assert scene["media_search"]["provider_requests_executed"] <= 18


def test_new_source_manual_apply_persists_same_evidence_and_rights(db, tmp_path, monkeypatch):
    from test_export import seed_project

    from clipforge import media_candidates
    from clipforge.services import get_project, serialize_project

    state = project(
        {
            "visual_goal": "lunar spacecraft",
            "search_queries": ["lunar spacecraft"],
            "preferred_media": "photo",
        }
    )
    project_id = "11111111-1111-4111-8111-111111111111"
    saved_project = seed_project(db, project_id, state)
    source = adapter(
        OpenverseProvider,
        lambda request: (
            httpx.Response(200, json=payload(OpenverseProvider), request=request)
            if request.url.host == "api.openverse.org"
            else httpx.Response(200, content=image_bytes(), request=request)
        ),
    )
    registry = ProviderRegistry([source])
    monkeypatch.setattr(media_candidates, "create_provider_registry", lambda *_a, **_kw: registry)
    settings = settings_for(tmp_path)
    _token, choices = discover_scene_media_candidates(
        state, project_id, 1, 1, settings, visual_verifier=Verifier()
    )
    assert choices
    media_candidates.apply_scene_media_candidate(
        db, saved_project, 1, choices[0]["token"], settings, auto_render=False
    )
    db.expire_all()
    reopened = serialize_project(get_project(db, project_id))["revision"]["state"]
    chosen = reopened["scenes"][0]["media"]
    assert chosen["origin"] == choices[0]["origin"]
    assert chosen["rights"] == choices[0]["rights"]
    assert chosen in reopened["assets"]["license_manifest"]
    assert media.cached_scene_asset_path(chosen, settings)
    assert media.destination_asset_allowed(chosen, reopened["scenes"][0], reopened)


def test_loc_search_fetches_item_rights_and_dimensions_under_budget():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/search/":
            body = payload(LOCProvider, loc_row(rights_advisory=[]))
        elif request.url.path.startswith("/item/"):
            body = {
                "item": {
                    "rights_advisory": ["Public domain."],
                    "contributor_names": ["Original photographer"],
                },
                "resources": [],
            }
        else:
            return httpx.Response(200, content=image_bytes(), request=request)
        return httpx.Response(200, json=body, request=request)

    budget = AcquisitionBudget()
    item = search(adapter(LOCProvider, handle), budget)[0]
    assert item.creator == "Original photographer"
    assert item.width == 900 and item.height == 1200
    assert evaluate_rights(item.rights).status == "usable"
    assert budget.search_requests == 2 and budget.downloads == 1
    assert len(requests) == 3


def test_medical_routing_does_not_approve_unrelated_lifestyle():
    state = project(
        {
            "visual_goal": "medical anatomical tissue",
            "search_queries": ["medical anatomical tissue"],
            "preferred_media": "photo",
        }
    )
    query = state["scenes"][0]["search_queries"][0]
    wrong = cand("wrong", query, "Person smiling in a park", provider="openverse", kind="photo")
    registry = ProviderRegistry(
        [
            ProviderAdapter("openverse", Commons(photos={query: [wrong]})),
            ProviderAdapter("wikimedia", Commons()),
        ]
    )
    verifier = Verifier()
    result = routed(state, registry, verifier=verifier)
    assert not result.ranked and not verifier.calls


def test_three_logical_queries_are_executed_when_coverage_never_arrives():
    state = project(
        {
            "visual_goal": "everyday running",
            "search_queries": ["running", "walking", "cycling", "fourth"],
            "preferred_media": "photo",
        }
    )
    registry = ProviderRegistry(
        [ProviderAdapter("wikimedia", Commons()), ProviderAdapter("openverse", Commons())]
    )
    result = routed(state, registry)
    assert result.provenance["logical_queries_executed"] == 3
    assert len(result.provenance["executed_queries"]) == 3
    assert result.provenance["provider_requests_executed"] <= 18


def test_routing_uses_scene_linked_story_arc_fact_context():
    state = {
        "script": {"blocks": [{"id": "b1", "fact_ids": ["fact1"]}]},
        "story_arc": {
            "units": [{"id": "fact1", "claim": "Archival photographs record a historical event"}]
        },
    }
    groups = route_sources(
        registry_all(), {"block_id": "b1", "visual_goal": "photo"}, state, "photo", "photo"
    )
    assert [s.adapter.provider for s in groups[0]] == ["loc", "europeana"]


def test_cached_primary_candidates_are_processed_without_provider_work():
    source = adapter(
        OpenverseProvider,
        lambda request: httpx.Response(200, json=payload(OpenverseProvider), request=request),
    )
    search(source)
    state = project(
        {
            "visual_goal": "lunar spacecraft",
            "search_queries": ["lunar spacecraft"],
            "preferred_media": "photo",
        }
    )
    result = routed(state, ProviderRegistry([source]))
    assert result.ranked
    assert result.provenance["provider_requests_executed"] == 0
    assert result.provenance["stages"][0]["providers"][0]["cache_hit"]


def test_new_provider_programming_assertions_remain_visible():
    def handle(_request):
        raise AssertionError("programming invariant")

    with pytest.raises(AssertionError, match="invariant"):
        search(adapter(OpenverseProvider, handle))


def test_auth_disabled_adapter_is_skipped_by_router():
    source = adapter(OpenverseProvider)
    source.disabled = True
    assert route_sources(ProviderRegistry([source]), {}, {}, "query", "photo") == []


def test_dedupe_preserves_identity_parameters_in_original_media_urls():
    first = replace(
        incidental_candidate(),
        source_url="https://archive.test/image?id=1&utm_source=feed",
        download_url="https://archive.test/download?item=1&width=800",
    )
    second = replace(
        first,
        provider_id="other",
        source_url="https://archive.test/image?id=2",
        download_url="https://archive.test/download?item=2&width=800",
    )
    same = replace(
        first,
        provider_id="rehost",
        provider="openverse",
        source_url="https://archive.test/image?id=1",
        download_url="https://archive.test/download?item=1&width=1200",
    )
    ledger = CandidateLedger()
    assert ledger.admit(first) and ledger.admit(second) and not ledger.admit(same)


def test_named_object_evidence_routes_to_entity_sources_without_topic_rules():
    groups = route_sources(
        registry_all(),
        {"visual_intent": {"objects": ["Example Monument"]}},
        {},
        "architecture",
        "photo",
    )
    assert [s.adapter.provider for s in groups[0]] == ["wikimedia", "openverse"]


def test_secondary_stock_alternate_kind_keeps_its_source_priority():
    groups = route_sources(registry_all(), {}, {}, "lunar spacecraft", "video")
    assert [s.adapter.provider for s in groups[0]] == ["nasa", "wikimedia"]
    entries = [(s.adapter.provider, s.kind) for group in groups for s in group]
    assert ("pexels", "video") in entries and ("pexels", "photo") in entries
    assert entries.index(("pexels", "video")) > entries.index(("openverse", "photo"))
    assert entries.index(("pexels", "photo")) > entries.index(("pexels", "video"))


def test_malformed_creator_and_attribution_do_not_establish_credit():
    row = ov_row(creator={"unexpected": "name"}, attribution=123)
    item = adapter(OpenverseProvider).normalize(payload(OpenverseProvider, row), "photo")[0]
    assert item.creator == ""
    assert evaluate_rights(item.rights).status == "unknown"
