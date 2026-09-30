"""Discovery source providers behind one small interface.

Each source fetches through ``cache.get_or_fetch`` (normalized payload, TTL,
single-flight) and converts that payload to ``RawTopic`` sightings.  A source
that is not configured is *skipped*, one that fails is *failed*; neither ever
produces invented data.  Adding a source = implementing ``TopicSource``.

Official APIs only:
* Wikimedia REST pageviews (de.wikipedia) + the MediaWiki Action API.
* YouTube Data API v3 (via ``youtube.provider``; quota-metered).
* Brave Search News API (the research key ClipForge already uses).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..language import detect_text_language
from ..youtube.provider import YouTubeApiError, YouTubeProvider
from .cache import BudgetExceeded, CallMeter, get_or_fetch
from .candidate import RawTopic
from .signals import (
    news_trend,
    outlier_vs_channel,
    trending_chart_trend,
    views_per_day,
    wikipedia_trend,
)
from .text import classify_niche, clean_title, compact, content_tokens, fold, similarity, topic_key

USER_AGENT = "ClipForge/0.2 (topic-intelligence; local desktop app)"
HTTP_TIMEOUT = 8.0


class SourceSkipped(RuntimeError):
    """The source is not configured/connected; not an error."""


class SourceFailed(RuntimeError):
    """The source was attempted and failed; its data is missing, never invented."""


HttpGet = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


def default_http_get(url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT, **headers}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise SourceFailed(f"network error: {type(exc).__name__}") from None
    if response.status_code == 404:
        raise FileNotFoundError(url)
    if response.status_code != 200:
        raise SourceFailed(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise SourceFailed("invalid JSON") from None


@dataclass
class DiscoveryContext:
    db: Session
    settings: Settings
    now: datetime
    meter: CallMeter
    language: str = "de"
    region: str = "DE"


@dataclass
class SourceReport:
    name: str
    status: str  # ok | cached | failed | skipped
    error: str | None = None
    fetched_at: datetime | None = None
    calls: int = 0
    quota_units: int = 0
    items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "calls": self.calls,
            "quota_units": self.quota_units,
            "items": self.items,
        }


@dataclass
class SourceResult:
    topics: list[RawTopic] = field(default_factory=list)
    report: SourceReport | None = None


class TopicSource(Protocol):
    name: str

    def discover(self, ctx: DiscoveryContext) -> SourceResult: ...


def _run_cached(source_name: str, ctx: DiscoveryContext, key: str, fetch: Callable[[CallMeter], dict[str, Any]]):
    hit = get_or_fetch(ctx.db, source_name, key, fetch, ctx.meter, now=ctx.now)
    return hit


def _parse_time(value: Any, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Wikipedia (de) pageviews: what German-speaking readers look up right now
# ---------------------------------------------------------------------------

_WIKI_EXCLUDED_PREFIXES = (
    "Spezial:", "Datei:", "Wikipedia:", "Portal:", "Kategorie:", "Hilfe:", "Benutzer:", "Vorlage:",
    "Liste_", "Nekrolog", "Special:", "File:", "Main_Page",
)
_WIKI_EXCLUDED_TITLES = {"Hauptseite", "-", "Wikipedia"}


def _wiki_flags(title: str, description: str, extract: str, disambiguation: bool) -> set[str]:
    flags: set[str] = set()
    text = f"{description} {extract[:220]}"
    if disambiguation:
        flags.add("disambiguation")
    folded = fold(text)
    if "(* " in extract[:160] or "(geb." in extract[:160] or any(
        marker in folded for marker in ("schauspieler", "saengerin", "saenger", "fussballspieler", "politiker", "moderator", "rapper", "musiker", "influencer", "unternehmer", "tennisspieler", "rennfahrer", "journalist")
    ):
        flags.add("person")
    niche, _strength = classify_niche(title, description)
    if niche in {"unterhaltung", "sport"}:
        flags.add("entertainment_or_sport")
    if niche == "unglueck_tragoedie":
        flags.add("tragedy")
    return flags


class WikipediaPageviewsSource:
    name = "wikipedia_pageviews"
    TOP_LIMIT = 60
    META_LIMIT = 20
    HISTORY_LIMIT = 12

    def __init__(self, http_get: HttpGet = default_http_get) -> None:
        self.http_get = http_get

    def _top(self, day: datetime, meter: CallMeter) -> list[dict[str, Any]]:
        url = (
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/de.wikipedia/all-access/"
            f"{day:%Y}/{day:%m}/{day:%d}"
        )
        meter.charge()
        payload = self.http_get(url, {}, {})
        items = payload.get("items") or []
        return list((items[0] if items else {}).get("articles") or [])

    def fetch(self, ctx: DiscoveryContext, meter: CallMeter) -> dict[str, Any]:
        day = ctx.now - timedelta(days=1)
        try:
            articles = self._top(day, meter)
        except FileNotFoundError:  # yesterday is not published yet
            day = day - timedelta(days=1)
            try:
                articles = self._top(day, meter)
            except FileNotFoundError:
                raise SourceFailed("pageview ranking not published") from None
        titles: list[tuple[str, int, int]] = []
        for article in articles:
            title = str(article.get("article") or "")
            if not title or title in _WIKI_EXCLUDED_TITLES or title.startswith(_WIKI_EXCLUDED_PREFIXES):
                continue
            titles.append((title, int(article.get("views") or 0), int(article.get("rank") or len(titles) + 1)))
            if len(titles) >= self.TOP_LIMIT:
                break
        meta: dict[str, dict[str, Any]] = {}
        if titles:
            meter.charge()
            try:
                response = self.http_get(
                    "https://de.wikipedia.org/w/api.php",
                    {
                        "action": "query", "format": "json", "formatversion": "2",
                        "prop": "description|extracts|pageprops", "exintro": "1", "explaintext": "1",
                        "exsentences": "2", "exlimit": str(self.META_LIMIT), "redirects": "1",
                        "titles": "|".join(title.replace("_", " ") for title, _v, _r in titles[: self.META_LIMIT]),
                    },
                    {},
                )
            except FileNotFoundError:
                response = {}
            for page in (response.get("query") or {}).get("pages") or []:
                meta[str(page.get("title") or "").replace(" ", "_")] = {
                    "description": compact(page.get("description"), 160),
                    "extract": compact(page.get("extract"), 360),
                    "disambiguation": "disambiguation" in (page.get("pageprops") or {}),
                }
        items: list[dict[str, Any]] = []
        history_budget = self.HISTORY_LIMIT
        start = (day - timedelta(days=29)).strftime("%Y%m%d")
        end = day.strftime("%Y%m%d")
        for title, views, rank in titles[: self.META_LIMIT]:
            info = meta.get(title, {})
            flags = _wiki_flags(title.replace("_", " "), info.get("description", ""), info.get("extract", ""), bool(info.get("disambiguation")))
            history: list[int] = []
            if history_budget > 0 and not flags & {"person", "disambiguation", "tragedy"}:
                history_budget -= 1
                meter.charge()
                try:
                    response = self.http_get(
                        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/de.wikipedia/all-access/user/"
                        f"{quote(title, safe='')}/daily/{start}/{end}",
                        {},
                        {},
                    )
                    history = [int(item.get("views") or 0) for item in response.get("items") or []]
                except (FileNotFoundError, SourceFailed):
                    history = []
            items.append({
                "title": title.replace("_", " "),
                "views": views,
                "rank": rank,
                "description": info.get("description", ""),
                "extract": info.get("extract", ""),
                "flags": sorted(flags),
                "history": history,
            })
        return {"day": day.strftime("%Y-%m-%d"), "items": items}

    def topics(self, payload: dict[str, Any], observed_at: datetime) -> list[RawTopic]:
        topics = []
        items = payload.get("items") or []
        for item in items:
            title = str(item.get("title") or "")
            if not title:
                continue
            trend = wikipedia_trend(item.get("history") or [], rank=int(item.get("rank") or 0), top_size=self.TOP_LIMIT)
            description = " ".join(part for part in (item.get("description"), item.get("extract")) if part)
            topics.append(RawTopic(
                key=topic_key(title),
                title=title,
                source=self.name,
                kind="article",
                observed_at=observed_at,
                description=compact(description, 400),
                url=f"https://de.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                trend=trend,
                metrics={"views_day": item.get("views"), "rank": item.get("rank"), **{k: v for k, v in trend.evidence.items() if k in {"ratio", "baseline_days"}}},
                flags=set(item.get("flags") or []),
            ))
        return topics

    def discover(self, ctx: DiscoveryContext) -> SourceResult:
        hit = _run_cached(self.name, ctx, f"top:{ctx.language}:{(ctx.now - timedelta(days=1)):%Y-%m-%d}", lambda meter: self.fetch(ctx, meter))
        topics = self.topics(hit.payload, hit.fetched_at)
        return SourceResult(topics, SourceReport(self.name, "cached" if hit.cached else "ok", fetched_at=hit.fetched_at, calls=hit.calls, items=len(topics)))


# ---------------------------------------------------------------------------
# YouTube: the German most-popular chart for knowledge categories, with each
# video's performance measured against its own channel's recent uploads.
# ---------------------------------------------------------------------------

TokenGetter = Callable[[], str]


def _is_german_video(snippet: dict[str, Any]) -> bool:
    language = str(snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "").casefold()
    if language:
        return language.startswith("de")
    return detect_text_language(f"{snippet.get('title', '')} {snippet.get('description', '')[:200]}") == "de"


class YouTubeTrendingSource:
    name = "youtube_trending_de"
    CATEGORIES: ClassVar[dict[str, str]] = {"27": "Bildung", "28": "Wissenschaft & Technik"}
    CHART_SIZE = 25
    MAX_CHANNELS = 8

    def __init__(self, provider: YouTubeProvider | None, token: TokenGetter | None) -> None:
        self.provider = provider
        self.token = token

    def fetch(self, ctx: DiscoveryContext, meter: CallMeter) -> dict[str, Any]:
        assert self.provider is not None and self.token is not None
        token = self.token()
        videos: list[dict[str, Any]] = []
        errors: list[str] = []
        for category_id, label in self.CATEGORIES.items():
            meter.charge(1)
            try:
                items = self.provider.list_popular_videos(token, ctx.region, category_id, self.CHART_SIZE)
            except YouTubeApiError as exc:
                errors.append(exc.code)
                continue
            for rank, item in enumerate(items, 1):
                snippet = item.get("snippet") or {}
                if not _is_german_video(snippet):
                    continue
                videos.append({
                    "video_id": item.get("id"),
                    "title": compact(snippet.get("title"), 160),
                    "description": compact(snippet.get("description"), 240),
                    "channel_id": snippet.get("channelId"),
                    "channel_title": compact(snippet.get("channelTitle"), 80),
                    "published_at": snippet.get("publishedAt"),
                    "views": int((item.get("statistics") or {}).get("viewCount") or 0),
                    "rank": rank,
                    "chart_size": len(items),
                    "category": label,
                })
        if not videos and errors:
            raise SourceFailed(f"YouTube chart unavailable ({', '.join(sorted(set(errors)))})")
        # Channel baselines: recent uploads of the charted channels (bounded).
        channel_ids = list(dict.fromkeys(str(video["channel_id"]) for video in videos if video.get("channel_id")))[: self.MAX_CHANNELS]
        # channel -> [(video_id, views per day)] of its recent uploads
        baselines: dict[str, list[tuple[str, float]]] = {}
        try:
            if channel_ids:
                meter.charge(1)
                channels = self.provider.list_channels(token, channel_ids, "contentDetails")
                uploads = {
                    str(item.get("id")): ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
                    for item in channels
                }
                recent_ids: dict[str, list[str]] = {}
                for channel_id, playlist in uploads.items():
                    if not playlist:
                        continue
                    meter.charge(1)
                    items, _next = self.provider.list_playlist_items(token, str(playlist))
                    recent_ids[channel_id] = [
                        str((item.get("contentDetails") or {}).get("videoId")) for item in items[:15]
                        if (item.get("contentDetails") or {}).get("videoId")
                    ]
                all_ids = [video_id for ids in recent_ids.values() for video_id in ids]
                stats: dict[str, dict[str, Any]] = {}
                for start in range(0, len(all_ids), 50):
                    meter.charge(1)
                    for item in self.provider.list_videos(token, all_ids[start : start + 50], "statistics,snippet"):
                        stats[str(item.get("id"))] = item
                for channel_id, ids in recent_ids.items():
                    rows = []
                    for video_id in ids:
                        item = stats.get(video_id)
                        if not item:
                            continue
                        published = _parse_time((item.get("snippet") or {}).get("publishedAt"), ctx.now)
                        views = float((item.get("statistics") or {}).get("viewCount") or 0)
                        rows.append((video_id, round(views_per_day(views, published, ctx.now), 2)))
                    baselines[channel_id] = rows
        except (YouTubeApiError, BudgetExceeded) as exc:
            errors.append(getattr(exc, "code", "budget"))
        for video in videos:
            # The charted video never counts towards its own channel's baseline.
            video["channel_recent_vpd"] = [
                value for video_id, value in baselines.get(str(video["channel_id"]), []) if video_id != video["video_id"]
            ]
        return {"videos": videos, "partial_errors": sorted(set(errors))}

    def topics(self, payload: dict[str, Any], observed_at: datetime, now: datetime) -> list[RawTopic]:
        topics = []
        for video in payload.get("videos") or []:
            title = clean_title(str(video.get("title") or ""))
            if len(content_tokens(title)) < 1:
                continue
            published = _parse_time(video.get("published_at"), observed_at)
            vpd = views_per_day(float(video.get("views") or 0), published, now)
            outlier = outlier_vs_channel(vpd, video.get("channel_recent_vpd") or [])
            topics.append(RawTopic(
                key=topic_key(title),
                title=title,
                source=self.name,
                kind="video",
                observed_at=observed_at,
                description=compact(video.get("description"), 240),
                url=f"https://www.youtube.com/watch?v={video.get('video_id')}",
                trend=trending_chart_trend(int(video.get("rank") or 1), int(video.get("chart_size") or self.CHART_SIZE), category=str(video.get("category") or "")),
                outlier=outlier,
                metrics={"views": video.get("views"), "rank": video.get("rank"), "views_per_day": round(vpd), "channel": video.get("channel_title")},
            ))
        return topics

    def discover(self, ctx: DiscoveryContext) -> SourceResult:
        if self.provider is None or self.token is None:
            raise SourceSkipped("YouTube is not connected")
        hit = _run_cached(self.name, ctx, f"chart:{ctx.region}", lambda meter: self.fetch(ctx, meter))
        topics = self.topics(hit.payload, hit.fetched_at, ctx.now)
        partial = hit.payload.get("partial_errors") or []
        return SourceResult(topics, SourceReport(
            self.name, "cached" if hit.cached else "ok", error=", ".join(partial) or None,
            fetched_at=hit.fetched_at, calls=hit.calls, quota_units=hit.quota_units, items=len(topics),
        ))


# ---------------------------------------------------------------------------
# Brave News (Germany, German, last day): what German outlets report now.
# ---------------------------------------------------------------------------


class BraveNewsSource:
    name = "brave_news_de"
    QUERIES = ("Wissenschaft Forschung", "Gesundheit Studie", "Natur Technik Phänomen")
    NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

    def __init__(self, api_key: str | None, http_get: HttpGet = default_http_get) -> None:
        self.api_key = api_key
        self.http_get = http_get

    def fetch(self, ctx: DiscoveryContext, meter: CallMeter) -> dict[str, Any]:
        articles: list[dict[str, Any]] = []
        failures = 0
        for query in self.QUERIES:
            meter.charge()
            try:
                payload = self.http_get(
                    self.NEWS_URL,
                    {"q": query, "country": "DE", "search_lang": "de", "freshness": "pd", "count": 20},
                    {"X-Subscription-Token": str(self.api_key), "Accept": "application/json"},
                )
            except (SourceFailed, FileNotFoundError):
                failures += 1
                continue
            for item in payload.get("results") or []:
                articles.append({
                    "title": compact(item.get("title"), 200),
                    "description": compact(item.get("description"), 280),
                    "url": item.get("url"),
                    "outlet": ((item.get("meta_url") or {}).get("hostname") or ""),
                    "age": item.get("page_age") or item.get("age"),
                })
        if failures == len(self.QUERIES):
            raise SourceFailed("Brave News unavailable")
        return {"articles": articles}

    def topics(self, payload: dict[str, Any], observed_at: datetime) -> list[RawTopic]:
        articles = [item for item in payload.get("articles") or [] if item.get("title")]
        clusters: list[list[dict[str, Any]]] = []
        for article in articles:
            for cluster in clusters:
                if similarity(article["title"], cluster[0]["title"]) >= 0.5:
                    cluster.append(article)
                    break
            else:
                clusters.append([article])
        topics = []
        for cluster in clusters:
            lead = cluster[0]
            title = clean_title(lead["title"])
            outlets = len({item.get("outlet") for item in cluster if item.get("outlet")})
            niche, _ = classify_niche(title, lead.get("description") or "")
            flags = {"tragedy"} if niche == "unglueck_tragoedie" else set()
            if niche == "politik_tagesgeschehen":
                flags.add("politics")
            topics.append(RawTopic(
                key=topic_key(title),
                title=title,
                source=self.name,
                kind="news",
                observed_at=observed_at,
                description=compact(lead.get("description"), 280),
                url=lead.get("url"),
                trend=news_trend(outlets, len(cluster)),
                metrics={"outlets": outlets, "articles": len(cluster)},
                flags=flags,
            ))
        return topics

    def discover(self, ctx: DiscoveryContext) -> SourceResult:
        if not self.api_key:
            raise SourceSkipped("Brave Search is not configured")
        hit = _run_cached(self.name, ctx, f"news:{ctx.region}:{ctx.language}", lambda meter: self.fetch(ctx, meter))
        topics = self.topics(hit.payload, hit.fetched_at)
        return SourceResult(topics, SourceReport(self.name, "cached" if hit.cached else "ok", fetched_at=hit.fetched_at, calls=hit.calls, items=len(topics)))


# ---------------------------------------------------------------------------
# Competition probe: one YouTube search per strong candidate (100 units each),
# bounded per refresh and cached for a day per query.
# ---------------------------------------------------------------------------


class YouTubeCompetitionProbe:
    name = "youtube_search_competition"
    SEARCH_UNITS = 100

    def __init__(self, provider: YouTubeProvider | None, token: TokenGetter | None) -> None:
        self.provider = provider
        self.token = token

    @property
    def available(self) -> bool:
        return self.provider is not None and self.token is not None

    @staticmethod
    def query_for(question: str, topic: str) -> str:
        tokens = content_tokens(question) or content_tokens(topic)
        words = [word for word in question.rstrip("?").split() if any(fold(word).startswith(token[:4]) for token in tokens)]
        return " ".join(words[:6]) or topic[:80]

    def fetch(self, ctx: DiscoveryContext, meter: CallMeter, query: str) -> dict[str, Any]:
        assert self.provider is not None and self.token is not None
        token = self.token()
        meter.charge(self.SEARCH_UNITS)
        results = self.provider.search_videos(token, {
            "q": query,
            "regionCode": ctx.region,
            "relevanceLanguage": ctx.language,
            "videoDuration": "short",
            "publishedAfter": (ctx.now - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": "25",
            "order": "relevance",
        })
        ids = [str((item.get("id") or {}).get("videoId")) for item in results if (item.get("id") or {}).get("videoId")]
        videos: list[dict[str, Any]] = []
        if ids:
            meter.charge(1)
            details = self.provider.list_videos(token, ids[:50], "snippet,statistics")
            channel_ids = list(dict.fromkeys(str((item.get("snippet") or {}).get("channelId")) for item in details))
            channels: dict[str, dict[str, Any]] = {}
            if channel_ids:
                meter.charge(1)
                for item in self.provider.list_channels(token, channel_ids[:50], "statistics"):
                    channels[str(item.get("id"))] = item.get("statistics") or {}
            for item in details:
                snippet = item.get("snippet") or {}
                stats = channels.get(str(snippet.get("channelId"))) or {}
                videos.append({
                    "title": compact(snippet.get("title"), 160),
                    "views": int((item.get("statistics") or {}).get("viewCount") or 0),
                    "published_at": snippet.get("publishedAt"),
                    "channel_views": int(stats.get("viewCount") or 0),
                    "channel_videos": int(stats.get("videoCount") or 0),
                })
        return {"query": query, "videos": videos}

    def probe(self, ctx: DiscoveryContext, question: str, topic: str) -> tuple[dict[str, Any], bool]:
        if not self.available:
            raise SourceSkipped("YouTube is not connected")
        query = self.query_for(question, topic)
        hit = get_or_fetch(ctx.db, self.name, f"{ctx.region}:{ctx.language}:{fold(query)}", lambda meter: self.fetch(ctx, meter, query), ctx.meter, now=ctx.now)
        return hit.payload, hit.cached


