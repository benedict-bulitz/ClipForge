import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import Settings


@dataclass(frozen=True)
class ResearchResult:
    facts: list[dict]
    sources: list[dict]
    status: str
    provider: str
    error: str | None = None


def _query_from_prompt(prompt: str) -> str:
    cleaned = re.sub(
        r"^(why|what|how|when|where|who|explain|tell me|warum|was|wie|wann|wo|wer|erkläre|erklare)\s+",
        "",
        prompt.strip(),
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ?!.,") or prompt


def _sentences(text: str, limit: int = 4) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", text.strip())
    return [part.strip() for part in parts if len(part.split()) >= 5][:limit]


def research_topic(prompt: str, language: str, settings: Settings) -> ResearchResult:
    """Collect attributable snippets. Brave is used when configured; Wikipedia is the free fallback."""
    query = _query_from_prompt(prompt)
    try:
        if settings.brave_search_api_key:
            response = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 5, "search_lang": language},
                headers={"X-Subscription-Token": settings.brave_search_api_key},
                timeout=6,
            )
            response.raise_for_status()
            results = response.json().get("web", {}).get("results", [])
            sources = [
                {"label": item.get("title") or item.get("url"), "url": item.get("url")}
                for item in results
                if item.get("url")
            ]
            claims = [item.get("description", "").strip() for item in results]
            facts = _fact_records([claim for claim in claims if claim], sources)
            return ResearchResult(facts, sources, "verified_sources", "brave")

        host = "de.wikipedia.org" if language == "de" else "en.wikipedia.org"
        search = httpx.get(
            f"https://{host}/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": 1,
                "format": "json",
            },
            headers={"User-Agent": "ClipForge/0.1 (local prototype)"},
            timeout=6,
        )
        search.raise_for_status()
        hit = (search.json().get("query", {}).get("search") or [])[0]
        title = hit["title"]
        extract = httpx.get(
            f"https://{host}/w/api.php",
            params={
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "titles": title,
                "format": "json",
                "redirects": 1,
            },
            headers={"User-Agent": "ClipForge/0.1 (local prototype)"},
            timeout=6,
        )
        extract.raise_for_status()
        page = next(iter(extract.json()["query"]["pages"].values()))
        source = {
            "label": f"Wikipedia — {page.get('title', title)}",
            "url": f"https://{host}/wiki/{quote(page.get('title', title).replace(' ', '_'))}",
        }
        facts = _fact_records(_sentences(page.get("extract", "")), [source])
        if not facts:
            return ResearchResult([], [], "unavailable", "wikipedia", "No usable source text found")
        return ResearchResult(facts, [source], "verified_sources", "wikipedia")
    except (httpx.HTTPError, IndexError, KeyError, ValueError) as exc:
        return ResearchResult([], [], "unavailable", "wikipedia", str(exc)[:240])


def _fact_records(claims: list[str], sources: list[dict]) -> list[dict]:
    return [
        {
            "id": f"fact_{index:02d}",
            "claim": claim,
            "confidence": 0.78,
            "importance": max(0.55, 0.95 - index * 0.1),
            "priority": "MUST_KNOW" if index <= 3 else "USEFUL",
            "sources": sources[:1],
            "verification": "source_snippet",
        }
        for index, claim in enumerate(claims[:6], 1)
    ]
