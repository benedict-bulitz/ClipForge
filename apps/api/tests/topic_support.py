"""Fakes for Topic Intelligence tests: Wikimedia HTTP, YouTube Data API, OpenAI."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from clipforge.config import Settings
from clipforge.topic_intelligence.service import DiscoveryDeps
from clipforge.topic_intelligence.sources import (
    BraveNewsSource,
    SourceFailed,
    WikipediaPageviewsSource,
    YouTubeCompetitionProbe,
    YouTubeTrendingSource,
)

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


def settings(**overrides: Any) -> Settings:
    values = {
        "clipforge_ai_mode": "local",
        "openai_api_key": None,
        "brave_search_api_key": None,
        "topic_pool_ttl_minutes": 45,
        "topic_youtube_quota_budget": 400,
        "topic_youtube_search_probes": 2,
        "topic_score_weights": None,
    }
    values.update(overrides)
    return Settings(**values)


def spike(base: int, recent: int, days: int = 30) -> list[int]:
    return [base] * (days - 2) + [recent, recent]


DEFAULT_ARTICLES: list[dict[str, Any]] = [
    {"title": "Hauptseite", "views": 900_000},
    {"title": "Polarlicht", "views": 60_000, "description": "Leuchterscheinung am Nachthimmel",
     "extract": "Ein Polarlicht ist eine Leuchterscheinung durch angeregte Stickstoff- und Sauerstoffatome.",
     "history": spike(2_000, 24_000)},
    {"title": "Schlafträgheit", "views": 30_000, "description": "Zustand verminderter Leistungsfähigkeit nach dem Aufwachen",
     "extract": "Schlafträgheit bezeichnet die Benommenheit und Müdigkeit direkt nach dem Aufwachen, etwa nach einem Mittagsschlaf.",
     "history": spike(400, 5_000)},
    {"title": "Max Mustermann", "views": 50_000, "description": "deutscher Schauspieler",
     "extract": "Max Mustermann (* 3. Mai 1980 in Köln) ist ein deutscher Schauspieler.", "history": spike(100, 50_000)},
    {"title": "Regenbogen", "views": 25_000, "description": "Optisches Phänomen",
     "extract": "Der Regenbogen ist eine optische Erscheinung aus Licht und Wassertropfen.", "history": spike(900, 4_000)},
    {"title": "Muskelkater", "views": 22_000, "description": "Muskelschmerz nach ungewohnter Belastung",
     "extract": "Muskelkater bezeichnet Muskelschmerzen nach ungewohnter Anstrengung.", "history": spike(700, 3_000)},
    {"title": "Deutschland", "views": 20_000, "description": "Staat in Mitteleuropa",
     "extract": "Deutschland ist ein Bundesstaat in Mitteleuropa.", "history": spike(20_000, 20_000)},
]


class FakeWiki:
    """Wikimedia REST + Action API stand-in; records every URL it serves."""

    def __init__(self, articles: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.articles = articles if articles is not None else DEFAULT_ARTICLES
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise SourceFailed("HTTP 503")
        if "/top/" in url:
            return {"items": [{"articles": [
                {"article": item["title"].replace(" ", "_"), "views": item["views"], "rank": index + 1}
                for index, item in enumerate(self.articles)
            ]}]}
        if "api.php" in url:
            titles = str(params["titles"]).split("|")
            by_title = {item["title"]: item for item in self.articles}
            return {"query": {"pages": [
                {"title": title, "description": by_title.get(title, {}).get("description", ""),
                 "extract": by_title.get(title, {}).get("extract", "")}
                for title in titles
            ]}}
        if "/per-article/" in url:
            title = unquote(url.split("/user/")[1].split("/daily/")[0]).replace("_", " ")
            history = next((item.get("history") or [] for item in self.articles if item["title"] == title), [])
            return {"items": [{"views": value} for value in history]}
        raise AssertionError(f"unexpected URL {url}")


def video(video_id: str, title: str, channel: str, views: int, *, age_days: float = 5, language: str = "de") -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": "",
            "channelId": channel,
            "channelTitle": f"Kanal {channel}",
            "publishedAt": (NOW - timedelta(days=age_days)).isoformat(),
            "defaultAudioLanguage": language,
        },
        "statistics": {"viewCount": str(views)},
    }


@dataclass
class FakeYouTube:
    """YouTube Data API stand-in with a quota ledger (search.list = 100 units)."""

    popular: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    recent: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # channel -> its recent uploads
    channel_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)
    fail_popular: bool = False
    missing_categories: set[str] = field(default_factory=set)  # charts YouTube does not offer in the region

    @property
    def units(self) -> int:
        return sum(units for _name, units, _params in self.calls)

    def count(self, name: str) -> int:
        return sum(1 for call, _units, _params in self.calls if call == name)

    def list_popular_videos(self, access_token: str, region_code: str, category_id: str | None, max_results: int):
        self.calls.append(("videos.list(chart)", 1, {"regionCode": region_code, "category": category_id}))
        from clipforge.youtube.provider import YouTubeApiError

        if self.fail_popular:
            raise YouTubeApiError("quota_exceeded", "quota")
        if str(category_id) in self.missing_categories:
            raise YouTubeApiError("not_found", "chart not found")
        return list(self.popular.get(str(category_id), []))

    def list_channels(self, access_token: str, channel_ids: list[str], parts: str):
        self.calls.append(("channels.list", 1, {"ids": list(channel_ids), "parts": parts}))
        return [
            {"id": channel_id, "contentDetails": {"relatedPlaylists": {"uploads": f"UU{channel_id}"}},
             "statistics": self.channel_stats.get(channel_id, {})}
            for channel_id in channel_ids
        ]

    def list_playlist_items(self, access_token: str, playlist_id: str, page_token: str | None = None):
        self.calls.append(("playlistItems.list", 1, {"playlist": playlist_id}))
        channel = playlist_id.removeprefix("UU")
        return [{"contentDetails": {"videoId": item["id"]}} for item in self.recent.get(channel, [])], None

    def list_videos(self, access_token: str, video_ids: list[str], parts: str):
        self.calls.append(("videos.list", 1, {"ids": list(video_ids)}))
        pool = [item for items in self.recent.values() for item in items] + self.search_results
        by_id = {item["id"]: item for item in pool}
        return [by_id[video_id] for video_id in video_ids if video_id in by_id]

    def search_videos(self, access_token: str, params: dict[str, str]):
        self.calls.append(("search.list", 100, dict(params)))
        return [{"id": {"videoId": item["id"]}} for item in self.search_results]


def deps(wiki: FakeWiki | None = None, youtube: FakeYouTube | None = None, brave: Any = None) -> DiscoveryDeps:
    token = (lambda: "token") if youtube is not None else None
    sources: list[Any] = [WikipediaPageviewsSource(wiki or FakeWiki()), YouTubeTrendingSource(youtube, token)]
    sources.append(brave or BraveNewsSource(None))
    return DiscoveryDeps(sources=sources, probe=YouTubeCompetitionProbe(youtube, token))


def curated(question: str, niche: str = "unknown", **overrides: Any) -> dict[str, Any]:
    """A usable curator answer for one topic."""
    return {"usable": True, "question": question, "niche": niche, **overrides}


GOOD_JUDGEMENT = {
    "self_contained_clarity": 9, "clear_factual_payoff": 8, "universal_12plus_relevance": 8,
    "prior_knowledge_free": 9, "natural_spoken_german": 9, "knowledge_short_fit": 8,
    "curiosity_gap": 8, "visual_potential": 7, "dach_relevance": 8, "grounded": True, "issues": [], "reason": "klar",
}


class FakeCurator:
    """Semantic curator stand-in (one call = question + judgement for <= 20 topics).

    ``by_topic`` scripts a topic (its title) -> fields (question, usable, dims ...);
    ``by_question`` scripts a final question -> judgement overrides.  Unscripted
    topics keep their local_question with a good judgement, or are unusable.
    """

    def __init__(self, by_topic: dict[str, dict[str, Any]] | None = None, *, by_question: dict[str, dict[str, Any]] | None = None,
                 default: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.by_topic = by_topic or {}
        self.by_question = by_question or {}
        self.default = GOOD_JUDGEMENT if default is None else default
        self.fail = fail
        self.requests: list[list[dict[str, Any]]] = []

    def __call__(self, api_key: str | None = None):
        return self

    @property
    def responses(self):
        return self

    def parse(self, **kwargs: Any):
        from clipforge.topic_intelligence.semantic import AICuratedBatch, AICuratedTopic

        topics = json.loads(kwargs["input"])["topics"]
        self.requests.append(topics)
        if self.fail:
            from openai import OpenAIError

            raise OpenAIError("curator down")
        items = []
        for topic in topics:
            scripted = self.by_topic.get(topic["topic"], {})
            question = scripted.get("question", topic.get("local_question") or "")
            usable = scripted.get("usable", bool(question))
            fields = {**self.default, **self.by_question.get(question, {}), **scripted}
            fields.update(question=question, usable=usable)
            fields.setdefault("niche", "unknown")
            items.append(AICuratedTopic(id=topic["id"], **fields))

        class Response:
            output_parsed = AICuratedBatch(items=items)

        return Response()


def bad(**overrides: Any) -> dict[str, Any]:
    """A judgement that fails the named dimensions/issues."""
    return {**GOOD_JUDGEMENT, **overrides}


def curate_groups(db, groups, curator, config=None, *, now=NOW):
    """Run the real curate() + curated_transform() path with a fake curator client."""
    from clipforge.topic_intelligence import semantic
    from clipforge.topic_intelligence.transform import curated_transform, deterministic_transform

    original = semantic.SEMANTIC_CLIENT_FACTORY
    semantic.SEMANTIC_CLIENT_FACTORY = curator
    try:
        outcome = semantic.curate(db, config or settings(openai_api_key="sk-test"), groups, requests_left=3, now=now)
    finally:
        semantic.SEMANTIC_CLIENT_FACTORY = original
    results = []
    for group in groups:
        judgement = outcome.judgements.get(group.key)
        if judgement is None:
            results.append(deterministic_transform(group))
        else:
            results.append(curated_transform(group, judgement, semantic.curated_signal(judgement, status=outcome.statuses[group.key])))
    return results, outcome
