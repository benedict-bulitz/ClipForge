"""Foundation regressions: production rights and acquisition, no live services."""

import copy
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_export import seed_project
from test_media_candidates import FakeProvider, FakeWikimedia, candidate, state
from test_staged_media_search import Commons, Provider, Verifier, breath_project, cand, settings_for

from clipforge import media, media_candidates
from clipforge.media import (
    MediaProviderError,
    PexelsMediaClient,
    WikimediaMediaClient,
    build_visual_query_plan,
    candidate_evidence,
    destination_asset_allowed,
    is_real_media_allowed,
    parse_photo_results,
    parse_video_results,
    prepare_project_media,
    run_staged_scene_search,
    verify_media_shortlist,
)
from clipforge.media_candidates import (
    CandidateError,
    apply_scene_media_candidate,
    discover_scene_media_candidates,
)
from clipforge.pixabay import PixabayMediaClient, parse_pixabay_photos, parse_pixabay_videos
from clipforge.renderer import RenderUnavailable, _create_visual_segment, _scene_media_path
from clipforge.schemas import SceneMediaCandidateRead
from clipforge.services import RevisionConflict, get_project, serialize_project
from clipforge.visual_providers import (
    AcquisitionBudget,
    CandidateLedger,
    ProviderAdapter,
    asset_keys,
    create_provider_registry,
    provider_call,
)
from clipforge.visual_rights import (
    MediaRights,
    commons_rights,
    evaluate_rights,
    provider_terms_rights,
)
from clipforge.visual_verifier import UnavailableVisualVerifier

PD = MediaRights(license_id="CC0-1.0", public_domain=True, rights_source="asset:dedication")
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
UNAVAILABLE = UnavailableVisualVerifier()


@pytest.mark.parametrize(
    "rights,modifies,status",
    [
        (MediaRights(), True, "unknown"),
        (PD, True, "usable"),
        (replace(PD, commercial_use_allowed=False), True, "unusable"),
        (replace(PD, modifications_allowed=False), True, "unusable"),
        (replace(PD, modifications_allowed=False), False, "usable"),
        (
            MediaRights(license_id="x", commercial_use_allowed=True, rights_source="api"),
            True,
            "unknown",
        ),
        (
            MediaRights(
                license_id="x",
                commercial_use_allowed=True,
                modifications_allowed=True,
                attribution_required=True,
                rights_source="api",
            ),
            True,
            "unknown",
        ),
        (
            replace(PD, attribution_required=True, attribution_text="Creator; source; CC0"),
            True,
            "usable",
        ),
        ({"license_id": "x", "public_domain": "true", "rights_source": "api"}, True, "unknown"),
    ],
)
def test_rights_authority(rights, modifies, status):
    assert evaluate_rights(rights, modifies=modifies).status == status


def test_default_candidate_and_legacy_cache_fail_closed(tmp_path):
    item = replace(candidate("legacy", "photo", 100), rights=MediaRights())
    assert not is_real_media_allowed(item)
    legacy = {
        "identity": item.identity,
        "provider": "pexels",
        "kind": "photo",
        "cache_path": "old.jpg",
    }
    (tmp_path / "old.jpg").write_bytes(b"old")
    assert _scene_media_path({"media": legacy}, settings_for(tmp_path))[0] is None
    s = breath_project()
    s["scenes"][0]["media"] = legacy
    prepare_project_media(
        s,
        "project",
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert legacy not in s["assets"]["license_manifest"]
    assert s["scenes"][0].get("media") != legacy


def test_pexels_photo_and_video_normalization_preserves_provider_terms():
    photo = parse_photo_results(
        {
            "photos": [
                {
                    "id": 1,
                    "width": 1200,
                    "height": 1600,
                    "src": {"original": "https://cdn.test/1"},
                    "photographer": "Alice",
                }
            ]
        },
        query="lighthouse",
        portrait=True,
    )[0]
    video = parse_video_results(
        {
            "videos": [
                {
                    "id": 2,
                    "duration": 8,
                    "video_files": [
                        {
                            "file_type": "video/mp4",
                            "link": "https://cdn.test/2",
                            "width": 1080,
                            "height": 1920,
                        }
                    ],
                }
            ]
        },
        query="lighthouse",
        portrait=True,
        scene_duration=6,
    )[0]
    for item in (photo, video):
        assert item.rights == provider_terms_rights("pexels")
        assert item.rights.public_domain is None
        assert item.rights.rights_source.startswith("provider_terms:")
        assert evaluate_rights(item.rights).status == "usable"


def test_pixabay_photo_and_video_normalization_preserves_provider_terms():
    photo = parse_pixabay_photos(
        {
            "hits": [
                {
                    "id": 1,
                    "imageWidth": 1200,
                    "imageHeight": 1600,
                    "largeImageURL": "https://cdn.test/1",
                    "user": "Alice",
                }
            ]
        },
        query="lighthouse",
        portrait=True,
    )[0]
    video = parse_pixabay_videos(
        {
            "hits": [
                {
                    "id": 2,
                    "duration": 8,
                    "videos": {
                        "large": {"url": "https://cdn.test/2", "width": 1080, "height": 1920}
                    },
                }
            ]
        },
        query="lighthouse",
        portrait=True,
        scene_duration=6,
    )[0]
    for item in (photo, video):
        assert item.rights == provider_terms_rights("pixabay")
        assert item.rights.public_domain is None  # API does not establish a CC0 publication date
        assert evaluate_rights(item.rights).status == "usable"


def commons_metadata(url="https://creativecommons.org/licenses/by/4.0/"):
    return {
        key: {"value": value}
        for key, value in {
            "License": "cc-" + url.split("/licenses/")[-1].strip("/").replace("/", "-"),
            "LicenseShortName": "CC BY 4.0",
            "LicenseUrl": url,
            "Artist": "<b>Alice</b>",
            "Credit": "Museum collection",
            "AttributionRequired": "true",
        }.items()
    }


def test_wikimedia_normalization_carries_actual_metadata_and_attribution():
    metadata = commons_metadata()
    payload = {
        "query": {
            "pages": {
                "1": {
                    "pageid": 1,
                    "title": "File:Lighthouse.jpg",
                    "imageinfo": [
                        {
                            "width": 1200,
                            "height": 1600,
                            "url": "https://cdn.test/1",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Lighthouse.jpg",
                            "extmetadata": metadata,
                        }
                    ],
                }
            }
        }
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    item = WikimediaMediaClient(client=client).search_photos("lighthouse", portrait=True)[0]
    assert item.rights.rights_source.startswith("wikimedia_extmetadata:")
    assert item.rights.evidence["Artist"] == "<b>Alice</b>"
    assert item.rights.attribution_required
    assert all(
        text in item.rights.attribution_text
        for text in (
            "Alice",
            "File:Lighthouse",
            "CC BY",
            "cropped/transformed",
            "Museum collection",
        )
    )
    assert evaluate_rights(item.rights).status == "usable"
    serialized = candidate_evidence(item)
    assert serialized["rights"]["attribution_text"] == item.rights.attribution_text
    assert (
        serialized["rights"]["rights_policy_version"] == evaluate_rights(item.rights).policy_version
    )


@pytest.mark.parametrize(
    "metadata,status",
    [
        ({}, "unknown"),
        ({"License": {"value": "pd"}}, "usable"),
        (commons_metadata("https://creativecommons.org/licenses/by-nc/4.0/"), "unusable"),
        (commons_metadata("https://creativecommons.org/licenses/by-nd/4.0/"), "unusable"),
        (commons_metadata("https://creativecommons.org/licenses/by-sa/4.0/"), "unusable"),
        (commons_metadata("https://evil.test/licenses/by/4.0/"), "unknown"),
        (commons_metadata() | {"Artist": {"value": ""}}, "unknown"),
        (commons_metadata() | {"Restrictions": {"value": "Permission required"}}, "unknown"),
    ],
)
def test_commons_ambiguous_or_incompatible_evidence_fails_closed(metadata, status):
    creator = "Alice" if metadata.get("Artist", {}).get("value") else ""
    rights = commons_rights(metadata, source_url="https://commons.test/file/1", creator=creator)
    assert evaluate_rights(rights).status == status


@pytest.mark.parametrize("factory", [PexelsMediaClient, PixabayMediaClient, WikimediaMediaClient])
@pytest.mark.parametrize(
    "problem,category",
    [
        (401, "invalid_credentials"),
        (429, "rate_limited"),
        ("timeout", "timeout"),
        ("malformed", "malformed_response"),
    ],
)
def test_provider_failures_are_categorized(factory, problem, category):
    def answer(request):
        if problem == "timeout":
            raise httpx.ReadTimeout("secret-url", request=request)
        return httpx.Response(problem if isinstance(problem, int) else 200, json=[])

    http = httpx.Client(transport=httpx.MockTransport(answer))
    client = (
        factory(client=http) if factory is WikimediaMediaClient else factory("SECRET", client=http)
    )
    adapter = ProviderAdapter(client.provider, client)
    with pytest.raises(MediaProviderError) as error:
        adapter.search(
            "lighthouse", "photo", portrait=True, scene_duration=6, budget=AcquisitionBudget()
        )
    assert error.value.category == category
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "exception,category",
    [
        (RuntimeError("SECRET"), "unexpected_provider_error"),
        (ValueError("bad dimensions"), "malformed_response"),
        (httpx.ReadTimeout("timeout"), "timeout"),
    ],
)
def test_one_failed_adapter_does_not_abort_other_provider_paths(tmp_path, exception, category):
    class Broken(Provider):
        def search_videos(self, *_args, **_kwargs):
            raise exception

        def search_photos(self, *_args, **_kwargs):
            raise exception

    s = breath_project()
    good = cand(
        "commons",
        "visible breath winter",
        "Visible breath in cold winter air",
        provider="wikimedia",
        kind="photo",
    )
    prepare_project_media(
        s,
        "project",
        settings_for(tmp_path),
        client=Broken(),
        fallback_client=Commons(photos={"visible breath winter": [good]}),
        extra_clients=[],
        visual_verifier=Verifier(),
    )
    assert s["scenes"][0]["media"]["identity"] == good.identity
    assert category in s["scenes"][0]["media_search"]["stages"][0]["errors"]


def test_programming_assertions_are_not_hidden():
    with pytest.raises(AssertionError):
        provider_call(lambda: (_ for _ in ()).throw(AssertionError("programming assertion")))


def test_registry_capabilities_and_cleanup(tmp_path):
    settings = settings_for(tmp_path)
    settings.pexels_api_key = None
    settings.pixabay_api_key = None
    registry = create_provider_registry(settings)
    assert [item.provider for item in registry.enabled()] == ["wikimedia", "openverse", "nasa", "loc"]
    assert registry.enabled("video") == []
    assert registry.get("wikimedia").capabilities.evidence == "imageinfo.extmetadata"
    registry.close()


def manual_setup(db, tmp_path):
    settings = settings_for(tmp_path)
    s = state()
    project = seed_project(db, PROJECT_ID, s)
    provider = FakeProvider()

    def download(_candidate, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"video")
        return destination

    provider.download = download
    _, choices = discover_scene_media_candidates(
        s,
        PROJECT_ID,
        1,
        1,
        settings,
        client=provider,
        fallback_client=FakeWikimedia(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    return settings, s, project, provider, choices


def test_change_media_discovery_apply_persistence_and_reopen(db, tmp_path):
    settings, _s, project, provider, choices = manual_setup(db, tmp_path)
    assert choices and choices[0]["rights"]["rights_source"]
    schema = SceneMediaCandidateRead.model_validate(choices[0]).model_dump()
    for key in (
        "rights",
        "rights_acceptance",
        "verification_url",
        "title",
        "description",
        "tags",
        "canonical_asset_key",
    ):
        assert schema[key] == choices[0][key]
    apply_scene_media_candidate(
        db,
        project,
        1,
        choices[0]["token"],
        settings,
        client=provider,
        fallback_client=FakeWikimedia(),
        auto_render=False,
    )
    db.expire_all()
    saved = serialize_project(get_project(db, PROJECT_ID))["revision"]["state"]
    chosen = saved["scenes"][0]["media"]
    assert chosen["rights"] == choices[0]["rights"]
    assert chosen["rights_acceptance"] == choices[0]["rights_acceptance"]
    assert chosen["relevance"] and chosen["acceptance_scene_key"]
    assert chosen in saved["assets"]["license_manifest"]
    assert destination_asset_allowed(chosen, saved["scenes"][0], saved)


@pytest.mark.parametrize(
    "change", ["rights", "revision", "expired", "wrong_scene", "rejected", "destination"]
)
def test_change_media_apply_revalidates_before_download(db, tmp_path, change):
    settings, s, project, provider, choices = manual_setup(db, tmp_path)
    token = choices[0]["token"]
    set_token = token.split(":")[0]
    candidate_set = media_candidates._SETS[set_token]
    if change == "rights":
        media_candidates._SETS[set_token] = replace(
            candidate_set,
            candidates=tuple(
                replace(item, rights=MediaRights()) for item in candidate_set.candidates
            ),
        )
    elif change == "revision":
        media_candidates._SETS[set_token] = replace(candidate_set, base_revision=0)
    elif change == "expired":
        media_candidates._SETS[set_token] = replace(
            candidate_set, created_at=time.monotonic() - 10000
        )
    elif change == "wrong_scene":
        media_candidates._SETS[set_token] = replace(candidate_set, scene_number=2)
    else:
        current = copy.deepcopy(s)
        if change == "rejected":
            current["scenes"][0]["rejected_media_identities"] = [
                candidate_set.candidates[0].identity
            ]
        else:
            current["scenes"][0].update(
                narration="A chemical reaction",
                visual_goal="water condensation droplets",
                visual_intent={"media_queries": ["water condensation droplets"]},
            )
        # Simulate current state, keeping revision number to prove Apply checks fit.
        project.revisions[-1].state = current
    calls = []
    provider.download = lambda *_args: calls.append(True)
    with pytest.raises((CandidateError, RevisionConflict)):
        apply_scene_media_candidate(
            db,
            project,
            1,
            token,
            settings,
            client=provider,
            fallback_client=FakeWikimedia(),
            auto_render=False,
        )
    assert calls == []
    assert project.current_revision == 1


def test_manual_unknown_rights_are_not_discovered(tmp_path):
    provider = FakeProvider()
    provider.videos = [replace(item, rights=MediaRights()) for item in provider.videos]
    provider.photos = [replace(item, rights=MediaRights()) for item in provider.photos]
    _, choices = discover_scene_media_candidates(
        state(),
        PROJECT_ID,
        1,
        1,
        settings_for(tmp_path),
        client=provider,
        fallback_client=FakeWikimedia(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert choices == []


def cached_asset(tmp_path, *, title="lighthouse", rights=PD):
    item = replace(candidate("borrow", "video", 100), title=title, query=title, rights=rights)
    metadata = (
        candidate_evidence(item)
        if evaluate_rights(rights).status == "usable"
        else {**vars(item), "rights": rights.serialize(), "identity": item.identity}
    )
    path = tmp_path / "borrow.mp4"
    path.write_bytes(b"video")
    return metadata | {"cache_path": path.name}


@pytest.mark.parametrize(
    "condition", ["unrelated", "rights", "no_reuse", "rejected", "reveal", "lock"]
)
def test_renderer_cannot_borrow_ineligible_asset(tmp_path, condition):
    s = state()
    s["timeline"]["fps"] = 30
    destination = s["scenes"][0]
    metadata = cached_asset(
        tmp_path,
        title="chemical condensation" if condition == "unrelated" else "lighthouse",
        rights=MediaRights() if condition == "rights" else PD,
    )
    if condition == "no_reuse":
        destination["media_repair"] = {"no_reuse": True}
    elif condition == "rejected":
        destination["rejected_media_identities"] = [metadata["identity"]]
    elif condition == "reveal":
        destination["visual_director"] = {"reveal_allowed": False}
        metadata["reveal_safe"] = False
    elif condition == "lock":
        destination["user_locked_visual"] = True
    s["scenes"].append({"media": metadata})
    with pytest.raises(RenderUnavailable):
        _create_visual_segment("ffmpeg", s, destination, 1, 8, tmp_path, settings_for(tmp_path))
    assert "media" not in destination


def test_renderer_valid_intentional_continuity_persists_manifest(tmp_path, monkeypatch):
    s = state()
    s["timeline"]["fps"] = 30
    destination = s["scenes"][0]
    metadata = cached_asset(tmp_path)
    s["scenes"].append({"media": metadata})
    monkeypatch.setattr("clipforge.renderer.analyze_scene_media", lambda *_a: None)

    def render(command, **_kwargs):
        Path(command[-1]).write_bytes(b"rendered")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("clipforge.renderer.subprocess.run", render)
    output = _create_visual_segment(
        "ffmpeg", s, destination, 1, 8, tmp_path, settings_for(tmp_path)
    )
    assert output.is_file()
    assert destination["media"]["rights"] == metadata["rights"]
    assert destination["media"] in s["assets"]["license_manifest"]


def test_automatic_continuity_honors_no_reuse(tmp_path):
    s = state()
    first = s["scenes"][0]
    first.update(block_id="same", preferred_media="photo")
    s["scenes"].append(copy.deepcopy(first) | {"id": "scene-2", "media_repair": {"no_reuse": True}})
    good = replace(candidate("base", "photo", 100), rights=PD)
    provider = Provider(photos={"lighthouse": [good]})
    prepare_project_media(
        s,
        "project",
        settings_for(tmp_path),
        client=provider,
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=Verifier(),
    )
    assert first["media"]["identity"] == good.identity
    assert s["scenes"][1].get("media", {}).get("identity") != good.identity


def test_destination_relevance_never_inherits_other_scenes_visual_score(tmp_path):
    s = state()
    metadata = cached_asset(tmp_path, title="water condensation droplets")
    metadata["relevance"] = {
        "confidence": "high",
        "visual": {"status": "verified", "score": 0.99, "scene_score": 0.99},
    }
    assert not destination_asset_allowed(metadata, s["scenes"][0], s, reuse=True)


def test_shared_budget_bounds_requests_verification_and_three_queries():
    s = breath_project()
    queries = ["visible breath winter", "cold air breath", "water vapor condensation", "extra"]
    provider = Provider(
        videos={
            query: [
                cand(f"{index}-{i}", query, "Visible breath winter cold air") for i in range(20)
            ]
            for index, query in enumerate(queries)
        }
    )
    verifier = Verifier(default=(0.245, 0.245))
    budget = AcquisitionBudget(max_search_requests=2, max_verifications=3)
    result = run_staged_scene_search(
        queries,
        s["scenes"][0],
        s,
        build_visual_query_plan(s["scenes"][0], s),
        pexels=provider,
        wikimedia=Commons(),
        preferred_kind="video",
        portrait=True,
        scene_duration=4,
        used=set(),
        verifier=verifier,
        acquisition_budget=budget,
    )
    assert len(provider.calls) <= 2
    assert len(verifier.calls) <= 3
    assert budget.search_requests == 2 and budget.verifications == 3
    assert result.provenance["logical_queries_executed"] <= 3
    assert "search_requests" in budget.exhausted


def test_verification_and_download_budgets_cannot_be_reset_by_fallback(tmp_path):
    budget = AcquisitionBudget(max_verifications=1, max_downloads=1)
    s = state()
    items = [candidate("1", "video", 100), candidate("2", "video", 99)]
    verifier = Verifier()
    assert (
        len(verify_media_shortlist(items, s["scenes"][0], s, verifier, acquisition_budget=budget))
        == 1
    )
    assert (
        len(verify_media_shortlist(items, s["scenes"][0], s, verifier, acquisition_budget=budget))
        == 1
    )
    assert len(verifier.calls) == 1
    provider = ProviderAdapter("pexels", Provider())
    provider.download(items[0], tmp_path / "one.mp4", budget=budget)
    with pytest.raises(MediaProviderError, match="budget exhausted"):
        provider.download(items[1], tmp_path / "two.mp4", budget=budget)


def test_budget_exhaustion_reaches_existing_visual_director(tmp_path, monkeypatch):
    from test_visual_director import FINGER_SCENES, FakeGenerator, finger_project

    s = finger_project(FINGER_SCENES[:1])
    monkeypatch.setattr(
        media,
        "AcquisitionBudget",
        lambda: AcquisitionBudget(max_search_requests=1, max_verifications=1, max_downloads=1),
    )
    generator = FakeGenerator()
    settings = settings_for(tmp_path)
    settings.openai_api_key = "test"
    prepare_project_media(
        s,
        "project",
        settings,
        client=Provider(),
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
        image_generator=generator,
    )
    search = s["scenes"][0]["media_search"]
    assert search["provider_requests_executed"] == 1
    assert search["acquisition_budget"]["used"]["search_requests"] == 1
    assert s["scenes"][0]["visual_director"]["generation"]
    assert len(generator.prompts) <= 1


def test_dedupe_identity_and_canonical_source_shared_automatic_manual(tmp_path):
    first = candidate("same", "video", 100)
    duplicate = replace(first, source_url="https://www.source.test/same/?utm_source=a#preview")
    other_provider = replace(
        first,
        provider="wikimedia",
        kind="photo",
        provider_id="other",
        source_url="http://source.test/same",
    )
    ledger = CandidateLedger()
    assert ledger.admit(first)
    assert not ledger.admit(duplicate)
    assert not ledger.admit(other_provider)
    assert asset_keys(first) & asset_keys(other_provider)
    provider = FakeProvider()
    provider.videos = [first, duplicate]
    provider.photos = [other_provider]
    _, choices = discover_scene_media_candidates(
        state(),
        PROJECT_ID,
        1,
        1,
        settings_for(tmp_path),
        client=provider,
        fallback_client=FakeWikimedia(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert len(choices) == 1
    s = state()
    result = run_staged_scene_search(
        ["lighthouse"],
        s["scenes"][0],
        s,
        build_visual_query_plan(s["scenes"][0], s),
        pexels=provider,
        wikimedia=FakeWikimedia(),
        preferred_kind="video",
        portrait=True,
        scene_duration=8,
        used=set(),
        verifier=Verifier(default=(0.245, 0.245)),
    )
    assert len(result.candidates) == 1


@pytest.mark.parametrize("limit,expected_calls", [(1, 1), (2, 2)])
def test_pexels_http_retries_spend_shared_request_budget(limit, expected_calls):
    calls = []

    def answer(request):
        calls.append(request)
        return httpx.Response(503, json={})

    provider = ProviderAdapter(
        "pexels",
        PexelsMediaClient("test", client=httpx.Client(transport=httpx.MockTransport(answer))),
    )
    budget = AcquisitionBudget(max_search_requests=limit)
    with pytest.raises(MediaProviderError):
        provider.search("lighthouse", "photo", portrait=True, scene_duration=8, budget=budget)
    assert len(calls) == budget.search_requests == expected_calls


def test_required_attribution_survives_revision_and_reopen(db, tmp_path):
    settings = settings_for(tmp_path)
    s = state()
    project = seed_project(db, PROJECT_ID, s)
    item = replace(
        candidate("attributed", "video", 100),
        rights=commons_rights(
            commons_metadata(), source_url="https://commons.test/file/attributed", creator="Alice"
        ),
    )
    provider = Provider(videos={"lighthouse": [item]})
    _, choices = discover_scene_media_candidates(
        s,
        PROJECT_ID,
        1,
        1,
        settings,
        client=provider,
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    apply_scene_media_candidate(
        db,
        project,
        1,
        choices[0]["token"],
        settings,
        client=provider,
        fallback_client=Commons(),
        auto_render=False,
    )
    db.expire_all()
    saved = serialize_project(get_project(db, PROJECT_ID))["revision"]["state"]
    rights = saved["scenes"][0]["media"]["rights"]
    assert rights["attribution_required"] is True
    assert "Alice" in rights["attribution_text"]
    assert rights == saved["assets"]["license_manifest"][0]["rights"]


def test_cached_reused_asset_cannot_bypass_destination_acceptance(tmp_path):
    s = state()
    s["scenes"][0].update(
        media=cached_asset(tmp_path, title="water condensation droplets"),
        asset_status="real_media_reused",
    )
    prepare_project_media(
        s,
        "project",
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert s["scenes"][0].get("media", {}).get("provider_id") != "borrow"
    assert all(item.get("provider_id") != "borrow" for item in s["assets"]["license_manifest"])


def test_rejected_source_page_cannot_resurface_under_other_provider(tmp_path):
    s = state()
    prior = cached_asset(tmp_path)
    s["scenes"][0].update(
        media=prior,
        rejected_media_identities=[prior["identity"]],
        asset_status="replacement_required",
    )
    duplicate = replace(
        candidate("alias", "photo", 100), provider="wikimedia", source_url=prior["source_url"]
    )
    _, choices = discover_scene_media_candidates(
        s,
        PROJECT_ID,
        1,
        1,
        settings_for(tmp_path),
        client=Provider(),
        fallback_client=Commons(photos={"lighthouse": [duplicate]}),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert choices == []
    assert not destination_asset_allowed(
        candidate_evidence(duplicate), s["scenes"][0], s, reuse=True
    )


def test_locked_unsafe_legacy_asset_is_preserved_for_user_correction_but_not_accepted(tmp_path):
    s = state()
    original = cached_asset(tmp_path, rights=MediaRights())
    s["scenes"][0].update(media=original, user_locked_visual=True)
    provider = Provider()
    prepare_project_media(
        s,
        "project",
        settings_for(tmp_path),
        client=provider,
        fallback_client=Commons(),
        extra_clients=[],
        visual_verifier=UNAVAILABLE,
    )
    assert s["scenes"][0]["media"] == original
    assert s["scenes"][0]["asset_status"] == "real_media_unavailable"
    assert provider.calls == []
    assert s["assets"]["license_manifest"] == []
    assert _scene_media_path(s["scenes"][0], settings_for(tmp_path))[0] is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"License": {"value": "pd"}, "Restrictions": {"value": "Permission required"}},
        {"License": {"value": "pd"}, "Copyrighted": {"value": "true"}},
        commons_metadata() | {"License": {"value": "cc-by-nc-4.0"}},
    ],
)
def test_commons_conflicts_never_become_permission(metadata):
    rights = commons_rights(metadata, source_url="https://commons.test/file", creator="Alice")
    assert evaluate_rights(rights).status == "unknown"


def test_manual_discovery_is_bounded_even_with_many_extra_providers(tmp_path):
    class Extra(Provider):
        provider = "pixabay"

    p = FakeProvider()
    p.videos = []
    p.photos = []
    extras = [Extra()]
    _, choices = discover_scene_media_candidates(
        state(),
        PROJECT_ID,
        1,
        1,
        settings_for(tmp_path),
        client=p,
        fallback_client=FakeWikimedia(),
        extra_clients=extras,
        visual_verifier=UNAVAILABLE,
    )
    assert choices == []
    assert len(extras[0].calls) <= 3
    assert len({query for _kind, query in extras[0].calls}) <= 3


def test_malformed_normalized_result_is_isolated_before_downstream_gates():
    class Malformed(Provider):
        def search_videos(self, *_args, **_kwargs):
            return [replace(candidate("bad", "video", 100), width="bad")]

    adapter = ProviderAdapter("pexels", Malformed())
    with pytest.raises(MediaProviderError) as error:
        adapter.search(
            "lighthouse", "video", portrait=True, scene_duration=8, budget=AcquisitionBudget()
        )
    assert error.value.category == "malformed_response"
