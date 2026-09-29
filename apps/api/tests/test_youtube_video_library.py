"""Video Library V1: the project-independent view of uploaded videos (fake Google only)."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import event, select
from test_youtube_learning_loop import CURVE, METRICS, connect
from test_youtube_publishing_v2 import (
    PID,
    _published_with_analytics,
    api_client,
    build_project,
    files_under,
    generated_thumbnail,
    upload,
)
from youtube_support import FakeYouTube, analytics_payload, publish_options, youtube_settings

from clipforge.main import app
from clipforge.models import Project, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import connection, library, lifecycle, publishing, uploads
from clipforge.youtube.provider import YouTubeApiError

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
OTHER = "33333333-3333-4333-8333-333333333333"
THIRD = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()
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


def project_with_upload(db, settings, store, fake, project_id: str, *, title: str, content: bytes, prompt: str | None = None):
    project = build_project(db, settings, project_id=project_id, content=content)
    if prompt is not None:
        project.title = title
        db.commit()
    return project, upload(db, project, settings, store, fake, publish_options(title=title, thumbnail=generated_thumbnail()))


def set_live_stats(db, row, fake, settings, store, views: int, likes: int = 1, comments: int = 0) -> None:
    fake.videos[row.youtube_video_id]["statistics"] = {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)}
    uploads.sync_status(db, row, settings, store, fake)


def library_page(client, **params):
    response = client.get("/api/videos", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def three_videos(db, settings, store, fake):
    """Published (with analytics), scheduled and private, in three projects."""
    connect(db, settings, store, fake)
    published = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    set_live_stats(db, published, fake, settings, store, views=900, likes=40, comments=3)
    _project, scheduled = project_with_upload(db, settings, store, fake, OTHER, title="Scheduled volcano short", content=b"S" * 20_000)
    fake.videos[scheduled.youtube_video_id]["status"].update(publishAt="2030-01-01T18:00:00Z", uploadStatus="processed")
    uploads.sync_status(db, scheduled, settings, store, fake)
    set_live_stats(db, scheduled, fake, settings, store, views=0)
    _project, private = project_with_upload(db, settings, store, fake, THIRD, title="Private concorde draft", content=b"P" * 20_000)
    fake.videos[private.youtube_video_id]["status"].update(uploadStatus="processed")
    uploads.sync_status(db, private, settings, store, fake)
    set_live_stats(db, private, fake, settings, store, views=50)
    stamps = {published.id: NOW - timedelta(days=3), scheduled.id: NOW - timedelta(days=1), private.id: NOW - timedelta(days=2)}
    for row in (published, scheduled, private):
        row.uploaded_at = stamps[row.id]
    db.commit()
    return published, scheduled, private


# ---------------------------------------------------------------------------
# Which videos appear
# ---------------------------------------------------------------------------


def test_successful_upload_appears_with_identity_state_and_links(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    client = api_client(db, settings, store, fake)
    page = library_page(client)
    assert page["total"] == 1 and page["summary"]["total"] == 1
    item = page["items"][0]
    assert item["id"] == row.id and item["youtube_video_id"] == row.youtube_video_id
    assert item["title"] == row.title and item["prompt"] == "Why are airplane windows round?"
    assert item["channel"] == {"id": "UC_fake_channel_01", "title": "Knowledge Lab"}
    assert item["state"] in {"private", "processing"} and item["state_label"]
    assert item["project"]["available"] is True and item["project"]["id"] == PID
    assert item["duration_seconds"] == 10.0 and item["scene_count"] == 4 and item["format"] == "explanation"
    assert item["watch_url"].endswith(row.youtube_video_id) and item["studio_url"] == f"https://studio.youtube.com/video/{row.youtube_video_id}/edit"
    # Not published: no analytics yet, and never a fake zero.
    assert item["analytics"]["state"] == "not_published" and item["analytics"]["views"] is None
    assert item["thumbnail_url"].startswith(f"/media/{library.LIBRARY_DIR}/{row.id}.")


def test_failed_uploads_do_not_appear(db, settings, store, fake):
    connect(db, settings, store, fake)
    failed_project = build_project(db, settings)
    fake.start_error = YouTubeApiError("quota_exceeded", "The YouTube quota is exhausted.")
    failed = upload(db, failed_project, settings, store, fake)
    assert failed.youtube_video_id is None and failed.state == "failed"
    fake.start_error = None
    _project, rejected = project_with_upload(db, settings, store, fake, OTHER, title="Rejected", content=b"R" * 20_000)
    fake.videos[rejected.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    uploads.sync_status(db, rejected, settings, store, fake)
    _project, good = project_with_upload(db, settings, store, fake, THIRD, title="Good", content=b"G" * 20_000)
    client = api_client(db, settings, store, fake)
    page = library_page(client)
    assert [item["id"] for item in page["items"]] == [good.id]
    assert client.get(f"/api/videos/{failed.id}").status_code == 404
    assert client.get(f"/api/videos/{rejected.id}").status_code == 404


def test_sql_membership_matches_the_deletion_lifecycle_classification(db, settings, store, fake):
    """One definition of a successful upload: the library's SQL == classify_upload."""
    connect(db, settings, store, fake)
    published = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    _p, rejected = project_with_upload(db, settings, store, fake, OTHER, title="Rejected", content=b"R" * 20_000)
    fake.videos[rejected.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    uploads.sync_status(db, rejected, settings, store, fake)
    # A video that was rejected later but already has analytics stays (historic learning data).
    published.upload_status = "rejected"
    db.commit()
    members = set(db.scalars(select(YouTubeUpload.id).where(library.library_condition())).all())
    for row in db.scalars(select(YouTubeUpload)).all():
        assert (row.id in members) == (lifecycle.classify_upload(db, row) == lifecycle.SUCCEEDED)
    assert published.id in members and rejected.id not in members


# ---------------------------------------------------------------------------
# Status, live stats, analytics summary
# ---------------------------------------------------------------------------


def test_published_scheduled_private_states_and_summary(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    page = library_page(client)
    by_id = {item["id"]: item for item in page["items"]}
    assert by_id[published.id]["state"] == "published" and by_id[published.id]["published_at"]
    assert by_id[scheduled.id]["state"] == "scheduled" and by_id[scheduled.id]["scheduled_for"].startswith("2030-01-01T18:00:00")
    assert by_id[private.id]["state"] == "private"
    summary = page["summary"]
    assert (summary["published"], summary["scheduled"], summary["private"], summary["total"]) == (1, 1, 1, 3)
    assert summary["analytics_available"] == 1 and summary["median_average_view_percentage"] == 74.0
    assert summary["median_average_view_percentage_n"] == 1
    # Live stats (Data API) and detailed analytics (Analytics API) are kept apart.
    item = by_id[published.id]
    assert item["live_stats"]["views"] == 900 and item["live_stats"]["source"] == "youtube_data_api_videos_list"
    assert item["analytics"]["views"] == 5400 and item["analytics"]["engagedViews"] == 2100
    assert item["analytics"]["averageViewPercentage"] == 74.0 and item["analytics"]["averageViewDuration"] == 7.4
    assert item["analytics"]["state"] == "available"


def test_published_video_without_analytics_is_processing_not_zero(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    fake.publish(row.youtube_video_id, "2026-09-10T11:00:00Z")
    uploads.sync_status(db, row, settings, store, fake)
    item = library_page(api_client(db, settings, store, fake))["items"][0]
    assert item["state"] == "published" and item["analytics"]["state"] == "processing"
    assert all(item["analytics"][name] is None for name in library.SUMMARY_METRICS)
    assert item["live_stats"] is None  # no statistics reported yet: "—", not 0


def test_deleted_on_youtube_with_learning_data_still_appears(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    del fake.videos[row.youtube_video_id]
    uploads.sync_status(db, row, settings, store, fake)
    client = api_client(db, settings, store, fake)
    item = library_page(client, status="deleted")["items"][0]
    assert item["id"] == row.id and item["state"] == "deleted" and item["state_label"] == "Deleted"
    assert client.get(f"/api/videos/{row.id}").json()["performance"]["status"] == "ready"


def test_remote_deleted_video_with_existing_project_has_no_dead_youtube_links(db, settings, store, fake):
    """Remote deletion and project deletion are independent: the project stays openable,
    the YouTube/Studio actions are withdrawn with a reason."""
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    del fake.videos[row.youtube_video_id]
    uploads.sync_status(db, row, settings, store, fake)
    assert db.get(Project, PID) is not None
    client = api_client(db, settings, store, fake)
    item = library_page(client)["items"][0]
    assert item["state"] == "deleted" and item["project"]["available"] is True
    assert item["youtube_actions"] == {"available": False, "reason": library.REMOTE_DELETED_REASON}
    assert item["watch_url"] is None and item["shorts_url"] is None and item["studio_url"] is None
    detail = client.get(f"/api/videos/{row.id}").json()["video"]
    assert detail["project"]["available"] is True and detail["youtube_actions"]["available"] is False
    assert detail["watch_url"] is None and detail["studio_url"] is None
    # the remote deletion does not remove the learning record or the project link
    assert detail["project"]["id"] == PID


def test_live_video_keeps_youtube_actions_even_after_project_deletion(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    item = library_page(api_client(db, settings, store, fake))["items"][0]
    assert item["project"]["available"] is False and item["youtube_actions"]["available"] is True
    assert item["studio_url"] == f"https://studio.youtube.com/video/{row.youtube_video_id}/edit"


def test_scheduled_video_placeholder_counters_are_not_shown_as_zero(db, settings, store, fake):
    connect(db, settings, store, fake)
    _project, scheduled = project_with_upload(db, settings, store, fake, OTHER, title="Scheduled", content=b"S" * 20_000)
    fake.videos[scheduled.youtube_video_id]["status"].update(publishAt="2030-01-01T18:00:00Z", uploadStatus="processed")
    scheduled.schedule_timezone = "Europe/Berlin"
    db.commit()
    set_live_stats(db, scheduled, fake, settings, store, views=0, likes=0, comments=0)
    assert scheduled.remote_view_count == 0  # what videos.list returned for the private video
    client = api_client(db, settings, store, fake)
    item = library_page(client)["items"][0]
    assert item["state"] == "scheduled" and item["live_stats"] is None and item["live_stats_state"] == "not_published"
    assert item["scheduled_for"].startswith("2030-01-01T18:00:00") and item["schedule_timezone"] == "Europe/Berlin"
    detail = client.get(f"/api/videos/{scheduled.id}").json()["video"]
    assert detail["live_stats"] is None and detail["live_stats_state"] == "not_published"


def test_published_video_keeps_a_real_zero(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    fake.publish(row.youtube_video_id, "2026-09-10T11:00:00Z")
    set_live_stats(db, row, fake, settings, store, views=0, likes=0, comments=0)
    client = api_client(db, settings, store, fake)
    item = library_page(client)["items"][0]
    assert item["live_stats_state"] == "available"
    assert (item["live_stats"]["views"], item["live_stats"]["likes"], item["live_stats"]["comments"]) == (0, 0, 0)
    assert client.get(f"/api/videos/{row.id}").json()["video"]["live_stats"]["views"] == 0


def test_published_video_without_statistics_is_not_reported(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    fake.publish(row.youtube_video_id, "2026-09-10T11:00:00Z")
    uploads.sync_status(db, row, settings, store, fake)
    item = library_page(api_client(db, settings, store, fake))["items"][0]
    assert item["live_stats"] is None and item["live_stats_state"] == "not_reported"


def test_index_never_calls_youtube(db, settings, store, fake):
    three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    before = len(fake.calls)
    library_page(client)
    library_page(client, sort="views", status="published", q="volcano")
    assert fake.calls[before:] == []


def test_index_query_does_not_load_raw_retention_or_raw_responses(db, settings, store, fake):
    three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    library_page(client)  # previews are backfilled on the first view
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        library_page(client, sort="average_view_percentage")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements
    assert not [item for item in statements if "youtube_retention_points" in item]
    assert not [item for item in statements if "raw_responses" in item]
    # the fingerprint document (with its per-scene rows) is only read through JSON paths
    whole = [re.sub(r"json_extract\(production_fingerprints\.fingerprint", "", item) for item in statements]
    assert not [item for item in whole if "production_fingerprints.fingerprint" in item]
    assert len(statements) <= 12  # bounded, independent of the number of videos


# ---------------------------------------------------------------------------
# Sorting, filtering, search, pagination
# ---------------------------------------------------------------------------


def test_sorting(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)

    def order(sort):
        return [item["id"] for item in library_page(client, sort=sort)["items"]]

    # Newest publication/upload first: published at 2026-09-10 11:00, then uploads.
    assert order("newest") == [published.id, scheduled.id, private.id]
    assert order("oldest") == [private.id, scheduled.id, published.id]
    # Only videos that have been public rank by live views; never-public counters do not.
    assert order("views") == [published.id, scheduled.id, private.id]
    # Only videos with the metric are ranked; the rest follow newest first.
    assert order("average_view_percentage") == [published.id, scheduled.id, private.id]
    assert order("average_view_duration") == [published.id, scheduled.id, private.id]
    assert library_page(client, sort="bogus")["filters"]["sort"] == "newest"


def test_filters(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    ids = lambda **params: {item["id"] for item in library_page(client, **params)["items"]}
    assert ids(status="published") == {published.id}
    assert ids(status="scheduled") == {scheduled.id}
    assert ids(status="private") == {private.id}
    assert ids(status="deleted") == set()
    assert ids(analytics="available") == {published.id}
    assert ids(analytics="processing") == set()
    lifecycle.delete_project_lifecycle(db, OTHER, settings, store, fake)
    assert ids(project="archived") == {scheduled.id}
    assert ids(project="available") == {published.id, private.id}
    assert ids(project="archived", status="published") == set()


def test_search_title_prompt_topic_and_video_id(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    ids = lambda q: {item["id"] for item in library_page(client, q=q)["items"]}
    assert ids("VOLCANO") == {scheduled.id}  # uploaded YouTube title, case-insensitive
    assert ids("airplane windows") == {published.id, scheduled.id, private.id}  # original prompt
    assert ids(private.youtube_video_id) == {private.id}  # video ID
    lifecycle.delete_project_lifecycle(db, THIRD, settings, store, fake)
    assert db.get(Project, THIRD) is None
    assert ids("airplane windows") == {published.id, scheduled.id, private.id}  # archived prompt
    assert ids("nothing matches this") == set()
    assert ids("%") == set() and ids("_") == set()  # wildcards are literal


def test_pagination_is_bounded(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    first = library_page(client, limit=2)
    assert [item["id"] for item in first["items"]] == [published.id, scheduled.id]
    assert first["total"] == 3 and first["next_offset"] == 2
    second = library_page(client, limit=2, offset=2)
    assert [item["id"] for item in second["items"]] == [private.id] and second["next_offset"] is None
    assert client.get("/api/videos", params={"limit": 500}).status_code == 422
    assert library.list_videos(db, settings, limit=10_000)["limit"] == library.MAX_PAGE_SIZE


def test_index_scales_without_per_video_queries(db, settings, store, fake):
    """Many library rows (no Google involved): the query count stays constant."""
    connect(db, settings, store, fake)
    for index in range(120):
        db.add(YouTubeUpload(
            project_id=f"{index:08d}-0000-4000-8000-000000000000", project_revision=1, render_revision=1,
            render_sha256=f"{index:064d}", render_file_size=10, channel_id="UC_fake_channel_01",
            youtube_video_id=f"bulk{index:06d}", state="ready", upload_status="processed",
            remote_privacy_status="public", remote_status_checked_at=NOW, published_at=NOW - timedelta(hours=index),
            title=f"Bulk video {index}", library_thumbnail="",
        ))
    db.commit()
    statements: list[str] = []
    engine = db.get_bind()
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        page = library.list_videos(db, settings, limit=24)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert page["total"] == 120 and len(page["items"]) == 24 and page["items"][0]["title"] == "Bulk video 0"
    assert page["items"][0]["thumbnail_url"] is None and page["items"][0]["project"]["available"] is False
    assert len(statements) <= 12


# ---------------------------------------------------------------------------
# Project deletion: the video, its record and a small preview remain
# ---------------------------------------------------------------------------


def test_project_deletion_keeps_the_video_detail_and_a_small_preview(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    client = api_client(db, settings, store, fake)
    before = client.get(f"/api/videos/{row.id}").json()
    assert before["video"]["project"]["available"] is True
    response = client.delete(f"/api/projects/{PID}")
    assert response.status_code == 200 and response.json()["mode"] == "archive"
    # heavy media are gone: no MP4, no covers, no project directory
    assert not (settings.render_root / PID).exists()
    assert not [path for path in files_under(settings.render_root) if path.suffix in {".mp4", ".wav", ".mp3"}]
    # ... but one small preview remains
    previews = files_under(library.library_directory(settings))
    assert len(previews) == 1
    preview = previews.pop()
    assert preview.stat().st_size <= library.THUMBNAIL_MAX_BYTES
    with Image.open(preview) as image:
        assert image.width <= library.THUMBNAIL_BOX[0] and image.height <= library.THUMBNAIL_BOX[1]
    item = library_page(client)["items"][0]
    assert item["id"] == row.id and item["project"] == {
        "id": PID, "available": False, "title": "Why are airplane windows round?", "archived_at": item["project"]["archived_at"],
    }
    assert item["project"]["archived_at"] and item["thumbnail_url"].startswith(f"/media/{library.LIBRARY_DIR}/")
    # served by the existing /media mount (bound to the configured render_root at startup)
    assert settings.render_root / item["thumbnail_url"].split("?")[0].removeprefix("/media/") == preview
    detail = client.get(f"/api/videos/{row.id}").json()
    assert detail["video"]["project"]["available"] is False and detail["video"]["prompt"] == "Why are airplane windows round?"
    assert detail["video"]["topic"] == "Airplane windows"
    assert detail["performance"]["status"] == "ready" and detail["performance"]["scene_retention"]
    assert detail["retention_curve"] and detail["video"]["studio_url"] and detail["video"]["watch_url"]
    assert detail["production"]["hook_strategy"] == before["production"]["hook_strategy"]


def test_full_deletion_removes_the_preview_and_legacy_records(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row = upload(db, project, settings, store, fake)
    library.ensure_library_thumbnail(db, row, settings)
    assert library.thumbnail_path(row, settings) is not None
    fake.videos[row.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert files_under(library.library_directory(settings)) == set()
    assert db.scalars(select(YouTubeUpload)).all() == []


def test_archived_video_without_any_image_still_renders(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings, thumbnails=False)
    row = upload(db, project, settings, store, fake)
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    db.refresh(row)
    assert row.library_thumbnail in {None, ""} or library.thumbnail_path(row, settings) is not None
    client = api_client(db, settings, store, fake)
    item = library_page(client)["items"][0]
    detail = client.get(f"/api/videos/{row.id}")
    assert detail.status_code == 200 and item["project"]["available"] is False
    if library.thumbnail_path(row, settings) is None:
        assert item["thumbnail_url"] is None  # the UI shows its placeholder


def test_archive_record_from_before_the_library_has_no_preview_and_still_works(db, settings, store, fake, monkeypatch):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    # an archive made by the previous release: no preview was ever kept
    with monkeypatch.context() as patch:
        patch.setattr(library, "ensure_library_thumbnail", lambda *args, **kwargs: None)
        lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    db.refresh(row)
    assert row.library_thumbnail is None
    client = api_client(db, settings, store, fake)
    item = library_page(client)["items"][0]
    assert item["thumbnail_url"] is None and item["project"]["available"] is False
    assert client.get(f"/api/videos/{row.id}").status_code == 200


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def test_detail_sections(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    client = api_client(db, settings, store, fake)
    detail = client.get(f"/api/videos/{row.youtube_video_id}").json()  # also by YouTube video id
    assert detail["video"]["id"] == row.id
    # live stats vs detailed analytics
    assert detail["upload"]["current"]["state"] == "published"
    metrics = detail["performance"]["latest_snapshot"]["metrics"]
    for name in ("engagedViews", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage", "shares", "subscribersGained", "subscribersLost"):
        assert metrics[name]["availability"] == "available"
    # raw retention points, unsmoothed, in order
    curve = detail["retention_curve"]
    assert len(curve) == len(CURVE) and [point["audience_watch_ratio"] for point in curve] == pytest.approx(CURVE)
    assert curve == sorted(curve, key=lambda point: point["elapsed_video_ratio"])
    assert detail["performance"]["opening_retention"]["status"] == "ok"
    assert [scene["index"] for scene in detail["performance"]["scene_retention"]] == [1, 2, 3, 4]
    assert all(scene.get("notable_drop") for scene in detail["major_drops"])
    # existing baseline/outlier system, with its sample size
    assert detail["performance"]["classification"]["label"] == "insufficient_data"
    assert detail["performance"]["baseline"]["sample_size"] == 0
    production = detail["production"]
    assert production["prompt"] == "Why are airplane windows round?" and production["format"] == "explanation"
    assert production["scene_count"] == 4 and production["duration_seconds"] == 10.0
    assert production["hook_strategy"] == "counterintuitive_insight" and production["verbal_hook"] == "Square windows once tore planes apart."
    assert production["on_screen_hook"] == "Why round?" and production["visual_hook"]["subject"] == "airplane window"
    assert production["answer_reveal_seconds"] == 5.0
    assert production["critic"] == {"status": "issues_remain", "issue_count": 2, "repairs_attempted": 2, "repairs_successful": 1, "unresolved_issue_count": 1}
    assert detail["publishing"]["schedule_source"] is None and detail["publishing"]["smart_scheduler_selected"] is False


def test_detail_reports_smart_scheduler_provenance(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    row.schedule_source, row.schedule_slot_time, row.schedule_timezone, row.schedule_local_time = "auto", "18:00", "Europe/Berlin", "2030-01-01T18:00"
    db.commit()
    publishing_context = api_client(db, settings, store, fake).get(f"/api/videos/{row.id}").json()["publishing"]
    assert publishing_context["smart_scheduler_selected"] is True and publishing_context["provenance"] == "Smart Scheduler slot"
    assert publishing_context["slot_time"] == "18:00" and publishing_context["timezone"] == "Europe/Berlin"


def test_detail_with_enough_videos_uses_the_channel_baseline(db, settings, store, fake):
    connect(db, settings, store, fake)
    rows = []
    for index in range(6):
        project_id = f"{index + 4:08d}-1111-4111-8111-111111111111"
        rows.append(_published_with_analytics(db, settings, store, fake, build_project(db, settings, project_id=project_id, content=bytes([index + 1]) * 20_000)))
    detail = api_client(db, settings, store, fake).get(f"/api/videos/{rows[0].id}").json()
    assert detail["performance"]["baseline"]["sample_size"] == 5 and detail["performance"]["baseline"]["sufficient"]
    assert detail["performance"]["classification"]["label"] in {"below_channel_baseline", "near_channel_baseline", "above_channel_baseline", "outlier_positive"}


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def test_refresh_recent_is_bounded_and_reuses_the_existing_sync(db, settings, store, fake):
    published, scheduled, private = three_videos(db, settings, store, fake)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    client = api_client(db, settings, store, fake)
    before = len(fake.calls)
    result = client.post("/api/videos/refresh-recent").json()
    assert result["checked"] == 3 and result["errors"] == 0
    listed = [call for name, call in fake.calls[before:] if name == "list_videos"]
    assert sorted(item[0] for item in listed) == sorted([published.youtube_video_id, scheduled.youtube_video_id, private.youtube_video_id])
    assert {item["upload_id"] for item in result["results"]} == {published.id, scheduled.id, private.id}


def test_refresh_recent_requires_a_connection(db, settings, store, fake):
    response = api_client(db, settings, store, fake).post("/api/videos/refresh-recent")
    assert response.status_code == 409 and response.json()["detail"]["status"] == "not_connected"


def test_recent_videos_limit(db, settings, store, fake):
    connect(db, settings, store, fake)
    for index in range(30):
        db.add(YouTubeUpload(
            project_id=f"{index:08d}-0000-4000-8000-000000000000", project_revision=1, render_revision=1,
            render_sha256=f"{index:064d}", render_file_size=10, channel_id="UC_fake_channel_01",
            youtube_video_id=f"recent{index:05d}", state="ready", upload_status="processed", uploaded_at=NOW - timedelta(hours=index),
        ))
    db.commit()
    recent = library.recent_videos(db, "UC_fake_channel_01")
    assert len(recent) == library.RECENT_REFRESH_LIMIT and recent[0].youtube_video_id == "recent00000"


def test_video_library_has_one_schema_addition_and_no_second_store():
    from clipforge.database import Base

    tables = set(Base.metadata.tables)
    assert not [name for name in tables if "library" in name or "video_summ" in name]
    assert "library_thumbnail" in YouTubeUpload.__table__.columns


def test_bulk_delete_keeps_uploaded_videos_in_the_library(db, settings, store, fake):
    """"Alle Projekte löschen" goes through the same lifecycle: heavy media go, videos stay."""
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    build_project(db, settings, project_id=OTHER, content=b"N" * 20_000)  # never uploaded
    client = api_client(db, settings, store, fake)
    assert client.get("/api/projects/delete-plan").json()["projects_keeping_learning_record"] == 1
    result = client.delete("/api/projects").json()
    assert result["deleted_projects"] == 2 and result["archived_projects"] == 1
    assert db.scalars(select(Project)).all() == []
    assert not (settings.render_root / PID).exists()
    page = library_page(client)
    assert [item["id"] for item in page["items"]] == [row.id]
    item = page["items"][0]
    assert item["project"]["available"] is False and item["thumbnail_url"]
    detail = client.get(f"/api/videos/{row.id}").json()
    assert detail["performance"]["status"] == "ready" and detail["retention_curve"]
