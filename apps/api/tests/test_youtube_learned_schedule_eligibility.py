"""Learned schedule eligibility: canonical content type, bounded bucket windows
and late-snapshot safety (fake Google only)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from test_youtube_learning_loop import (
    CURVE,
    METRICS,
    connect,
    exported_project,
    published,
    upload_now,
)
from youtube_support import FakeYouTube, analytics_payload, youtube_settings

from clipforge.models import YouTubeAnalyticsSnapshot, YouTubeMetricValue, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import (
    analytics,
    connection,
    content_type,
    publishing,
    schedule_learning,
    uploads,
)
from clipforge.youtube import schedule as schedule_authority
from clipforge.youtube import slots as planner

BERLIN = "Europe/Berlin"
CHANNEL = "UC_fake_channel_01"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
BUCKET_HOURS = {"24h": 24, "72h": 72, "7d": 168}


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()


@pytest.fixture()
def settings(tmp_path):
    return youtube_settings(tmp_path)


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def store():
    return SecretStore()


def schedule(db, count: int = 3):
    schedule_authority.save_schedule(db, CHANNEL, schedule_authority.ScheduleUpdate(
        videos_per_day=count, timezone=BERLIN, slots=list(planner.SEED_PRESETS[count]),
    ))
    return schedule_authority.get_schedule(db, CHANNEL)


def seed(
    db, index: int, *, kind: str | None = "shorts", buckets: dict[str, float] | None = None, published_at: datetime | None = None,
    late_end: dict[str, date] | None = None, deleted: bool = False, scheduled: bool = False, views: float = 100.0, minute: int = 12 * 60 + 40,
) -> YouTubeUpload:
    """One mapped upload with API snapshots; ``buckets`` maps a bucket to the age it was captured at.

    A snapshot's window ends at its bucket's day (a bounded capture) unless
    ``late_end`` gives a legacy, open-ended end date.
    """
    published_at = published_at or datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=index, minutes=minute)
    row = YouTubeUpload(
        project_id=str(uuid.uuid4()), project_revision=1, render_revision=1, render_sha256=f"{index:064d}", render_file_size=10,
        channel_id=CHANNEL, youtube_video_id=f"elig{index:04d}", state="ready", content_type=kind,
        published_at=None if scheduled else published_at, deleted_on_youtube=deleted, schedule_source="auto",
    )
    db.add(row)
    db.flush()
    for bucket, age in (buckets or {}).items():
        end = (late_end or {}).get(bucket) or analytics.bucket_end_date(published_at, bucket)
        snapshot = YouTubeAnalyticsSnapshot(
            upload_id=row.id, youtube_video_id=row.youtube_video_id, channel_id=CHANNEL, project_id=row.project_id,
            project_revision=1, render_revision=1, age_bucket=bucket, published_age_hours=age, status="ok",
            date_range={"start": (published_at - timedelta(days=1)).date().isoformat(), "end": end.isoformat()},
            fetched_at=published_at + timedelta(hours=age),
        )
        snapshot.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name="engagedViews", value=views, source=analytics.SOURCE_API))
        db.add(snapshot)
    db.commit()
    return row


def analyze(db, count: int = 3) -> dict:
    return schedule_learning.analyze(db, schedule(db, count))


# ---------------------------------------------------------------------------
# One canonical content type: writer stores it, readers accept any casing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("shorts", "SHORTS"), ("SHORTS", "SHORTS"), ("Shorts", "SHORTS"), (" shorts ", "SHORTS"),
    ("videoOnDemand", "VIDEO_ON_DEMAND"), ("VIDEO_ON_DEMAND", "VIDEO_ON_DEMAND"), ("liveStream", "LIVE_STREAM"),
    ("unspecified", None), ("UNSPECIFIED", None), ("", None), (None, None), (7, None),
])
def test_content_type_normalizes_to_one_canonical_form(raw, expected):
    assert content_type.normalize(raw) == expected
    assert content_type.is_short(raw) is (expected == "SHORTS")


def test_lowercase_shorts_are_eligible(db):
    for index in range(4):
        seed(db, index, kind="shorts", buckets={"72h": 75})
    result = analyze(db)
    assert (result["eligible_count"], result["shorts_count"], result["reference_age_bucket"]) == (4, 4, "72h")


def test_uppercase_shorts_from_legacy_or_api_data_are_eligible(db):
    for index, kind in enumerate(("SHORTS", "Shorts", "shorts")):
        seed(db, index, kind=kind, buckets={"24h": 25})
    assert analyze(db)["eligible_count"] == 3


def test_null_and_other_content_types_stay_ineligible(db):
    seed(db, 0, kind=None, buckets={"24h": 25})
    seed(db, 1, kind="videoOnDemand", buckets={"24h": 25})
    seed(db, 2, kind="unspecified", buckets={"24h": 25})
    seed(db, 3, kind="shorts", buckets={"24h": 25})
    result = analyze(db)
    assert (result["published_count"], result["shorts_count"], result["eligible_count"]) == (4, 1, 1)


def test_existing_lowercase_rows_need_no_migration(db):
    row = seed(db, 0, buckets={"7d": 170})
    db.execute(text("UPDATE youtube_uploads SET content_type = 'shorts' WHERE id = :id"), {"id": row.id})
    db.commit()
    assert analyze(db)["eligible_count"] == 1
    # Read-time normalisation: the stored value is left as it was, the API shows the canonical one.
    assert db.execute(text("SELECT content_type FROM youtube_uploads WHERE id = :id"), {"id": row.id}).scalar() == "shorts"
    assert uploads.serialize_upload(db.get(YouTubeUpload, row.id))["content_type"] == "SHORTS"


def test_fetch_content_type_returns_the_canonical_value():
    class Provider:
        def __init__(self, value):
            self.value = value

        def analytics_report(self, token, params):
            return {"columnHeaders": [{"name": "creatorContentType"}, {"name": "views"}], "rows": [[self.value, 10]]}

    day = date(2026, 10, 1)
    assert analytics.fetch_content_type(Provider("shorts"), "t", "v", day, day)[0] == "SHORTS"
    assert analytics.fetch_content_type(Provider("videoOnDemand"), "t", "v", day, day)[0] == "VIDEO_ON_DEMAND"
    assert analytics.fetch_content_type(Provider("unspecified"), "t", "v", day, day)[0] is None


def test_capture_stores_the_canonical_content_type(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store, when="2026-10-04T10:00:00Z")
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE, content_type="shorts")  # what the API really answers
    analytics.refresh_analytics(db, upload, settings, store, fake, now=datetime(2026, 10, 5, 11, 0, tzinfo=UTC))
    snapshot = db.scalars(select(YouTubeAnalyticsSnapshot)).one()
    assert (db.get(YouTubeUpload, upload.id).content_type, snapshot.content_type) == ("SHORTS", "SHORTS")


# ---------------------------------------------------------------------------
# Bucket captures use a window bounded to the bucket's age
# ---------------------------------------------------------------------------


def test_report_window_is_bounded_per_bucket_and_open_for_manual_captures():
    published_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    start = date(2026, 8, 31)
    assert analytics.report_window(published_at, now, "24h") == (start, date(2026, 9, 2))
    assert analytics.report_window(published_at, now, "72h") == (start, date(2026, 9, 4))
    assert analytics.report_window(published_at, now, "7d") == (start, date(2026, 9, 8))
    assert analytics.report_window(published_at, now, None) == (start, date(2026, 10, 5))
    assert analytics.report_window(published_at, now, "manual") == (start, date(2026, 10, 5))
    # Never past today, even for a bucket the video has not reached on the calendar yet.
    assert analytics.report_window(published_at, datetime(2026, 9, 1, 15, 0, tzinfo=UTC), "7d")[1] == date(2026, 9, 1)


def _days_handler(calls: list[dict]):
    """Engaged views grow by 100 per reported day, so a window's length shows in the numbers."""
    inner = analytics_payload(METRICS, curve=CURVE, content_type="shorts")

    def handler(params):
        calls.append(params)
        response = inner(params)
        if params["dimensions"] == "video":
            days = (date.fromisoformat(params["endDate"]) - date.fromisoformat(params["startDate"])).days
            names = params["metrics"].split(",")
            response["rows"] = [[response["rows"][0][0], *(100.0 * days if name == "engagedViews" else METRICS.get(name, 0) for name in names)]]
        return response

    return handler


@pytest.mark.parametrize(("bucket", "age_hours"), [("24h", 30), ("72h", 80)])
def test_bucket_capture_uses_a_bounded_window(db, settings, store, fake, bucket, age_hours):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store, when="2026-09-20T14:00:00Z")
    calls: list[dict] = []
    fake.analytics_handler = _days_handler(calls)
    published_at = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=published_at + timedelta(hours=age_hours), due_only=True)
    assert result["age_bucket"] == bucket
    expected_end = (published_at + timedelta(hours=BUCKET_HOURS[bucket])).date().isoformat()
    assert {call["endDate"] for call in calls} == {expected_end}
    snapshot = db.scalars(select(YouTubeAnalyticsSnapshot)).one()
    assert snapshot.date_range["end"] == expected_end and snapshot.date_range["bounded_to_bucket"] is True


def test_late_7d_capture_does_not_hold_lifetime_metrics(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store, when="2026-08-26T14:00:00Z")
    calls: list[dict] = []
    fake.analytics_handler = _days_handler(calls)
    # First capture 40 days after publication (an older video): only 7d is due.
    result = analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW, due_only=True)
    assert result["age_bucket"] == "7d"
    assert {call["endDate"] for call in calls} == {"2026-09-02"}  # published + 7 days, not today (2026-10-05)
    snapshot = db.scalars(select(YouTubeAnalyticsSnapshot)).one()
    assert snapshot.published_age_hours > 900  # the real age stays recorded
    assert snapshot.metrics and next(item.value for item in snapshot.metrics if item.name == "engagedViews") == 100.0 * 8  # 8 calendar days, not 41


def test_manual_capture_still_reads_the_numbers_so_far(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload = upload_now(db, exported_project(db, settings), settings, store, fake)
    published(db, upload, fake, settings, store, when="2026-08-26T14:00:00Z")
    calls: list[dict] = []
    fake.analytics_handler = _days_handler(calls)
    analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW, due_only=True)  # 7d
    calls.clear()
    assert analytics.refresh_analytics(db, upload, settings, store, fake, now=NOW)["age_bucket"] == "manual"
    assert {call["endDate"] for call in calls} == {"2026-10-05"}


# ---------------------------------------------------------------------------
# Late (legacy, open-ended) bucket snapshots never contaminate learning
# ---------------------------------------------------------------------------


def test_late_bucket_snapshot_is_excluded_and_due_again(db):
    published_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    # Legacy "7d" row captured on day 40 with a window ending that day: lifetime data.
    late = seed(db, 0, buckets={"7d": 960}, published_at=published_at, late_end={"7d": date(2026, 9, 29)}, views=99_999)
    result = analyze(db)
    assert (result["eligible_count"], result["shorts_count"], result["late_snapshot_count"]) == (0, 1, 1)
    history = list(db.scalars(select(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == late.id)).all())
    age = (NOW - published_at).total_seconds() / 3600
    assert analytics.due_bucket(history, age, NOW, published_at) == "7d"  # recaptured with a bounded window
    assert analytics.due_bucket(history, age, NOW) is None  # (without published_at: legacy behaviour)


def test_slightly_late_legacy_capture_is_kept_and_a_bounded_one_is_valid(db):
    published_at = datetime(2026, 8, 20, 22, 0, tzinfo=UTC)
    # Taken 8h late; its open-ended window ends one day after the bucket's day.
    seed(db, 0, buckets={"72h": 80}, published_at=published_at, late_end={"72h": date(2026, 8, 24)})
    seed(db, 1, buckets={"72h": 200}, published_at=published_at)  # bounded capture, whatever the real age
    result = analyze(db)
    assert (result["eligible_count"], result["late_snapshot_count"]) == (2, 0)


def test_valid_and_late_snapshots_of_one_bucket_use_the_valid_one(db):
    published_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    row = seed(db, 0, buckets={"7d": 170}, published_at=published_at, views=500)
    late = YouTubeAnalyticsSnapshot(
        upload_id=row.id, youtube_video_id=row.youtube_video_id, channel_id=CHANNEL, project_id=row.project_id,
        project_revision=1, render_revision=1, age_bucket="7d", published_age_hours=900, status="ok",
        date_range={"start": "2026-08-19", "end": "2026-09-26"}, fetched_at=published_at + timedelta(hours=900),
    )
    late.metrics.append(YouTubeMetricValue(youtube_video_id=row.youtube_video_id, name="engagedViews", value=99_999, source=analytics.SOURCE_API))
    db.add(late)
    db.commit()
    rows = schedule_learning.publication_rows(db, CHANNEL, BERLIN)
    assert rows[0]["buckets"]["7d"]["engagedViews"] == 500


# ---------------------------------------------------------------------------
# The status states the real counts; the threshold stays 30
# ---------------------------------------------------------------------------


def test_learned_status_reports_the_real_counts(db):
    """The confirmed channel: 18 published (12 shorts, 6 unconfirmed), 11 with 72h, 3 with 24h."""
    for index in range(12):
        buckets = {"72h": 75.0} if index < 11 else {}
        if index < 3:
            buckets["24h"] = 25.0
        seed(db, index, kind="shorts", buckets=buckets)
    for index in range(12, 18):
        seed(db, index, kind=None)
    seed(db, 18, scheduled=True, buckets={"72h": 75})
    seed(db, 19, deleted=True, buckets={"72h": 75})
    result = analyze(db)
    assert (result["eligible_count"], result["published_count"], result["shorts_count"]) == (11, 18, 12)
    assert result["reference_age_bucket"] == "72h" and not result["available"]
    assert result["reason"] == "Needs at least 30 Shorts with 72h analytics; has 11 (18 published, 12 confirmed as Shorts by YouTube)."


def test_status_without_any_bucket_data_does_not_claim_a_single_age(db):
    seed(db, 0, kind=None)
    result = analyze(db)
    assert result["reason"] == "Needs at least 30 Shorts with 24h/72h/7d analytics; has 0 (1 published, 0 confirmed as Shorts by YouTube)."


def test_threshold_stays_30(db):
    assert schedule_learning.LEARNED_MIN_ELIGIBLE == 30
    for index in range(29):
        seed(db, index, buckets={"24h": 25})
    below = analyze(db, 1)
    assert not below["available"] and below["eligible_count"] == 29 and below["min_eligible"] == 30
    seed(db, 29, buckets={"24h": 25})
    at_threshold = analyze(db, 1)
    assert at_threshold["available"] and at_threshold["based_on"] == 30


def test_scheduled_and_deleted_videos_stay_excluded(db):
    seed(db, 0, scheduled=True, buckets={"24h": 25})
    seed(db, 1, deleted=True, buckets={"24h": 25})
    seed(db, 2, buckets={"24h": 25})
    result = analyze(db)
    assert (result["published_count"], result["shorts_count"], result["eligible_count"]) == (1, 1, 1)
