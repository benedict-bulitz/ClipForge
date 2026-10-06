"""The unified read model over every publication (no provider calls).

YouTube publications stay in ``youtube_uploads`` (their mature authority:
analytics, learning, remote status) and Instagram/TikTok publications in
``social_publications``; this module only *reads* both so the Videos tab,
the Queue Overview and the publishing sheet can show one list.  The same
project can have several publications (e.g. YouTube channel A, Instagram
account A, TikTok account B); each stays its own entry.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Project, PublishingAccount, SocialPublication, YouTubeUpload
from . import accounts
from .publications import STATE_LABELS, actions, aware, serialize_error

# Library status buckets of social publications.
SOCIAL_BUCKETS = {
    "published": "published",
    "scheduled": "scheduled",
    "pending": "uploading",
    "uploading": "uploading",
    "processing": "processing",
    "failed": "failed",
    "missed": "failed",
    "cancelled": "cancelled",
}


def social_sort_date(row: SocialPublication) -> datetime:
    return aware(row.published_at) or aware(row.scheduled_at) or aware(row.created_at) or datetime.min.replace(tzinfo=UTC)


def account_map(db: Session) -> dict[str, PublishingAccount]:
    return {item.id: item for item in accounts.list_accounts(db, include_disconnected=True)}


def serialize_library_item(row: SocialPublication, *, project: Project | None, account: PublishingAccount | None) -> dict[str, Any]:
    """A Videos-tab entry for an Instagram/TikTok publication (shape-compatible
    with the YouTube library entries where the concept exists)."""
    snapshot = row.metadata_snapshot or {}
    caption = str(snapshot.get("caption") or "")
    first_line = caption.split("\n", 1)[0].strip()
    return {
        "id": row.id,
        "kind": "social",
        "platform": row.platform,
        "youtube_video_id": None,
        "title": row.project_title or first_line or "Untitled",
        "caption": caption,
        "prompt": project.original_prompt if project is not None else None,
        "topic": None,
        "account": {
            "id": row.account_id,
            "label": account.display_name if account is not None else row.account_label,
            "handle": account.handle if account is not None else None,
            "connected": accounts.is_active(account),
        },
        "channel": None,
        "state": row.state,
        "status_bucket": SOCIAL_BUCKETS.get(row.state, row.state),
        "state_label": STATE_LABELS.get(row.state, row.state),
        "scheduled_for": aware(row.scheduled_at),
        "schedule_timezone": row.schedule_timezone,
        "published_at": aware(row.published_at),
        "uploaded_at": aware(row.created_at),
        "sort_date": social_sort_date(row),
        "project": {
            "id": row.project_id,
            "available": project is not None,
            "title": project.title if project is not None else row.project_title,
            "archived_at": None,
        },
        "thumbnail_url": snapshot.get("poster_url") if project is not None else None,
        "remote_url": row.remote_url,
        "remote_post_id": row.remote_post_id,
        "error": serialize_error(row),
        "actions": actions(row),
        "privacy_level": snapshot.get("privacy_level"),
        "live_stats": None,
        "live_stats_state": "not_applicable",
        "analytics": {"state": "not_applicable"},
    }


def social_library(
    db: Session,
    *,
    platform: str = "all",
    account_id: str | None = None,
    query: str = "",
    include_cancelled: bool = False,
    known: dict[str, PublishingAccount] | None = None,
) -> list[tuple[SocialPublication, dict[str, Any]]]:
    statement = select(SocialPublication)
    if platform in ("instagram", "tiktok"):
        statement = statement.where(SocialPublication.platform == platform)
    elif platform not in ("all",):
        return []
    if account_id:
        statement = statement.where(SocialPublication.account_id == account_id)
    if not include_cancelled:
        statement = statement.where(SocialPublication.state != "cancelled")
    text = query.strip()[:200]
    if text:
        like = f"%{text}%"
        statement = statement.where(or_(SocialPublication.project_title.ilike(like), SocialPublication.account_label.ilike(like)))
    rows = list(db.scalars(statement).all())
    projects = {
        item.id: item
        for item in db.scalars(select(Project).where(Project.id.in_({row.project_id for row in rows}))).all()
    } if rows else {}
    known = known if known is not None else account_map(db)
    return [(row, serialize_library_item(row, project=projects.get(row.project_id), account=known.get(row.account_id))) for row in rows]


def project_publications(db: Session, project_id: str) -> list[dict[str, Any]]:
    """Every publication of one project across platforms/accounts (newest first), compact."""
    known = account_map(db)
    by_channel = {item.external_account_id: item for item in known.values() if item.platform == "youtube"}
    items: list[dict[str, Any]] = []
    from ..youtube.library import STATE_LABELS as YOUTUBE_LABELS
    from ..youtube.library import library_state

    now = datetime.now(UTC)
    for upload in db.scalars(select(YouTubeUpload).where(YouTubeUpload.project_id == project_id)).all():
        account = by_channel.get(upload.channel_id)
        if upload.youtube_video_id:
            state = library_state(upload, now)
            label = YOUTUBE_LABELS.get(state, state.capitalize())
        else:
            state = upload.state if upload.state in ("pending", "uploading") else "failed"
            label = STATE_LABELS[state]
        items.append({
            "id": upload.id,
            "platform": "youtube",
            "account_id": account.id if account else None,
            "account_label": account.display_name if account else upload.channel_id,
            "state": state,
            "state_label": label,
            "scheduled_at": aware(upload.publish_at),
            "published_at": aware(upload.published_at),
            "remote_url": f"https://www.youtube.com/shorts/{upload.youtube_video_id}" if upload.youtube_video_id else None,
            "render_revision": upload.render_revision,
            "created_at": aware(upload.created_at),
            "active": upload.idempotency_key is not None,
        })
    for row in db.scalars(select(SocialPublication).where(SocialPublication.project_id == project_id)).all():
        account = known.get(row.account_id)
        items.append({
            "id": row.id,
            "platform": row.platform,
            "account_id": row.account_id,
            "account_label": (f"@{account.handle}" if account is not None and account.handle else (account.display_name if account else row.account_label)),
            "state": row.state,
            "state_label": STATE_LABELS.get(row.state, row.state),
            "scheduled_at": aware(row.scheduled_at),
            "published_at": aware(row.published_at),
            "remote_url": row.remote_url,
            "render_revision": row.render_revision,
            "created_at": aware(row.created_at),
            "active": row.idempotency_key is not None,
            "error": serialize_error(row),
            "actions": actions(row),
        })
    items.sort(key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return items
