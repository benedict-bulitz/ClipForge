"""The one publishing-schedule authority (Smart Slot Planner V1).

* **Cadence** - ``YouTubePublishingSchedule`` + ``YouTubeScheduleSlot``: videos
  per day, IANA zone, preferred local slots (seed / manual / learned), per
  channel.  Learned slots are only ever applied by the user.
* **What is really on the channel** - ``YouTubeScheduleEntry``: a bounded
  cache filled from the channel's uploads playlist + ``videos.list``.  YouTube
  is authoritative, so videos scheduled in YouTube Studio or by other tools
  count exactly like ClipForge's own; ``youtube_uploads`` is never used as
  the source of the remote schedule.
* **ClipForge's in-flight claims** - ``YouTubeSlotReservation``:
  reserved -> confirmed (YouTube returned the publishAt) or released (failed,
  rejected, cancelled, deleted, expired).  A slot is never permanently
  occupied before YouTube confirms it.

The slot selection itself lives in ``slots`` (the one algorithm).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    YouTubePublishingSchedule,
    YouTubeScheduleEntry,
    YouTubeScheduleSlot,
    YouTubeSlotReservation,
    YouTubeUpload,
)
from ..security.secrets import SecretStore
from . import slots as planner
from .connection import access_token
from .provider import PAGE_SIZE, YouTubeApiError, YouTubeProvider
from .publishing import ScheduleChoice, load_defaults, resolve_schedule, valid_timezone

logger = logging.getLogger(__name__)

# Freshness of the cached YouTube schedule.
FRESH_FOR = timedelta(minutes=10)  # opening the sheet refreshes anything older
CONFLICT_CHECK_MAX_AGE = timedelta(seconds=60)  # the final write re-checks anything older
CACHE_USABLE_FOR = timedelta(hours=6)  # older caches are never offered for automatic slots
MIN_SYNC_GAP = timedelta(seconds=20)  # page reloads cannot hammer the API
# Bounded scan: the uploads playlist is read newest-first until a whole page
# was uploaded before LOOKBACK (or MAX_PAGES), never the entire channel.
LOOKBACK = timedelta(days=60)
MAX_PAGES = 20
KEEP_PAST = timedelta(days=2)  # enough to count today's published videos in every zone
RESERVATION_TTL = timedelta(hours=6)
OVERVIEW_DAYS = 4
VIDEO_PARTS = "status,snippet"
MODES = ("seed", "manual", "learned")


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


class SlotUnavailable(RuntimeError):
    """The requested automatic slot cannot be used; ``detail`` carries the next one."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# Cadence (settings)
# ---------------------------------------------------------------------------


def get_schedule(db: Session, channel_id: str) -> YouTubePublishingSchedule | None:
    return db.get(YouTubePublishingSchedule, channel_id)


def _default_timezone(db: Session, hint: str | None) -> str:
    """Browser zone when the schedule is first created; else saved defaults; else UTC."""
    if hint and valid_timezone(hint):
        return hint
    saved = load_defaults(db).timezone
    return saved if saved and valid_timezone(saved) else "UTC"


def ensure_schedule(db: Session, channel_id: str, *, timezone_hint: str | None = None) -> YouTubePublishingSchedule:
    schedule = get_schedule(db, channel_id)
    if schedule is not None:
        return schedule
    schedule = YouTubePublishingSchedule(
        channel_id=channel_id,
        timezone=_default_timezone(db, timezone_hint),
        videos_per_day=1,
        mode="seed",
        enabled=True,
        occupancy_tolerance_minutes=int(planner.DEFAULT_TOLERANCE.total_seconds() // 60),
        min_lead_minutes=int(planner.DEFAULT_MIN_LEAD.total_seconds() // 60),
        horizon_days=planner.DEFAULT_HORIZON_DAYS,
    )
    db.add(schedule)
    for position, value in enumerate(planner.SEED_PRESETS[1]):
        db.add(YouTubeScheduleSlot(channel_id=channel_id, weekday=None, position=position, local_time=value, source="seed"))
    try:
        db.commit()
    except IntegrityError:  # created concurrently
        db.rollback()
        existing = get_schedule(db, channel_id)
        assert existing is not None
        return existing
    db.refresh(schedule)
    return schedule


def slot_map(db: Session, channel_id: str) -> dict[int | None, tuple[str, ...]]:
    rows = db.scalars(
        select(YouTubeScheduleSlot).where(YouTubeScheduleSlot.channel_id == channel_id).order_by(YouTubeScheduleSlot.position)
    ).all()
    result: dict[int | None, list[str]] = {}
    for row in rows:
        result.setdefault(row.weekday, []).append(row.local_time)
    return {key: tuple(sorted(value)) for key, value in result.items()}


def planner_config(db: Session, schedule: YouTubePublishingSchedule) -> planner.PlannerConfig:
    return planner.PlannerConfig(
        timezone=schedule.timezone,
        videos_per_day=schedule.videos_per_day,
        slots=slot_map(db, schedule.channel_id),
        tolerance=timedelta(minutes=schedule.occupancy_tolerance_minutes),
        min_lead=timedelta(minutes=schedule.min_lead_minutes),
        horizon_days=schedule.horizon_days,
    )


class ScheduleUpdate(BaseModel):
    videos_per_day: int = Field(ge=planner.MIN_VIDEOS_PER_DAY, le=planner.MAX_VIDEOS_PER_DAY)
    timezone: str = Field(max_length=64)
    slots: list[str] = Field(max_length=planner.MAX_VIDEOS_PER_DAY)
    # Optional per-weekday overrides (0 = Monday); not exposed in the V1 UI.
    weekday_slots: dict[int, list[str]] = Field(default_factory=dict)
    enabled: bool = True
    occupancy_tolerance_minutes: int = Field(default=60, ge=0, le=240)
    min_lead_minutes: int = Field(default=15, ge=15, le=24 * 60)


class ScheduleInvalid(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(" ".join(errors))
        self.errors = errors


def save_schedule(db: Session, channel_id: str, update: ScheduleUpdate) -> YouTubePublishingSchedule:
    errors: list[str] = []
    if not valid_timezone(update.timezone):
        errors.append("Unknown time zone.")
    slot_errors, _warnings = planner.validate_slots(update.slots, update.videos_per_day)
    errors += slot_errors
    for weekday, values in update.weekday_slots.items():
        if not 0 <= int(weekday) <= 6:
            errors.append("Weekdays are 0 (Monday) to 6 (Sunday).")
            continue
        errors += planner.validate_slots(values, update.videos_per_day)[0]
    if errors:
        raise ScheduleInvalid(errors)
    schedule = ensure_schedule(db, channel_id, timezone_hint=update.timezone)
    wanted = tuple(sorted(item.strip() for item in update.slots))
    previous = slot_map(db, channel_id)
    unchanged = previous.get(None) == wanted and schedule.videos_per_day == update.videos_per_day and not update.weekday_slots
    if schedule.mode == "learned" and unchanged:
        mode, source = "learned", "learned"
    elif wanted == planner.SEED_PRESETS[update.videos_per_day] and not update.weekday_slots:
        mode, source = "seed", "seed"
    else:
        mode, source = "manual", "manual"
    schedule.timezone = update.timezone
    schedule.videos_per_day = update.videos_per_day
    schedule.mode = mode
    schedule.enabled = update.enabled
    schedule.occupancy_tolerance_minutes = update.occupancy_tolerance_minutes
    schedule.min_lead_minutes = update.min_lead_minutes
    if mode != "learned":
        schedule.learned_sample_size = None
    _replace_slots(db, channel_id, wanted, {int(key): tuple(sorted(value)) for key, value in update.weekday_slots.items()}, source)
    schedule.updated_at = _now()
    db.commit()
    db.refresh(schedule)
    return schedule


def _replace_slots(db: Session, channel_id: str, every_day: tuple[str, ...], weekdays: dict[int, tuple[str, ...]], source: str) -> None:
    db.execute(delete(YouTubeScheduleSlot).where(YouTubeScheduleSlot.channel_id == channel_id))
    for position, value in enumerate(every_day):
        db.add(YouTubeScheduleSlot(channel_id=channel_id, weekday=None, position=position, local_time=value, source=source))
    for weekday, values in weekdays.items():
        for position, value in enumerate(values):
            db.add(YouTubeScheduleSlot(channel_id=channel_id, weekday=weekday, position=position, local_time=value, source=source))


def apply_learned_slots(db: Session, schedule: YouTubePublishingSchedule, suggested: list[str], sample_size: int, *, now: datetime | None = None) -> YouTubePublishingSchedule:
    """Only called on the user's explicit approval; never automatically."""
    errors, _ = planner.validate_slots(suggested, schedule.videos_per_day)
    if errors:
        raise ScheduleInvalid(errors)
    _replace_slots(db, schedule.channel_id, tuple(sorted(suggested)), {}, "learned")
    schedule.mode = "learned"
    schedule.learned_sample_size = sample_size
    schedule.learned_applied_at = now or _now()
    db.commit()
    db.refresh(schedule)
    return schedule


# ---------------------------------------------------------------------------
# The remote schedule cache (YouTube is authoritative)
# ---------------------------------------------------------------------------


def entry_values(video: dict[str, Any]) -> dict[str, Any] | None:
    """What a video means for the calendar, or None if it occupies nothing.

    * private + ``status.publishAt``: scheduled at publishAt;
    * public: published at ``snippet.publishedAt`` (the publication time
      while public, as in the status authority);
    * unlisted, private without publishAt, or failed/rejected/deleted uploads
      are not publications on the channel and occupy no slot.
    """
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
    privacy = status.get("privacyStatus")
    upload_status = status.get("uploadStatus")
    if upload_status in {"failed", "rejected", "deleted"}:
        return None
    publish_at = _parse(status.get("publishAt"))
    published_at = _parse(snippet.get("publishedAt"))
    if privacy == "private" and publish_at is not None:
        kind, occupies = planner.SCHEDULED, publish_at
    elif privacy == "public" and published_at is not None:
        kind, occupies = planner.PUBLISHED, published_at
    else:
        return None
    return {
        "kind": kind,
        "privacy_status": privacy,
        "upload_status": upload_status,
        "publish_at": publish_at,
        "published_at": published_at if privacy == "public" else None,
        "occupies_at": occupies,
        "title": str(snippet.get("title") or "")[:120],
    }


def _entries(db: Session, channel_id: str) -> dict[str, YouTubeScheduleEntry]:
    return {row.video_id: row for row in db.scalars(select(YouTubeScheduleEntry).where(YouTubeScheduleEntry.channel_id == channel_id)).all()}


def _upsert(db: Session, channel_id: str, video_id: str, values: dict[str, Any], now: datetime, existing: YouTubeScheduleEntry | None) -> None:
    row = existing or YouTubeScheduleEntry(channel_id=channel_id, video_id=video_id)
    for key, value in values.items():
        setattr(row, key, value)
    row.source = "youtube"
    row.last_seen_at = now
    if existing is None:
        db.add(row)


def record_video(db: Session, channel_id: str, video: dict[str, Any], *, now: datetime | None = None) -> None:
    """Fold one video resource YouTube just returned into the cache (no API call)."""
    video_id = str(video.get("id") or "")
    if not video_id:
        return
    now = now or _now()
    existing = db.scalar(select(YouTubeScheduleEntry).where(YouTubeScheduleEntry.channel_id == channel_id, YouTubeScheduleEntry.video_id == video_id))
    values = entry_values(video)
    if values is None:
        if existing is not None:
            db.delete(existing)
    else:
        _upsert(db, channel_id, video_id, values, now, existing)
    db.commit()


def _list_videos(provider: YouTubeProvider, token: str, ids: list[str]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for start in range(0, len(ids), PAGE_SIZE):
        videos += provider.list_videos(token, ids[start:start + PAGE_SIZE], VIDEO_PARTS)
    return videos


@dataclass
class SyncResult:
    pages: int
    videos: int
    entries: int
    complete: bool
    removed: int


def sync_remote(
    db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, channel_id: str, *, now: datetime | None = None,
) -> SyncResult:
    """Read the channel's real upcoming/today schedule from YouTube into the cache.

    Quota: 1 unit for the uploads playlist id (once, then stored), 1 per
    playlistItems page and 1 per 50-video videos.list batch.  A failure is
    recorded on the schedule (the last good cache is kept) and re-raised.
    """
    now = now or _now()
    schedule = ensure_schedule(db, channel_id)
    started = _now()
    try:
        record, token = access_token(db, settings, store, provider, capability="read")
        if record.channel_id != channel_id:
            raise YouTubeApiError("wrong_channel", "YouTube is connected to a different channel than this schedule belongs to.")
        playlist = schedule.uploads_playlist_id or provider.get_uploads_playlist_id(token)
        cutoff = now - LOOKBACK
        seen: dict[str, dict[str, Any]] = {}
        present: set[str] = set()
        page_token: str | None = None
        pages, videos_read, complete = 0, 0, False
        while True:
            items, next_token = provider.list_playlist_items(token, playlist, page_token)
            pages += 1
            ids = list(dict.fromkeys(
                str((item.get("contentDetails") or {}).get("videoId"))
                for item in items if (item.get("contentDetails") or {}).get("videoId")
            ))
            videos = _list_videos(provider, token, ids)
            videos_read += len(videos)
            newest: datetime | None = None
            for item in items:
                stamp = _parse((item.get("contentDetails") or {}).get("videoPublishedAt"))
                newest = stamp if newest is None or (stamp is not None and stamp > newest) else newest
            for video in videos:
                video_id = str(video.get("id") or "")
                present.add(video_id)
                stamp = _parse((video.get("snippet") or {}).get("publishedAt"))
                newest = stamp if newest is None or (stamp is not None and stamp > newest) else newest
                values = entry_values(video)
                if values is not None:
                    seen[video_id] = values
            if not next_token or not items or (newest is not None and newest < cutoff):
                complete = True
                break
            if pages >= MAX_PAGES:
                break
            page_token = next_token
        # Videos known to be on the calendar (and confirmed ClipForge slots)
        # that the bounded scan did not reach - e.g. uploaded long ago and
        # scheduled far ahead - are re-checked by id instead of being dropped.
        existing = _entries(db, channel_id)
        confirmed = db.scalars(select(YouTubeSlotReservation).where(
            YouTubeSlotReservation.channel_id == channel_id,
            YouTubeSlotReservation.state == "confirmed",
            YouTubeSlotReservation.video_id.is_not(None),
        )).all()
        recheck = [video_id for video_id in dict.fromkeys([*existing, *(item.video_id for item in confirmed)]) if video_id and video_id not in present]
        for video in _list_videos(provider, token, recheck):
            video_id = str(video.get("id") or "")
            present.add(video_id)
            values = entry_values(video)
            if values is not None:
                seen[video_id] = values
    except YouTubeApiError as exc:
        db.rollback()
        schedule = ensure_schedule(db, channel_id)
        schedule.remote_sync_attempted_at = now
        schedule.remote_sync_error_code = exc.code
        schedule.remote_sync_error = exc.message[:500]
        db.commit()
        raise
    floor = now - KEEP_PAST
    removed = 0
    for video_id, row in existing.items():
        if video_id not in seen or seen[video_id]["occupies_at"] < floor:
            db.delete(row)
            removed += 1
    for video_id, values in seen.items():
        if values["occupies_at"] >= floor:
            _upsert(db, channel_id, video_id, values, now, existing.get(video_id))
    # A confirmed ClipForge slot whose video YouTube no longer schedules or
    # publishes (deleted / unscheduled in Studio) stops occupying anything.
    for item in confirmed:
        if item.video_id not in seen and _utc(item.updated_at) is not None and _utc(item.updated_at) <= started:  # type: ignore[operator]
            _release(item, "deleted_on_youtube" if item.video_id not in present else "not_scheduled_on_youtube")
    schedule.uploads_playlist_id = playlist
    schedule.remote_synced_at = now
    schedule.remote_sync_attempted_at = now
    schedule.remote_sync_complete = complete
    schedule.remote_sync_error_code = None
    schedule.remote_sync_error = None
    db.commit()
    return SyncResult(pages=pages, videos=videos_read, entries=len(seen), complete=complete, removed=removed)


def freshness(schedule: YouTubePublishingSchedule, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    synced = _utc(schedule.remote_synced_at)
    attempted = _utc(schedule.remote_sync_attempted_at)
    failed = bool(schedule.remote_sync_error_code) and (synced is None or (attempted is not None and attempted >= synced))
    age = (now - synced).total_seconds() if synced else None
    return {
        "checked_at": _iso(synced),
        "age_seconds": int(age) if age is not None else None,
        "fresh": synced is not None and not failed and now - synced <= FRESH_FOR,
        "usable": synced is not None and now - synced <= CACHE_USABLE_FOR,
        "complete": bool(schedule.remote_sync_complete),
        "error": {"code": schedule.remote_sync_error_code, "message": schedule.remote_sync_error} if failed else None,
    }


def refresh_if_due(
    db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, channel_id: str,
    *, max_age: timedelta = FRESH_FOR, force: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh a stale cache (never raises); returns freshness + whether it is verified now."""
    now = now or _now()
    schedule = ensure_schedule(db, channel_id)
    synced = _utc(schedule.remote_synced_at)
    attempted = _utc(schedule.remote_sync_attempted_at)
    due = force or synced is None or now - synced > max_age
    if due and not force and attempted is not None and now - attempted < MIN_SYNC_GAP and (synced is None or attempted > synced):
        due = False  # a failure moments ago; do not retry on every page load
    if due:
        try:
            sync_remote(db, settings, store, provider, channel_id, now=now)
        except YouTubeApiError:
            pass  # recorded on the schedule; freshness() reports it
        db.refresh(schedule)
    state = freshness(schedule, now=now)
    synced = _utc(schedule.remote_synced_at)
    state["verified"] = state["error"] is None and synced is not None and now - synced <= max(max_age, CONFLICT_CHECK_MAX_AGE)
    return state


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def _release(item: YouTubeSlotReservation, reason: str) -> None:
    item.state = "released"
    item.active_key = None
    item.release_reason = reason[:64]
    item.updated_at = _now()


def _active_key(channel_id: str, instant: datetime) -> str:
    return f"{channel_id}:{_iso(instant)}"


def active_reservations(db: Session, channel_id: str, *, now: datetime | None = None) -> list[YouTubeSlotReservation]:
    """Reserved (not expired) and confirmed claims; expired ones are released here."""
    now = now or _now()
    rows = db.scalars(select(YouTubeSlotReservation).where(
        YouTubeSlotReservation.channel_id == channel_id,
        YouTubeSlotReservation.state.in_(("reserved", "confirmed")),
    )).all()
    active, expired = [], False
    for row in rows:
        if row.state == "reserved" and _utc(row.expires_at) <= now:  # type: ignore[operator]
            _release(row, "expired")
            expired = True
        else:
            active.append(row)
    if expired:
        db.commit()
    return active


def occupants(db: Session, schedule: YouTubePublishingSchedule, *, now: datetime | None = None, exclude_upload_id: str | None = None) -> list[planner.Occupant]:
    now = now or _now()
    floor = now - KEEP_PAST
    entries = db.scalars(select(YouTubeScheduleEntry).where(
        YouTubeScheduleEntry.channel_id == schedule.channel_id, YouTubeScheduleEntry.occupies_at >= floor,
    )).all()
    items = [planner.Occupant(_utc(row.occupies_at), row.kind, row.video_id, "youtube") for row in entries]  # type: ignore[arg-type]
    known = {row.video_id for row in entries}
    for row in active_reservations(db, schedule.channel_id, now=now):
        if exclude_upload_id and row.upload_id == exclude_upload_id:
            continue
        if row.video_id and row.video_id in known:
            continue  # YouTube's own entry already represents it
        if _utc(row.slot_at) < floor:  # type: ignore[operator]
            continue
        kind = planner.SCHEDULED if row.state == "confirmed" else planner.RESERVED
        items.append(planner.Occupant(_utc(row.slot_at), kind, row.video_id, "clipforge_reservation"))  # type: ignore[arg-type]
    return items


def reserve(
    db: Session, channel_id: str, *, publish_at: datetime, local_time: str, timezone: str, source: Literal["auto", "manual"],
    project_id: str | None = None, now: datetime | None = None,
) -> YouTubeSlotReservation:
    """Claim a slot.  Automatic claims are exclusive (unique ``active_key``)."""
    now = now or _now()
    row = YouTubeSlotReservation(
        channel_id=channel_id, slot_at=_utc(publish_at), local_time=local_time, timezone=timezone, source=source,
        state="reserved", active_key=_active_key(channel_id, publish_at) if source == "auto" else None,
        project_id=project_id, expires_at=now + RESERVATION_TTL, created_at=now, updated_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SlotUnavailable("slot_taken", "That slot was just taken.") from exc
    db.refresh(row)
    return row


def attach_upload(db: Session, reservation: YouTubeSlotReservation, upload: YouTubeUpload) -> None:
    for other in db.scalars(select(YouTubeSlotReservation).where(
        YouTubeSlotReservation.upload_id == upload.id,
        YouTubeSlotReservation.state.in_(("reserved", "confirmed")),
        YouTubeSlotReservation.id != reservation.id,
    )).all():
        _release(other, "replaced")
    reservation.upload_id = upload.id
    reservation.project_id = upload.project_id
    reservation.updated_at = _now()
    db.commit()


def release_reservation(db: Session, reservation: YouTubeSlotReservation | None, reason: str) -> None:
    if reservation is None or reservation.state == "released":
        return
    _release(reservation, reason)
    db.commit()


def _for_upload(db: Session, upload_id: str) -> list[YouTubeSlotReservation]:
    return list(db.scalars(select(YouTubeSlotReservation).where(
        YouTubeSlotReservation.upload_id == upload_id,
        YouTubeSlotReservation.state.in_(("reserved", "confirmed")),
    )).all())


def release_for_upload(db: Session, upload_id: str, reason: str) -> int:
    rows = _for_upload(db, upload_id)
    for row in rows:
        _release(row, reason)
    if rows:
        db.commit()
    return len(rows)


def confirm_for_upload(db: Session, upload: YouTubeUpload, publish_at: datetime, *, now: datetime | None = None) -> None:
    """YouTube returned a publishAt for this upload: its claim is now confirmed."""
    for row in _for_upload(db, upload.id):
        row.state = "confirmed"
        row.video_id = upload.youtube_video_id
        row.slot_at = _utc(publish_at)
        row.updated_at = now or _now()
    db.commit()


def on_video_recorded(db: Session, upload: YouTubeUpload, video: dict[str, Any], *, now: datetime | None = None) -> None:
    """After videos.insert / videos.list: confirm or release, and update the cache."""
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    publish_at = _parse(status.get("publishAt"))
    if status.get("uploadStatus") in {"rejected", "failed", "deleted"}:
        release_for_upload(db, upload.id, f"youtube_{status.get('uploadStatus')}")
    elif publish_at is not None and status.get("privacyStatus") == "private":
        confirm_for_upload(db, upload, publish_at, now=now)
    elif status.get("privacyStatus") in {"public", "unlisted"}:
        pass  # published: the cache entry (if public) takes over
    else:
        # YouTube holds the video private without a publication time (for
        # example an unverified API project): never pretend it is scheduled.
        release_for_upload(db, upload.id, "not_scheduled_on_youtube")
    record_video(db, upload.channel_id, video, now=now)


# ---------------------------------------------------------------------------
# Planning, conflict check and the claim used by the upload
# ---------------------------------------------------------------------------


def _slot_payload(slot: planner.SlotState) -> dict[str, Any]:
    return {
        "local_time": slot.local_time,
        "position": slot.position,
        "status": slot.status,
        "publish_at": _iso(slot.publish_at),
        "abbreviation": slot.abbreviation,
        "utc_offset": slot.utc_offset,
        "occupant": {"kind": slot.occupant.kind, "at": _iso(slot.occupant.at), "source": slot.occupant.source} if slot.occupant else None,
        "message": slot.message,
    }


def serialize_recommendation(recommendation: planner.Recommendation | None) -> dict[str, Any] | None:
    if recommendation is None:
        return None
    slot = recommendation.slot
    return {
        "choice": recommendation.choice.model_dump(),
        "publish_at": _iso(slot.publish_at),
        "local_date": slot.local_date.isoformat(),
        "local_time": slot.local_time,
        "timezone": recommendation.timezone,
        "abbreviation": slot.abbreviation,
        "utc_offset": slot.utc_offset,
        "day_offset": recommendation.day_offset,
        "slot_position": slot.position,
        "reason": recommendation.reason,
        "day": [_slot_payload(item) for item in recommendation.day.slots],
    }


def serialize_day(day: planner.DayPlan) -> dict[str, Any]:
    return {
        "date": day.local_date.isoformat(),
        "target": day.target,
        "count": day.count,
        "published": day.published,
        "scheduled": day.scheduled,
        "reserved": day.reserved,
        "full": day.full,
        "slots": [_slot_payload(item) for item in day.slots],
        "other": [{"kind": item.kind, "at": _iso(item.at), "source": item.source} for item in day.other],
    }


def recommend(db: Session, schedule: YouTubePublishingSchedule, *, now: datetime | None = None) -> planner.Recommendation | None:
    now = now or _now()
    return planner.next_free_slot(planner_config(db, schedule), occupants(db, schedule, now=now), now=now)


def serialize_schedule(db: Session, schedule: YouTubePublishingSchedule) -> dict[str, Any]:
    slots = slot_map(db, schedule.channel_id)
    return {
        "channel_id": schedule.channel_id,
        "timezone": schedule.timezone,
        "videos_per_day": schedule.videos_per_day,
        "mode": schedule.mode,
        "enabled": schedule.enabled,
        "slots": list(slots.get(None, ())),
        "weekday_slots": {str(key): list(value) for key, value in slots.items() if key is not None},
        "occupancy_tolerance_minutes": schedule.occupancy_tolerance_minutes,
        "min_lead_minutes": schedule.min_lead_minutes,
        "horizon_days": schedule.horizon_days,
        "learned_sample_size": schedule.learned_sample_size,
        "learned_applied_at": _iso(schedule.learned_applied_at),
        "seed_presets": {str(key): list(value) for key, value in planner.SEED_PRESETS.items()},
    }


def smart_state(
    db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, channel_id: str,
    *, timezone_hint: str | None = None, refresh: bool = True, force: bool = False, days: int = OVERVIEW_DAYS, now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the sheet and Settings show: cadence, freshness, next slot, overview.

    ``status``: verified (YouTube checked recently) | stale_cache (refresh
    failed; an older cache exists and is labelled) | unverified (no usable
    cache) | disabled.  Only a verified recommendation is ever pre-selected.
    """
    now = now or _now()
    schedule = ensure_schedule(db, channel_id, timezone_hint=timezone_hint)
    # A disabled planner costs no quota unless the user explicitly refreshes.
    if (refresh and schedule.enabled) or force:
        state = refresh_if_due(db, settings, store, provider, channel_id, force=force, now=now)
    else:
        state = freshness(schedule, now=now)
        state["verified"] = state["fresh"]
    config = planner_config(db, schedule)
    items = occupants(db, schedule, now=now)
    recommendation = planner.next_free_slot(config, items, now=now)
    if not schedule.enabled:
        status = "disabled"
    elif state["verified"]:
        status = "verified"
    elif state["usable"]:
        status = "stale_cache"
    else:
        status = "unverified"
    return {
        "status": status,
        "enabled": schedule.enabled,
        "schedule": serialize_schedule(db, schedule),
        "freshness": state,
        "recommendation": serialize_recommendation(recommendation) if status == "verified" else None,
        "cached_recommendation": serialize_recommendation(recommendation) if status == "stale_cache" else None,
        "horizon_full": recommendation is None and status in {"verified", "stale_cache"},
        "days": [serialize_day(day) for day in planner.plan_days(config, items, now=now, days=days)],
    }


def claim_auto_slot(
    db: Session, settings: Settings, store: SecretStore, provider: YouTubeProvider, *,
    channel_id: str, choice: ScheduleChoice, project_id: str | None, allow_cached: bool = False, now: datetime | None = None,
) -> YouTubeSlotReservation:
    """The final double-booking check before the schedule is written to YouTube.

    Re-reads YouTube when the cache is older than CONFLICT_CHECK_MAX_AGE, then
    checks the exact instant against every known video and every active
    ClipForge reservation, then claims it exclusively.  Raises
    ``SlotUnavailable`` (with the next free slot) instead of scheduling blindly.
    """
    now = now or _now()
    schedule = ensure_schedule(db, channel_id)
    resolution = resolve_schedule(choice, now=now)
    if resolution.status != "ok" or resolution.publish_at is None:
        raise SlotUnavailable("slot_missed", resolution.message or "That slot is no longer valid.", recommendation=serialize_recommendation(recommend(db, schedule, now=now)))
    state = refresh_if_due(db, settings, store, provider, channel_id, max_age=CONFLICT_CHECK_MAX_AGE, now=now)
    if not state["verified"] and not (allow_cached and state["usable"]):
        raise SlotUnavailable(
            "schedule_unverified", "Could not verify YouTube schedule.",
            freshness=state, cached_recommendation=serialize_recommendation(recommend(db, schedule, now=now)) if state["usable"] else None,
        )
    verdict = planner.check_instant(planner_config(db, schedule), occupants(db, schedule, now=now), resolution.publish_at, now=now)
    if verdict != "free":
        raise SlotUnavailable(
            "slot_taken", "That slot was just taken." if verdict != "missed" else "That slot is too close to now.",
            reason=verdict, recommendation=serialize_recommendation(recommend(db, schedule, now=now)),
        )
    try:
        return reserve(
            db, channel_id, publish_at=resolution.publish_at, local_time=resolution.local_time or f"{choice.date}T{choice.time}",
            timezone=choice.timezone, source="auto", project_id=project_id, now=now,
        )
    except SlotUnavailable as exc:
        # Another upload claimed it between the check and the claim.
        exc.detail["reason"] = "reserved"
        exc.detail["recommendation"] = serialize_recommendation(recommend(db, schedule, now=now))
        raise
