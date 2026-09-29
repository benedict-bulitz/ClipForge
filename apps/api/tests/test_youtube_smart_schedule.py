"""Smart Slot Planner V1: algorithm, YouTube schedule sync, reservations (fake Google only)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_youtube_learning_loop import connect
from test_youtube_publishing_v2 import PID, api_client, build_project
from youtube_support import FakeYouTube, publish_options, youtube_settings

from clipforge.models import (
    YouTubeAnalyticsSnapshot,
    YouTubeMetricValue,
    YouTubeScheduleEntry,
    YouTubeSlotReservation,
    YouTubeUpload,
)
from clipforge.security.secrets import SecretStore
from clipforge.youtube import connection, publishing, schedule_learning, uploads
from clipforge.youtube import schedule as schedule_authority
from clipforge.youtube import slots as planner
from clipforge.youtube.provider import YouTubeApiError
from clipforge.youtube.publishing import ScheduleChoice
from clipforge.youtube.slots import Occupant, PlannerConfig

BERLIN = "Europe/Berlin"
CHANNEL = "UC_fake_channel_01"
PID_B = "33333333-3333-4333-8333-333333333333"
PID_C = "44444444-4444-4444-8444-444444444444"


def at(local: str, tz: str = BERLIN) -> datetime:
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def cfg(count: int, slots: tuple[str, ...] | None = None, tz: str = BERLIN, **kwargs) -> PlannerConfig:
    return PlannerConfig(tz, count, {None: slots or planner.SEED_PRESETS[count]}, **kwargs)


def occ(local: str, kind: str = "scheduled", tz: str = BERLIN) -> Occupant:
    return Occupant(at(local, tz), kind)


def pick(config: PlannerConfig, occupants: list[Occupant], now_local: str, tz: str = BERLIN) -> tuple[str, str] | None:
    result = planner.next_free_slot(config, occupants, now=at(now_local, tz))
    return (result.slot.local_date.isoformat(), result.slot.local_time) if result else None


# ---------------------------------------------------------------------------
# 33. The slot algorithm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("count", "expected"), [(1, "20:00"), (2, "15:00"), (3, "12:30"), (4, "11:00"), (5, "09:30")])
def test_every_daily_count_starts_with_its_first_seed_slot(count, expected):
    assert pick(cfg(count), [], "2026-09-28T08:00") == ("2026-09-28", expected)
    assert len(planner.SEED_PRESETS[count]) == count


def test_seed_presets_are_the_product_defaults():
    assert planner.SEED_PRESETS == {
        1: ("20:00",),
        2: ("15:00", "20:30"),
        3: ("12:30", "17:00", "21:30"),
        4: ("11:00", "15:00", "18:30", "22:00"),
        5: ("09:30", "12:30", "16:00", "19:30", "22:30"),
    }


def test_today_partially_occupied():
    assert pick(cfg(3), [occ("2026-09-28T12:30")], "2026-09-28T10:00") == ("2026-09-28", "17:00")


def test_spec_example_published_and_scheduled_today():
    occupants = [occ("2026-09-28T12:30", "published"), occ("2026-09-28T17:00", "scheduled")]
    assert pick(cfg(3), occupants, "2026-09-28T18:00") == ("2026-09-28", "21:30")


def test_today_full_by_count_even_if_a_preferred_time_is_free():
    # Three videos today (none at a preferred time): a fourth is never added.
    occupants = [occ("2026-09-28T08:00", "published"), occ("2026-09-28T09:00", "published"), occ("2026-09-28T10:00", "scheduled")]
    assert pick(cfg(3), occupants, "2026-09-28T07:00") == ("2026-09-29", "12:30")


def test_tomorrow_partially_occupied_and_multiple_future_days():
    today = [occ(f"2026-09-28T{time}") for time in planner.SEED_PRESETS[3]]
    assert pick(cfg(3), [*today, occ("2026-09-29T12:30")], "2026-09-28T08:00") == ("2026-09-29", "17:00")
    tomorrow = [occ(f"2026-09-29T{time}") for time in planner.SEED_PRESETS[3]]
    assert pick(cfg(3), [*today, *tomorrow, occ("2026-09-30T12:30"), occ("2026-09-30T17:00")], "2026-09-28T08:00") == ("2026-09-30", "21:30")


def test_already_published_video_today_counts_toward_the_target():
    # 2/day: one published this morning off-slot + one scheduled at 15:00 = full.
    occupants = [occ("2026-09-28T07:10", "published"), occ("2026-09-28T15:00")]
    assert pick(cfg(2), occupants, "2026-09-28T09:00") == ("2026-09-29", "15:00")


def test_slot_inside_the_minimum_lead_time_is_missed():
    config = cfg(3)
    assert pick(config, [occ("2026-09-28T12:30", "published")], "2026-09-28T16:52") == ("2026-09-28", "21:30")
    day = planner.plan_day(config, [], at("2026-09-28T16:52").astimezone(ZoneInfo(BERLIN)).date(), now=at("2026-09-28T16:52"))
    assert [slot.status for slot in day.slots] == ["missed", "missed", "free"]
    longer = cfg(3, min_lead=timedelta(minutes=60))
    assert pick(longer, [], "2026-09-28T16:10") == ("2026-09-28", "21:30")
    assert pick(cfg(3), [], "2026-09-28T16:10") == ("2026-09-28", "17:00")


def test_near_slot_occupancy_within_and_outside_the_tolerance():
    assert pick(cfg(3), [occ("2026-09-28T16:45")], "2026-09-28T13:00") == ("2026-09-28", "21:30")
    assert pick(cfg(3), [occ("2026-09-28T15:30")], "2026-09-28T13:00") == ("2026-09-28", "17:00")  # 90 min away
    tight = cfg(3, tolerance=timedelta(minutes=10))
    assert pick(tight, [occ("2026-09-28T16:45")], "2026-09-28T13:00") == ("2026-09-28", "17:00")


def test_occupancy_tolerance_crosses_midnight():
    config = cfg(1, ("00:15",))
    assert pick(config, [occ("2026-09-28T23:50")], "2026-09-28T20:00") == ("2026-09-30", "00:15")


def test_dst_spring_forward_skips_the_missing_wall_time():
    config = cfg(1, ("02:30",))  # 28 March 2027 in Berlin: 02:00 -> 03:00
    assert pick(config, [], "2027-03-27T12:00") == ("2027-03-29", "02:30")
    day = planner.plan_day(config, [], date(2027, 3, 28), now=at("2027-03-27T12:00"))
    assert day.slots[0].status == "unavailable" and "does not exist" in day.slots[0].message
    seed = planner.next_free_slot(cfg(3), [], now=at("2027-03-28T08:00"))
    assert seed.slot.publish_at == datetime(2027, 3, 28, 10, 30, tzinfo=UTC) and seed.slot.abbreviation == "CEST"


def test_dst_fall_back_skips_the_ambiguous_wall_time():
    config = cfg(1, ("02:30",))  # 25 October 2026: 02:30 happens twice
    assert pick(config, [], "2026-10-24T12:00") == ("2026-10-26", "02:30")
    evening = planner.next_free_slot(cfg(1), [], now=at("2026-10-25T08:00"))
    assert evening.slot.publish_at == datetime(2026, 10, 25, 19, 0, tzinfo=UTC) and evening.slot.abbreviation == "CET"


def test_different_timezone_uses_its_own_calendar_day():
    ny = "America/New_York"
    config = cfg(1, tz=ny)
    now = datetime(2026, 9, 28, 23, 30, tzinfo=UTC)  # 19:30 on 28 Sept in New York, 01:30 on 29 Sept in Berlin
    result = planner.next_free_slot(config, [], now=now)
    assert (result.slot.local_date.isoformat(), result.slot.local_time) == ("2026-09-28", "20:00")
    assert result.slot.publish_at == datetime(2026, 9, 29, 0, 0, tzinfo=UTC)
    # A video at 00:10 UTC on the 29th is still the 28th in New York: that day is full.
    full = planner.next_free_slot(config, [Occupant(datetime(2026, 9, 29, 0, 10, tzinfo=UTC), "scheduled")], now=now)
    assert full.slot.local_date.isoformat() == "2026-09-29"


def test_all_slots_occupied_in_the_horizon_returns_none():
    config = cfg(1, horizon_days=3)
    occupants = [occ(f"2026-09-{day}T20:00") for day in (28, 29, 30)] + [occ("2026-10-01T20:00")]
    assert planner.next_free_slot(config, occupants, now=at("2026-09-28T08:00")) is None


def test_weekday_specific_slots_override_every_day():
    config = PlannerConfig(BERLIN, 1, {None: ("20:00",), 4: ("11:30",)})  # Friday
    assert pick(config, [], "2026-10-02T08:00") == ("2026-10-02", "11:30")  # a Friday
    assert pick(config, [], "2026-10-01T08:00") == ("2026-10-01", "20:00")


def test_check_instant():
    config = cfg(3)
    now = at("2026-09-28T10:00")
    assert planner.check_instant(config, [], at("2026-09-28T17:00"), now=now) == "free"
    assert planner.check_instant(config, [occ("2026-09-28T16:30")], at("2026-09-28T17:00"), now=now) == "occupied"
    assert planner.check_instant(config, [], at("2026-09-28T10:05"), now=now) == "missed"
    full = [occ("2026-09-28T07:00", "published"), occ("2026-09-28T08:00", "published"), occ("2026-09-28T12:30")]
    assert planner.check_instant(config, full, at("2026-09-28T21:30"), now=now) == "day_full"


def test_slot_validation():
    assert planner.validate_slots(["12:00", "16:30", "21:00"], 3) == ([], [])
    assert planner.validate_slots(["12:00", "16:30"], 3)[0]
    assert planner.validate_slots(["12:00", "12:00", "21:00"], 3)[0]
    assert planner.validate_slots(["12:00", "25:00", "21:00"], 3)[0]
    errors, warnings = planner.validate_slots(["12:00", "12:30", "21:00"], 3)
    assert errors == [] and warnings  # close slots are allowed, only hinted


# ---------------------------------------------------------------------------
# Fixtures for the service / API
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)  # 08:00 in Berlin


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()
    from clipforge.main import app

    app.dependency_overrides.clear()


@pytest.fixture()
def settings(tmp_path):
    return youtube_settings(tmp_path)


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def store():
    return SecretStore()


def configure(db, count: int = 3, slots: list[str] | None = None, **extra):
    return schedule_authority.save_schedule(db, CHANNEL, schedule_authority.ScheduleUpdate(
        videos_per_day=count, timezone=BERLIN, slots=slots or list(planner.SEED_PRESETS[count]), **extra,
    ))


def z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def entries(db) -> dict[str, YouTubeScheduleEntry]:
    return {row.video_id: row for row in db.scalars(select(YouTubeScheduleEntry)).all()}


# ---------------------------------------------------------------------------
# 1-4, 27, 28. Channel cadence settings
# ---------------------------------------------------------------------------


def test_new_schedule_defaults_to_one_video_seed_in_the_browser_zone(db):
    record = schedule_authority.ensure_schedule(db, CHANNEL, timezone_hint=BERLIN)
    data = schedule_authority.serialize_schedule(db, record)
    assert (data["videos_per_day"], data["slots"], data["mode"], data["timezone"], data["enabled"]) == (1, ["20:00"], "seed", BERLIN, True)
    assert (data["occupancy_tolerance_minutes"], data["min_lead_minutes"]) == (60, 15)
    assert data["seed_presets"]["3"] == ["12:30", "17:00", "21:30"]


def test_custom_slots_persist_and_are_labelled_manual(db):
    configure(db, 3, ["12:00", "16:30", "21:00"])
    record = schedule_authority.get_schedule(db, CHANNEL)
    assert (record.mode, schedule_authority.slot_map(db, CHANNEL)[None]) == ("manual", ("12:00", "16:30", "21:00"))
    configure(db, 3)
    assert schedule_authority.get_schedule(db, CHANNEL).mode == "seed"
    with pytest.raises(schedule_authority.ScheduleInvalid):
        configure(db, 3, ["12:00", "12:00", "21:00"])
    with pytest.raises(schedule_authority.ScheduleInvalid):
        schedule_authority.save_schedule(db, CHANNEL, schedule_authority.ScheduleUpdate(videos_per_day=2, timezone="Mars/Base", slots=["10:00", "18:00"]))


# ---------------------------------------------------------------------------
# 34. YouTube schedule sync (uploads playlist + videos.list, never search.list)
# ---------------------------------------------------------------------------


def test_uploads_playlist_is_discovered_once_and_pages_are_followed(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.playlist_page_size = 2
    for index in range(5):
        fake.add_studio_video(f"studio{index}", publish_at=z(at(f"2026-09-{29 + index % 2}T{10 + index}:00")), published_at="2026-09-20T10:00:00Z")
    result = schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    assert (result.pages, result.entries, result.complete) == (3, 5, True)
    assert [name for name, _ in fake.calls].count("uploads_playlist") == 1
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    assert [name for name, _ in fake.calls].count("uploads_playlist") == 1  # stored on the schedule
    assert not any(name == "search" for name, _ in fake.calls)


def test_videos_list_is_batched_by_fifty(db, settings, store, fake):
    connect(db, settings, store, fake)
    for index in range(60):
        fake.add_studio_video(f"v{index:03d}", publish_at=z(NOW + timedelta(days=1 + index // 3, hours=index % 3)), published_at="2026-09-20T10:00:00Z")
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    batches = [ids for name, ids in fake.calls if name == "list_videos"]
    assert [len(ids) for ids in batches] == [50, 10]
    assert len(entries(db)) == 60


def test_scan_is_bounded_by_the_lookback(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.playlist_page_size = 2
    for index in range(4):  # inserted oldest first; the playlist is newest first
        fake.add_studio_video(f"old{index}", privacy="public", published_at="2026-03-01T10:00:00Z")
    fake.add_studio_video("new0", privacy="public", published_at=z(NOW - timedelta(hours=2)))
    fake.add_studio_video("new1", publish_at=z(NOW + timedelta(hours=8)), published_at=z(NOW - timedelta(days=1)))
    result = schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    assert result.pages == 2 and result.complete  # page 2 was entirely older than the lookback: stop
    assert set(entries(db)) == {"new0", "new1"}


def test_privacy_rules_for_what_occupies_a_slot(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.add_studio_video("scheduled", publish_at=z(at("2026-09-28T17:00")), published_at="2026-09-27T10:00:00Z")
    fake.add_studio_video("public_today", privacy="public", published_at=z(at("2026-09-28T07:30")))
    fake.add_studio_video("unlisted", privacy="unlisted", published_at=z(at("2026-09-28T07:40")))
    fake.add_studio_video("private_draft", published_at=z(at("2026-09-28T07:45")))
    fake.add_studio_video("rejected", publish_at=z(at("2026-09-28T21:30")), upload_status="rejected", published_at="2026-09-27T10:00:00Z")
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    rows = entries(db)
    assert set(rows) == {"scheduled", "public_today"}
    assert rows["scheduled"].kind == "scheduled" and rows["scheduled"].occupies_at.replace(tzinfo=UTC) == at("2026-09-28T17:00")
    assert rows["public_today"].kind == "published"


def test_studio_changes_new_moved_and_deleted_videos(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.add_studio_video("moved", publish_at=z(at("2026-09-29T12:30")), published_at="2026-09-27T10:00:00Z")
    fake.add_studio_video("deleted", publish_at=z(at("2026-09-29T17:00")), published_at="2026-09-27T10:00:00Z")
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    assert set(entries(db)) == {"moved", "deleted"}
    fake.videos["moved"]["status"]["publishAt"] = z(at("2026-09-30T21:30"))
    del fake.videos["deleted"]
    fake.add_studio_video("new", publish_at=z(at("2026-09-28T12:30")), published_at="2026-09-28T05:00:00Z")
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW + timedelta(minutes=5))
    rows = entries(db)
    assert set(rows) == {"moved", "new"}
    assert rows["moved"].occupies_at.replace(tzinfo=UTC) == at("2026-09-30T21:30")


def test_known_far_future_video_beyond_the_scan_is_rechecked_not_dropped(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.playlist_page_size = 1
    fake.add_studio_video("far", publish_at=z(at("2026-10-10T17:00")), published_at="2026-01-05T10:00:00Z")
    schedule_authority.record_video(db, CHANNEL, fake.videos["far"], now=NOW)  # e.g. seen by an earlier, wider scan
    for index in range(3):
        fake.add_studio_video(f"old{index}", privacy="public", published_at="2026-02-01T10:00:00Z")
    fake.add_studio_video("recent", privacy="public", published_at=z(NOW - timedelta(hours=1)))
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    assert "far" in entries(db)
    assert ["far"] in [ids for name, ids in fake.calls if name == "list_videos"]


def test_api_error_keeps_the_cache_and_never_pretends_a_slot_is_free(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    fake.add_studio_video("studio", publish_at=z(at("2026-09-28T12:30")), published_at="2026-09-27T10:00:00Z")
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    fake.playlist_error = YouTubeApiError("quota_exceeded", "Quota exceeded.", retryable=True)
    later = NOW + timedelta(minutes=30)
    with pytest.raises(YouTubeApiError):
        schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=later)
    assert "studio" in entries(db)  # last good cache kept
    state = schedule_authority.smart_state(db, settings, store, fake, CHANNEL, now=later + timedelta(minutes=1))
    assert state["status"] == "stale_cache"
    assert state["recommendation"] is None  # never pre-selected
    assert state["cached_recommendation"]["local_time"] == "17:00"
    assert state["freshness"]["error"]["code"] == "quota_exceeded"
    assert state["freshness"]["age_seconds"] == 31 * 60  # "checked 31 min ago" is shown, not hidden


def test_stale_cache_beyond_the_usable_age_is_unverified(db, settings, store, fake):
    connect(db, settings, store, fake)
    schedule_authority.sync_remote(db, settings, store, fake, CHANNEL, now=NOW)
    fake.playlist_error = YouTubeApiError("network_error", "YouTube could not be reached.", retryable=True)
    state = schedule_authority.smart_state(db, settings, store, fake, CHANNEL, now=NOW + timedelta(hours=7))
    assert state["status"] == "unverified" and state["cached_recommendation"] is None and state["recommendation"] is None
    assert not state["freshness"]["usable"]


def test_fresh_cache_is_not_refetched(db, settings, store, fake):
    connect(db, settings, store, fake)
    schedule_authority.smart_state(db, settings, store, fake, CHANNEL, now=NOW)
    calls = len([name for name, _ in fake.calls if name == "playlist_items"])
    schedule_authority.smart_state(db, settings, store, fake, CHANNEL, now=NOW + timedelta(minutes=5))
    assert len([name for name, _ in fake.calls if name == "playlist_items"]) == calls


# ---------------------------------------------------------------------------
# 35. Reservations / concurrency
# ---------------------------------------------------------------------------


def auto_options(choice: dict) -> dict:
    return publish_options(visibility="schedule", schedule=choice, schedule_source="auto").model_dump()


def post_upload(client, project_id: str, options: dict, **extra):
    return client.post(f"/api/youtube/projects/{project_id}/uploads", json={"base_revision": 1, "options": options, **extra})


def test_two_uploads_for_the_same_slot_only_one_reserves_it(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    build_project(db, settings, project_id=PID_B, content=b"\x02" * 30_000)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)  # uploads stay in flight
    first_choice = client.get("/api/youtube/schedule/next").json()["recommendation"]["choice"]
    assert post_upload(client, PID, auto_options(first_choice)).status_code == 202
    taken = post_upload(client, PID_B, auto_options(first_choice))
    assert taken.status_code == 409
    detail = taken.json()["detail"]
    assert detail["status"] == "slot_taken" and detail["message"] == "That slot was just taken."
    retry_choice = detail["recommendation"]["choice"]
    assert retry_choice != first_choice
    assert post_upload(client, PID_B, auto_options(retry_choice)).status_code == 202
    active = db.scalars(select(YouTubeSlotReservation).where(YouTubeSlotReservation.state == "reserved")).all()
    assert len(active) == 2 and len({row.slot_at for row in active}) == 2


def test_the_exclusive_claim_itself_rejects_a_race(db):
    instant = at("2026-09-28T17:00")
    schedule_authority.reserve(db, CHANNEL, publish_at=instant, local_time="2026-09-28T17:00", timezone=BERLIN, source="auto", now=NOW)
    with pytest.raises(schedule_authority.SlotUnavailable) as raised:
        schedule_authority.reserve(db, CHANNEL, publish_at=instant, local_time="2026-09-28T17:00", timezone=BERLIN, source="auto", now=NOW)
    assert raised.value.code == "slot_taken"


def test_failed_upload_releases_its_reservation(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    project = build_project(db, settings)
    state = schedule_authority.smart_state(db, settings, store, fake, CHANNEL)
    choice = ScheduleChoice(**state["recommendation"]["choice"])
    reservation = schedule_authority.claim_auto_slot(db, settings, store, fake, channel_id=CHANNEL, choice=choice, project_id=project.id)
    fake.start_error = YouTubeApiError("quota_exceeded", "Quota exceeded.", retryable=True)
    row, source, _run = uploads.request_upload(db, project, settings, channel_id=CHANNEL, options=publish_options(visibility="schedule", schedule=choice.model_dump(), schedule_source="auto"))
    schedule_authority.attach_upload(db, reservation, row)
    uploads.run_upload(db, row.id, source.path, settings, store, fake)
    db.refresh(reservation)
    assert (reservation.state, reservation.active_key) == ("released", None)
    assert reservation.release_reason.startswith("upload_failed")
    again = schedule_authority.smart_state(db, settings, store, fake, CHANNEL, refresh=False)
    assert again["recommendation"]["choice"] == choice.model_dump()  # the slot is free again


def test_successful_upload_confirms_and_youtube_rejection_releases(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    project = build_project(db, settings)
    choice = ScheduleChoice(**schedule_authority.smart_state(db, settings, store, fake, CHANNEL)["recommendation"]["choice"])
    reservation = schedule_authority.claim_auto_slot(db, settings, store, fake, channel_id=CHANNEL, choice=choice, project_id=project.id)
    row, source, _run = uploads.request_upload(db, project, settings, channel_id=CHANNEL, options=publish_options(visibility="schedule", schedule=choice.model_dump(), schedule_source="auto"))
    schedule_authority.attach_upload(db, reservation, row)
    uploads.run_upload(db, row.id, source.path, settings, store, fake)
    db.refresh(reservation)
    db.refresh(row)
    assert (reservation.state, reservation.video_id) == ("confirmed", row.youtube_video_id)
    assert (row.schedule_source, row.schedule_slot_time) == ("auto", choice.time)
    assert row.youtube_video_id in entries(db)  # the cache knows immediately
    fake.videos[row.youtube_video_id]["status"]["uploadStatus"] = "rejected"
    uploads.sync_status(db, row, settings, store, fake)
    db.refresh(reservation)
    assert (reservation.state, reservation.release_reason) == ("released", "youtube_rejected")
    assert row.youtube_video_id not in entries(db)


def test_expired_reservation_stops_occupying(db):
    record = schedule_authority.ensure_schedule(db, CHANNEL)
    schedule_authority.reserve(db, CHANNEL, publish_at=at("2026-09-28T20:00"), local_time="x", timezone=BERLIN, source="auto", now=NOW)
    assert len(schedule_authority.occupants(db, record, now=NOW)) == 1
    assert schedule_authority.occupants(db, record, now=NOW + schedule_authority.RESERVATION_TTL + timedelta(seconds=1)) == []


def test_upload_is_refused_when_youtube_cannot_be_verified_unless_cache_is_accepted(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)
    choice = client.get("/api/youtube/schedule/next").json()["recommendation"]["choice"]
    record = schedule_authority.get_schedule(db, CHANNEL)
    record.remote_synced_at = datetime.now(UTC) - timedelta(minutes=8)
    db.commit()
    fake.playlist_error = YouTubeApiError("network_error", "YouTube could not be reached.", retryable=True)
    refused = post_upload(client, PID, auto_options(choice))
    assert refused.status_code == 409 and refused.json()["detail"]["status"] == "schedule_unverified"
    assert refused.json()["detail"]["message"] == "Could not verify YouTube schedule."
    record.remote_sync_attempted_at = None
    db.commit()
    accepted = post_upload(client, PID, auto_options(choice), allow_cached_schedule=True)
    assert accepted.status_code == 202


# ---------------------------------------------------------------------------
# 13, 19. Draft pre-selection, priority and manual override
# ---------------------------------------------------------------------------


def test_draft_preselects_the_next_free_slot_and_studio_videos_occupy_it(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    client = api_client(db, settings, store, fake)
    first = client.get("/api/youtube/schedule/next").json()["recommendation"]
    fake.add_studio_video("studio", publish_at=first["publish_at"], published_at=z(datetime.now(UTC) - timedelta(hours=1)))
    client.post("/api/youtube/schedule/refresh")
    draft = client.get(f"/api/youtube/projects/{PID}/draft").json()
    smart = draft["smart_schedule"]
    assert smart["status"] == "verified"
    options = draft["options"]
    assert options["visibility"] == "schedule" and options["schedule_source"] == "auto"
    assert options["schedule"] == smart["recommendation"]["choice"] != first["choice"]
    assert smart["recommendation"]["reason"] == "Next free slot in your 3-videos/day schedule"


def test_smart_schedule_beats_the_last_used_time_but_not_a_manual_override(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 1)
    publishing.record_last_used(db, CHANNEL, {"visibility": "schedule"}, schedule={"time": "07:15", "timezone": BERLIN})
    build_project(db, settings)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)
    draft = client.get(f"/api/youtube/projects/{PID}/draft").json()
    assert draft["options"]["schedule"]["time"] == "20:00"  # not the old 07:15
    # The user's own time is respected as is - even next to an existing video.
    recommended = draft["options"]["schedule"]
    fake.add_studio_video("studio", publish_at=draft["smart_schedule"]["recommendation"]["publish_at"])
    client.post("/api/youtube/schedule/refresh")
    manual = publish_options(visibility="schedule", schedule={**recommended, "time": "20:10"}, schedule_source="manual").model_dump()
    assert post_upload(client, PID, manual).json()["detail"]["status"] == "schedule_conflict"  # warned first
    response = post_upload(client, PID, manual, accept_schedule_conflict=True)  # "Keep anyway"
    assert response.status_code == 202
    row = db.get(YouTubeUpload, response.json()["upload"]["id"])
    assert (row.schedule_source, row.schedule_slot_time, row.schedule_local_time[-5:]) == ("manual", None, "20:10")
    held = db.scalar(select(YouTubeSlotReservation).where(YouTubeSlotReservation.upload_id == row.id))
    assert (held.source, held.active_key, held.state) == ("manual", None, "reserved")


def test_disabled_smart_schedule_falls_back_to_the_last_used_time(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 1, enabled=False)
    publishing.record_last_used(db, CHANNEL, {"visibility": "schedule"}, schedule={"time": "07:15", "timezone": BERLIN})
    build_project(db, settings)
    draft = api_client(db, settings, store, fake).get(f"/api/youtube/projects/{PID}/draft").json()
    assert draft["smart_schedule"]["status"] == "disabled"
    assert (draft["options"]["schedule"]["time"], draft["options"]["schedule_source"]) == ("07:15", "manual")
    assert not any(name == "playlist_items" for name, _ in fake.calls)  # no quota spent while off


def test_unverified_schedule_never_invents_a_time(db, settings, store, fake):
    connect(db, settings, store, fake)
    build_project(db, settings)
    fake.playlist_error = YouTubeApiError("quota_exceeded", "Quota exceeded.", retryable=True)
    draft = api_client(db, settings, store, fake).get(f"/api/youtube/projects/{PID}/draft?timezone={BERLIN}").json()
    assert draft["smart_schedule"]["status"] == "unverified"
    assert draft["options"]["schedule"] is None and draft["options"]["schedule_source"] is None
    assert draft["smart_schedule"]["schedule"]["timezone"] == BERLIN  # created in the browser's zone


# ---------------------------------------------------------------------------
# 38. The real-world sequence, end to end against the fake
# ---------------------------------------------------------------------------


def test_real_world_sequence_studio_slot_then_clipforge_slot(db, settings, store, fake):
    connect(db, settings, store, fake)
    client = api_client(db, settings, store, fake, dispatch=lambda upload_id, path: run(upload_id, path))

    def run(upload_id, path):
        row = uploads.run_upload(db, upload_id, path, settings, store, fake)
        uploads.after_upload(db, row, settings, store, fake)

    # A. configure 3/day
    saved = client.put("/api/youtube/schedule", json={"videos_per_day": 3, "timezone": BERLIN, "slots": ["12:30", "17:00", "21:30"]})
    assert saved.status_code == 200 and saved.json()["schedule"]["mode"] == "seed"
    # B. one video scheduled manually in YouTube Studio at the first free slot
    first = client.get("/api/youtube/schedule/next").json()["recommendation"]
    fake.add_studio_video("studio1", publish_at=first["publish_at"], published_at=z(datetime.now(UTC) - timedelta(minutes=30)))
    client.post("/api/youtube/schedule/refresh")
    # C-E. the sheet sees it as occupied and pre-selects the next configured slot
    build_project(db, settings)
    draft = client.get(f"/api/youtube/projects/{PID}/draft").json()
    second = draft["smart_schedule"]["recommendation"]
    assert second["publish_at"] != first["publish_at"]
    assert any(slot["publish_at"] == first["publish_at"] and slot["status"] == "scheduled" for day in draft["smart_schedule"]["days"] for slot in day["slots"])
    # F. schedule through ClipForge
    response = post_upload(client, PID, {**draft["options"], "thumbnail": {"source": "youtube_auto"}, "made_for_kids": False, "contains_synthetic_media": False})
    assert response.status_code == 202, response.text
    row = db.get(YouTubeUpload, response.json()["upload"]["id"])
    assert row.youtube_video_id and row.schedule_source == "auto"
    # G-H. a new project: the ClipForge video now occupies its slot; the next one is selected
    build_project(db, settings, project_id=PID_C, content=b"\x03" * 30_000)
    third = client.get(f"/api/youtube/projects/{PID_C}/draft").json()["smart_schedule"]["recommendation"]
    assert third["publish_at"] not in {first["publish_at"], second["publish_at"]}
    reservation = db.scalar(select(YouTubeSlotReservation).where(YouTubeSlotReservation.upload_id == row.id))
    assert reservation.state == "confirmed"


# ---------------------------------------------------------------------------
# 20-26. Learning: gated, comparable-age, never automatic
# ---------------------------------------------------------------------------


def seed_published_shorts(db, count: int, *, minute_for, views_for) -> None:
    for index in range(count):
        local = datetime(2026, 8, 1, tzinfo=ZoneInfo(BERLIN)) + timedelta(days=index, minutes=minute_for(index))
        row = YouTubeUpload(
            project_id=str(uuid.uuid4()), project_revision=1, render_revision=1, render_sha256=f"{index:064d}", render_file_size=10,
            channel_id=CHANNEL, youtube_video_id=f"learn{index:04d}", state="ready", published_at=local.astimezone(UTC),
            content_type="SHORTS", schedule_source="auto", schedule_slot_time=None,
        )
        db.add(row)
        db.flush()
        snapshot = YouTubeAnalyticsSnapshot(
            upload_id=row.id, youtube_video_id=row.youtube_video_id, channel_id=CHANNEL, project_id=row.project_id,
            project_revision=1, render_revision=1, age_bucket="24h", published_age_hours=24.5, status="ok",
        )
        snapshot.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name="engagedViews", value=float(views_for(index)), source="youtube_analytics_api"))
        snapshot.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name="averageViewPercentage", value=80.0, source="youtube_analytics_api"))
        db.add(snapshot)
        # A 7d snapshot too: comparisons must stay within one age bucket.
        later = YouTubeAnalyticsSnapshot(
            upload_id=row.id, youtube_video_id=row.youtube_video_id, channel_id=CHANNEL, project_id=row.project_id,
            project_revision=1, render_revision=1, age_bucket="7d", published_age_hours=170, status="ok",
        )
        later.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name="engagedViews", value=float(views_for(index)) * 10, source="youtube_analytics_api"))
        db.add(later)
    db.commit()


WINDOWS = (12 * 60 + 40, 17 * 60 + 20, 21 * 60)  # 12:40, 17:20, 21:00 local


def test_learned_mode_is_unavailable_below_the_sample_threshold(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    seed_published_shorts(db, 12, minute_for=lambda i: WINDOWS[i % 3], views_for=lambda i: 100 + i)
    analysis = schedule_learning.analyze(db, schedule_authority.get_schedule(db, CHANNEL))
    assert not analysis["available"] and analysis["eligible_count"] == 12
    assert "30" in analysis["reason"]
    response = api_client(db, settings, store, fake).post("/api/youtube/schedule/learned/apply")
    assert response.status_code == 409


def test_learned_recommendation_needs_approval_and_states_its_sample(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    # Synthetic channel: 18:00-20:00 (no current slot) does best, 20:00-22:00 worst.
    minutes = (12 * 60 + 40, 17 * 60 + 20, 19 * 60 + 10, 21 * 60)
    views = {minutes[0]: 120, minutes[1]: 130, minutes[2]: 150, minutes[3]: 60}
    seed_published_shorts(db, 44, minute_for=lambda i: minutes[i % 4], views_for=lambda i: views[minutes[i % 4]] + i % 5)
    record = schedule_authority.get_schedule(db, CHANNEL)
    analysis = schedule_learning.analyze(db, record)
    assert analysis["available"] and analysis["based_on"] == 44 and analysis["reference_age_bucket"] == "24h"
    assert analysis["auto_applied"] is False and analysis["language"] == "association_only"
    # Windows that contain a current slot keep it; the new window gets its median time.
    assert (analysis["current"], analysis["suggested"], analysis["differs"]) == (["12:30", "17:00", "21:30"], ["12:30", "17:00", "19:00"], True)
    windows = {item["window"]: item for item in analysis["windows"]}
    assert windows["18:00–20:00"]["n"] == 11 and windows["18:00–20:00"]["median_normalized"] > 1 > windows["20:00–22:00"]["median_normalized"]
    # Nothing changed until the user approves.
    assert schedule_authority.slot_map(db, CHANNEL)[None] == ("12:30", "17:00", "21:30")
    assert schedule_authority.get_schedule(db, CHANNEL).mode == "seed"
    applied = api_client(db, settings, store, fake).post("/api/youtube/schedule/learned/apply").json()
    assert applied["schedule"]["mode"] == "learned" and applied["schedule"]["learned_sample_size"] == 44
    assert applied["schedule"]["slots"] == ["12:30", "17:00", "19:00"]


def test_schedule_settings_api_roundtrip(db, settings, store, fake):
    connect(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    initial = client.get(f"/api/youtube/schedule?timezone={BERLIN}").json()
    assert initial["schedule"]["videos_per_day"] == 1 and initial["learning"]["available"] is False
    assert len(initial["days"]) == 7
    bad = client.put("/api/youtube/schedule", json={"videos_per_day": 3, "timezone": BERLIN, "slots": ["12:00", "12:00", "20:00"]})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"]
    good = client.put("/api/youtube/schedule", json={"videos_per_day": 3, "timezone": BERLIN, "slots": ["12:00", "16:30", "21:00"]}).json()
    assert (good["schedule"]["mode"], good["schedule"]["slots"]) == ("manual", ["12:00", "16:30", "21:00"])
    assert client.get("/api/youtube/schedule").json()["schedule"]["slots"] == ["12:00", "16:30", "21:00"]


# ---------------------------------------------------------------------------
# Manual date/time override: prefill, never lock; conflicts warned, never rewritten
# ---------------------------------------------------------------------------


def shifted(choice: dict, minutes: int) -> dict:
    hour, minute = map(int, choice["time"].split(":"))
    total = hour * 60 + minute + minutes
    return {**choice, "time": f"{total // 60:02d}:{total % 60:02d}"}


def manual(choice: dict) -> dict:
    return publish_options(visibility="schedule", schedule=choice, schedule_source="manual").model_dump()


def free_morning() -> dict:
    """10:00 in two days: 150 min from the nearest 3/day slot, always in the future."""
    day = (datetime.now(ZoneInfo(BERLIN)) + timedelta(days=2)).date().isoformat()
    return {"date": day, "time": "10:00", "timezone": BERLIN}


def preflight(client, options: dict) -> dict:
    return client.post(f"/api/youtube/projects/{PID}/preflight", json={"options": options}).json()


def test_recommendation_prefills_date_and_time_as_an_editable_auto_choice(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    draft = api_client(db, settings, store, fake).get(f"/api/youtube/projects/{PID}/draft").json()
    recommendation = draft["smart_schedule"]["recommendation"]
    assert draft["options"]["schedule"] == recommendation["choice"]  # date + time + zone prefilled
    assert draft["options"]["schedule_source"] == "auto"
    assert set(recommendation["choice"]) == {"date", "time", "timezone"}


def test_manual_time_conflict_is_warned_and_needs_keep_anyway(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)
    first = client.get("/api/youtube/schedule/next").json()["recommendation"]
    fake.add_studio_video("studio", publish_at=first["publish_at"])
    client.post("/api/youtube/schedule/refresh")
    before = client.get("/api/youtube/schedule").json()["schedule"]
    chosen = shifted(first["choice"], 20)  # 20 minutes after the Studio video
    checked = preflight(client, manual(chosen))
    # A warning, not a blocking issue; the user's time is left exactly as chosen.
    assert checked["issues"] == [] and checked["schedule"]["local_time"] == f"{chosen['date']}T{chosen['time']}"
    conflict = checked["schedule_conflict"]
    assert (conflict["reason"], conflict["message"]) == ("occupied", "Selected time conflicts with another scheduled video.")
    assert conflict["recommendation"]["publish_at"] != first["publish_at"]  # "Use next recommended slot"
    refused = post_upload(client, PID, manual(chosen))
    assert refused.status_code == 409 and refused.json()["detail"]["status"] == "schedule_conflict"
    assert refused.json()["detail"]["conflict"]["recommendation"]["choice"] == conflict["recommendation"]["choice"]
    kept = post_upload(client, PID, manual(chosen), accept_schedule_conflict=True)
    assert kept.status_code == 202
    row = db.get(YouTubeUpload, kept.json()["upload"]["id"])
    assert (row.schedule_source, row.schedule_slot_time, row.schedule_local_time) == ("manual", None, f"{chosen['date']}T{chosen['time']}")
    assert row.upload_settings["options"]["schedule"] == chosen  # the exact choice persists
    after = client.get("/api/youtube/schedule").json()["schedule"]
    assert after == before  # per-upload only: the channel's Smart Schedule is unchanged


def test_manual_time_on_a_full_day_is_warned(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 1)
    build_project(db, settings)
    client = api_client(db, settings, store, fake)
    tomorrow = (datetime.now(ZoneInfo(BERLIN)) + timedelta(days=1)).date().isoformat()
    fake.add_studio_video("morning", publish_at=z(at(f"{tomorrow}T09:00")))
    client.post("/api/youtube/schedule/refresh")
    conflict = preflight(client, manual({"date": tomorrow, "time": "20:00", "timezone": BERLIN}))["schedule_conflict"]
    assert conflict["reason"] == "day_full" and "1-video/day target" in conflict["message"]


def test_free_manual_time_has_no_warning_and_uploads_directly(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)
    chosen = free_morning()  # away from every slot and video
    assert preflight(client, manual(chosen))["schedule_conflict"] is None
    response = post_upload(client, PID, manual(chosen))
    assert response.status_code == 202
    assert db.get(YouTubeUpload, response.json()["upload"]["id"]).schedule_source == "manual"


def test_manual_time_inside_the_lead_time_is_an_inline_error_never_moved(db, settings, store, fake):
    connect(db, settings, store, fake)
    build_project(db, settings)
    client = api_client(db, settings, store, fake)
    soon = datetime.now(ZoneInfo(BERLIN)) + timedelta(minutes=5)
    chosen = {"date": soon.date().isoformat(), "time": soon.strftime("%H:%M"), "timezone": BERLIN}
    checked = preflight(client, manual(chosen))
    assert {"field": "schedule", "message": "Choose a time at least 15 minutes from now."} in checked["issues"]
    assert checked["schedule"]["status"] == "past" and checked["schedule_conflict"] is None
    refused = post_upload(client, PID, manual(chosen))
    assert refused.status_code == 422 and refused.json()["detail"]["status"] == "preflight_failed"


def test_auto_and_manual_provenance_on_the_upload_record(db, settings, store, fake):
    connect(db, settings, store, fake)
    configure(db, 3)
    build_project(db, settings)
    build_project(db, settings, project_id=PID_B, content=b"\x02" * 30_000)
    client = api_client(db, settings, store, fake, dispatch=lambda *_: None)
    recommendation = client.get("/api/youtube/schedule/next").json()["recommendation"]
    auto = post_upload(client, PID, auto_options(recommendation["choice"]))
    row = db.get(YouTubeUpload, auto.json()["upload"]["id"])
    assert (row.schedule_source, row.schedule_slot_time) == ("auto", recommendation["local_time"])
    # The same wall time chosen by hand stays "manual": provenance follows the user's action.
    by_hand = post_upload(client, PID_B, manual(free_morning()))
    other = db.get(YouTubeUpload, by_hand.json()["upload"]["id"])
    assert (other.schedule_source, other.schedule_slot_time) == ("manual", None)
