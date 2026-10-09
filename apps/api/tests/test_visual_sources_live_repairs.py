"""Regression tests for the first real Commons/NASA API validation (fixtures follow its report).

Mocked official API shapes only; no live requests, keys or Keychain access.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from test_staged_media_search import cand
from test_visual_sources_phase1 import CommonsAPI, cc_by, commons_client, ext

from clipforge.media import (
    MIN_USABLE_SHORT_SIDE,
    MediaProviderError,
    _cache_candidate,
    commons_delivered_size,
    delivered_photo,
    parse_photo_results,
)
from clipforge.open_media import NASAProvider, clear_search_cache, uri_rights
from clipforge.pixabay import parse_pixabay_photos
from clipforge.still_image import decoded_size
from clipforge.visual_providers import (
    AcquisitionBudget,
    CandidateLedger,
    ProviderAdapter,
    asset_keys,
    canonical_source,
)
from clipforge.visual_rights import (
    MediaRights,
    commons_rights,
    evaluate_rights,
    provider_terms_rights,
)

BASE = "https://upload.wikimedia.org/wikipedia/commons/a/ab"
UTM = "utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=thumbnail"


def live_page(pageid, index, name, original, api_thumb, *, thumb, metadata=None, mime="image/jpeg"):
    """A Commons search page as the real API returned it when the 1600px thumbnail was requested.

    ``thumb``: ``"unscaled"`` (the original is served), an int (standard ``<N>px-`` thumbnail on
    thumb.wikimedia.org) or a literal URL.
    """
    if thumb == "unscaled":
        thumb_url = f"{BASE}/{name}?{UTM}_unscaled"
    elif isinstance(thumb, int):
        thumb_url = f"https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/{thumb}px-{name}?{UTM}"
    else:
        thumb_url = thumb
    return {"pageid": pageid, "ns": 6, "title": f"File:{name}", "index": index, "imageinfo": [{
        "url": f"{BASE}/{name}", "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
        "width": original[0], "height": original[1], "mime": mime,
        "thumburl": thumb_url, "thumbwidth": api_thumb[0], "thumbheight": api_thumb[1],
        "extmetadata": cc_by() if metadata is None else metadata,
    }]}


def search(*pages, query="mars surface rocks", portrait=True):
    payload = {"batchcomplete": "", "query": {"pages": {str(p["pageid"]): p for p in pages}}}
    client = commons_client(CommonsAPI(payload))
    return ProviderAdapter("wikimedia", client).search(query, "photo", portrait=portrait, scene_duration=4, budget=AcquisitionBudget())


def jpeg(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 60, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --- Commons delivered dimensions (live: unscaled originals and 1920px thumbnails) ------------


def test_original_smaller_than_requested_size_reports_the_original_not_1600():
    [mars, square, unscaled] = search(
        live_page(1, 1, "Polygons_Mars.jpg", (1287, 1425), (1600, 1772), thumb="unscaled"),
        live_page(2, 2, "RASP_tool.jpg", (1024, 1024), (1600, 1600), thumb="unscaled"),
        live_page(3, 3, "Licenced_victuallers.jpg", (895, 1413), (1600, 2526), thumb="unscaled"),
    )
    assert [(c.width, c.height) for c in (mars, square, unscaled)] == [(1287, 1425), (1024, 1024), (895, 1413)]
    assert mars.origin["dimension_source"] == "original_unscaled"
    # API-reported thumbnail and original sizes stay available for diagnostics.
    assert (mars.origin["api_thumb_width"], mars.origin["api_thumb_height"]) == (1600, 1772)
    assert (mars.origin["original_width"], mars.origin["original_height"]) == (1287, 1425)


def test_standard_1920px_thumbnail_reports_scaled_original_dimensions():
    portrait, landscape = search(
        live_page(1, 1, "Person_fills_a_glass.jpg", (4032, 6048), (1600, 2400), thumb=1920),
        live_page(2, 2, "Buffalo_pond.jpg", (6393, 3596), (1600, 900), thumb=1920),
        portrait=True,
    )
    assert (portrait.width, portrait.height) == (1920, 2880)
    assert (landscape.width, landscape.height) == (1920, 1080)
    assert portrait.origin["dimension_source"] == "thumb_url"
    assert portrait.download_url.startswith("https://thumb.wikimedia.org/")  # the delivered URL is untouched


def test_api_thumbnail_metadata_is_never_trusted_over_the_url():
    [from_url] = search(live_page(1, 1, "A.jpg", (4000, 3000), (1600, 1200), thumb=1280))
    assert (from_url.width, from_url.height) == (1280, 960)  # the URL says 1280px, not the API's 1600
    # A thumbnail request wider than the original is never an upscale.
    [capped] = search(live_page(2, 1, "B.jpg", (1000, 800), (1600, 1280), thumb=1600))
    assert (capped.width, capped.height) == (1000, 800)
    assert capped.origin["dimension_source"] == "thumb_url_capped"


@pytest.mark.parametrize("thumb", [
    "https://cdn.example.test/anything.jpg",  # other host
    "http://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/C.jpg/1600px-C.jpg",  # not https
    "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/C.jpg/preview-C.jpg",  # no <N>px-
    "https://upload.wikimedia.org/wikipedia/commons/a/ab/Other_file.jpg?utm_content=thumbnail_unscaled",  # other path
])
def test_unexpected_url_shapes_assume_no_more_than_the_original(thumb):
    [item] = search(live_page(1, 1, "C.jpg", (2000, 1500), (1600, 1200), thumb=thumb))
    # Capped at what the API claims AND the original allows; flagged as unverified.
    assert (item.width, item.height) == (1600, 1200)
    assert item.origin["dimension_source"] == "api_thumb_capped"
    [small] = search(live_page(2, 1, "D.jpg", (1100, 900), (1600, 1309), thumb=thumb))
    assert (small.width, small.height) == (1100, 900)  # never above the original


def test_unknown_original_size_assumes_nothing():
    page = live_page(1, 1, "E.jpg", (0, 0), (1600, 1200), thumb=1600)
    page["imageinfo"][0].pop("width"), page["imageinfo"][0].pop("height")
    assert commons_delivered_size(page["imageinfo"][0]) == (0, 0, "original_unknown")
    assert search(page) == []  # 0x0 fails the quality floor: nothing undersized is admitted


def test_undersized_originals_are_rejected_by_the_quality_floor():
    # Live: a 1024x513 original was served unscaled but reported as 1600x802 and admitted.
    kept = search(
        live_page(1, 1, "Panorama.jpg", (1024, 513), (1600, 802), thumb="unscaled"),
        live_page(2, 2, "Tall_strip.jpg", (500, 3000), (500, 3000), thumb="unscaled"),
        live_page(3, 3, "Big_enough.jpg", (1024, 1024), (1600, 1600), thumb="unscaled"),
    )
    assert [c.provider_id for c in kept] == ["3"]


@pytest.mark.parametrize("info,expected", [
    ({"width": 1287, "height": 1425, "url": f"{BASE}/P.jpg", "thumburl": f"{BASE}/P.jpg?{UTM}_unscaled"}, (1287, 1425, "original_unscaled")),
    ({"width": 1200, "height": 900, "url": f"{BASE}/P.jpg"}, (1200, 900, "original")),
    ({"width": 6000, "height": 4000, "url": f"{BASE}/P.jpg", "thumburl": f"{BASE.replace('/a/ab', '/thumb/a/ab')}/P.jpg/600px-P.jpg"}, (600, 400, "thumb_url")),
    ({"width": 6000, "height": 4000, "url": f"{BASE}/P.pdf", "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/P.pdf/page1-800px-P.pdf.jpg"}, (800, 533, "thumb_url")),
])
def test_commons_delivered_size_rule(info, expected):
    assert commons_delivered_size(info) == expected


# --- decoded dimensions after download -------------------------------------------------------


class Downloader:
    def __init__(self, content):
        self.content = content

    def download(self, _candidate, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.content)
        return destination


def cached(tmp_path, candidate, content):
    return _cache_candidate(candidate, {}, asset_root=tmp_path / "project" / "assets", render_root=tmp_path, downloader=Downloader(content))


def test_decoded_dimensions_are_authoritative_after_download(tmp_path):
    candidate = replace(cand("77", "mars", "Mars", kind="photo", provider="wikimedia"), width=1600, height=2135,
                        origin={"provider": "Wikimedia Commons", "original_width": 811, "original_height": 1082})
    evidence = cached(tmp_path, candidate, jpeg(811, 1082))
    assert (evidence["width"], evidence["height"]) == (811, 1082)
    assert evidence["delivered_dimensions"] == {"width": 811, "height": 1082, "source": "decoded", "reported": [1600, 2135]}
    assert evidence["asset_metadata"]["dimensions"] == {"width": 811, "height": 1082, "known": True}
    assert evidence["origin"] == candidate.origin  # provider provenance is not rewritten
    assert decoded_size(tmp_path / evidence["cache_path"]) == (811, 1082)


def test_decoded_undersized_download_is_refused_and_removed(tmp_path):
    candidate = replace(cand("78", "mars", "Mars", kind="photo", provider="wikimedia"), width=1600, height=900)
    with pytest.raises(MediaProviderError) as error:
        cached(tmp_path, candidate, jpeg(640, MIN_USABLE_SHORT_SIDE - 1))
    assert error.value.category == "undersized_media" and "too small" in str(error.value)
    assert not list((tmp_path / "project" / "assets").rglob("photo-78.jpg"))  # nothing left to reuse
    # The shared quality floor is inclusive.
    assert cached(tmp_path, replace(candidate, provider_id="79"), jpeg(640, MIN_USABLE_SHORT_SIDE))["height"] == MIN_USABLE_SHORT_SIDE


def test_unidentifiable_download_keeps_provider_dimensions(tmp_path):
    candidate = replace(cand("80", "mars", "Mars", kind="photo", provider="wikimedia"), width=1600, height=1067)
    evidence = cached(tmp_path, candidate, b"not an image FFmpeg might still read")
    assert (evidence["width"], evidence["height"]) == (1600, 1067) and "delivered_dimensions" not in evidence
    assert delivered_photo(candidate, tmp_path / "missing.jpg") == (candidate, None)


def test_pexels_and_pixabay_photos_keep_their_behavior(tmp_path):
    pexels = parse_photo_results({"photos": [{
        "id": 5, "width": 6000, "height": 4000, "url": "https://www.pexels.com/photo/5/", "photographer": "P",
        "src": {"portrait": "https://images.pexels.com/p.jpeg?h=1200", "landscape": "https://images.pexels.com/l.jpeg"},
    }]}, query="dust", portrait=True)[0]
    pixabay = parse_pixabay_photos({"hits": [{
        "id": 9, "imageWidth": 4000, "imageHeight": 3000, "largeImageURL": "https://pixabay.com/l.jpg",
        "pageURL": "https://pixabay.com/p/9/", "user": "u", "tags": "dust, wind",
    }]}, query="dust", portrait=False)[0]
    assert (pexels.width, pexels.height, pexels.rights) == (6000, 4000, provider_terms_rights("pexels"))
    assert (pixabay.width, pixabay.height, pixabay.rights) == (4000, 3000, provider_terms_rights("pixabay"))
    for candidate, delivered in ((pexels, (800, 1200)), (pixabay, (1280, 960))):
        # Non-decodable provider bytes: exactly the previous behavior.
        legacy = cached(tmp_path / "legacy", candidate, b"mock-media")
        assert (legacy["width"], legacy["height"]) == (candidate.width, candidate.height)
        assert legacy["rights"]["license_id"] == f"{candidate.provider}-license" and "delivered_dimensions" not in legacy
        # A real delivered file is measured; rights/identity are untouched.
        real = cached(tmp_path / "real", candidate, jpeg(*delivered))
        assert (real["width"], real["height"]) == delivered
        assert real["delivered_dimensions"]["reported"] == [candidate.width, candidate.height]
        assert real["rights"] == legacy["rights"] and real["identity"] == legacy["identity"]


# --- Wikimedia host normalization and cross-variant deduplication ----------------------------

FILE = "Person_fills_a_glass_(water),_home.jpg"
KEY = f"commons.wikimedia.org/wiki/File:{FILE}"
ENCODED = "Person_fills_a_glass_%28water%29%2C_home.jpg"
VARIANTS = [
    f"https://upload.wikimedia.org/wikipedia/commons/0/0a/{ENCODED}",
    f"https://upload.wikimedia.org/wikipedia/commons/0/0a/{ENCODED}?{UTM}_unscaled",
    f"https://upload.wikimedia.org/wikipedia/commons/thumb/0/0a/{ENCODED}/1600px-{ENCODED}",
    f"https://thumb.wikimedia.org/wikipedia/commons/thumb/0/0a/{ENCODED}/1920px-{ENCODED}?{UTM}",
    f"https://thumb.wikimedia.org/wikipedia/commons/thumb/0/0a/{ENCODED}/320px-{ENCODED}",
    f"https://commons.wikimedia.org/wiki/File:{ENCODED}",
    f"https://commons.wikimedia.org/wiki/Special:FilePath/{ENCODED}",
    f"https://commons.wikimedia.org/wiki/File:{FILE}",
]


@pytest.mark.parametrize("address", VARIANTS)
def test_every_wikimedia_variant_has_one_canonical_identity(address):
    assert canonical_source(address) == KEY


@pytest.mark.parametrize("address", [
    "https://thumb.wikimedia.org.evil.test/wikipedia/commons/thumb/0/0a/A.jpg/1px-A.jpg",
    "https://evil.test/wikipedia/commons/thumb/0/0a/A.jpg/1920px-A.jpg",
    "https://thumb.wikimedia.org/wikipedia/en/thumb/0/0a/A.jpg/200px-A.jpg",  # not the Commons repository
])
def test_only_commons_media_on_wikimedia_hosts_is_normalized(address):
    assert not (canonical_source(address) or "").startswith("commons.wikimedia.org/wiki/File:")


def test_different_commons_files_stay_distinct_and_pages_keep_deduping():
    assert canonical_source(VARIANTS[3]) != canonical_source(VARIANTS[3].replace("Person_fills", "Person_pours"))
    other = replace(cand("1", "q", "t", kind="photo", provider="wikimedia"), source_url=f"https://commons.wikimedia.org/wiki/File:{ENCODED}")
    page_only = replace(other, provider_id="2", download_url="https://media.test/other", source_url=VARIANTS[5])
    assert asset_keys(other) & asset_keys(page_only)


def test_thumbnail_original_and_rehosted_variants_are_one_asset_in_the_ledger():
    ledger = CandidateLedger()
    # Distinct page URLs: only the media URL can identify these as one asset.
    original = replace(cand("1", "q", "t", kind="photo", provider="wikimedia"), download_url=VARIANTS[0],
                       source_url="https://source-a.test/item")
    thumb_1920 = replace(original, provider_id="2", download_url=VARIANTS[3], source_url="https://source-b.test/item")
    thumb_320 = replace(original, provider_id="3", download_url=VARIANTS[4], source_url="https://source-c.test/item")
    aggregator = replace(original, provider="openverse", provider_id="uuid", source_url="https://aggregator.test/item/1",
                         download_url=VARIANTS[2])
    assert ledger.admit(thumb_1920)
    assert not ledger.admit(original) and not ledger.admit(thumb_320) and not ledger.admit(aggregator)
    unrelated = replace(original, provider_id="9", source_url="https://source-d.test/item",
                        download_url=VARIANTS[0].replace("Person_fills", "Other_file"))
    assert ledger.admit(unrelated)


# --- NASA enrichment diagnostics (fail closed, names and statuses only) -----------------------


def nasa_items(count):
    return [{
        "data": [{"nasa_id": f"PIA{index:05d}", "media_type": "image", "title": f"Mars {index}", "description": "Mars", "keywords": ["Mars"]}],
        "links": [{"href": f"https://images-assets.nasa.gov/image/PIA{index:05d}/PIA{index:05d}~thumb.jpg", "rel": "preview", "render": "image"}],
    } for index in range(1, count + 1)]


def nasa_search(metadata_by_id, *, count=5, location_host="images-assets.nasa.gov", budget=None, broken=()):
    def handle(request):
        path = request.url.path
        if path == "/search":
            return httpx.Response(200, json={"collection": {"items": nasa_items(count)}}, request=request)
        identifier = path.rsplit("/", 1)[-1] if path.startswith(("/metadata/", "/asset/")) else path.split("/")[2]
        if identifier in broken and path.startswith("/metadata/"):
            return httpx.Response(500, request=request)
        if path.startswith("/metadata/"):
            return httpx.Response(200, json={"location": f"https://{location_host}/image/{identifier}/metadata.json"}, request=request)
        if path.startswith("/asset/"):
            return httpx.Response(200, json={"collection": {"items": [{"href": f"https://images-assets.nasa.gov/image/{identifier}/{identifier}~orig.jpg"}]}}, request=request)
        return httpx.Response(200, json=metadata_by_id.get(identifier, {}), request=request)

    clear_search_cache()  # the process-wide metadata cache would answer a repeated query with only cleared items
    provider = NASAProvider(client=httpx.Client(transport=httpx.MockTransport(handle)))
    return {c.provider_id: c for c in provider.search("mars", "photo", portrait=True, scene_duration=4, budget=budget or AcquisitionBudget())}


def test_nasa_diagnostics_record_present_and_missing_rights_fields():
    items = nasa_search({"PIA00001": {"AVAIL:NASAID": "PIA00001"}}, count=1)
    evidence = items["PIA00001"].rights.evidence
    assert evidence["rights_fields_present"] == ["AVAIL:NASAID"]
    assert {"XMP:Marked", "XMP:Rights", "IPTC:CopyrightNotice"} <= set(evidence["rights_fields_missing"])
    assert evidence["enrichment"] == {"metadata": "fetched", "asset_manifest": "not_needed"}
    # Live finding: a matching NASA ID without XMP:Marked establishes nothing.
    assert evaluate_rights(items["PIA00001"].rights).reason == "missing_license_evidence"


def test_nasa_enrichment_is_bounded_and_unattempted_items_are_labelled():
    items = nasa_search({f"PIA{i:05d}": {"AVAIL:NASAID": f"PIA{i:05d}"} for i in range(1, 6)}, count=5)
    statuses = {key: value.rights.evidence["enrichment"]["metadata"] for key, value in items.items()}
    assert [statuses[f"PIA{i:05d}"] for i in (1, 2, 3)] == ["fetched"] * 3
    assert [statuses[f"PIA{i:05d}"] for i in (4, 5)] == ["not_attempted:beyond_enrichment_limit"] * 2
    assert all(evaluate_rights(c.rights).status == "unknown" for c in items.values())


def test_nasa_failed_or_rejected_metadata_lookups_are_diagnosed_and_stay_unknown():
    failed = nasa_search({}, count=1, broken={"PIA00001"})["PIA00001"]
    assert failed.rights.evidence["enrichment"]["metadata"].startswith("error:")
    foreign = nasa_search({}, count=1, location_host="metadata.evil.test")["PIA00001"]
    assert foreign.rights.evidence["enrichment"]["metadata"] == "location_rejected"
    for item in (failed, foreign):
        assert evaluate_rights(item.rights).status == "unknown" and item.width == item.height == 0


def test_nasa_budget_exhaustion_skips_enrichment_with_a_reason():
    tight = AcquisitionBudget(max_search_requests=3)  # search leaves < 3 slots
    item = nasa_search({"PIA00001": {"XMP:Marked": False, "AVAIL:NASAID": "PIA00001"}}, count=1, budget=tight)["PIA00001"]
    assert item.rights.evidence["enrichment"]["metadata"] == "not_attempted:budget_headroom"
    assert evaluate_rights(item.rights).status == "unknown" and tight.search_requests <= 3


@pytest.mark.parametrize("metadata,status", [
    ({"XMP:Marked": False, "AVAIL:NASAID": "PIA00001"}, "usable"),
    ({"XMP:Marked": "False", "AVAIL:NASAID": "PIA00001"}, "unknown"),  # a string is not the boolean statement
    ({"XMP:Marked": True, "AVAIL:NASAID": "PIA00001"}, "unknown"),
    ({"XMP:Marked": False, "AVAIL:NASAID": "PIA99999"}, "unknown"),  # another asset's metadata
    ({"XMP:Marked": False}, "unknown"),
    ({"XMP:Marked": False, "AVAIL:NASAID": "PIA00001", "IPTC:CopyrightNotice": "Third party"}, "unknown"),
    ({"XMP:Marked": False, "AVAIL:NASAID": "PIA00001", "XMP:UsageTerms": "Contact the owner"}, "unknown"),
])
def test_nasa_rights_policy_is_unchanged_and_diagnostics_never_grant_permission(metadata, status):
    item = nasa_search({"PIA00001": metadata}, count=1)["PIA00001"]
    assert evaluate_rights(item.rights).status == status
    present = item.rights.evidence["rights_fields_present"]
    assert set(present) <= {"XMP:Marked", "AVAIL:NASAID", "XMP:Rights", "XMP:UsageTerms", "XMP:WebStatement", "IPTC:CopyrightNotice", "Photoshop:CopyrightFlag"}
    # Diagnostics are names and statuses: raw rights text appears only in the pre-existing evidence keys.
    extra = json.dumps({k: item.rights.evidence[k] for k in ("rights_fields_present", "rights_fields_missing", "enrichment")})
    assert "Third party" not in extra and "Contact the owner" not in extra


# --- Creative Commons recognition ------------------------------------------------------------

CC0_DEED = "http://creativecommons.org/publicdomain/zero/1.0/deed.en"


def lic(**values):
    """Commons extmetadata fields of one test case."""
    return values


def rights(**values):
    return commons_rights(ext(**values), source_url="https://commons.wikimedia.org/wiki/File:X.jpg",
                          creator="Ann" if "Artist" in values else "")


@pytest.mark.parametrize("values,status,reason", [
    # The confirmed false negative: identifier "cc0" with a localized deed URL.
    (lic(License="cc0", LicenseShortName="CC0", LicenseUrl=CC0_DEED), "usable", "established_reuse_rights"),
    (lic(License="cc0", LicenseUrl="https://creativecommons.org/publicdomain/zero/1.0/"), "usable", "established_reuse_rights"),
    (lic(License="cc0"), "usable", "established_reuse_rights"),
    (lic(License="cc-zero", LicenseUrl="https://creativecommons.org/publicdomain/zero/1.0/deed.de"), "usable", "established_reuse_rights"),
    # CC BY keeps working, with or without the deed suffix.
    (lic(License="cc-by-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deed.de", Artist="Ann", AttributionRequired="true"), "usable", "established_reuse_rights"),
    (lic(License="cc-by-3.0", LicenseUrl="https://creativecommons.org/licenses/by/3.0/deed.pt-br", Artist="Ann", AttributionRequired="true"), "usable", "established_reuse_rights"),
    (lic(License="cc-by-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/", Artist="Ann", AttributionRequired="true"), "usable", "established_reuse_rights"),
    # Share-alike, non-commercial and no-derivatives obligations are still enforced.
    (lic(License="cc-by-sa-4.0", LicenseUrl="https://creativecommons.org/licenses/by-sa/4.0/deed.en", Artist="Ann"), "unusable", "unsupported_license_obligations"),
    (lic(License="cc-by-sa-3.0", LicenseUrl="https://creativecommons.org/licenses/by-sa/3.0/deed.en", Artist="Ann"), "unusable", "unsupported_license_obligations"),
    (lic(License="cc-by-nc-4.0", LicenseUrl="https://creativecommons.org/licenses/by-nc/4.0/deed.en", Artist="Ann"), "unusable", "commercial_use_denied"),
    (lic(License="cc-by-nd-4.0", LicenseUrl="https://creativecommons.org/licenses/by-nd/4.0/deed.en", Artist="Ann"), "unusable", "modifications_denied"),
    # Fake domains, spoofed hosts and malformed suffixes are never accepted.
    (lic(License="cc0", LicenseUrl="https://evil.test/publicdomain/zero/1.0/deed.en"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc0", LicenseUrl="https://creativecommons.org.evil.test/publicdomain/zero/1.0/deed.en"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc0", LicenseUrl="https://creativecommons.org@evil.test/publicdomain/zero/1.0/"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc-by-4.0", LicenseUrl="https://evil.test/licenses/by/4.0/deed.en", Artist="Ann"), "unknown", "commercial_use_unknown"),
    (lic(License="cc-by-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deedx", Artist="Ann"), "unknown", "commercial_use_unknown"),
    (lic(License="cc-by-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deed.en/extra", Artist="Ann"), "unknown", "commercial_use_unknown"),
    # Unknown or conflicting evidence stays blocked.
    (lic(License="cc0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deed.en"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc0", LicenseUrl=CC0_DEED, Copyrighted="True"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc0", LicenseUrl=CC0_DEED, Restrictions="trademarked"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc-by-sa-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deed.en", Artist="Ann"), "unknown", "ambiguous_license_evidence"),
    (lic(License="cc-by-4.0", LicenseUrl="https://creativecommons.org/licenses/by/4.0/deed.en"), "unknown", "attribution_missing"),
    (lic(License="all-rights-reserved"), "unknown", "commercial_use_unknown"),
    (lic(Artist="Ann"), "unknown", "missing_license_evidence"),
])
def test_creative_commons_recognition_matrix(values, status, reason):
    decision = evaluate_rights(rights(**values))
    assert (decision.status, decision.reason) == (status, reason)


def test_deed_suffix_is_also_accepted_for_aggregator_license_uris():
    usable = uri_rights("https://creativecommons.org/licenses/by/4.0/deed.en", source="x", creator="Ann", page="https://p.test/1", attribution="Ann, CC BY")
    assert evaluate_rights(usable).status == "usable"
    blocked = uri_rights("https://creativecommons.org/licenses/by-sa/4.0/deed.en", source="x", creator="Ann", page="https://p.test/1")
    assert evaluate_rights(blocked).status == "unusable"
    assert evaluate_rights(MediaRights()).status == "unknown"


def test_live_cc0_commons_page_is_admitted_end_to_end():
    cc0_page = {"pageid": 5, "index": 1, "title": "File:Amsterdam.jpg", "imageinfo": [{
        "url": f"{BASE}/Amsterdam.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Amsterdam.jpg",
        "width": 4000, "height": 3000, "mime": "image/jpeg", "thumburl": f"{BASE}/thumb/Amsterdam.jpg/1920px-Amsterdam.jpg",
        "thumbwidth": 1600, "thumbheight": 1200,
        "extmetadata": ext(License="cc0", LicenseShortName="CC0", LicenseUrl=CC0_DEED),
    }]}
    [item] = search(cc0_page)
    assert evaluate_rights(item.rights).status == "usable" and item.rights.public_domain is True
    assert CandidateLedger().admit(item)
