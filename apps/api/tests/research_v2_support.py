"""Offline fixtures for Research Pipeline V2: a deterministic web behind httpx.MockTransport.

Pages are small generic HTML documents; topics are fixtures only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx

from clipforge.config import Settings


def html_page(
    title: str,
    paragraphs: list[str],
    *,
    published: str | None = None,
    site: str = "",
    schema: str | None = None,
    extra_head: str = "",
    chrome: bool = True,
) -> str:
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    ld = f'<script type="application/ld+json">{json.dumps({"@type": schema, "headline": title})}</script>' if schema else ""
    nav = (
        '<nav><ul><li>Startseite Themen Wissen Technik Gesundheit Kontakt Impressum Datenschutz</li></ul></nav>'
        '<div class="cookie-banner"><p>Wir verwenden Cookies, um Ihnen das beste Erlebnis auf unserer Website zu bieten.</p></div>'
        if chrome else ""
    )
    related = (
        '<aside class="related"><p>Mehr zum Thema: Zehn Dinge, die Sie über Ihre Küche wissen sollten und mehr.</p></aside>'
        '<footer><p>Alle Rechte vorbehalten. Impressum Datenschutz Kontakt Newsletter abonnieren jetzt.</p></footer>'
        if chrome else ""
    )
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return (
        f'<html lang="de"><head><title>{title}</title><meta property="og:site_name" content="{site}">{meta}{ld}{extra_head}</head>'
        f"<body>{nav}<main><article><h1>{title}</h1>{body}</article></main>{related}</body></html>"
    )


@dataclass
class FakeWeb:
    """Routes requests to fixture pages; records every request."""

    pages: dict[str, tuple[int, str, str]] = field(default_factory=dict)  # url -> (status, content-type, body)
    robots: dict[str, str] = field(default_factory=dict)  # host -> robots.txt
    wiki_search: dict[str, list[dict]] = field(default_factory=dict)  # keyword -> results
    wiki_extracts: dict[str, str] = field(default_factory=dict)  # title -> plaintext
    wiki_links: dict[str, list[str]] = field(default_factory=dict)  # title -> external links
    brave: dict[str, list[dict]] = field(default_factory=dict)  # query substring -> results
    fail_hosts: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)

    def page(self, url: str, body: str, *, status: int = 200, kind: str = "text/html; charset=utf-8") -> None:
        self.pages[url] = (status, kind, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host in self.fail_hosts:
            raise httpx.ConnectError("connection refused", request=request)
        if parts.path == "/robots.txt":
            if host in self.robots:
                return httpx.Response(200, text=self.robots[host])
            return httpx.Response(404, text="")
        if host.endswith("wikipedia.org") and parts.path == "/w/api.php":
            params = {key: values[0] for key, values in parse_qs(parts.query).items()}
            if params.get("list") == "search":
                hits = self.wiki_search.get(params.get("srsearch", ""), [])
                return httpx.Response(200, json={"query": {"search": hits}})
            title = params.get("titles", "")
            if params.get("prop") == "extlinks":
                links = [{"*": link} for link in self.wiki_links.get(title, [])]
                return httpx.Response(200, json={"query": {"pages": {"1": {"title": title, "extlinks": links}}}})
            if "extracts" in params.get("prop", ""):
                text = self.wiki_extracts.get(title)
                if text is None:
                    return httpx.Response(200, json={"query": {"pages": {"-1": {"title": title, "missing": ""}}}})
                return httpx.Response(200, json={"query": {"pages": {"1": {"title": title, "extract": text, "touched": "2024-05-01T00:00:00Z"}}}})
        if host == "api.search.brave.com":
            query = parse_qs(parts.query).get("q", [""])[0]
            for needle, results in self.brave.items():
                if needle.casefold() in query.casefold():
                    return httpx.Response(200, json={"web": {"results": results}})
            return httpx.Response(200, json={"web": {"results": []}})
        if url in self.pages:
            status, kind, body = self.pages[url]
            return httpx.Response(status, headers={"content-type": kind}, text=body)
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def research_settings(**overrides) -> Settings:
    values = {"brave_search_api_key": None, "clipforge_ai_mode": "local", "openai_api_key": None}
    values.update(overrides)
    return Settings(**values)


def brave_hit(url: str, title: str, description: str, age: str | None = None) -> dict:
    return {"url": url, "title": title, "description": description, **({"page_age": age} if age else {})}
