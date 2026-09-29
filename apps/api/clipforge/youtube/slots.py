"""The one slot-selection algorithm (Smart Slot Planner V1).

Pure and deterministic: no database, no Google.  Inputs are the channel's
cadence (zone, videos per day, preferred local slots, occupancy tolerance,
minimum lead time, horizon) plus what is already on the channel's calendar
(published and scheduled videos from YouTube, and ClipForge's own in-flight
reservations).  Output: per-day slot states and the earliest valid free slot.

Local wall times are resolved with ``publishing.resolve_schedule`` - the same
tz-database, DST-safe implementation every manual schedule goes through - so
a slot that falls into a DST gap (nonexistent) or overlap (ambiguous) is
never suggested; it is reported as unavailable on that day instead.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from .publishing import MIN_SCHEDULE_LEAD, ScheduleChoice, resolve_schedule

MIN_VIDEOS_PER_DAY = 1
MAX_VIDEOS_PER_DAY = 5
# Product starter defaults, editable by the user.  Not derived from any data
# and never presented as "best" or "optimal" times.
SEED_PRESETS: dict[int, tuple[str, ...]] = {
    1: ("20:00",),
    2: ("15:00", "20:30"),
    3: ("12:30", "17:00", "21:30"),
    4: ("11:00", "15:00", "18:30", "22:00"),
    5: ("09:30", "12:30", "16:00", "19:30", "22:30"),
}
DEFAULT_TOLERANCE = timedelta(minutes=60)
DEFAULT_MIN_LEAD = timedelta(minutes=15)
DEFAULT_HORIZON_DAYS = 30
MIN_SPACING_HINT = timedelta(minutes=60)
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Slot states, in the words the UI uses.
FREE = "free"
PUBLISHED = "published"
SCHEDULED = "scheduled"
RESERVED = "reserved"
MISSED = "missed"  # passed, or inside the minimum lead time
NOT_NEEDED = "not_needed"  # the day already has its target number of videos
UNAVAILABLE = "unavailable"  # the wall time does not exist / is ambiguous that day (DST)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class Occupant:
    """Something already on the channel's calendar."""

    at: datetime  # UTC instant
    kind: str  # published | scheduled | reserved
    video_id: str | None = None
    source: str = "youtube"  # youtube | clipforge_reservation


@dataclass(frozen=True)
class PlannerConfig:
    timezone: str
    videos_per_day: int
    # None -> every day; 0..6 (Monday = 0) -> that weekday only (overrides None).
    slots: Mapping[int | None, tuple[str, ...]] = field(default_factory=dict)
    tolerance: timedelta = DEFAULT_TOLERANCE
    min_lead: timedelta = DEFAULT_MIN_LEAD
    horizon_days: int = DEFAULT_HORIZON_DAYS

    def slots_for(self, day: date) -> tuple[str, ...]:
        chosen = self.slots.get(day.weekday()) or self.slots.get(None) or ()
        return tuple(sorted(chosen))

    @property
    def lead(self) -> timedelta:
        # Never shorter than what YouTube scheduling itself is checked against.
        return max(self.min_lead, MIN_SCHEDULE_LEAD)


@dataclass(frozen=True)
class SlotState:
    local_date: date
    local_time: str
    position: int
    status: str
    publish_at: datetime | None
    abbreviation: str | None = None
    utc_offset: str | None = None
    occupant: Occupant | None = None
    message: str | None = None


@dataclass(frozen=True)
class DayPlan:
    local_date: date
    target: int
    slots: tuple[SlotState, ...]
    published: int
    scheduled: int
    reserved: int
    # Videos that day not within the tolerance of any preferred slot.
    other: tuple[Occupant, ...]

    @property
    def count(self) -> int:
        return self.published + self.scheduled + self.reserved

    @property
    def full(self) -> bool:
        return self.count >= self.target


def validate_slots(slots: Iterable[str], videos_per_day: int) -> tuple[list[str], list[str]]:
    """(errors, warnings).  Errors block saving; warnings never do."""
    items = [str(item).strip() for item in slots]
    errors: list[str] = []
    warnings: list[str] = []
    if not MIN_VIDEOS_PER_DAY <= videos_per_day <= MAX_VIDEOS_PER_DAY:
        errors.append(f"Choose between {MIN_VIDEOS_PER_DAY} and {MAX_VIDEOS_PER_DAY} videos per day.")
        return errors, warnings
    if len(items) != videos_per_day:
        errors.append(f"Set exactly {videos_per_day} time{'s' if videos_per_day != 1 else ''} for {videos_per_day} video{'s' if videos_per_day != 1 else ''} per day.")
    bad = [item for item in items if not _TIME.match(item)]
    if bad:
        errors.append(f"Use valid 24-hour times like 17:00 ({', '.join(bad[:3])} is not valid).")
    valid = sorted(item for item in items if _TIME.match(item))
    if len(set(valid)) != len(valid):
        errors.append("Two slots have the same time; each slot needs its own time.")
    minutes = sorted({int(item[:2]) * 60 + int(item[3:]) for item in valid})
    close = [(a, b) for a, b in pairwise(minutes) if timedelta(minutes=b - a) < MIN_SPACING_HINT]
    if close:
        warnings.append("Some slots are less than an hour apart; videos this close may compete with each other.")
    return errors, warnings


def resolve_slot(day: date, local_time: str, timezone: str, *, now: datetime) -> tuple[datetime | None, str | None, str | None, str | None]:
    """(instant, abbreviation, utc_offset, problem) - DST-safe via resolve_schedule."""
    resolution = resolve_schedule(ScheduleChoice(date=day.isoformat(), time=local_time, timezone=timezone), now=now)
    if resolution.status in {"nonexistent", "ambiguous", "invalid", "invalid_timezone"}:
        return None, None, None, resolution.message
    return resolution.publish_at, resolution.abbreviation, resolution.utc_offset, None


def local_date(instant: datetime, timezone: str) -> date:
    return _utc(instant).astimezone(ZoneInfo(timezone)).date()


def _nearest(occupants: list[Occupant], instant: datetime, tolerance: timedelta) -> Occupant | None:
    near = [item for item in occupants if abs(_utc(item.at) - instant) <= tolerance]
    return min(near, key=lambda item: abs(_utc(item.at) - instant)) if near else None


def plan_day(config: PlannerConfig, occupants: list[Occupant], day: date, *, now: datetime) -> DayPlan:
    """Every preferred slot of one local calendar day, with its state."""
    now = _utc(now)
    todays = [item for item in occupants if local_date(item.at, config.timezone) == day]
    counts = {kind: sum(1 for item in todays if item.kind == kind) for kind in (PUBLISHED, SCHEDULED, RESERVED)}
    full = sum(counts.values()) >= config.videos_per_day
    states: list[SlotState] = []
    matched: set[int] = set()
    for position, local_time in enumerate(config.slots_for(day)):
        instant, abbreviation, offset, problem = resolve_slot(day, local_time, config.timezone, now=now)
        if instant is None:
            states.append(SlotState(day, local_time, position, UNAVAILABLE, None, message=problem))
            continue
        # Any video within the tolerance consumes the slot (also across midnight).
        occupant = _nearest(occupants, instant, config.tolerance)
        if occupant is not None:
            matched.add(id(occupant))
            status = occupant.kind
        elif instant < now + config.lead:
            status = MISSED
        elif full:
            status = NOT_NEEDED
        else:
            status = FREE
        states.append(SlotState(day, local_time, position, status, instant, abbreviation, offset, occupant))
    other = tuple(sorted((item for item in todays if id(item) not in matched), key=lambda item: _utc(item.at)))
    return DayPlan(day, config.videos_per_day, tuple(states), counts[PUBLISHED], counts[SCHEDULED], counts[RESERVED], other)


def plan_days(config: PlannerConfig, occupants: Iterable[Occupant], *, now: datetime, days: int) -> list[DayPlan]:
    items = list(occupants)
    today = local_date(now, config.timezone)
    return [plan_day(config, items, today + timedelta(days=offset), now=now) for offset in range(max(0, days))]


@dataclass(frozen=True)
class Recommendation:
    slot: SlotState
    day_offset: int
    videos_per_day: int
    timezone: str
    day: DayPlan

    @property
    def choice(self) -> ScheduleChoice:
        return ScheduleChoice(date=self.slot.local_date.isoformat(), time=self.slot.local_time, timezone=self.timezone)

    @property
    def reason(self) -> str:
        count = self.videos_per_day
        return f"Next free slot in your {count}-video{'s' if count != 1 else ''}/day schedule"


def next_free_slot(config: PlannerConfig, occupants: Iterable[Occupant], *, now: datetime) -> Recommendation | None:
    """The earliest valid, future, free preferred slot within the horizon.

    A slot qualifies when no known video is within the tolerance, it is not
    inside the minimum lead time, the wall time exists that day, and the day
    has fewer videos (published + scheduled + reserved, from any tool) than
    the daily target.  ``None`` = every slot in the horizon is taken.
    """
    items = list(occupants)
    today = local_date(now, config.timezone)
    for offset in range(config.horizon_days + 1):
        day = plan_day(config, items, today + timedelta(days=offset), now=now)
        if day.full:
            continue
        slot = next((item for item in day.slots if item.status == FREE), None)
        if slot is not None:
            return Recommendation(slot, offset, config.videos_per_day, config.timezone, day)
    return None


def check_instant(config: PlannerConfig, occupants: Iterable[Occupant], instant: datetime, *, now: datetime) -> str:
    """Is this exact instant still free?  free | occupied | day_full | missed.

    Used for the final double-booking check; it does not require the instant
    to be a configured slot (the configuration may have changed meanwhile).
    """
    instant = _utc(instant)
    items = list(occupants)
    if _nearest(items, instant, config.tolerance) is not None:
        return "occupied"
    if instant < _utc(now) + config.lead:
        return "missed"
    day = local_date(instant, config.timezone)
    if sum(1 for item in items if local_date(item.at, config.timezone) == day) >= config.videos_per_day:
        return "day_full"
    return "free"
