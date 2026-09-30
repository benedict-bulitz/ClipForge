"""Provider-specific TTL cache of normalized discovery results (quota protection).

Only compact, normalized items are stored - never raw API responses - and
entries expire.  A repeated "Generate Next Video" within the TTL therefore
costs no external call at all.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..models import TopicSourceCache

# Documented TTLs: trend sources move within hours, competition within days.
PROVIDER_TTL: dict[str, timedelta] = {
    "wikipedia_pageviews": timedelta(hours=3),
    "youtube_trending_de": timedelta(hours=3),
    "brave_news_de": timedelta(hours=2),
    "youtube_search_competition": timedelta(hours=24),
}

_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BudgetExceeded(RuntimeError):
    """A call would exceed the refresh's quota budget; nothing was sent."""


@dataclass
class CallMeter:
    """Counts external calls and YouTube quota units against one refresh budget."""

    quota_budget: int
    calls: int = 0
    quota_units: int = 0

    def charge(self, units: int = 0) -> None:
        if units and self.quota_units + units > self.quota_budget:
            raise BudgetExceeded(f"YouTube quota budget of {self.quota_budget} units for this refresh reached")
        self.calls += 1
        self.quota_units += units


@dataclass(frozen=True)
class CacheHit:
    payload: dict[str, Any]
    cached: bool
    fetched_at: datetime
    calls: int
    quota_units: int


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(key, threading.Lock())


def get_or_fetch(
    db: Session,
    provider: str,
    key: str,
    fetch: Callable[[CallMeter], dict[str, Any]],
    meter: CallMeter,
    *,
    now: datetime,
    ttl: timedelta | None = None,
) -> CacheHit:
    """Return a fresh cached payload or fetch once (single-flight per key)."""
    cache_key = f"{provider}:{key}"[:200]
    with _key_lock(cache_key):
        entry = db.get(TopicSourceCache, cache_key)
        if entry is not None and _utc(entry.expires_at) > now:
            return CacheHit(dict(entry.payload), True, _utc(entry.fetched_at), 0, 0)
        # End the read transaction before waiting on the network: an open SQLite
        # transaction would hold up writers such as a new generation job.
        db.commit()
        before_calls, before_units = meter.calls, meter.quota_units
        payload = fetch(meter)
        calls, units = meter.calls - before_calls, meter.quota_units - before_units
        expires = now + (ttl or PROVIDER_TTL.get(provider, timedelta(hours=1)))
        if entry is None:
            entry = TopicSourceCache(key=cache_key, provider=provider)
            db.add(entry)
        entry.payload = payload
        entry.calls = calls
        entry.quota_units = units
        entry.fetched_at = now
        entry.expires_at = expires
        db.commit()
        return CacheHit(payload, False, now, calls, units)


def prune_expired(db: Session, *, now: datetime, keep: timedelta = timedelta(days=2)) -> int:
    """Forget long-expired entries so external payloads are not kept indefinitely."""
    result = db.execute(delete(TopicSourceCache).where(TopicSourceCache.expires_at < now - keep))
    db.commit()
    return int(result.rowcount or 0)
