"""Shared research records and the per-run request budget."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .extraction import ExtractedPage


@dataclass(frozen=True)
class SubQuestion:
    """One bounded research sub-question (``core`` always exists)."""

    id: str
    kind: str  # core | mechanism | detail | misconception | current
    question: str
    query: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "question": self.question, "query": self.query}


@dataclass
class Discovered:
    """A discovery hit: a promising URL, never evidence by itself."""

    url: str
    title: str
    snippet: str
    provider: str
    sub_question: str
    rank: int
    age_hint: str | None = None


@dataclass
class FetchOutcome:
    url: str
    status: str  # ok | cached | robots_disallowed | access_denied | rate_limited | not_found | timeout | http_error | network_error | not_html | too_large | js_only | extraction_failed | budget_exhausted | host_skipped
    method: str = "none"  # http | dynamic | encyclopedia_api | cache | none
    page: ExtractedPage | None = None
    http_status: int | None = None
    error: str | None = None
    elapsed_ms: int = 0
    fetched_at: str | None = None
    attempts: int = 0

    @property
    def usable(self) -> bool:
        return self.page is not None and bool(self.page.paragraphs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "method": self.method,
            "http_status": self.http_status,
            "error": (self.error or None) and str(self.error)[:160],
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "extractor": self.page.extractor if self.page else None,
            "paragraphs": len(self.page.paragraphs) if self.page else 0,
        }


@dataclass
class ResearchBudget:
    """Hard per-run limits; every expensive step asks before it spends."""

    max_searches: int = 4
    max_documents: int = 6
    max_dynamic: int = 1
    max_llm_calls: int = 2
    max_retries: int = 2
    used: dict[str, int] = field(default_factory=lambda: {"searches": 0, "documents": 0, "dynamic": 0, "llm_calls": 0, "retries": 0})
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self, kind: str) -> bool:
        limit = {
            "searches": self.max_searches,
            "documents": self.max_documents,
            "dynamic": self.max_dynamic,
            "llm_calls": self.max_llm_calls,
            "retries": self.max_retries,
        }[kind]
        with self._lock:
            if self.used[kind] >= limit:
                return False
            self.used[kind] += 1
            return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "limits": {
                "searches": self.max_searches,
                "documents": self.max_documents,
                "dynamic": self.max_dynamic,
                "llm_calls": self.max_llm_calls,
                "retries": self.max_retries,
            },
            "used": dict(self.used),
        }
