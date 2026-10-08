from __future__ import annotations

"""HTTP surface of Topic Intelligence.  Proposes topics; never generates or publishes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import get_db
from ..integrations import get_secret_store
from ..schemas import TopicProposalRequest, TopicSuggestionsRequest
from ..security.secrets import SecretStore
from ..youtube.provider import YouTubeProvider
from ..youtube.routes import get_youtube_provider
from . import service

router = APIRouter(prefix="/api/topic-intelligence", tags=["topic-intelligence"])
DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ProviderDep = Annotated[YouTubeProvider, Depends(get_youtube_provider)]


def get_discovery_deps(db: DbSession, config: SettingsDep, store: StoreDep, provider: ProviderDep) -> service.DiscoveryDeps:
    return service.default_deps(db, config, store, provider)


DepsDep = Annotated[service.DiscoveryDeps, Depends(get_discovery_deps)]


@router.post("/next")
def next_topic_route(db: DbSession, config: SettingsDep, deps: DepsDep, payload: TopicProposalRequest | None = None) -> dict:
    """Generate Next Video: propose the best unused topic (reusing the fresh pool)."""
    return service.next_topic(db, config, deps, refresh=bool(payload and payload.refresh))


@router.post("/auto")
def auto_topic_route(db: DbSession, config: SettingsDep, deps: DepsDep) -> dict:
    """Full Auto: the strongest eligible next question, or ``no_strong_candidate`` - never a forced pick.

    Selection only; the client starts generation through the normal generation entry point.
    """
    return service.auto_topic(db, config, deps)


@router.post("/candidates/{candidate_id}/skip")
def skip_topic_route(candidate_id: str, db: DbSession, config: SettingsDep, deps: DepsDep) -> dict:
    """Try another: remember the skip and propose the next-best candidate."""
    try:
        return service.skip_topic(db, config, deps, candidate_id)
    except service.TopicHandoffError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"status": exc.code, "message": exc.message}) from exc


@router.post("/suggestions")
def suggestions_route(payload: TopicSuggestionsRequest, db: DbSession, config: SettingsDep, deps: DepsDep) -> dict:
    """Home chips: ranked candidates from the pool, excluding what is already shown."""
    return service.suggestions(
        db, config, deps, count=payload.count, exclude=payload.exclude, picked=payload.picked, dismissed=payload.dismissed,
    )


@router.get("/diagnostics")
def diagnostics_route(db: DbSession, config: SettingsDep, limit: int = 20, shown: bool = False, rescore: bool = False) -> dict:
    """Developer-only tuning view (every component, penalty, gate, rejection); off in production."""
    if config.environment == "production":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return service.diagnostics(db, config, limit=limit, shown=shown, rescore=rescore)


@router.get("/status")
def topic_status_route(db: DbSession, config: SettingsDep) -> dict:
    """Internal state: diagnosis code, sources, pool (accepted/rejected + reasons), caches."""
    return service.discovery_status(db, config)
