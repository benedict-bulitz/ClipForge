from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any
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
    # Research Pipeline V2: the structured research package and compact diagnostics.
    package: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None


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


def research_topic(
    prompt: str, language: str, settings: Settings, *, context: dict[str, Any] | None = None
) -> ResearchResult:
    """Research one question: V2 (retrieved, attributable evidence) with V1 as the isolated fallback.

    ``context`` carries what the query alone loses: the user's original
    ``question``, the intent ``content_type`` and a retry ``focus``.
    """
    if str(settings.research_pipeline).casefold() != "v1":
        try:
            from .research_v2 import run_research

            run = run_research(prompt, language, settings, context=context)
            return ResearchResult(
                run.facts, run.sources, run.status, "research_v2", run.error, run.package, run.diagnostics
            )
        except Exception as exc:  # noqa: BLE001 - V2 must never be a single point of failure
            fallback = _research_topic_v1(prompt, language, settings)
            note = {"v2_error": f"{type(exc).__name__}: {str(exc)[:200]}", "fallback": "v1"}
            return ResearchResult(
                fallback.facts, fallback.sources, fallback.status, fallback.provider, fallback.error, None, note
            )
    return _research_topic_v1(prompt, language, settings)


def research_with_strengthening(
    query: str,
    language: str,
    settings: Settings,
    *,
    context: dict[str, Any],
    research: Any = None,
) -> tuple[ResearchResult, dict[str, Any]]:
    """The production first research pass, including its single weak-core retry.

    When the package's core answer is valid but rests only on a snippet from an
    unknown/low-authority source, research runs once more with focus
    ``strengthen``; the stronger package (core present -> full text -> authority)
    replaces the original, otherwise the original is kept as it is.  Returns the
    result and a report: attempted, reason, result, replaced.  Used by the
    pipeline and by ``scripts/research_v2_audit.py`` so both exercise one path.
    """
    from .research_v2.package import core_strength, weak_core

    research = research or research_topic
    first = research(query, language, settings, context=context)
    package = getattr(first, "package", None)
    if not weak_core(package):
        reason = "no_core_answer" if not (package or {}).get("core_answer") else "core_answer_not_weak"
        return first, {"attempted": False, "reason": reason, "result": None, "replaced": False}
    question = str(context.get("question") or query)
    stronger = research(question, language, settings, context={**context, "focus": "strengthen"})
    stronger_package = getattr(stronger, "package", None)
    replaced = core_strength(stronger_package) > core_strength(package)
    core = (stronger_package or {}).get("core_answer") or {}
    report = {
        "attempted": True,
        "reason": "weak_core_source",
        "focus": "strengthen",
        "result": {
            "status": (stronger_package or {}).get("status"),
            "core_basis": core.get("basis"),
            "core_authority_tier": core.get("authority_tier"),
        },
        "replaced": replaced,
    }
    chosen = stronger if replaced else first
    # The strengthening pass is a research retry even though it is a second
    # bounded run. Keep its audit flag and counter internally consistent.
    diagnostics = dict(chosen.diagnostics or {})
    budget = dict(diagnostics.get("budget") or {})
    used = dict(budget.get("used") or {})
    used["retries"] = max(1, int(used.get("retries") or 0))
    budget["used"] = used
    diagnostics.update({"retry_attempted": True, "budget": budget})
    return replace(chosen, diagnostics=diagnostics), report


def _research_topic_v1(prompt: str, language: str, settings: Settings) -> ResearchResult:
    """V1: attributable snippets. Brave is used when configured; Wikipedia is the free fallback."""
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
            # Each snippet keeps the page it came from (provenance).
            pairs = [
                (item.get("description", "").strip(), {"label": item.get("title") or item.get("url"), "url": item.get("url")})
                for item in results
                if item.get("url") and item.get("description", "").strip()
            ]
            facts = _fact_records([claim for claim, _source in pairs], [source for _claim, source in pairs], per_claim=True)
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


def _fact_records(claims: list[str], sources: list[dict], *, per_claim: bool = False) -> list[dict]:
    return [
        {
            "id": f"fact_{index:02d}",
            "claim": claim,
            "confidence": 0.78,
            "importance": max(0.55, 0.95 - index * 0.1),
            "priority": "MUST_KNOW" if index <= 3 else "USEFUL",
            "sources": sources[index - 1:index] if per_claim else sources[:1],
            "verification": "source_snippet",
        }
        for index, claim in enumerate(claims[:6], 1)
    ]
