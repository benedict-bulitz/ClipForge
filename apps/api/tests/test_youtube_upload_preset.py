"""Last-used upload preset: per channel, successful uploads only, never project content."""
from __future__ import annotations

import copy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from test_youtube_learning_loop import connect
from test_youtube_publishing_v2 import api_client, build_project, upload
from youtube_support import FakeYouTube, publish_options, youtube_settings

from clipforge.models import ProjectRevision
from clipforge.youtube import connection, publishing, uploads
from clipforge.youtube import schedule as schedule_authority
from clipforge.youtube.provider import ChannelIdentity, YouTubeApiError
from clipforge.youtube.publishing import ScheduleChoice, UploadDefaults

BEFORE = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
OTHER = "33333333-3333-4333-8333-333333333333"
USED = {
    "made_for_kids": True, "contains_synthetic_media": True, "visibility": "schedule",
    "schedule": {"date": "2026-09-28", "time": "20:30", "timezone": "Europe/Berlin"},
    "category_id": "28", "default_language": "de", "default_audio_language": "de", "license": "creativeCommon",
    "embeddable": False, "public_stats_viewable": False, "notify_subscribers": False, "paid_product_placement": True,
    "title": "Old video title", "description": "Old description", "tags": ["old", "tags"],
}


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
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
    from clipforge.security.secrets import SecretStore

    return SecretStore()


def first_upload(db, settings, store, fake):
    connect(db, settings, store, fake)
    return upload(db, build_project(db, settings), settings, store, fake, publish_options(**USED), now=BEFORE)


def second_project(db, settings):
    """Another project with its own YouTube metadata and cover."""
    project = build_project(db, settings, project_id=OTHER, content=b"\x01" * 30_000)
    state = copy.deepcopy(project.revisions[0].state)
    state["social_metadata"] = {"status": "available", "platforms": {"youtube": {"title": "Why do cats purr?", "description": "Purring explained.", "hashtags": ["#cats", "#science"]}}}
    project.revisions.append(ProjectRevision(number=2, parent_revision=1, instruction="Edit social hashtags", kind="user", state=state, changed_components=["social_metadata"]))
    project.current_revision = project.active_tip_revision = 2
    db.commit()
    return project


def test_successful_upload_stores_only_reusable_settings(db, settings, store, fake):
    row = first_upload(db, settings, store, fake)
    assert row.youtube_video_id
    preset = publishing.last_used_preset(db, "UC_fake_channel_01")
    for key in ("made_for_kids", "contains_synthetic_media", "visibility", "category_id", "default_language", "license", "embeddable", "public_stats_viewable", "notify_subscribers", "paid_product_placement"):
        assert preset[key] == USED[key], key
    assert (preset["timezone"], preset["schedule_time"]) == ("Europe/Berlin", "20:30")
    for leaked in ("title", "description", "tags", "thumbnail", "schedule", "date"):
        assert leaked not in preset


def test_next_project_gets_the_preset_but_its_own_content(db, settings, store, fake):
    first_upload(db, settings, store, fake)
    # Smart Schedule off: the last-used time of day is the schedule source
    # (with it on, the planner wins - see test_youtube_smart_schedule).
    schedule_authority.ensure_schedule(db, "UC_fake_channel_01").enabled = False
    db.commit()
    second_project(db, settings)
    draft = api_client(db, settings, store, fake).get(f"/api/youtube/projects/{OTHER}/draft").json()
    options = draft["options"]
    assert draft["preset_source"] == "last_upload"
    assert (options["made_for_kids"], options["contains_synthetic_media"]) == (True, True)
    assert (options["visibility"], options["category_id"], options["license"]) == ("schedule", "28", "creativeCommon")
    assert (options["embeddable"], options["public_stats_viewable"], options["notify_subscribers"], options["paid_product_placement"]) == (False, False, False, True)
    assert options["default_language"] == "de"
    # content comes from this project only
    assert options["title"] == "Why do cats purr?"
    assert options["tags"] == ["cats", "science"] and "Purring explained." in options["description"]
    assert "Old" not in options["title"] + options["description"]
    assert options["thumbnail"] == {"source": "generated", "asset": "youtube-cover-3"}
    selected = next(item for item in draft["thumbnails"] if item["asset"] == "youtube-cover-3")
    assert f"/media/{OTHER}/" in selected["url"]  # this project's own cover file
    # schedule: same time of day and zone, a valid future date (never the old one)
    schedule = options["schedule"]
    assert (schedule["time"], schedule["timezone"]) == ("20:30", "Europe/Berlin")
    assert schedule["date"] != "2026-09-28" or datetime.now(UTC) < datetime(2026, 9, 28, 18, 15, tzinfo=UTC)
    assert publishing.resolve_schedule(ScheduleChoice(**schedule)).status == "ok"


def test_preset_is_scoped_by_channel(db, settings, store, fake):
    first_upload(db, settings, store, fake)
    fake.channel = ChannelIdentity("UC_channel_B", "Other channel")
    channel_b = connect(db, settings, store, fake)
    second_project(db, settings)
    # Channel A stays connected (multi-account); the sheet targets the selected channel B.
    draft = api_client(db, settings, store, fake).get(f"/api/youtube/projects/{OTHER}/draft", params={"account_id": channel_b.id}).json()
    assert draft["account"]["channel_id"] == "UC_channel_B"
    assert draft["preset_source"] == "settings"
    assert draft["options"]["made_for_kids"] is None
    # No preset or saved visibility on this channel: Smart Schedule's default workflow.
    assert (draft["options"]["visibility"], draft["options"]["schedule_source"]) == ("schedule", "auto")
    assert publishing.last_used_preset(db, "UC_channel_B") is None


@pytest.mark.parametrize("failure", ["upload_error", "preflight"])
def test_failed_or_blocked_upload_does_not_update_the_preset(db, settings, store, fake, failure):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    if failure == "upload_error":
        fake.start_error = YouTubeApiError("quota_exceeded", "Quota exceeded.", retryable=True)
        row = upload(db, project, settings, store, fake, publish_options(**USED), now=BEFORE)
        assert row.state == "failed"
    else:
        with pytest.raises(uploads.UploadRefused):
            uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options(**{**USED, "made_for_kids": None}), now=BEFORE)
    assert publishing.last_used_preset(db, "UC_fake_channel_01") is None


def test_failed_schedule_change_keeps_the_previous_preset(db, settings, store, fake):
    row = first_upload(db, settings, store, fake)
    fake.update_error = YouTubeApiError("bad_request", "The video's publish time is invalid.")
    with pytest.raises(YouTubeApiError):
        uploads.schedule_publication(db, row, ScheduleChoice(date="2026-09-29", time="07:15", timezone="Europe/Berlin"), settings, store, fake, now=BEFORE)
    assert publishing.last_used_preset(db, "UC_fake_channel_01")["schedule_time"] == "20:30"
    fake.update_error = None
    uploads.schedule_publication(db, row, ScheduleChoice(date="2026-09-29", time="07:15", timezone="Europe/Berlin"), settings, store, fake, now=BEFORE)
    assert publishing.last_used_preset(db, "UC_fake_channel_01")["schedule_time"] == "07:15"


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 9, 28, 18, 0, tzinfo=UTC), "2026-09-28"),  # 20:00 Berlin: 20:30 today is fine
    (datetime(2026, 9, 28, 18, 20, tzinfo=UTC), "2026-09-29"),  # 20:20: less than the 15-min lead
    (datetime(2026, 9, 28, 19, 0, tzinfo=UTC), "2026-09-29"),  # 21:00: today's slot is past
])
def test_past_dates_are_never_suggested(now, expected):
    suggestion = publishing.suggest_schedule("20:30", "Europe/Berlin", now=now)
    assert suggestion.date == expected and suggestion.time == "20:30"
    assert publishing.resolve_schedule(suggestion, now=now).status == "ok"


def test_suggestion_skips_dst_gaps_and_refuses_unsafe_input():
    now = datetime(2027, 3, 27, 23, 0, tzinfo=UTC)  # 00:00 on 28 March in Berlin; 02:30 does not exist
    suggestion = publishing.suggest_schedule("02:30", "Europe/Berlin", now=now)
    assert suggestion.date == "2027-03-29"
    assert publishing.suggest_schedule(None, "Europe/Berlin") is None
    assert publishing.suggest_schedule("25:99", "Europe/Berlin") is None
    assert publishing.suggest_schedule("20:30", "Mars/Base") is None


def test_explicitly_saved_settings_newer_than_the_preset_win(db, settings, store, fake):
    first_upload(db, settings, store, fake)
    preset = publishing.last_used_preset(db, "UC_fake_channel_01")
    assert publishing.preset_applies(db, preset)
    publishing.save_defaults(db, UploadDefaults(made_for_kids=False, visibility="private"))
    assert not publishing.preset_applies(db, preset)
    assert publishing.last_used_preset(db, "UC_fake_channel_01") == preset  # Settings save keeps the preset
    options = publishing.options_with_defaults({"title": "t"}, publishing.load_defaults(db), None)
    assert (options["made_for_kids"], options["visibility"], options["schedule"]) == (False, "private", None)


def test_suggested_date_is_computed_in_the_chosen_zone():
    now = datetime(2026, 9, 28, 22, 30, tzinfo=UTC)  # already 29 Sept in Berlin (00:30)
    suggestion = publishing.suggest_schedule("09:00", "Europe/Berlin", now=now)
    assert suggestion.date == now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat() == "2026-09-29"
