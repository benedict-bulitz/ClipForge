"""Discovery: find promising URLs.  A hit is a lead, never evidence.

Providers (all already part of ClipForge, no paid additions):

* Brave Web Search, when ``BRAVE_SEARCH_API_KEY`` is configured - one
  request per research sub-question;
* Wikipedia search (free) - the question's reference article;
* the reference article's *cited* external links, filtered by host shape to
  government / academic / institutional / scholarly hosts - a free route from
  a secondary summary to the primary sources it is built on.

Search ranking is recorded but is not authority: selection happens later.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .models import Discovered, ResearchBudget, SubQuestion
from .quality import AUTHORITY, classify_source, host_of

MAX_CITATION_LINKS = 3
_TAG = re.compile(r"<[^>]+>")
_MIRROR = re.compile(r"(?i)(?:^|\.)(?:web\.archive\.org|archive\.(?:today|ph|is)|webcitation\.org)$")


_KEYWORD_STOP = {
    "warum", "wieso", "weshalb", "was", "wie", "wann", "wo", "wer", "welche", "welcher", "welches", "why", "what", "how",
    "when", "where", "who", "which", "der", "die", "das", "dem", "den", "des", "ein", "eine", "the", "and", "does", "do",
    "is", "are", "were", "ist", "sind", "bleibt", "wird", "wurde", "hat", "haben", "kann", "können",
    "ursache", "erklärung", "funktioniert", "cause", "explanation", "works", "work",
}


def encyclopedia_keywords(query: str, language: str) -> list[str]:
    """Subject keywords for a full-text encyclopedia search, most specific fallback second.

    German nouns are capitalised, so they carry the subject; other languages
    use the content words.
    """
    tokens = [token for token in re.findall(r"[\wÄÖÜäöüß-]+", str(query or "")) if token.casefold() not in _KEYWORD_STOP]
    if language == "de":
        nouns = [token for token in tokens if token[:1].isupper()] or tokens
    else:
        nouns = [token for token in tokens if len(token) >= 4] or tokens
    nouns = list(dict.fromkeys(nouns))[:3]
    if not nouns:
        return [query] if query else []
    longest = max(nouns, key=len)
    return [" ".join(nouns), longest] if len(nouns) > 1 else nouns


def _plain(value: object) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", str(value or ""))).strip()


class Discovery:
    def __init__(
        self,
        client: httpx.Client,
        budget: ResearchBudget,
        *,
        language: str,
        brave_key: str | None = None,
    ) -> None:
        self.client = client
        self.budget = budget
        self.language = "de" if str(language).startswith("de") else "en"
        self.brave_key = brave_key
        self.log: list[dict[str, Any]] = []
        self._down: set[str] = set()
        self._queries: set[str] = set()

    def _record(self, provider: str, query: str, sub_question: str, status: str, hits: int = 0, error: str | None = None) -> None:
        self.log.append({
            "provider": provider, "query": query[:160], "sub_question": sub_question, "status": status, "hits": hits,
            **({"error": error[:160]} if error else {}),
        })

    def _get(self, provider: str, url: str, **kwargs: Any) -> dict[str, Any] | None:
        if provider in self._down:
            return None
        try:
            response = self.client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 429}:
                self._down.add(provider)
            raise
        except httpx.TransportError:
            self._down.add(provider)  # unreachable: no further calls this run
            raise

    def discover(self, sub_questions: list[SubQuestion]) -> list[Discovered]:
        hits: list[Discovered] = []
        if self.brave_key:
            for sub in sub_questions:
                hits += self._brave(sub)
        core = sub_questions[0]
        wiki_queries = [core] if self.brave_key else sub_questions[:2]
        articles: list[Discovered] = []
        for sub in wiki_queries:
            articles += self._wikipedia(sub, limit=3 if sub is core else 2)
        hits += articles
        if articles:
            hits += self._citations(articles[0], core)
        return hits

    def _brave(self, sub: SubQuestion) -> list[Discovered]:
        if "brave" in self._down:
            self._record("brave", sub.query, sub.id, "skipped_provider_down")
            return []
        if not self.budget.take("searches"):
            self._record("brave", sub.query, sub.id, "budget_exhausted")
            return []
        try:
            data = self._get(
                "brave",
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": sub.query, "count": 8, "search_lang": self.language, "text_decorations": False},
                headers={"X-Subscription-Token": str(self.brave_key), "Accept": "application/json"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            self._record("brave", sub.query, sub.id, "failed", error=str(exc))
            return []
        if data is None:
            self._record("brave", sub.query, sub.id, "skipped_provider_down")
            return []
        results = (data.get("web") or {}).get("results") or []
        found = [
            Discovered(
                url=str(item.get("url")),
                title=_plain(item.get("title")),
                snippet=_plain(" ".join([str(item.get("description") or ""), *[str(x) for x in item.get("extra_snippets") or []][:2]])),
                provider="brave",
                sub_question=sub.id,
                rank=rank,
                age_hint=str(item.get("page_age") or item.get("age") or "") or None,
            )
            for rank, item in enumerate(results, 1)
            if str(item.get("url") or "").startswith("http")
        ]
        self._record("brave", sub.query, sub.id, "ok", len(found))
        return found

    def _wikipedia(self, sub: SubQuestion, *, limit: int) -> list[Discovered]:
        host = f"{self.language}.wikipedia.org"
        keywords = encyclopedia_keywords(sub.query, self.language)
        results: list[dict[str, Any]] = []
        # Full-text search needs every word: a sentence-shaped query often finds
        # nothing, so the subject nouns go first and the single most specific
        # noun is the (budgeted) second try.
        for query in dict.fromkeys(keywords):
            if query.casefold() in self._queries:
                self._record("wikipedia", query, sub.id, "duplicate_query")
                continue
            self._queries.add(query.casefold())
            if "wikipedia" in self._down:
                self._record("wikipedia", query, sub.id, "skipped_provider_down")
                return []
            if not self.budget.take("searches"):
                self._record("wikipedia", query, sub.id, "budget_exhausted")
                return []
            try:
                data = self._get(
                    "wikipedia",
                    f"https://{host}/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"},
                )
            except (httpx.HTTPError, ValueError) as exc:
                self._record("wikipedia", query, sub.id, "failed", error=str(exc))
                return []
            if data is None:
                self._record("wikipedia", query, sub.id, "skipped_provider_down")
                return []
            results = (data.get("query") or {}).get("search") or []
            if results:
                break
            self._record("wikipedia", query, sub.id, "no_hits")
        found = [
            Discovered(
                url=f"https://{host}/wiki/{str(item['title']).replace(' ', '_')}",
                title=str(item["title"]),
                snippet=_plain(item.get("snippet")),
                provider="wikipedia",
                sub_question=sub.id,
                rank=rank,
            )
            for rank, item in enumerate(results, 1)
            if item.get("title")
        ]
        if found:
            self._record("wikipedia", keywords[0] if keywords else sub.query, sub.id, "ok", len(found))
        return found

    def _citations(self, article: Discovered, core: SubQuestion) -> list[Discovered]:
        """High-authority hosts the reference article cites (host shape only)."""
        if "wikipedia" in self._down or not self.budget.take("searches"):
            self._record("encyclopedia_citations", article.title, core.id, "budget_exhausted")
            return []
        host = host_of(article.url)
        try:
            data = self._get(
                "wikipedia",
                f"https://{host}/w/api.php",
                params={"action": "query", "prop": "extlinks", "titles": article.title, "ellimit": 80, "format": "json"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            self._record("encyclopedia_citations", article.title, core.id, "failed", error=str(exc))
            return []
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        links = [
            str(link.get("*") or link.get("url") or "")
            for page in pages.values() for link in page.get("extlinks") or []
        ]
        found: list[Discovered] = []
        seen_hosts: set[str] = set()
        for link in links:
            link_host = host_of(link)
            if not link.startswith("http") or not link_host or _MIRROR.search(link_host) or link_host in seen_hosts:
                continue
            if AUTHORITY[classify_source(link).source_type] != "high":
                continue
            seen_hosts.add(link_host)
            found.append(Discovered(
                url=link, title="", snippet="", provider="encyclopedia_citation", sub_question=core.id,
                rank=len(found) + 1,
            ))
            if len(found) >= MAX_CITATION_LINKS:
                break
        self._record("encyclopedia_citations", article.title, core.id, "ok", len(found))
        return found
