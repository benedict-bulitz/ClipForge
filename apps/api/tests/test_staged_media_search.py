"""Deterministic tests for adaptive (staged) scene media search.

All providers and the OpenCLIP verifier are mocked; no network or model
weights are required.
"""
import copy
import json
from pathlib import Path

import httpx
import pytest

import clipforge.media as media_module
from clipforge.config import Settings
from clipforge.media import (
    COVERAGE_PARTIAL,
    COVERAGE_STRONG,
    COVERAGE_WEAK,
    MAX_SCENE_QUERY_BUDGET,
    MediaCandidate,
    MediaProviderError,
    build_visual_query_plan,
    candidate_target_coverage,
    prepare_project_media,
    run_staged_scene_search,
    scene_coverage_targets,
    select_fallback_query,
    summarize_coverage,
    verify_media_shortlist,
)
from clipforge.visual_verifier import UnavailableVisualVerifier, VisualVerification

STRONG = (0.31, 0.31)
WEAK_PASS = (0.245, 0.245)  # passes the scene gate but has no strong margin


def cand(
    provider_id: str,
    query: str,
    title: str,
    *,
    kind: str = "video",
    provider: str = "pexels",
    source_url: str | None = None,
    duration: float = 12,
) -> MediaCandidate:
    return MediaCandidate(
        provider_id=provider_id,
        kind=kind,
        download_url=f"https://media.test/{provider_id}",
        source_url=source_url or f"https://www.{provider}.test/{kind}/{provider_id}/",
        creator="Unit Tester",
        creator_url=None,
        width=1080,
        height=1920,
        duration=duration if kind == "video" else None,
        query=query,
        rank=100,
        provider=provider,
        title=title,
    )


class Provider:
    """Records every search request; responses are keyed by query."""

    def __init__(self, videos=None, photos=None, errors=()):
        self.videos = videos or {}
        self.photos = photos or {}
        self.errors = set(errors)
        self.calls: list[tuple[str, str]] = []

    def _answer(self, kind, query, table):
        self.calls.append((kind, query))
        if query in self.errors:
            raise MediaProviderError("network_error", "Pexels search timed out.")
        return list(table.get(query, []))

    def search_videos(self, query, *, portrait, scene_duration):
        assert scene_duration > 0
        return self._answer("video", query, self.videos)

    def search_photos(self, query, *, portrait):
        return self._answer("photo", query, self.photos)

    @property
    def queries(self) -> list[str]:
        return list(dict.fromkeys(query for _kind, query in self.calls))

    def download(self, selected, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-media")
        return destination

    def close(self):
        return None


class Commons(Provider):
    def __init__(self, photos=None, *, fail=False):
        super().__init__(photos=photos, errors=())
        self.fail = fail

    def search_photos(self, query, *, portrait):
        if self.fail:
            self.calls.append(("photo", query))
            raise MediaProviderError("wikimedia_unavailable", "Wikimedia timed out.")
        return super().search_photos(query, portrait=portrait)


class Verifier:
    """Mock OpenCLIP: per-provider-id (score, scene_score)."""

    status = "available"

    def __init__(self, scores=None, default=STRONG, raises=False):
        self.scores = scores or {}
        self.default = default
        self.raises = raises
        self.calls: list[str] = []

    def verify_candidate(self, candidate, _texts):
        self.calls.append(candidate.provider_id)
        if self.raises:
            raise RuntimeError("OpenCLIP exploded")
        score, scene_score = self.scores.get(candidate.provider_id, self.default)
        return VisualVerification(score, "verified", subject_score=score, scene_score=scene_score)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        clipforge_ai_mode="local",
        openai_api_key=None,
        brave_search_api_key=None,
        pexels_api_key="unit-test-token",
        render_root=tmp_path,
    )


def project(scene: dict, **extra) -> dict:
    scene = {"id": "s1", "start": 0, "end": 4, "preferred_media": "video", **scene}
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {}, **extra}


def breath_project() -> dict:
    return project(
        {
            "narration": "Your warm breath meets cold air and becomes visible.",
            "visual_goal": "visible breath in winter",
            "visual_intent": {
                "visual_goal": "visible breath in winter",
                "objects": ["breath"],
                "media_queries": ["visible breath winter", "water vapor condensation", "cold air breath"],
            },
        },
        intent={"topic": "Why does breath turn white in winter?"},
        format_plan={"selected_format": "explanation"},
    )


EGYPT_SUDAN_QUERIES = ["egyptian pyramids", "sudanese pyramids", "pyramids aerial"]
COMPARISON = {
    "intent": {"topic": "Sweden vs Indonesia islands", "question": "Sweden or Indonesia: which has more islands?"},
    "format_plan": {"selected_format": "comparison"},
}


def comparison_project(**scene_extra) -> dict:
    scene = {
        "narration": "Sweden has thousands of islands, and Indonesia has thousands too.",
        "visual_goal": "islands of Sweden and Indonesia",
        "visual_intent": {
            "visual_goal": "islands of Sweden and Indonesia",
            "media_queries": ["swedish islands", "indonesian islands", "islands aerial"],
        },
        **scene_extra,
    }
    return project(scene, **copy.deepcopy(COMPARISON))


def run(state, tmp_path, pexels, *, commons=None, verifier=None):
    commons = commons if commons is not None else Commons()
    prepare_project_media(
        state, "project", settings_for(tmp_path), client=pexels, fallback_client=commons,
        visual_verifier=verifier if verifier is not None else Verifier(),
    )
    return state["scenes"][0], commons


# 1 + 9: metadata + OpenCLIP strong on query 1 stops the search after one request.
def test_strong_first_query_stops_further_searches(tmp_path):
    state = breath_project()
    pexels = Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]})

    scene, commons = run(state, tmp_path, pexels)

    search = scene["media_search"]
    assert pexels.calls == [("video", "visible breath winter")]
    assert not commons.calls
    assert search["logical_queries_executed"] == 1
    assert search["executed_query_count"] == 1
    assert search["provider_requests_executed"] == 1
    assert search["planned_query_count"] == 3
    assert search["early_stop"] is True
    assert search["fallback_count"] == 0
    assert search["fallback_reason"] is None
    assert search["final_coverage"] == COVERAGE_STRONG
    assert scene["media"]["provider_id"] == "1"


# 2 + 8: a passing-but-weak OpenCLIP match triggers the targeted fallback.
def test_weak_openclip_match_triggers_fallback(tmp_path):
    state = breath_project()
    pexels = Provider(videos={
        "visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")],
        "cold air breath": [cand("2", "cold air breath", "Person breath visible in cold winter air")],
    })
    verifier = Verifier(scores={"1": WEAK_PASS})

    scene, _ = run(state, tmp_path, pexels, verifier=verifier)

    search = scene["media_search"]
    assert search["executed_query_count"] == 2
    assert search["fallback_count"] == 1
    assert search["fallback_reason"].startswith("partial_coverage")
    assert search["coverage_before_fallback"]["overall"] == COVERAGE_PARTIAL
    assert search["coverage_after_fallback"]["overall"] == COVERAGE_STRONG
    assert search["winning_query"] == "cold air breath"
    assert scene["media"]["provider_id"] == "2"


# 3: a strong second stage prevents the third query.
def test_second_strong_result_prevents_third_query(tmp_path):
    state = breath_project()
    pexels = Provider(videos={
        "cold air breath": [cand("2", "cold air breath", "Visible breath in cold winter air")],
    })

    scene, _ = run(state, tmp_path, pexels)

    assert pexels.queries == ["visible breath winter", "cold air breath"]
    assert scene["media_search"]["fallback_reason"] == "no_results"
    assert scene["media_search"]["stop_reason"] == "strong_coverage"
    assert "water vapor condensation" not in scene["media_search"]["executed_queries"]


# 4: the budget is a hard cap even when more queries are planned.
def test_maximum_three_queries_per_scene():
    state = breath_project()
    scene = state["scenes"][0]
    plan = build_visual_query_plan(scene, state)
    queries = ["breath one", "breath two", "breath three", "breath four", "breath five"]
    pexels = Provider()

    result = run_staged_scene_search(
        queries, scene, state, plan, pexels=pexels, wikimedia=Commons(), preferred_kind="video",
        portrait=True, scene_duration=4, used=set(), verifier=Verifier(), budget=10,
    )

    assert MAX_SCENE_QUERY_BUDGET == 3
    assert len(pexels.queries) == 3
    assert result.provenance["executed_query_count"] == 3
    assert result.provenance["query_budget"] == 3
    assert result.provenance["stop_reason"] == "budget_exhausted"
    assert result.provenance["provider_requests_executed"] == 6  # video + photo per stage


# 5: comparison fallback targets the missing side, skipping generic islands.
def test_comparison_fallback_targets_missing_side(tmp_path):
    state = comparison_project()
    pexels = Provider(videos={
        "swedish islands": [cand("1", "swedish islands", "Swedish archipelago islands from above")],
        "indonesian islands": [cand("2", "indonesian islands", "Indonesian tropical islands from above")],
    })

    scene, _ = run(state, tmp_path, pexels)

    search = scene["media_search"]
    assert search["coverage_mode"] == "comparison"
    assert search["coverage_before_fallback"]["targets"] == {
        "swedish": COVERAGE_STRONG, "indonesian": COVERAGE_WEAK, "island": COVERAGE_STRONG,
    }
    assert search["fallback_reason"] == "weak_coverage:indonesian"
    assert pexels.queries == ["swedish islands", "indonesian islands"]
    assert "islands aerial" not in pexels.queries
    assert search["coverage_after_fallback"]["overall"] == COVERAGE_STRONG


# 6: an already strong side is never searched again.
def test_strong_comparison_side_is_not_searched_repeatedly(tmp_path):
    state = comparison_project(visual_intent={
        "visual_goal": "islands of Sweden and Indonesia",
        "media_queries": ["swedish islands", "sweden archipelago", "indonesian islands"],
    })
    assert state["scenes"][0]["visual_intent"]["media_queries"] == build_visual_query_plan(state["scenes"][0], state)["queries"]
    pexels = Provider(videos={
        "swedish islands": [cand("1", "swedish islands", "Swedish archipelago islands from above")],
    })

    scene, _ = run(state, tmp_path, pexels)

    assert "sweden archipelago" not in pexels.queries
    assert pexels.queries == ["swedish islands", "indonesian islands"]
    assert scene["media_search"]["stop_reason"] == "budget_exhausted" or scene["media_search"]["stop_reason"] == "no_targeted_query"


def test_single_side_scene_does_not_require_other_side():
    state = comparison_project(
        narration="Sweden alone has more than two hundred thousand islands.",
        visual_goal="swedish islands",
        visual_intent={"visual_goal": "swedish islands", "media_queries": ["swedish islands", "indonesian islands", "islands aerial"]},
    )
    scene = state["scenes"][0]

    targets = scene_coverage_targets(scene, state, build_visual_query_plan(scene, state))["targets"]

    assert targets == {"swedish": "subject_a", "island": "shared"}


# 7: a generic topic-context match is never strong primary coverage.
def test_generic_context_result_is_not_strong_primary_coverage():
    state = comparison_project()
    scene = state["scenes"][0]
    context = cand("9", "swedish islands", "Sweden and Indonesia flags")
    rows = verify_media_shortlist([context], scene, state, Verifier())

    assert rows and rows[0][1]["confidence"] in {"high", "acceptable"}
    assert candidate_target_coverage(context, rows[0][1], "island", 4) == COVERAGE_PARTIAL
    targets = {"sweden": "subject_a", "indonesia": "subject_b", "island": "shared"}
    assert summarize_coverage(rows, targets, 4)["overall"] != COVERAGE_STRONG


def test_high_openclip_alone_is_not_strong_coverage():
    state = breath_project()
    scene = state["scenes"][0]
    unrelated = cand("3", "visible breath winter", "Cold air over a frozen lake")
    rows = verify_media_shortlist([unrelated], scene, state, Verifier(default=(0.4, 0.4)))

    assert rows
    assert candidate_target_coverage(unrelated, rows[0][1], "breath", 4) != COVERAGE_STRONG


# 10: the same provider asset found by two queries is kept once.
def test_duplicate_asset_across_queries_is_deduplicated():
    state = breath_project()
    scene = state["scenes"][0]
    shared = cand("5", "visible breath winter", "Winter morning outdoors")
    mirror = cand("6", "cold air breath", "Winter morning mirror", source_url=shared.source_url + "/")
    pexels = Provider(videos={
        "visible breath winter": [shared],
        "water vapor condensation": [shared],
        "cold air breath": [shared, mirror],
    })

    result = run_staged_scene_search(
        ["visible breath winter", "water vapor condensation", "cold air breath"], scene, state,
        build_visual_query_plan(scene, state), pexels=pexels, wikimedia=Commons(),
        preferred_kind="video", portrait=True, scene_duration=4, used=set(), verifier=Verifier(),
    )

    identities = [item.identity for item in result.candidates]
    assert identities.count(shared.identity) == 1
    assert mirror.identity not in identities
    assert result.provenance["duplicate_count"] >= 2
    assert len({row[0].identity for row in result.ranked}) == len(result.ranked)


# 11: a provider timeout moves safely to the next query.
def test_provider_timeout_moves_to_fallback(tmp_path):
    state = breath_project()
    pexels = Provider(
        videos={"cold air breath": [cand("2", "cold air breath", "Visible breath in cold winter air")]},
        errors={"visible breath winter"},
    )

    scene, _ = run(state, tmp_path, pexels)

    search = scene["media_search"]
    assert search["fallback_reason"] == "provider_error"
    assert search["stages"][0]["errors"] == ["network_error", "network_error"]
    assert scene["asset_status"] == "video_ready"
    assert scene["media"]["provider_id"] == "2"


# 12: every search failing is nonfatal and keeps the existing safe fallback.
def test_all_searches_failing_is_nonfatal(tmp_path):
    state = breath_project()
    pexels = Provider(errors={"visible breath winter", "water vapor condensation", "cold air breath", "visible breath", "breath turn white", "nature landscape", "ocean water", "trees outdoors"})

    scene, commons = run(state, tmp_path, pexels, commons=Commons(fail=True))

    assert scene["asset_status"] == "real_media_unavailable"
    assert scene["fallback_reason"]
    assert scene["media_search"]["executed_query_count"] <= 3
    assert scene["media_search"]["winning_asset"] is None
    assert state["assets"]["status"] == "fallback_only"
    assert commons.calls


def test_all_weak_searches_use_existing_relaxed_fallback(tmp_path):
    state = breath_project()
    # Real media that fails the strict relevance gate is still usable by the
    # relaxed fallback, which works from the already searched pool.
    pexels = Provider(photos={"visible breath winter": [cand("p1", "visible breath winter", "Frosty meadow", kind="photo")]})

    scene, _ = run(state, tmp_path, pexels)

    assert scene["media_search"]["logical_queries_executed"] == 3
    assert scene["media_search"]["relaxed_fallback"] is True
    assert "relaxed_queries" not in scene["media_search"]
    assert scene["media"]["provider_id"] == "p1"
    assert scene["media"]["relevance"]["fallback_stage"] == "real_media_only_relaxed_fit"


# 13: OpenCLIP unavailable or raising stays nonfatal and conservative.
def test_openclip_unavailable_uses_metadata_and_provenance(tmp_path):
    state = breath_project()
    pexels = Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]})

    scene, _ = run(state, tmp_path, pexels, verifier=UnavailableVisualVerifier())

    assert scene["asset_status"] == "video_ready"
    assert scene["media_search"]["executed_query_count"] == 1
    assert scene["media_search"]["final_coverage"] == COVERAGE_STRONG


def test_openclip_unavailable_without_provenance_stays_conservative(tmp_path):
    state = breath_project()
    untargeted = cand("1", "winter scenes", "Visible breath in cold winter air")
    pexels = Provider(videos={"visible breath winter": [untargeted]})

    scene, _ = run(state, tmp_path, pexels, verifier=UnavailableVisualVerifier())

    assert scene["media_search"]["executed_query_count"] > 1
    assert scene["asset_status"] == "video_ready"


def test_verification_exception_falls_back_to_metadata(tmp_path):
    state = breath_project()
    pexels = Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]})

    scene, _ = run(state, tmp_path, pexels, verifier=Verifier(raises=True))

    assert scene["asset_status"] == "video_ready"
    assert scene["media_search"]["visual_verification"] == "failed_metadata_fallback"


# 14: protected payoff subjects are never searched or required.
def test_protected_payoff_subject_is_never_searched(tmp_path):
    state = project(
        {"narration": "Most people guess Egypt.", "visual_intent": {"media_queries": EGYPT_SUDAN_QUERIES, "must_not_show": ["Sudan"]}},
        intent={"topic": "Egypt or Sudan pyramids", "question": "Which has more pyramids, Egypt or Sudan?"},
        payoff_plan={"hook_must_not_reveal": "Sudan"},
        format_plan={"selected_format": "comparison"},
    )
    pexels = Provider()

    scene, commons = run(state, tmp_path, pexels)

    searched = " ".join(pexels.queries + commons.queries).casefold()
    assert "sudan" not in searched
    assert "sudan" not in scene["media_search"]["coverage_targets"]
    assert scene["media_search"]["executed_query_count"] <= 3


# 15: ranking scenes search the ranked item, not the generic category.
def test_ranking_fallback_targets_the_ranked_item(tmp_path):
    state = project(
        {
            "narration": "The cheetah is the fastest land animal.",
            "visual_goal": "cheetah running",
            "visual_intent": {
                "visual_goal": "cheetah running",
                "media_queries": ["cheetah running", "cheetah sprinting", "fast animal running"],
            },
        },
        intent={"topic": "Fastest animals", "question": "Top fastest animals"},
        format_plan={"selected_format": "ranking"},
    )
    pexels = Provider(videos={
        "cheetah running": [cand("1", "cheetah running", "Animals running in the savanna")],
        "cheetah sprinting": [cand("2", "cheetah sprinting", "Cheetah sprinting across the savanna")],
    })

    scene, _ = run(state, tmp_path, pexels)

    assert scene["media_search"]["coverage_targets"] == {"cheetah": "primary"}
    assert pexels.queries == ["cheetah running", "cheetah sprinting"]
    assert "fast animal running" not in pexels.queries
    assert scene["media"]["provider_id"] == "2"


# 16: explanation searches stay concrete and phenomenon-first.
def test_explanation_fallback_stays_concrete(tmp_path):
    state = breath_project()
    pexels = Provider()

    scene, _ = run(state, tmp_path, pexels)

    executed = scene["media_search"]["executed_queries"]
    assert executed[0] == "visible breath winter"
    assert all(len(query.split()) <= 6 for query in executed)
    assert not {"why", "because", "reason", "warum"} & set(" ".join(executed).split())


def test_select_fallback_query_prefers_missing_subject():
    targets = {"sweden": "subject_a", "indonesia": "subject_b", "island": "shared"}
    coverage = {"overall": COVERAGE_WEAK, "targets": {"sweden": COVERAGE_STRONG, "indonesia": COVERAGE_WEAK, "island": COVERAGE_STRONG}}

    assert select_fallback_query(["islands aerial", "indonesian islands"], coverage, targets) == "indonesian islands"
    assert select_fallback_query(["islands aerial", "sweden archipelago"], coverage, targets) is None
    missing_side = {"overall": "none", "targets": {"sweden": COVERAGE_STRONG, "indonesia": "none", "island": COVERAGE_STRONG}}
    assert select_fallback_query(["islands aerial"], missing_side, targets) is None
    nothing = {"overall": "none", "targets": dict.fromkeys(targets, "none")}
    assert select_fallback_query(["islands aerial"], nothing, targets) == "islands aerial"


def test_staged_search_without_pexels_key_uses_wikimedia_stages():
    state = breath_project()
    scene = state["scenes"][0]
    commons = Commons(photos={"visible breath winter": [cand("w1", "visible breath winter", "Visible breath in cold winter air", kind="photo", provider="wikimedia")]})

    result = run_staged_scene_search(
        build_visual_query_plan(scene, state)["queries"], scene, state, build_visual_query_plan(scene, state),
        pexels=None, wikimedia=commons, preferred_kind="video", portrait=True, scene_duration=4,
        used=set(), verifier=Verifier(),
    )

    assert commons.queries == ["visible breath winter"]
    assert result.provenance["early_stop"] is True
    assert result.ranked[0][0].provider == "wikimedia"


# 17: compact query provenance persists with the project state.
def test_query_provenance_persists_compactly(tmp_path):
    state = breath_project()
    pexels = Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]})

    run(state, tmp_path, pexels)
    restored = json.loads(json.dumps(state))

    search = restored["scenes"][0]["media_search"]
    for key in (
        "planned_queries", "executed_queries", "planned_query_count", "executed_query_count",
        "early_stop", "fallback_count", "fallback_reason", "coverage_before_fallback",
        "coverage_after_fallback", "winning_query", "winning_asset",
    ):
        assert key in search
    assert search["winning_asset"] == {
        "identity": "pexels:video:1", "provider": "pexels", "provider_id": "1", "kind": "video",
        "source_url": "https://www.pexels.test/video/1/",
    }
    assert len(json.dumps(search)) < 2500
    assert restored["assets"]["media_search_summary"] == {
        "scenes_searched": 1, "planned_query_count": 3, "logical_queries_executed": 1,
        "provider_requests_executed": 1, "early_stop_count": 1, "fallback_count": 0,
    }


# 18: old project data without search provenance remains compatible.
def test_old_project_data_remains_compatible(tmp_path):
    state = breath_project()
    cached = tmp_path / "project" / "assets" / "pexels" / "video-old.mp4"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old")
    state["scenes"][0]["media"] = {
        "identity": "pexels:video:old", "provider": "pexels", "provider_id": "old", "kind": "video",
        "cache_path": "project/assets/pexels/video-old.mp4", "query": "breath",
    }
    legacy = copy.deepcopy(breath_project())
    legacy["scenes"][0]["search_queries"] = ["visible breath winter"]
    pexels = Provider()

    scene, _ = run(state, tmp_path, pexels)
    legacy_scene, _ = run(legacy, tmp_path, Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]}))

    assert pexels.calls == []
    assert "media_search" not in scene
    assert scene["media"]["identity"] == "pexels:video:old"
    assert "media_search_summary" not in state["assets"]
    assert legacy_scene["asset_status"] == "video_ready"


# 19 + 20: staged search adds no AI call and no provider beyond Pexels/Wikimedia.
def test_no_new_ai_call_or_provider(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected external call")

    monkeypatch.setattr("clipforge.ai.OpenAI", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    state = comparison_project()
    pexels = Provider(videos={"swedish islands": [cand("1", "swedish islands", "Swedish archipelago islands from above")]})

    scene, _ = run(state, tmp_path, pexels)

    assert "openai" not in media_module.__dict__
    assert {call[0] for call in pexels.calls} <= {"video", "photo"}
    assert scene["media"]["provider"] in {"pexels", "wikimedia"}
    assert scene["media_search"]["executed_query_count"] <= 3


@pytest.mark.parametrize("preferred", ["video", "photo"])
def test_preferred_kind_is_searched_first(tmp_path, preferred):
    state = breath_project()
    state["scenes"][0]["preferred_media"] = preferred
    pexels = Provider()

    run(state, tmp_path, pexels)

    assert pexels.calls[0][0] == preferred


# ---------------------------------------------------------------------------
# Pre-merge hardening: logical query budget across the whole acquisition.
# ---------------------------------------------------------------------------


def all_search_strings(*providers) -> set[str]:
    return {query for provider in providers for _kind, query in provider.calls}


def pyramids_project(narration: str = "Most people guess Egypt.") -> dict:
    return project(
        {"narration": narration, "visual_intent": {"media_queries": EGYPT_SUDAN_QUERIES, "must_not_show": ["Sudan"]}},
        intent={"topic": "Egypt or Sudan pyramids", "question": "Which has more pyramids, Egypt or Sudan?"},
        payoff_plan={"hook_must_not_reveal": "Sudan"},
        format_plan={"selected_format": "comparison"},
    )


# 1 + 3 + 7 + 8: a failing scene never sends a fourth logical query string.
def test_full_acquisition_never_exceeds_three_logical_queries(tmp_path):
    state = breath_project()
    pexels = Provider()
    commons = Commons()

    scene, _ = run(state, tmp_path, pexels, commons=commons)

    search = scene["media_search"]
    strings = all_search_strings(pexels, commons)
    assert strings == set(search["executed_queries"])
    assert len(strings) == 3
    assert search["logical_queries_executed"] == 3
    assert search["stop_reason"] == "budget_exhausted"
    assert search["relaxed_fallback"] is True
    assert "relaxed_queries" not in search
    assert not strings & {"nature landscape", "ocean water", "trees outdoors", "visible breath"}
    assert scene["asset_status"] == "real_media_unavailable"


# 2 + 4: Wikimedia may retry executed strings; provider requests counted separately.
def test_wikimedia_fallback_reuses_executed_strings_and_counts_requests(tmp_path):
    state = breath_project()
    pexels = Provider()
    commons = Commons()

    scene, _ = run(state, tmp_path, pexels, commons=commons)

    search = scene["media_search"]
    assert commons.queries == search["executed_queries"]
    assert search["provider_requests_by_source"] == {"staged_search": 6, "wikimedia_fallback": 3}
    assert search["provider_requests_executed"] == 9
    assert search["provider_requests_executed"] == len(pexels.calls) + len(commons.calls)
    assert search["logical_queries_executed"] == 3
    summary = state["assets"]["media_search_summary"]
    assert summary["logical_queries_executed"] == 3
    assert summary["provider_requests_executed"] == 9


# 3: relaxed fallback may only spend budget left unused, never query #4.
def test_relaxed_fallback_only_uses_remaining_logical_budget(tmp_path):
    state = pyramids_project()
    pexels = Provider()
    commons = Commons()

    scene, _ = run(state, tmp_path, pexels, commons=commons)

    search = scene["media_search"]
    strings = all_search_strings(pexels, commons)
    assert search["planned_query_count"] == 2
    assert len(search["relaxed_queries"]) == 1
    assert len(strings) == 3 == search["logical_queries_executed"]
    assert strings == set(search["executed_queries"])
    assert search["provider_requests_by_source"]["relaxed_fallback"] == 2  # pexels + wikimedia


def test_claim_logical_query_refuses_query_four():
    provenance = {"executed_queries": ["a", "b", "c"], "query_budget": 3}

    assert media_module._claim_logical_query(provenance, "b") is True
    assert media_module._claim_logical_query(provenance, "d") is False
    assert provenance["executed_queries"] == ["a", "b", "c"]


# 5 + 6 (hardening view): logical/provider counts for early stop and fallback.
def test_early_stop_and_fallback_report_logical_and_provider_counts(tmp_path):
    strong = breath_project()
    run(strong, tmp_path, Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]}))
    weak = breath_project()
    run(weak, tmp_path, Provider(videos={
        "visible breath winter": [cand("1", "visible breath winter", "Cold air over a frozen lake")],
        "cold air breath": [cand("2", "cold air breath", "Visible breath in cold winter air")],
    }))

    first, second = strong["scenes"][0]["media_search"], weak["scenes"][0]["media_search"]
    assert (first["logical_queries_executed"], first["provider_requests_executed"]) == (1, 1)
    assert first["early_stop"] is True
    assert (second["logical_queries_executed"], second["provider_requests_executed"]) == (2, 3)
    assert second["executed_queries"] == ["visible breath winter", "cold air breath"]


# 9 + 10 + comparison subjects: the protected reveal never reaches a provider.
def test_protected_payoff_never_leaks_through_any_query_path(tmp_path):
    state = pyramids_project()
    pexels = Provider(errors={"egyptian pyramids"})
    commons = Commons()

    scene, _ = run(state, tmp_path, pexels, commons=commons)

    search = scene["media_search"]
    searched = " ".join(all_search_strings(pexels, commons)).casefold()
    assert search["fallback_reason"] == "provider_error"  # targeted fallback ran
    assert search["wikimedia_fallback"] is True
    assert search["relaxed_fallback"] is True
    assert commons.calls and search["relaxed_queries"]
    assert "sudan" not in searched
    assert "sudan" not in json.dumps(search).casefold()
    assert search["coverage_targets"] == {"egyptian": "subject_a", "pyramid": "shared"}


def test_protected_payoff_does_not_leak_on_any_pre_reveal_scene(tmp_path):
    state = pyramids_project()
    state["scenes"].append({
        "id": "s2", "start": 4, "end": 8, "preferred_media": "video",
        "narration": "Egypt is famous for its pyramids.", "visual_goal": "egyptian pyramids",
    })
    pexels = Provider()
    commons = Commons()

    prepare_project_media(
        state, "project", settings_for(tmp_path), client=pexels, fallback_client=commons, visual_verifier=Verifier(),
    )

    assert "sudan" not in " ".join(all_search_strings(pexels, commons)).casefold()
    for scene in state["scenes"]:
        assert scene["media_search"]["logical_queries_executed"] <= 3


# 11: deduplication across providers and generic provider URLs.
def test_deduplication_across_providers_and_generic_source_urls():
    state = breath_project()
    scene = state["scenes"][0]
    video = cand("7", "visible breath winter", "Visible breath in cold winter air")
    photo_same_page = cand("8", "visible breath winter", "Visible breath", kind="photo", source_url="https://pexels.test/video/7")
    generic_a = cand("9", "visible breath winter", "Breath one", source_url="https://www.pexels.com/videos/")
    generic_b = cand("10", "visible breath winter", "Breath two", source_url="https://www.pexels.com/videos/")
    pexels = Provider(
        videos={"visible breath winter": [cand("7", "visible breath winter", "Cold air over a frozen lake", source_url="https://www.pexels.test/video/7/"), generic_a, generic_b]},
        photos={"visible breath winter": [video, photo_same_page]},
    )

    result = run_staged_scene_search(
        ["visible breath winter"], scene, state, build_visual_query_plan(scene, state), pexels=pexels,
        wikimedia=Commons(), preferred_kind="video", portrait=True, scene_duration=4, used=set(),
        verifier=Verifier(scores={"7": WEAK_PASS, "9": WEAK_PASS, "10": WEAK_PASS}),
    )

    identities = [item.identity for item in result.candidates]
    assert len(identities) == len(set(identities))
    assert "pexels:photo:8" not in identities  # same canonical page as video 7
    assert {"pexels:video:9", "pexels:video:10"} <= set(identities)  # generic URL is not identity
    assert result.provenance["duplicate_count"] == 2


def test_wikimedia_fallback_does_not_reverify_or_duplicate_staged_assets(tmp_path):
    state = breath_project()
    shared = cand("w5", "visible breath winter", "Visible breath in cold winter air", kind="photo", provider="wikimedia")

    class BrokenDownload(Provider):
        def download(self, selected, destination):
            raise MediaProviderError("network_error", "download failed")

    pexels = BrokenDownload(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]})
    commons = Commons(photos={"visible breath winter": [shared, shared]})
    verifier = Verifier()

    scene, _ = run(state, tmp_path, pexels, commons=commons, verifier=verifier)

    assert scene["media"]["identity"] == shared.identity
    assert verifier.calls.count("w5") == 1
    assert verifier.calls.count("1") == 1  # relaxed fallback never re-verifies


# 12: an OpenCLIP failure is latched: nonfatal and not retried per stage.
def test_openclip_failure_is_nonfatal_and_not_retried(tmp_path):
    state = breath_project()
    pexels = Provider()
    commons = Commons(photos={"visible breath winter": [cand("w1", "visible breath winter", "Visible breath in cold winter air", kind="photo", provider="wikimedia")]})
    verifier = Verifier(raises=True)
    pexels.videos = {"visible breath winter": [cand("1", "visible breath winter", "Snow")], "cold air breath": [cand("2", "cold air breath", "Snow")]}

    scene, _ = run(state, tmp_path, pexels, commons=commons, verifier=verifier)

    assert scene["asset_status"] in {"photo_ready", "video_ready"}
    assert scene["media_search"]["visual_verification"] == "failed_metadata_fallback"
    assert len(verifier.calls) == 1


# 13: a project persisted before staged search still loads and re-runs.
def test_pre_staged_search_project_state_is_compatible(tmp_path):
    state = breath_project()
    scene = state["scenes"][0]
    scene["search_queries"] = ["visible breath winter"]
    scene["visual_query_plan"] = {"primary_subjects": ["breath"]}
    scene["asset_status"] = "replacement_required"
    state["assets"] = {"license_manifest": [], "status": "media_ready", "provider": "pexels"}
    restored = json.loads(json.dumps(state))

    scene, _ = run(restored, tmp_path, Provider(videos={"visible breath winter": [cand("1", "visible breath winter", "Visible breath in cold winter air")]}))

    assert scene["asset_status"] == "video_ready"
    assert scene["media_search"]["logical_queries_executed"] == 1
    assert restored["assets"]["status"] == "media_ready"


# --- German projects, last-resort visual safety, scene-aware query order ---

GERMAN_COMPARISON = {
    "intent": {
        "topic": "Welches Land hat mehr Inseln – Schweden oder Indonesien?",
        "question": "Welches Land hat mehr Inseln – Schweden oder Indonesien?",
    },
    "format_plan": {"selected_format": "comparison"},
}
POOR = (0.12, 0.12)  # verified, but far below the scene gate


ISLAND_QUERIES = ["swedish islands", "indonesian islands", "islands aerial"]
# The canonical, provider-facing visual intent the AI planner emits per scene
# (English, independent of the German narration).
SCENE_INTENTS = {
    "Schweden hat besonders viele Inseln.": {"visual_goal": "Swedish archipelago islands from above", "objects": ["islands"], "media_queries": ISLAND_QUERIES},
    "Indonesien hat rund 17.000 Inseln.": {"visual_goal": "Indonesian islands from above", "objects": ["islands"], "media_queries": ISLAND_QUERIES},
    "Your warm breath meets cold air.": {"visual_goal": "visible breath in winter", "objects": ["breath"], "media_queries": ["visible breath winter"]},
}


def german_scene(narration: str) -> dict:
    return {"narration": narration, "visual_intent": copy.deepcopy(SCENE_INTENTS[narration])}


def german_project(*narrations: str) -> dict:
    state = project(german_scene(narrations[0]), **copy.deepcopy(GERMAN_COMPARISON))
    for index, narration in enumerate(narrations[1:], 2):
        state["scenes"].append(
            {"id": f"s{index}", "start": 4 * index, "end": 4 * index + 4, "preferred_media": "video", **german_scene(narration)}
        )
    return state


def island_provider() -> Provider:
    return Provider(videos={
        "swedish islands": [
            cand("city", "swedish islands", "Stockholm city street traffic"),
            cand("se", "swedish islands", "Sweden archipelago islands aerial"),
        ],
        "indonesian islands": [
            cand("road", "indonesian islands", "Tropical road with scooters in Bali Indonesia"),
            cand("id", "indonesian islands", "Indonesia islands aerial drone shot"),
        ],
    })


def test_german_comparison_matches_english_metadata_and_verifies_it(tmp_path):
    state = german_project("Schweden hat besonders viele Inseln.", "Indonesien hat rund 17.000 Inseln.")
    verifier = Verifier({"city": POOR, "road": POOR})

    prepare_project_media(
        state, "project", settings_for(tmp_path), client=island_provider(), fallback_client=Commons(),
        visual_verifier=verifier,
    )

    sweden, indonesia = state["scenes"]
    # The topic-specific assets survive metadata relevance and reach OpenCLIP.
    assert {"se", "id"} <= set(verifier.calls)
    assert sweden["media"]["provider_id"] == "se"
    assert indonesia["media"]["provider_id"] == "id"
    for scene in (sweden, indonesia):
        assert scene["media_search"]["winning_source"] == "staged_search"
        assert scene["media_search"]["final_coverage"] == COVERAGE_STRONG
        assert "relaxed_fallback" not in scene["media_search"]
    assert sweden["media"]["relevance"]["confidence"] == "high"


def test_last_resort_does_not_pick_openclip_rejected_first_candidate(tmp_path):
    state = breath_project()
    # Neither title matches the scene, so only the relaxed fallback can choose.
    pexels = Provider(videos={"visible breath winter": [
        cand("road", "visible breath winter", "Tropical road at noon"),
        cand("good", "visible breath winter", "Untitled clip 42"),
    ]})
    verifier = Verifier({"road": POOR, "good": STRONG})

    scene, _ = run(state, tmp_path, pexels, verifier=verifier)

    assert scene["media_search"]["relaxed_fallback"] is True
    assert scene["media"]["provider_id"] == "good"
    assert "quality_degraded" not in scene["media_search"]


def test_last_resort_reuses_staged_openclip_rejection(tmp_path):
    state = breath_project()
    # "rejected" passes metadata, so staged search verifies (and rejects) it;
    # the last resort must honour that result without calling OpenCLIP again.
    pexels = Provider(videos={"visible breath winter": [
        cand("rejected", "visible breath winter", "Visible breath in cold winter air"),
        cand("good", "visible breath winter", "Untitled clip 42"),
    ]})
    verifier = Verifier({"rejected": POOR, "good": STRONG})

    scene, _ = run(state, tmp_path, pexels, verifier=verifier)

    assert scene["media"]["provider_id"] == "good"
    assert verifier.calls.count("rejected") == 1


def test_all_visually_poor_candidates_stay_nonfatal_and_flag_degraded(tmp_path):
    state = breath_project()
    pexels = Provider(videos={"visible breath winter": [
        cand("worse", "visible breath winter", "Tropical road at noon"),
        cand("less-bad", "visible breath winter", "City street at night"),
    ]})
    verifier = Verifier({"worse": (0.05, 0.05), "less-bad": POOR})

    scene, _ = run(state, tmp_path, pexels, verifier=verifier)

    assert scene["asset_status"] == "video_ready"
    assert scene["media"]["provider_id"] == "less-bad"  # best score, not provider order
    assert scene["media"]["relevance"]["fallback_stage"] == "visually_rejected_last_resort"
    assert scene["media_search"]["quality_degraded"] is True
    assert scene["media_search"]["winning_source"] == "degraded_fallback"
    assert scene["visual_quality"] == "degraded"
    assert state["assets"]["status"] == "media_ready"


def test_all_poor_scene_prefers_reusing_verified_project_media(tmp_path):
    state = german_project("Schweden hat besonders viele Inseln.", "Your warm breath meets cold air.")
    pexels = Provider(videos={
        "swedish islands": [cand("se", "swedish islands", "Sweden archipelago islands aerial")],
        "visible breath winter": [cand("road", "visible breath winter", "Tropical road at noon")],
    })
    verifier = Verifier({"road": POOR})

    prepare_project_media(
        state, "project", settings_for(tmp_path), client=pexels, fallback_client=Commons(),
        visual_verifier=verifier,
    )

    second = state["scenes"][1]
    assert second["asset_status"] == "related_media_reused"
    assert second["media"]["provider_id"] == "se"
    assert "quality_degraded" not in second["media_search"]


@pytest.mark.parametrize(
    ("narration", "expected_first", "other_side"),
    [
        ("Indonesien hat rund 17.000 Inseln.", "indonesian islands", "swedish islands"),
        ("Schweden hat besonders viele Inseln.", "swedish islands", "indonesian islands"),
    ],
)
def test_single_side_scene_searches_its_own_side_first(tmp_path, narration, expected_first, other_side):
    state = german_project(narration)
    pexels = island_provider()

    scene, _ = run(state, tmp_path, pexels, verifier=Verifier({"city": POOR, "road": POOR}))

    search = scene["media_search"]
    assert search["executed_queries"][0] == expected_first
    assert pexels.calls[0] == ("video", expected_first)
    assert search["early_stop"] is True
    assert other_side not in pexels.queries
    assert search["logical_queries_executed"] <= MAX_SCENE_QUERY_BUDGET


def test_scene_query_order_keeps_protected_payoff_out():
    state = copy.deepcopy(GERMAN_COMPARISON) | {"payoff_plan": {"hook_must_not_reveal": "Indonesien"}}
    scene = german_scene("Schweden hat besonders viele Inseln.")
    plan = build_visual_query_plan(scene, state)

    assert all("indonesia" not in query for query in plan["queries"])
    result = run_staged_scene_search(
        plan["queries"], scene, state, plan, pexels=island_provider(), wikimedia=Commons(),
        preferred_kind="video", portrait=True, scene_duration=4, used=set(), verifier=Verifier(),
    )
    assert all("indonesia" not in query for query in result.provenance["executed_queries"])
    assert result.provenance["executed_queries"][0] == "swedish islands"
