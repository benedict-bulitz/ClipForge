"""Visual Sources V2 phase 1: Commons search contract, NASA mirror dedupe and
normalized asset metadata. Mocked official API shapes only; no live requests."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from test_staged_media_search import Verifier, cand, project
from test_visual_sources_v2 import nasa_row, routed

from clipforge.media import (
    MediaProviderError,
    WikimediaMediaClient,
    asset_metadata,
    candidate_evidence,
    commons_nasa_identity,
    commons_relaxed_query,
)
from clipforge.open_media import NASAProvider
from clipforge.schemas import SceneMediaCandidateRead
from clipforge.visual_providers import (
    AcquisitionBudget,
    CandidateLedger,
    ProviderAdapter,
    ProviderRegistry,
    asset_keys,
)
from clipforge.visual_rights import (
    MediaRights,
    evaluate_rights,
    provider_terms_rights,
    usage_restrictions,
)

BY = "https://creativecommons.org/licenses/by/4.0/"


def ext(**values):
    """Commons ``extmetadata`` entries as the API returns them ({"value": ...})."""
    return {key: {"value": value} for key, value in values.items()}


def cc_by(**extra):
    return ext(
        License="cc-by-4.0", LicenseShortName="CC BY 4.0", LicenseUrl=BY,
        Artist="<a href='//commons.wikimedia.org/wiki/User:Ann'>Ann</a>", AttributionRequired="true",
        **extra,
    )


def commons_page(pageid, index, title, *, width=4000, height=3000, thumb=(1600, 1200), mime="image/jpeg", metadata=None):
    info = {
        "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{title}",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
        "width": width, "height": height, "mime": mime,
        "extmetadata": cc_by() if metadata is None else metadata,
    }
    if thumb:
        info |= {
            "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{title}/1600px-{title}",
            "thumbwidth": thumb[0], "thumbheight": thumb[1],
        }
    return {"pageid": pageid, "ns": 6, "title": f"File:{title}", "index": index, "imageinfo": [info]}


def commons_payload(*pages):
    # generator=search keys pages by page id; dict order is NOT the search rank.
    return {"batchcomplete": "", "query": {"pages": {str(page["pageid"]): page for page in pages}}}


class CommonsAPI:
    """Mock commons.wikimedia.org/w/api.php recording each search string."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.searches: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "commons.wikimedia.org" and request.url.path == "/w/api.php"
        assert request.url.params["generator"] == "search" and request.url.params["gsrnamespace"] == "6"
        self.searches.append(request.url.params["gsrsearch"])
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(response):
            return response(request)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response, request=request)


def commons_client(api: CommonsAPI) -> WikimediaMediaClient:
    return WikimediaMediaClient(client=httpx.Client(transport=httpx.MockTransport(api)))


def adapter_search(client, query="mars rover surface", budget=None):
    return ProviderAdapter("wikimedia", client).search(
        query, "photo", portrait=False, scene_duration=4, budget=budget or AcquisitionBudget()
    )


# --- Wikimedia Commons search contract -------------------------------------


def test_commons_keeps_search_rank_not_page_id_order():
    api = CommonsAPI(commons_payload(
        commons_page(11, 3, "Third.jpg"), commons_page(22, 1, "First.jpg"), commons_page(33, 2, "Second.jpg"),
    ))
    results = adapter_search(commons_client(api))
    assert [item.title for item in results] == ["File:First.jpg", "File:Second.jpg", "File:Third.jpg"]


def test_commons_reports_the_delivered_thumbnail_size_and_keeps_the_original():
    api = CommonsAPI(commons_payload(
        commons_page(1, 1, "Rover.jpg", width=6000, height=4000, thumb=(1600, 1067)),
        # A panorama whose delivered 1600px thumbnail is only 400px tall.
        commons_page(2, 2, "Panorama.jpg", width=12000, height=3000, thumb=(1600, 400)),
    ))
    [item] = adapter_search(commons_client(api))
    assert (item.width, item.height) == (1600, 1067)
    assert item.download_url.endswith("1600px-Rover.jpg")
    assert (item.origin["original_width"], item.origin["original_height"]) == (6000, 4000)
    assert item.origin["original_media_url"].endswith("/Rover.jpg")


def test_commons_without_thumbnail_uses_original_and_never_invents_dimensions():
    api = CommonsAPI(commons_payload(
        commons_page(1, 1, "Small.jpg", width=1200, height=900, thumb=None),
        commons_page(2, 2, "Unknown.jpg", width="n/a", height=None, thumb=None),
    ))
    [item] = adapter_search(commons_client(api))
    assert (item.width, item.height) == (1200, 900) and item.download_url.endswith("/Small.jpg")


def test_commons_skips_non_image_media():
    api = CommonsAPI(commons_payload(
        commons_page(1, 1, "Clip.webm", mime="video/webm"), commons_page(2, 2, "Photo.jpg"),
    ))
    assert [item.title for item in adapter_search(commons_client(api))] == ["File:Photo.jpg"]


def test_zero_hit_query_spends_one_bounded_any_keyword_request():
    api = CommonsAPI({"batchcomplete": ""}, commons_payload(commons_page(1, 1, "Mars_rover.jpg")))
    budget = AcquisitionBudget()
    [item] = adapter_search(commons_client(api), "rust colored dust lifting dry ground", budget)
    assert api.searches == [
        "filetype:bitmap rust colored dust lifting dry ground",
        "filetype:bitmap rust OR colored OR dust OR lifting OR dry OR ground",
    ]
    assert budget.search_requests == 2  # both physical requests are accounted for
    assert item.origin["search_mode"] == "any_keyword"


def test_relaxed_request_never_consumes_the_last_shared_budget_slots():
    api = CommonsAPI({"batchcomplete": ""})
    budget = AcquisitionBudget(max_search_requests=4)
    budget.search_requests = 0
    assert adapter_search(commons_client(api), "dust lifting dry ground", budget) == []
    # The adapter's own claim leaves 3 < 4 headroom: no second request.
    assert len(api.searches) == 1 and budget.search_requests == 1


def test_strict_hits_never_trigger_relaxation():
    api = CommonsAPI(commons_payload(commons_page(1, 1, "Rover.jpg")))
    [item] = adapter_search(commons_client(api), "mars rover")
    assert len(api.searches) == 1 and item.origin["search_mode"] == "all_keywords"


@pytest.mark.parametrize("query,expected", [
    ("rust colored dust lifting dry ground", "rust OR colored OR dust OR lifting OR dry OR ground"),
    ("the dust of the planet", "dust OR planet"),
    ("himmel auf dem mars rot", "himmel OR mars OR rot"),
    ("dust", None),
    ("a b", None),
])
def test_relaxed_query_uses_content_words_only(query, expected):
    assert commons_relaxed_query(query) == expected


def test_rate_limit_sets_nonblocking_cooldown_for_this_client_only():
    api = CommonsAPI(httpx.Response(429, headers={"retry-after": "120"}))
    client = commons_client(api)
    for _ in range(2):
        with pytest.raises(MediaProviderError) as error:
            adapter_search(client)
        assert error.value.category == "rate_limited"
    assert len(api.searches) == 1  # the cooldown answered the second search locally
    other = commons_client(CommonsAPI(commons_payload(commons_page(1, 1, "Rover.jpg"))))
    assert adapter_search(other)


@pytest.mark.parametrize("code,category", [("ratelimited", "rate_limited"), ("maxlag", "rate_limited"), ("badvalue", "provider_error")])
def test_mediawiki_error_envelope_is_never_an_empty_search(code, category):
    api = CommonsAPI({"error": {"code": code, "info": "rejected"}})
    with pytest.raises(MediaProviderError) as error:
        adapter_search(commons_client(api))
    assert error.value.category == category


def _raise(error):
    def answer(request):
        raise error(request)
    return answer


@pytest.mark.parametrize("failure,category", [
    (_raise(lambda request: httpx.ConnectTimeout("slow", request=request)), "timeout"),
    (_raise(lambda request: httpx.ConnectError("down", request=request)), "network_error"),
    (lambda request: httpx.Response(503, request=request), "provider_error"),
    (lambda request: httpx.Response(200, content=b"<html>", request=request), "malformed_response"),
    (lambda request: httpx.Response(200, json=[], request=request), "malformed_response"),
    (lambda request: httpx.Response(200, json={"query": {"pages": []}}, request=request), "malformed_response"),
])
def test_commons_transport_failures_are_categorized(failure, category):
    with pytest.raises(MediaProviderError) as error:
        adapter_search(commons_client(CommonsAPI(failure)))
    assert error.value.category == category


def test_unknown_or_restricted_commons_license_is_never_usable():
    api = CommonsAPI(commons_payload(
        commons_page(1, 1, "NoLicense.jpg", metadata=ext(Artist="Ann")),
        commons_page(2, 2, "Logo.jpg", metadata=cc_by(Restrictions="trademarked|personality")),
    ))
    unknown, restricted = adapter_search(commons_client(api))
    assert evaluate_rights(unknown.rights).status == "unknown"
    assert evaluate_rights(restricted.rights).status == "unknown"
    assert {"asset_specific_restrictions", "commons:trademarked", "commons:personality"} <= set(
        usage_restrictions(restricted.rights)
    )
    # The shared ledger never admits uncleared media.
    ledger = CandidateLedger()
    assert not ledger.admit(unknown) and not ledger.admit(restricted)


# --- Cross-source duplicates (NASA library <-> Commons mirror) --------------


@pytest.mark.parametrize("title,metadata,expected", [
    ("File:PIA24546 Perseverance selfie.jpg", {}, "PIA24546"),
    ("File:Rover.jpg", ext(Credit='<a href="https://images.nasa.gov/details/as11-40-5874">NASA</a>'), "as11-40-5874"),
    ("File:Rover.jpg", ext(Credit="https://images.nasa.gov/details-PIA00407.html"), "PIA00407"),
    ("File:Rover.jpg", ext(ImageDescription="A photo in the style of NASA PIA archives"), None),
    ("File:Red_planet_art.jpg", ext(Credit="Own work, inspired by NASA"), None),
])
def test_commons_nasa_identity_requires_explicit_identifier(title, metadata, expected):
    assert commons_nasa_identity(title, metadata) == expected


def nasa_public_domain(nasa_id="PIA24546"):
    row = nasa_row({"XMP:Marked": False, "AVAIL:NASAID": nasa_id}, nasa_id=nasa_id)
    row["_image_url"] = f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~orig.jpg"
    row["_metadata"] |= {"File:ImageWidth": 3000, "File:ImageHeight": 2000}
    return NASAProvider(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))\
        .normalize({"collection": {"items": [row]}}, "photo", query="mars rover")[0]


def test_nasa_item_and_commons_mirror_are_one_asset():
    nasa = nasa_public_domain()
    api = CommonsAPI(commons_payload(
        commons_page(7, 1, "PIA24546_Perseverance_selfie.jpg"), commons_page(8, 2, "Unrelated_rover.jpg"),
    ))
    mirror, unrelated = adapter_search(commons_client(api))
    assert nasa.origin["canonical_id"] == "PIA24546"
    assert asset_keys(nasa) & asset_keys(mirror) == {"origin:NASA:PIA24546"}
    ledger = CandidateLedger()
    assert ledger.admit(nasa) and not ledger.admit(mirror) and ledger.admit(unrelated)
    # Scene reuse checks read the persisted record: the alias survives serialization.
    assert "origin:NASA:PIA24546" in asset_keys(candidate_evidence(mirror))


NASA_ID = "PIA24546"


def space_registry(nasa_metadata=None):
    def nasa_api(request):
        if request.url.path == "/search":
            body = {"collection": {"items": [nasa_row(nasa_id=NASA_ID) | {"_metadata": {}}]}}
            body["collection"]["items"][0].pop("_metadata")
        elif request.url.path.startswith("/metadata/"):
            body = {"location": f"https://images-assets.nasa.gov/image/{NASA_ID}/metadata.json"}
        elif request.url.path.endswith("metadata.json"):
            body = nasa_metadata or {"XMP:Marked": False, "AVAIL:NASAID": NASA_ID, "File:ImageWidth": 3000, "File:ImageHeight": 2000}
        else:
            body = {"collection": {"items": [{"href": f"https://images-assets.nasa.gov/image/{NASA_ID}/{NASA_ID}~orig.jpg"}]}}
        return httpx.Response(200, json=body, request=request)

    commons = CommonsAPI(commons_payload(
        commons_page(7, 1, f"{NASA_ID}_Mars_rover_on_the_surface.jpg"),
        commons_page(8, 2, "Mars_rover_tracks.jpg", metadata=ext(Artist="Ann")),  # no license
    ))
    return ProviderRegistry([
        NASAProvider(client=httpx.Client(transport=httpx.MockTransport(nasa_api))),
        ProviderAdapter("wikimedia", commons_client(commons)),
    ])


def space_state():
    return project({
        "narration": "The rover crosses the planet surface.", "visual_goal": "mars rover on the planet surface",
        "search_queries": ["mars rover surface"], "preferred_media": "photo",
    })


def widened(registry):
    from clipforge import media
    from clipforge.routed_search import run_routed_scene_search

    state = space_state()
    scene = state["scenes"][0]
    return run_routed_scene_search(
        scene["search_queries"], scene, state, media.build_visual_query_plan(scene, state), registry=registry,
        preferred_kind="photo", portrait=True, scene_duration=4, used=set(), verifier=Verifier(),
        query_budget=1, widen_fully=True,
    )


def test_routed_space_scene_dedupes_mirror_and_records_normalized_rights():
    # Widening forced so the NASA fallback tier runs after Commons admitted the mirror first.
    result = widened(space_registry())
    stage = result.provenance["stages"][0]
    assert [(row["provider"], row["tier"]) for row in stage["routed_providers"][:2]] == [("wikimedia", 0), ("nasa", 1)]
    stats = {row["provider"]: row for row in stage["providers"]}
    assert stats["wikimedia"]["returned"] == 2 and stats["nasa"]["returned"] == 1
    assert stats["wikimedia"]["rights_rejects"] == 1  # unknown license never admitted
    assert stats["wikimedia"]["dedupe_rejects"] == 0 and stats["nasa"]["dedupe_rejects"] == 1  # NASA copy of the mirror
    evidence = {row["identity"]: row for row in stage["candidate_evidence"]}
    unlicensed = evidence["wikimedia:photo:8"]
    assert unlicensed["rights_status"] == "unknown" and unlicensed["license_id"] is None
    assert unlicensed["usage_restrictions"] == ["not_cleared:missing_license_evidence"]
    assert evidence[f"nasa:photo:{NASA_ID}"]["dimensions"] == [3000, 2000]
    assert {candidate.identity for candidate in result.candidates} == {"wikimedia:photo:7"}


def test_routed_space_scene_without_widening_leads_with_wikimedia_and_spares_nasa_requests():
    registry = space_registry()
    result = routed(space_state(), registry, verifier=Verifier())
    stage = result.provenance["stages"][0]
    assert [(row["provider"], row["tier"]) for row in stage["routed_providers"][:2]] == [("wikimedia", 0), ("nasa", 1)]
    nasa_requests = sum(row["requests"] for row in stage["providers"] if row["provider"] == "nasa")
    if not stage["widening_reasons"]:
        assert nasa_requests == 0  # strong first-tier coverage: the 7-9 NASA metadata requests are not spent
    else:
        assert nasa_requests > 0


# --- Normalized candidate metadata -----------------------------------------


def test_asset_metadata_for_provider_grant_stock():
    stock = replace(cand("123", "rover", "Rover", kind="video"), rights=provider_terms_rights("pexels"))
    metadata = asset_metadata(stock)
    assert metadata["source"] == "pexels" and metadata["asset_type"] == "video"
    assert metadata["dimensions"]["known"] and metadata["license"]["status"] == "usable"
    assert metadata["license"]["public_domain"] is None  # a grant, not public domain
    assert metadata["attribution"]["required"] is False
    assert metadata["usage_restrictions"] == ["provider_prohibited_uses_apply", "third_party_rights_not_cleared"]


def test_asset_metadata_for_commons_cc_by_and_nasa_public_domain():
    [commons] = adapter_search(commons_client(CommonsAPI(commons_payload(commons_page(1, 1, "Rover.jpg")))))
    metadata = asset_metadata(commons)
    assert metadata["license"]["id"] == "cc-by-4.0" and metadata["license"]["url"] == BY
    assert metadata["attribution"]["required"] is True and "Ann" in metadata["attribution"]["text"]
    assert metadata["usage_restrictions"] == ["attribution_required"]
    assert metadata["dimensions"] == {"width": 1600, "height": 1200, "known": True}
    assert metadata["source_url"] == "https://commons.wikimedia.org/wiki/File:Rover.jpg"
    nasa = asset_metadata(nasa_public_domain())
    assert nasa["license"]["public_domain"] is True and nasa["license"]["status"] == "usable"
    assert nasa["attribution"]["required"] is True and nasa["attribution"]["text"].startswith("NASA")


@pytest.mark.parametrize("rights,reason", [
    (MediaRights(), "missing_license_evidence"),
    (MediaRights(license_id="cc-by-nc-4.0", license_url="https://creativecommons.org/licenses/by-nc/4.0/",
                 commercial_use_allowed=False, rights_source="x"), "commercial_use_denied"),
    (MediaRights(license_id="cc-by-sa-4.0", license_url="https://creativecommons.org/licenses/by-sa/4.0/",
                 commercial_use_allowed=True, modifications_allowed=True, attribution_required=True,
                 attribution_text="Ann", rights_source="x"), "unsupported_license_obligations"),
])
def test_asset_metadata_never_reports_unknown_or_incompatible_as_reusable(rights, reason):
    item = replace(cand("u", "rover", "Rover", kind="photo", provider="openverse"), rights=rights, width=0, height=0)
    metadata = asset_metadata(item)
    assert metadata["license"]["status"] != "usable" and metadata["license"]["reason"] == reason
    assert metadata["usage_restrictions"][0] == f"not_cleared:{reason}"
    assert metadata["dimensions"] == {"width": None, "height": None, "known": False}


def test_asset_metadata_reaches_persisted_evidence_and_api_schema():
    [commons] = adapter_search(commons_client(CommonsAPI(commons_payload(commons_page(1, 1, "Rover.jpg")))))
    evidence = candidate_evidence(commons)
    assert evidence["asset_metadata"] == asset_metadata(commons)
    read = SceneMediaCandidateRead(**(evidence | {"token": "t:0", "preview_url": commons.preview_url}))
    assert read.asset_metadata["license"]["id"] == "cc-by-4.0"
