"""Instagram/TikTok publications: render-bound records and their execution.

A publication is bound to the exact video the user selected: project,
project revision, render revision and the SHA-256 of the canonical final
master (``exporter.resolve_final_master`` - the same file YouTube uploads,
with narration, music and the current audio settings; nothing is re-rendered
or copied).  Before any byte is sent the bound video is resolved again and
re-hashed; a missing or different file fails closed (never a silent upload
of a newer render).

Lifecycle (``state``)::

    scheduled --due--> uploading --bytes sent--> processing --> published
    pending ---------/        \\--transient error--> pending (bounded backoff)
                               \\--permanent error--> failed
    scheduled/pending --backend was offline past the grace window--> missed
    scheduled/pending/missed --user--> cancelled

Execution is exclusive (``lease_until`` claim) and idempotent:
``idempotency_key`` (platform, account, project, render SHA) is unique while
a publication of that render on that account is scheduled, running or
published, and a published/failed row is never executed again.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Project, PublishingAccount, SocialPublication
from ..security.secrets import SecretStore
from ..services import effective_revision_state, get_project
from ..social_metadata import normalize_hashtag, normalize_hashtags
from ..youtube.publishing import ScheduleChoice, resolve_schedule, synthetic_suggestion
from ..youtube.uploads import UploadRefused, UploadSource, resolve_upload_source
from . import accounts, connections
from . import instagram as ig
from . import tiktok as tt
from .capabilities import CAPTION_LIMIT, INSTAGRAM_HASHTAG_LIMIT, account_capabilities
from .errors import AUTH_CODES, PublishingApiError

logger = logging.getLogger(__name__)

SOCIAL_PLATFORMS = ("instagram", "tiktok")
STATES = ("pending", "scheduled", "uploading", "processing", "published", "failed", "cancelled", "missed")
# Rows that still hold their render's idempotency key.
HOLDS_KEY = ("pending", "scheduled", "uploading", "processing", "published", "missed")
RUNNABLE = ("pending", "scheduled")
STATE_LABELS = {
    "pending": "Waiting to upload",
    "scheduled": "Scheduled",
    "uploading": "Uploading",
    "processing": "Processing",
    "published": "Published",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "missed": "Missed (ClipForge was offline)",
}
LEASE = timedelta(minutes=15)
RETRY_BASE = timedelta(minutes=1)
RETRY_CAP = timedelta(minutes=30)
POLL_FIRST = timedelta(seconds=5)
POLL_CAP = timedelta(seconds=60)
PROCESSING_TIMEOUT = {"instagram": timedelta(hours=2), "tiktok": timedelta(hours=24)}
OFFLINE_NOTICE = (
    "Instagram and TikTok have no scheduled-publishing API, so ClipForge publishes at the scheduled time "
    "itself - only while the ClipForge backend is running with internet access. If your Mac is off or asleep "
    "at that time, the post is marked Missed and waits for you (it is never published late on its own)."
)
TIKTOK_MUSIC_CONFIRMATION = "By posting, you agree to TikTok's Music Usage Confirmation."


class PublicationRefused(RuntimeError):
    def __init__(self, code: str, message: str, publication: SocialPublication | None = None, *, issues: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.publication = publication
        self.issues = issues or []


@dataclass
class Apis:
    """The provider boundaries (real ones in the app, fakes in tests)."""

    tiktok: tt.TikTokApi
    instagram: ig.InstagramApi


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class InstagramOptions(BaseModel):
    caption: str = Field(default="", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=60)
    share_to_feed: bool = True
    # Reel cover = a frame of the video (``thumb_offset``, milliseconds).
    cover_frame_ms: int | None = Field(default=None, ge=0, le=15 * 60 * 1000)


class TikTokOptions(BaseModel):
    # TikTok's post "title" is the caption; hashtags belong in it.
    caption: str = Field(default="", max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=60)
    # No default: TikTok requires the creator to choose (from creator_info).
    privacy_level: str | None = Field(default=None, max_length=40)
    allow_comments: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    cover_frame_ms: int | None = Field(default=None, ge=0, le=60 * 60 * 1000)
    # AI-generated content disclosure (``post_info.is_aigc``); must be answered.
    is_aigc: bool | None = None
    brand_content: bool = False
    brand_organic: bool = False
    music_usage_confirmed: bool = False


class PublicationRequest(BaseModel):
    account_id: str = Field(max_length=36)
    base_revision: int
    mode: Literal["now", "schedule"] = "now"
    schedule: ScheduleChoice | None = None
    instagram: InstagramOptions | None = None
    tiktok: TikTokOptions | None = None
    # Publish a render again on the same account after it was published.
    force_new: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    return accounts.aware(value)


def _event(publication: SocialPublication, state: str, note: str | None = None, *, now: datetime | None = None) -> None:
    events = list(publication.events or [])
    events.append({"at": (now or _now()).isoformat(), "state": state, **({"note": note[:300]} if note else {})})
    publication.events = events[-40:]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clean_hashtags(values: list[str]) -> list[str]:
    """Valid, de-duplicated hashtags in the user's order (no silent cap: the
    platform limit is checked by the preflight instead)."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        token = normalize_hashtag(value)
        if token is not None and token.casefold() not in seen:
            seen.add(token.casefold())
            result.append(token)
    return result


def compose_caption(caption: str, hashtags: list[str]) -> str:
    """Caption text plus the hashtags it does not already contain."""
    text = (caption or "").replace("\r\n", "\n").strip()
    present = {token.casefold() for token in re.findall(r"#[\w-]+", text, flags=re.UNICODE)}
    extra = [tag for tag in clean_hashtags(hashtags) if tag.casefold() not in present]
    if not extra:
        return text
    return f"{text}\n\n{' '.join(extra)}".strip()


def caption_hashtag_count(text: str) -> int:
    return len(re.findall(r"#[\w-]+", text or "", flags=re.UNICODE))


def idempotency_key(platform: str, account_id: str, project_id: str, render_sha256: str) -> str:
    return f"{platform}:{account_id}:{project_id}:{render_sha256}"


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def video_duration(state: dict[str, Any]) -> float | None:
    for value in ((state.get("duration") or {}).get("actual_seconds"), (state.get("timeline") or {}).get("duration")):
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return None


def platform_metadata(state: dict[str, Any], platform: str, project_title: str) -> dict[str, Any]:
    """ClipForge's own generated metadata for this platform (never YouTube's)."""
    platforms = ((state.get("social_metadata") or {}).get("platforms") or {})
    entry = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
    description = str(entry.get("description") or "").strip()
    title = str(entry.get("title") or "").strip()
    caption = description or title or project_title
    return {"caption": caption, "hashtags": normalize_hashtags(entry.get("hashtags") or []), "source": "social_metadata" if entry else "project_title"}


def poster_url(state: dict[str, Any] | None) -> str | None:
    thumbnails = (state or {}).get("thumbnails") if isinstance((state or {}).get("thumbnails"), dict) else {}
    variants = [item for item in thumbnails.get("variants") or [] if isinstance(item, dict) and item.get("url")]
    selected = next((item for item in variants if item.get("id") == thumbnails.get("selected_variant_id")), None)
    choice = selected or (variants[0] if variants else None)
    return str(choice["url"]) if choice else None


def _account(db: Session, account_id: str) -> PublishingAccount:
    account = accounts.get_account(db, account_id)
    if account is None or account.platform not in SOCIAL_PLATFORMS:
        raise PublicationRefused("unknown_account", "Choose a connected Instagram or TikTok account.")
    if not accounts.is_active(account):
        raise PublicationRefused("not_connected", "This account is disconnected. Reconnect it in Settings → Integrations.")
    return account


# ---------------------------------------------------------------------------
# Validation (the preflight)
# ---------------------------------------------------------------------------


def validate(
    account: PublishingAccount,
    request: PublicationRequest,
    *,
    config: dict[str, Any],
    creator: tt.CreatorInfo | None,
    duration: float | None,
    now: datetime | None = None,
) -> tuple[list[dict[str, str]], Any]:
    """Everything that must be fixed before anything is sent, in plain words."""
    issues: list[dict[str, str]] = []

    def need(field: str, message: str) -> None:
        issues.append({"field": field, "message": message})

    caps = account_capabilities(account, config=config)
    if not caps["upload"]:
        blocking = next((item["message"] for item in account.restrictions or [] if item.get("blocks_publishing")), None)
        need("account", blocking or account.last_error_message or "This account cannot publish right now. Reconnect it in Settings → Integrations.")
    resolution = None
    if request.mode == "schedule":
        if request.schedule is None:
            need("schedule", "Choose a date and time.")
        else:
            resolution = resolve_schedule(request.schedule, now=now)
            if resolution.status != "ok":
                need("schedule", resolution.message or "Choose a valid time.")
    if account.platform == "instagram":
        options = request.instagram
        if options is None:
            need("caption", "Add the Instagram post settings.")
            return issues, resolution
        caption = compose_caption(options.caption, options.hashtags)
        if len(caption) > CAPTION_LIMIT:
            need("caption", f"Instagram captions are limited to {CAPTION_LIMIT} characters (now {len(caption)}).")
        if caption_hashtag_count(caption) > INSTAGRAM_HASHTAG_LIMIT:
            need("hashtags", f"Instagram allows at most {INSTAGRAM_HASHTAG_LIMIT} hashtags.")
        if duration is not None and options.cover_frame_ms is not None and options.cover_frame_ms > duration * 1000:
            need("cover", "The cover frame is after the end of the video.")
        if duration is not None and not 3 <= duration <= 15 * 60:
            need("video", "Instagram Reels must be between 3 seconds and 15 minutes long.")
        return issues, resolution
    options = request.tiktok
    if options is None:
        need("caption", "Add the TikTok post settings.")
        return issues, resolution
    caption = compose_caption(options.caption, options.hashtags)
    if utf16_length(caption) > CAPTION_LIMIT:
        need("caption", f"TikTok captions are limited to {CAPTION_LIMIT} characters.")
    if creator is None:
        need("account", "TikTok's creator settings could not be loaded. Try again.")
        return issues, resolution
    if not options.privacy_level:
        need("privacy_level", "Choose who can view this video.")
    elif options.privacy_level not in creator.privacy_level_options:
        need("privacy_level", "TikTok does not offer this privacy option for this account right now.")
    elif not config.get("app_audited") and options.privacy_level != tt.PRIVATE_LEVEL:
        # Never silently downgraded: the user must choose "Only me" themselves.
        need("privacy_level", "Public Direct Post requires TikTok app approval. Until the app passes TikTok's audit, choose \"Only me\".")
    if options.allow_comments and creator.comment_disabled:
        need("allow_comments", "Comments are turned off for this account in TikTok.")
    if options.allow_duet and creator.duet_disabled:
        need("allow_duet", "Duet is turned off for this account in TikTok.")
    if options.allow_stitch and creator.stitch_disabled:
        need("allow_stitch", "Stitch is turned off for this account in TikTok.")
    if options.is_aigc is None:
        need("is_aigc", "Answer whether this video should carry TikTok's AI-generated content label.")
    if options.brand_content and options.privacy_level == tt.PRIVATE_LEVEL:
        need("brand_content", "Branded content cannot be posted as \"Only me\" on TikTok.")
    if not options.music_usage_confirmed:
        need("music_usage_confirmed", TIKTOK_MUSIC_CONFIRMATION)
    limit = creator.max_video_post_duration_sec
    if duration is not None and limit and duration > limit:
        need("video", f"This account can post videos up to {limit} seconds; this video is {duration:.0f} seconds.")
    if duration is not None and options.cover_frame_ms is not None and options.cover_frame_ms > duration * 1000:
        need("cover", "The cover frame is after the end of the video.")
    return issues, resolution


def _creator_info(db: Session, settings: Settings, store: SecretStore, apis: Apis, account: PublishingAccount) -> tt.CreatorInfo:
    token = connections.access_token(db, settings, store, account=account, tiktok_api=apis.tiktok)
    creator = apis.tiktok.creator_info(token)
    details = dict(account.details or {})
    details["creator_info"] = creator.as_dict()
    account.details = details
    if creator.username and creator.username != account.handle:
        account.handle = creator.username
    db.commit()
    return creator


def _source(project: Project, settings: Settings) -> UploadSource:
    try:
        return resolve_upload_source(project, settings)
    except UploadRefused as exc:
        raise PublicationRefused(exc.code, exc.message.replace(" to YouTube", "")) from exc


# ---------------------------------------------------------------------------
# Draft (what the unified sheet shows for an Instagram/TikTok account)
# ---------------------------------------------------------------------------


def draft(db: Session, project: Project, account: PublishingAccount, settings: Settings, store: SecretStore, apis: Apis) -> dict[str, Any]:
    state = effective_revision_state(project)
    config = accounts.platform_config(db, account.platform)
    caps = account_capabilities(account, config=config)
    meta = platform_metadata(state, account.platform, project.title)
    render_status = {"uploadable": True, "code": None, "message": None}
    try:
        source = resolve_upload_source(project, settings)
        render_status["render_revision"] = source.render_revision
    except UploadRefused as exc:
        render_status = {"uploadable": False, "code": exc.code, "message": exc.message.replace(" to YouTube", "")}
    duration = video_duration(state)
    from ..youtube.fingerprint import build_fingerprint

    render = state.get("render") if isinstance(state.get("render"), dict) else {}
    render_revision = int(render.get("revision") or project.current_revision)
    fingerprint_state = {rev.number: rev.state for rev in project.revisions}.get(render_revision, state)
    synthetic = synthetic_suggestion(build_fingerprint(fingerprint_state, project_id=project.id, render_revision=render_revision, render_sha256="", file_size=0))
    restrictions = list(account.restrictions or [])
    creator_payload: dict[str, Any] | None = None
    creator_error: dict[str, str] | None = None
    if account.platform == "tiktok" and caps["upload"]:
        try:
            creator_payload = _creator_info(db, settings, store, apis, account).as_dict()
        except PublishingApiError as exc:
            creator_error = {"code": exc.code, "message": exc.message}
    if account.platform == "instagram":
        warning = connections.instagram_token_warning(account)
        if warning:
            restrictions.append(warning)
    options: dict[str, Any] = {"caption": meta["caption"], "hashtags": meta["hashtags"]}
    if account.platform == "instagram":
        options |= {"share_to_feed": True, "cover_frame_ms": None}
    else:
        options |= {
            "privacy_level": None, "allow_comments": False, "allow_duet": False, "allow_stitch": False,
            "cover_frame_ms": None, "is_aigc": synthetic["value"], "brand_content": False, "brand_organic": False,
            "music_usage_confirmed": False,
        }
    return {
        "account": accounts.serialize_account(account, caps) | {"restrictions": restrictions},
        "platform": account.platform,
        "capabilities": caps,
        "options": options,
        "metadata_source": meta["source"],
        "creator_info": creator_payload,
        "creator_info_error": creator_error,
        "ai_suggestion": synthetic,
        "render_status": render_status,
        "duration_seconds": duration,
        "caption_limit": CAPTION_LIMIT,
        "hashtag_limit": INSTAGRAM_HASHTAG_LIMIT if account.platform == "instagram" else None,
        "scheduling": {"mode": "clipforge", "notice": OFFLINE_NOTICE, "grace_minutes": settings.publishing_missed_grace_minutes},
        "music_usage_confirmation": TIKTOK_MUSIC_CONFIRMATION if account.platform == "tiktok" else None,
        "app_audited": bool(config.get("app_audited")) if account.platform == "tiktok" else None,
        "existing": [serialize(row) for row in project_rows(db, project.id, account_id=account.id)],
    }


def preflight(
    db: Session, project: Project, settings: Settings, store: SecretStore, apis: Apis, request: PublicationRequest, *, now: datetime | None = None,
) -> tuple[list[dict[str, str]], Any, PublishingAccount, tt.CreatorInfo | None]:
    account = _account(db, request.account_id)
    config = accounts.platform_config(db, account.platform)
    creator = None
    issues: list[dict[str, str]] = []
    if account.platform == "tiktok":
        try:
            creator = _creator_info(db, settings, store, apis, account)
        except PublishingApiError as exc:
            issues.append({"field": "account", "message": exc.message})
    state = effective_revision_state(project)
    try:
        resolve_upload_source(project, settings)
    except UploadRefused as exc:
        issues.append({"field": "video", "message": exc.message.replace(" to YouTube", "")})
    found, resolution = validate(account, request, config=config, creator=creator, duration=video_duration(state), now=now)
    if creator is None and account.platform == "tiktok":
        found = [item for item in found if item["field"] != "account" or "creator settings" not in item["message"]]
    return issues + found, resolution, account, creator


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def _snapshot(account: PublishingAccount, request: PublicationRequest, creator: tt.CreatorInfo | None) -> dict[str, Any]:
    if account.platform == "instagram":
        options = request.instagram or InstagramOptions()
        return {
            "platform": "instagram",
            "caption": compose_caption(options.caption, options.hashtags),
            "caption_text": options.caption,
            "hashtags": clean_hashtags(options.hashtags),
            "share_to_feed": options.share_to_feed,
            "cover_frame_ms": options.cover_frame_ms,
        }
    options = request.tiktok or TikTokOptions()
    return {
        "platform": "tiktok",
        "caption": compose_caption(options.caption, options.hashtags),
        "caption_text": options.caption,
        "hashtags": clean_hashtags(options.hashtags),
        "privacy_level": options.privacy_level,
        "allow_comments": options.allow_comments,
        "allow_duet": options.allow_duet,
        "allow_stitch": options.allow_stitch,
        "cover_frame_ms": options.cover_frame_ms,
        "is_aigc": bool(options.is_aigc),
        "brand_content": options.brand_content,
        "brand_organic": options.brand_organic,
        "music_usage_confirmed": options.music_usage_confirmed,
        "creator_info_at_request": creator.as_dict() if creator else None,
    }


def request_publication(
    db: Session,
    project: Project,
    settings: Settings,
    store: SecretStore,
    apis: Apis,
    request: PublicationRequest,
    *,
    now: datetime | None = None,
) -> tuple[SocialPublication, bool]:
    """Create the publication of the current final video; ``(row, should_run_now)``."""
    now = now or _now()
    if request.base_revision != project.current_revision:
        raise PublicationRefused("revision_conflict", "Project changed; reload before publishing.")
    issues, resolution, account, creator = preflight(db, project, settings, store, apis, request, now=now)
    if issues:
        raise PublicationRefused("preflight_failed", f"{len(issues)} item{'s' if len(issues) != 1 else ''} need{'s' if len(issues) == 1 else ''} attention.", issues=issues)
    source = _source(project, settings)
    sha = file_sha256(source.path)
    key = idempotency_key(account.platform, account.id, project.id, sha)
    existing = db.scalar(select(SocialPublication).where(SocialPublication.idempotency_key == key))
    if existing is not None:
        if existing.state in ("published", "failed") and request.force_new:
            existing.idempotency_key = None  # history stays; a deliberate second post
            db.commit()
        elif existing.state == "published":
            raise PublicationRefused("already_published", "This exact video is already published on this account.", existing)
        elif existing.state == "failed":
            raise PublicationRefused("unknown_outcome", "An earlier attempt may have been published. Check the app first; to post again anyway, choose \"Publish again\".", existing)
        else:
            raise PublicationRefused("already_scheduled", "This exact video is already scheduled or being published on this account.", existing)
    scheduled = request.mode == "schedule" and resolution is not None
    row = SocialPublication(
        platform=account.platform,
        account_id=account.id,
        external_account_id=account.external_account_id,
        account_label=accounts.label(account)[:200],
        project_id=project.id,
        project_title=(project.title or "")[:200],
        project_revision=source.project_revision,
        render_revision=source.render_revision,
        render_sha256=sha,
        render_file_size=source.path.stat().st_size,
        source_kind=source.kind,
        idempotency_key=key,
        state="scheduled" if scheduled else "pending",
        mode="schedule" if scheduled else "now",
        scheduled_at=resolution.publish_at if scheduled else None,
        schedule_timezone=resolution.timezone if scheduled else None,
        schedule_local_time=resolution.local_time if scheduled else None,
        metadata_snapshot={**_snapshot(account, request, creator), "poster_url": poster_url(source.current_state)},
        events=[],
    )
    _event(row, row.state, "requested", now=now)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(select(SocialPublication).where(SocialPublication.idempotency_key == key))
        raise PublicationRefused("already_scheduled", "This exact video is already scheduled or being published on this account.", raced) from None
    db.refresh(row)
    return row, not scheduled


# ---------------------------------------------------------------------------
# User actions
# ---------------------------------------------------------------------------


def cancel(db: Session, row: SocialPublication, *, now: datetime | None = None) -> SocialPublication:
    if row.state not in ("pending", "scheduled", "missed") or row.remote_container_id:
        raise PublicationRefused("not_cancellable", "Only a publication that has not started uploading can be cancelled.", row)
    now = now or _now()
    row.state = "cancelled"
    row.cancelled_at = now
    row.idempotency_key = None
    row.next_attempt_at = None
    _event(row, "cancelled", "cancelled by the user", now=now)
    db.commit()
    return row


def publish_missed_now(db: Session, row: SocialPublication, *, now: datetime | None = None) -> SocialPublication:
    """The user's explicit decision to publish a missed post now."""
    if row.state != "missed":
        raise PublicationRefused("not_missed", "Only a missed publication can be published from here.", row)
    now = now or _now()
    row.state = "pending"
    row.mode = "now"
    # Due now (not at the missed time), so it is not marked missed again.
    row.next_attempt_at = now
    row.scheduled_at = None
    row.last_error_code = None
    row.last_error_message = None
    _event(row, "pending", "publish now (after a missed schedule)", now=now)
    db.commit()
    return row


def reschedule(db: Session, row: SocialPublication, choice: ScheduleChoice, *, now: datetime | None = None) -> SocialPublication:
    if row.state not in ("scheduled", "missed", "pending") or row.remote_container_id:
        raise PublicationRefused("not_reschedulable", "Only a publication that has not started uploading can be rescheduled.", row)
    now = now or _now()
    resolution = resolve_schedule(choice, now=now)
    if resolution.status != "ok":
        raise PublicationRefused("invalid_schedule", resolution.message or "Choose a valid time.", row)
    row.state = "scheduled"
    row.mode = "schedule"
    row.scheduled_at = resolution.publish_at
    row.schedule_timezone = resolution.timezone
    row.schedule_local_time = resolution.local_time
    row.next_attempt_at = None
    row.last_error_code = None
    row.last_error_message = None
    _event(row, "scheduled", f"rescheduled to {resolution.local_time} {resolution.timezone}", now=now)
    db.commit()
    return row


def retry_as_new(db: Session, row: SocialPublication, *, now: datetime | None = None) -> SocialPublication:
    """A failed attempt stays as it is; a retry is a new attempt of the same bound render."""
    if row.state != "failed":
        raise PublicationRefused("not_retryable", "Only a failed publication can be retried.", row)
    if row.idempotency_key is not None:
        raise PublicationRefused("unknown_outcome", "The provider may have published this attempt. Check the app first; to post again anyway, use Upload with \"Publish again\".", row)
    now = now or _now()
    key = idempotency_key(row.platform, row.account_id, row.project_id, row.render_sha256)
    if db.scalar(select(SocialPublication.id).where(SocialPublication.idempotency_key == key)) is not None:
        raise PublicationRefused("already_scheduled", "This video is already scheduled or published on this account.", row)
    clone = SocialPublication(
        platform=row.platform, account_id=row.account_id, external_account_id=row.external_account_id,
        account_label=row.account_label, project_id=row.project_id, project_title=row.project_title,
        project_revision=row.project_revision, render_revision=row.render_revision, render_sha256=row.render_sha256,
        render_file_size=row.render_file_size, source_kind=row.source_kind, idempotency_key=key,
        state="pending", mode="now", metadata_snapshot=dict(row.metadata_snapshot or {}), events=[],
    )
    _event(clone, "pending", f"retry of {row.id}", now=now)
    db.add(clone)
    db.commit()
    db.refresh(clone)
    return clone


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _backoff(attempt: int) -> timedelta:
    return min(RETRY_CAP, RETRY_BASE * (2 ** max(0, attempt - 1)))


def claim(db: Session, publication_id: str, *, now: datetime) -> bool:
    """Atomically take a due pending/scheduled row (one executor, one run)."""
    result = db.execute(
        update(SocialPublication)
        .where(
            SocialPublication.id == publication_id,
            SocialPublication.state.in_(RUNNABLE),
            or_(SocialPublication.scheduled_at.is_(None), SocialPublication.scheduled_at <= now),
            or_(SocialPublication.next_attempt_at.is_(None), SocialPublication.next_attempt_at <= now),
            or_(SocialPublication.lease_until.is_(None), SocialPublication.lease_until < now),
        )
        .values(state="uploading", lease_until=now + LEASE, attempt_count=SocialPublication.attempt_count + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def claim_poll(db: Session, publication_id: str, *, now: datetime) -> bool:
    result = db.execute(
        update(SocialPublication)
        .where(
            SocialPublication.id == publication_id,
            SocialPublication.state == "processing",
            or_(SocialPublication.next_attempt_at.is_(None), SocialPublication.next_attempt_at <= now),
            or_(SocialPublication.lease_until.is_(None), SocialPublication.lease_until < now),
        )
        .values(lease_until=now + LEASE, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _fail(db: Session, row: SocialPublication, code: str, message: str, *, now: datetime, outcome_unknown: bool = False) -> None:
    """A permanent failure.  The render's idempotency key is released (a new
    attempt is allowed) unless the provider may still have posted it."""
    row.state = "failed"
    row.last_error_code = code
    row.last_error_message = message[:500]
    row.lease_until = None
    row.next_attempt_at = None
    if not outcome_unknown:
        row.idempotency_key = None
    _event(row, "failed", f"{code}: {message}", now=now)
    db.commit()


def _transient(db: Session, row: SocialPublication, error: PublishingApiError, settings: Settings, *, now: datetime) -> None:
    """Bounded retry; the next attempt starts over (no partial post exists)."""
    if row.attempt_count >= max(1, settings.publishing_max_attempts):
        _fail(db, row, error.code, f"{error.message} (gave up after {row.attempt_count} attempts)", now=now)
        return
    row.state = "pending" if row.mode == "now" else "scheduled"
    row.remote_container_id = None
    row.bytes_uploaded = 0
    row.upload_complete = False
    row.lease_until = None
    row.next_attempt_at = now + _backoff(row.attempt_count)
    row.last_error_code = error.code
    row.last_error_message = error.message[:500]
    _event(row, row.state, f"retry after {error.code}", now=now)
    db.commit()


def _verify_binding(db: Session, row: SocialPublication, settings: Settings) -> UploadSource:
    """The bound final video must still exist and be byte-identical."""
    project = get_project(db, row.project_id)
    if project is None:
        raise PublicationRefused("project_missing", "The project of this post was deleted, so its video is gone.")
    try:
        source = resolve_upload_source(project, settings)
    except UploadRefused as exc:
        raise PublicationRefused(
            "render_missing",
            "The video this post was scheduled with is no longer the project's current final video (it was edited or is being re-rendered). "
            "ClipForge will not upload a different video: open Upload again to publish the current render.",
        ) from exc
    if source.render_revision != row.render_revision or file_sha256(source.path) != row.render_sha256:
        raise PublicationRefused(
            "render_changed",
            "The final video changed since this post was scheduled (re-rendered or new audio). ClipForge will not upload a different video: "
            "open Upload again to publish the current render.",
        )
    return source


def run(
    db: Session,
    publication_id: str,
    settings: Settings,
    store: SecretStore,
    apis: Apis,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _now,
) -> SocialPublication | None:
    """Upload one due publication.  Never raises; outcomes are persisted.

    With an explicit ``now`` (the scheduler's tick time) every timestamp of
    this run uses it, so retries and polls are scheduled on one clock."""
    if now is not None:
        fixed = now
        clock = lambda: fixed
    now = now or clock()
    if not claim(db, publication_id, now=now):
        return db.get(SocialPublication, publication_id)
    row = db.get(SocialPublication, publication_id)
    if row is None:
        return None
    db.refresh(row)
    _event(row, "uploading", f"attempt {row.attempt_count}", now=now)
    db.commit()
    try:
        account = db.get(PublishingAccount, row.account_id)
        if account is None or account.external_account_id != row.external_account_id:
            raise PublicationRefused("account_changed", "The account this post was prepared for no longer exists.")
        if not accounts.is_active(account):
            raise PublicationRefused("not_connected", "The account this post was prepared for is disconnected. Reconnect it, then publish again.")
        source = _verify_binding(db, row, settings)
        token = connections.access_token(db, settings, store, account=account, tiktok_api=apis.tiktok)
        if row.platform == "tiktok":
            _start_tiktok(db, row, account, token, source, settings, apis, clock=clock)
        else:
            _start_instagram(db, row, account, token, source, settings, apis, clock=clock)
    except PublicationRefused as exc:
        _fail(db, row, exc.code, exc.message, now=clock())
    except PublishingApiError as exc:
        if exc.retryable:
            _transient(db, row, exc, settings, now=clock())
        else:
            _fail(db, row, exc.code, exc.message, now=clock())
    except OSError:
        _fail(db, row, "render_missing", "The final video could not be read.", now=clock())
    except Exception:  # a publishing failure must never corrupt project state
        logger.exception("Publication failed unexpectedly publication_id=%s", publication_id)
        db.rollback()
        row = db.get(SocialPublication, publication_id)
        if row is not None:
            _fail(db, row, "internal_error", "Publishing failed unexpectedly.", now=clock())
    if row is not None:
        db.refresh(row)
    return row


def _renew(db: Session, row: SocialPublication, now: datetime) -> None:
    row.lease_until = now + LEASE
    db.commit()


def _start_tiktok(db: Session, row: SocialPublication, account: PublishingAccount, token: str, source: UploadSource, settings: Settings, apis: Apis, *, clock: Callable[[], datetime]) -> None:
    snapshot = row.metadata_snapshot or {}
    config = accounts.platform_config(db, "tiktok")
    # creator_info is queried before every Direct Post and is authoritative.
    creator = apis.tiktok.creator_info(token)
    details = dict(account.details or {})
    details["creator_info"] = creator.as_dict()
    account.details = details
    db.commit()
    privacy = snapshot.get("privacy_level")
    if privacy not in creator.privacy_level_options:
        raise PublicationRefused("invalid_option", "TikTok no longer offers the chosen privacy option for this account. Open Upload again to choose from the current options.")
    if not config.get("app_audited") and privacy != tt.PRIVATE_LEVEL:
        raise PublicationRefused("unaudited_client", "Public Direct Post requires TikTok app approval. The post was not published (ClipForge never downgrades privacy on its own).")
    for flag, disabled in (("allow_comments", creator.comment_disabled), ("allow_duet", creator.duet_disabled), ("allow_stitch", creator.stitch_disabled)):
        if snapshot.get(flag) and disabled:
            raise PublicationRefused("invalid_option", f"{flag.split('_', 1)[1].capitalize()} is now turned off for this TikTok account. Open Upload again.")
    duration = video_duration(source.current_state)
    if duration and creator.max_video_post_duration_sec and duration > creator.max_video_post_duration_sec:
        raise PublicationRefused("invalid_media", f"This account can post videos up to {creator.max_video_post_duration_sec} seconds.")
    post_info: dict[str, Any] = {
        "title": snapshot.get("caption") or "",
        "privacy_level": privacy,
        "disable_comment": not snapshot.get("allow_comments"),
        "disable_duet": not snapshot.get("allow_duet"),
        "disable_stitch": not snapshot.get("allow_stitch"),
        "brand_content_toggle": bool(snapshot.get("brand_content")),
        "brand_organic_toggle": bool(snapshot.get("brand_organic")),
        "is_aigc": bool(snapshot.get("is_aigc")),
    }
    if snapshot.get("cover_frame_ms") is not None:
        post_info["video_cover_timestamp_ms"] = int(snapshot["cover_frame_ms"])
    size = row.render_file_size
    chunk_size, total = tt.chunk_plan(size)
    publish_id, upload_url = apis.tiktok.init_direct_post(token, post_info, {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": total,
    })
    row.remote_container_id = publish_id
    row.bytes_uploaded = 0
    _event(row, "uploading", "TikTok post initialized", now=clock())
    db.commit()
    with source.path.open("rb") as handle:
        for first, last in tt.chunk_ranges(size, chunk_size, total):
            handle.seek(first)
            data = handle.read(last - first + 1)
            if len(data) != last - first + 1:
                raise OSError("short read")
            if last == size - 1:
                row.upload_complete = True  # final bytes in flight: never re-initialize blindly
                db.commit()
            try:
                apis.tiktok.upload_chunk(upload_url, data, first, last, size)
            except PublishingApiError:
                if last == size - 1:
                    # The final chunk's outcome is unknown; the status poll decides.
                    row.state = "processing"
                    row.lease_until = None
                    row.next_attempt_at = clock() + POLL_FIRST
                    _event(row, "processing", "final chunk outcome unknown; checking TikTok", now=clock())
                    db.commit()
                    return
                row.upload_complete = False
                raise
            row.bytes_uploaded = last + 1
            _renew(db, row, clock())
    row.state = "processing"
    row.lease_until = None
    row.next_attempt_at = clock() + POLL_FIRST
    row.last_error_code = None
    row.last_error_message = None
    _event(row, "processing", "video sent to TikTok", now=clock())
    db.commit()


def _start_instagram(db: Session, row: SocialPublication, account: PublishingAccount, token: str, source: UploadSource, settings: Settings, apis: Apis, *, clock: Callable[[], datetime]) -> None:
    snapshot = row.metadata_snapshot or {}
    app = connections.meta_app(settings, accounts.platform_config(db, "instagram"))
    if app is None:
        raise PublicationRefused("client_not_configured", "The Meta app is not configured.")
    params: dict[str, Any] = {"caption": snapshot.get("caption") or "", "share_to_feed": bool(snapshot.get("share_to_feed", True))}
    if snapshot.get("cover_frame_ms") is not None:
        params["thumb_offset"] = int(snapshot["cover_frame_ms"])
    container, upload_uri = apis.instagram.create_reel_container(app, token, account.external_account_id, params)
    row.remote_container_id = container
    _event(row, "uploading", "Reel container created", now=clock())
    db.commit()
    apis.instagram.upload_video(app, token, upload_uri, source.path, row.render_file_size)
    row.bytes_uploaded = row.render_file_size
    row.upload_complete = True
    row.state = "processing"
    row.lease_until = None
    row.next_attempt_at = clock() + POLL_FIRST
    row.last_error_code = None
    row.last_error_message = None
    _event(row, "processing", "video sent to Instagram", now=clock())
    db.commit()


def _poll_later(db: Session, row: SocialPublication, now: datetime, *, note: str | None = None, error: PublishingApiError | None = None) -> None:
    started = _processing_since(row) or now
    waited = max(POLL_FIRST, min(POLL_CAP, (now - started) / 4 if now > started else POLL_FIRST))
    row.next_attempt_at = now + waited
    row.lease_until = None
    if error is not None:
        row.last_error_code = error.code
        row.last_error_message = error.message[:500]
    if note:
        _event(row, "processing", note, now=now)
    db.commit()


def _processing_since(row: SocialPublication) -> datetime | None:
    for event in reversed(row.events or []):
        if event.get("state") == "uploading":
            try:
                return datetime.fromisoformat(event["at"])
            except (KeyError, ValueError):
                return None
    return aware(row.created_at)


def poll(
    db: Session,
    publication_id: str,
    settings: Settings,
    store: SecretStore,
    apis: Apis,
    *,
    now: datetime | None = None,
) -> SocialPublication | None:
    """Ask the provider about one processing publication (and publish an
    Instagram container only once Meta reports FINISHED).  Never raises."""
    now = now or _now()
    if not claim_poll(db, publication_id, now=now):
        return db.get(SocialPublication, publication_id)
    row = db.get(SocialPublication, publication_id)
    if row is None:
        return None
    db.refresh(row)
    since = _processing_since(row) or now
    if now - since > PROCESSING_TIMEOUT[row.platform]:
        _fail(
            db, row, "status_unknown",
            f"{'TikTok' if row.platform == 'tiktok' else 'Instagram'} never confirmed this post. Check the app before publishing again.",
            now=now, outcome_unknown=row.upload_complete,
        )
        return row
    account = db.get(PublishingAccount, row.account_id)
    try:
        if account is None or account.external_account_id != row.external_account_id:
            raise PublicationRefused("account_changed", "The account this post was prepared for no longer exists.")
        token = connections.access_token(db, settings, store, account=account, tiktok_api=apis.tiktok)
        if row.platform == "tiktok":
            _poll_tiktok(db, row, account, token, apis, now=now)
        else:
            _poll_instagram(db, row, account, token, settings, apis, now=now)
    except PublicationRefused as exc:
        _fail(db, row, exc.code, exc.message, now=now)
    except PublishingApiError as exc:
        if exc.retryable or exc.code in AUTH_CODES or exc.code == "media_not_ready":
            # The video already reached the provider: keep asking (after a
            # reconnect for auth problems) instead of uploading it again.
            _poll_later(db, row, now, error=exc)
        else:
            _fail(db, row, exc.code, exc.message, now=now)
    except Exception:
        logger.exception("Publication status check failed publication_id=%s", publication_id)
        db.rollback()
        row = db.get(SocialPublication, publication_id)
        if row is not None:
            _poll_later(db, row, now)
    if row is not None:
        db.refresh(row)
    return row


TIKTOK_FAIL_REASONS = {
    "file_format_check_failed": "TikTok could not read the video format.",
    "duration_check_failed": "The video is longer than this account may post.",
    "frame_rate_check_failed": "TikTok rejected the video's frame rate.",
    "picture_size_check_failed": "TikTok rejected the video's resolution.",
    "internal": "TikTok had an internal problem publishing the video.",
    "video_pull_failed": "TikTok could not receive the video.",
    "publish_cancelled": "The post was cancelled in TikTok.",
    "auth_removed": "ClipForge's access was removed from this TikTok account.",
    "spam_risk_too_many_posts": "TikTok's daily posting limit for this account was reached.",
    "spam_risk_user_banned_from_posting": "TikTok does not currently allow this account to post.",
    "spam_risk_text": "TikTok flagged the caption as spam.",
    "spam_risk": "TikTok flagged this post as spam risk.",
}


def _published(db: Session, row: SocialPublication, now: datetime, *, note: str) -> None:
    row.state = "published"
    row.published_at = now
    row.lease_until = None
    row.next_attempt_at = None
    row.last_error_code = None
    row.last_error_message = None
    _event(row, "published", note, now=now)
    db.commit()


def _poll_tiktok(db: Session, row: SocialPublication, account: PublishingAccount, token: str, apis: Apis, *, now: datetime) -> None:
    if not row.remote_container_id:
        raise PublicationRefused("internal_error", "This TikTok post has no publish id.")
    data = apis.tiktok.fetch_status(token, row.remote_container_id)
    status = str(data.get("status") or "")
    row.remote_status = status[:48] or None
    if status == "PUBLISH_COMPLETE":
        ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        post_id = str(ids[0]) if isinstance(ids, list) and ids else None
        row.remote_post_id = post_id
        handle = account.handle or ((account.details or {}).get("creator_info") or {}).get("username")
        row.remote_url = f"https://www.tiktok.com/@{handle}/video/{post_id}" if post_id and handle else None
        note = "published on TikTok" if post_id else "published on TikTok (private posts have no public id)"
        _published(db, row, now, note=note)
        return
    if status == "FAILED":
        reason = str(data.get("fail_reason") or "")
        _fail(db, row, f"tiktok_{reason or 'failed'}"[:64], TIKTOK_FAIL_REASONS.get(reason, f"TikTok could not publish the video ({reason or 'no reason given'})."), now=now)
        return
    uploaded = data.get("uploaded_bytes")
    if isinstance(uploaded, int) and uploaded >= row.render_file_size:
        row.upload_complete = True
    _poll_later(db, row, now)


def _poll_instagram(db: Session, row: SocialPublication, account: PublishingAccount, token: str, settings: Settings, apis: Apis, *, now: datetime) -> None:
    app = connections.meta_app(settings, accounts.platform_config(db, "instagram"))
    if app is None:
        raise PublicationRefused("client_not_configured", "The Meta app is not configured.")
    if not row.remote_container_id:
        raise PublicationRefused("internal_error", "This Instagram post has no media container.")
    status, detail = apis.instagram.container_status(app, token, row.remote_container_id)
    row.remote_status = status[:48]
    if status == "PUBLISHED":
        # Published by an earlier media_publish whose answer was lost.
        _published(db, row, now, note="Instagram reports the container as published")
        return
    if status in {"ERROR", "EXPIRED"}:
        message = "Instagram could not process the video." if status == "ERROR" else "Instagram's upload container expired before it was published (24 hours)."
        _fail(db, row, f"instagram_{status.lower()}", f"{message}{f' ({detail})' if detail else ''}", now=now)
        return
    if status != "FINISHED":
        _poll_later(db, row, now)
        return
    # Only now: Meta reported the container ready.  A container can be
    # published once, so repeating this after a lost answer cannot duplicate.
    row.publish_requested_at = now
    _event(row, "processing", "container FINISHED; publishing", now=now)
    db.commit()
    media_id = apis.instagram.publish_container(app, token, account.external_account_id, row.remote_container_id)
    row.remote_post_id = media_id
    db.commit()
    try:
        row.remote_url = apis.instagram.media_permalink(app, token, media_id)
    except PublishingApiError:
        row.remote_url = None
    _published(db, row, now, note="published on Instagram")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def project_rows(db: Session, project_id: str, *, account_id: str | None = None) -> list[SocialPublication]:
    query = select(SocialPublication).where(SocialPublication.project_id == project_id)
    if account_id:
        query = query.where(SocialPublication.account_id == account_id)
    return list(db.scalars(query.order_by(SocialPublication.created_at.desc())).all())


def actions(row: SocialPublication) -> dict[str, bool]:
    not_started = not row.remote_container_id
    return {
        "cancel": row.state in ("pending", "scheduled", "missed") and not_started,
        "reschedule": row.state in ("scheduled", "missed") and not_started,
        "publish_now": row.state == "missed",
        "retry": row.state == "failed" and row.idempotency_key is None,
    }


def serialize(row: SocialPublication) -> dict[str, Any]:
    snapshot = row.metadata_snapshot or {}
    return {
        "id": row.id,
        "platform": row.platform,
        "account_id": row.account_id,
        "account_label": row.account_label,
        "project_id": row.project_id,
        "project_title": row.project_title,
        "project_revision": row.project_revision,
        "render_revision": row.render_revision,
        "render_sha256": row.render_sha256,
        "state": row.state,
        "state_label": STATE_LABELS.get(row.state, row.state),
        "mode": row.mode,
        "scheduled_at": aware(row.scheduled_at),
        "schedule_timezone": row.schedule_timezone,
        "schedule_local_time": row.schedule_local_time,
        "published_at": aware(row.published_at),
        "created_at": aware(row.created_at),
        "updated_at": aware(row.updated_at),
        "remote_post_id": row.remote_post_id,
        "remote_url": row.remote_url,
        "remote_status": row.remote_status,
        "attempt_count": row.attempt_count,
        "next_attempt_at": aware(row.next_attempt_at),
        "error": {"code": row.last_error_code, "message": row.last_error_message} if row.last_error_code else None,
        "caption": snapshot.get("caption"),
        "privacy_level": snapshot.get("privacy_level"),
        "is_aigc": snapshot.get("is_aigc"),
        "events": list(row.events or [])[-10:],
        "actions": actions(row),
        "requires_running_backend": row.state in ("scheduled", "pending"),
    }
