"""YouTube publishing UX + storage lifecycle V2 (fake Google only)."""
from __future__ import annotations

import base64
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from test_youtube_learning_loop import CURVE, METRICS, connect
from youtube_support import (
    FakeYouTube,
    add_export_revision,
    analytics_payload,
    publish_options,
    rendered_state,
    youtube_settings,
)

from clipforge import exporter
from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import (
    ProductionFingerprint,
    Project,
    ProjectRevision,
    YouTubeAnalyticsSnapshot,
    YouTubeLearningArchive,
    YouTubeRetentionPoint,
    YouTubeUpload,
)
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, learning, lifecycle, publishing, uploads
from clipforge.youtube.provider import YouTubeApiError
from clipforge.youtube.publishing import PublishOptions, ScheduleChoice, UploadDefaults
from clipforge.youtube.routes import get_upload_dispatcher, get_youtube_provider

PID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "youtube_discovery_subset.json").read_text())


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


def _image(width: int, height: int, fmt: str = "JPEG", *, noise: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), (200, 80, 40))
    if noise:
        image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def build_project(db, settings, *, project_id: str = PID, thumbnails: bool = True, content: bytes = b"\x00mp4" * 6000, youtube_cover: bytes | None = None) -> Project:
    """A rendered project that was never exported (render lives in ClipForge storage)."""
    state = rendered_state(10.0)
    state["music"] = {"enabled": False}
    render = settings.render_root / project_id / "renders" / "v1" / "clipforge.mp4"
    render.parent.mkdir(parents=True, exist_ok=True)
    render.write_bytes(content)
    state["render"] = {"status": "complete", "url": f"/media/{project_id}/renders/v1/clipforge.mp4", "revision": 1, "stale": False}
    if thumbnails:
        variants = []
        for index, platform in enumerate(("tiktok", "instagram", "youtube")):
            path = settings.render_root / project_id / "thumbnails" / f"{platform}-cover-{index + 1}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(youtube_cover if platform == "youtube" and youtube_cover else _image(1080, 1920))
            variants.append({"id": f"{platform}-cover-{index + 1}", "platform": platform, "url": f"/media/{project_id}/thumbnails/{platform}-cover-{index + 1}.jpg"})
        state["thumbnails"] = {"status": "available", "selected_variant_id": "youtube-cover-3", "variants": variants}
    project = Project(id=project_id, original_prompt="Why are airplane windows round?", title="Why are airplane windows round?", status="rendered", current_revision=1)
    project.revisions.append(ProjectRevision(number=1, parent_revision=None, instruction="Original prompt", kind="initial", state=state, changed_components=["render"]))
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def upload(db, project, settings, store, fake, options: PublishOptions | None = None, **kwargs) -> YouTubeUpload:
    record = connection.active_connection(db)
    row, source, run = uploads.request_upload(db, project, settings, channel_id=record.channel_id, options=options or publish_options(), **kwargs)
    if run:
        uploads.run_upload(db, row.id, source.path, settings, store, fake)
        db.refresh(row)
        uploads.after_upload(db, row, settings, store, fake)
    db.refresh(row)
    return row


def generated_thumbnail(asset: str = "youtube-cover-3") -> dict:
    return {"source": "generated", "asset": asset}


def api_client(db, settings, store, fake, dispatch=None) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    if dispatch is not None:
        app.dependency_overrides[get_upload_dispatcher] = lambda: dispatch
    return TestClient(app)


def files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()} if root.exists() else set()


# ---------------------------------------------------------------------------
# Direct upload from the canonical final render
# ---------------------------------------------------------------------------


def test_render_is_uploaded_directly_without_export_or_copy(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    before = files_under(settings.render_root)
    row = upload(db, project, settings, store, fake)
    assert row.youtube_video_id and row.source_kind == "render"
    render = settings.render_root / PID / "renders" / "v1" / "clipforge.mp4"
    assert bytes(next(iter(fake.sessions.values()))["data"]) == render.read_bytes()
    assert files_under(settings.render_root) == before  # no copy of the MP4 was made
    assert not settings.resolved_downloads_root.exists()  # nothing was exported / downloaded
    db.refresh(project)
    assert all("export" not in (revision.state or {}) for revision in project.revisions)


def test_music_master_is_the_one_final_video_and_is_reused(db, settings, store, fake, monkeypatch):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    track = settings.render_root.parent / "track.mp3"
    track.write_bytes(b"music")
    calls = []

    def fake_mix(source, destination, _state, _track):
        calls.append(destination)
        destination.write_bytes(source.read_bytes() + b"+music")

    state = project.revisions[0].state
    state = {**state, "music": {"enabled": True, "track": {"id": "t1", "file": "t1.mp3"}, "volume": 0.2}}
    monkeypatch.setattr(exporter, "resolve_track_path", lambda _music: track)
    monkeypatch.setattr(exporter, "_mix_music", fake_mix)
    first = exporter.resolve_final_master(PID, project.title, state, settings, verifier=lambda _path: None)
    second = exporter.resolve_final_master(PID, project.title, state, settings, verifier=lambda _path: None)
    assert first.kind == "mixed_master" and first.path == second.path and len(calls) == 1
    assert first.path.read_bytes().endswith(b"+music")
    assert first.path.parent == settings.render_root / PID / "renders" / "v1"


def test_unrendered_project_cannot_be_uploaded(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    project.revisions.append(ProjectRevision(number=2, parent_revision=1, instruction="edit", kind="user", state={**project.revisions[0].state, "render": {"status": "stale", "stale": True}}, changed_components=["script"]))
    project.current_revision = project.active_tip_revision = 2
    db.commit()
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options())
    assert refused.value.code == "preflight_failed"
    assert any("Render this revision" in item["message"] for item in refused.value.issues)


# ---------------------------------------------------------------------------
# Made for kids (the real-world regression)
# ---------------------------------------------------------------------------


def test_missing_audience_answer_blocks_upload_before_any_byte(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options(made_for_kids=None, contains_synthetic_media=None, thumbnail=None))
    messages = [item["message"] for item in refused.value.issues]
    assert "Choose whether this video is made for kids" in messages
    assert "Choose whether the video contains realistic altered or synthetic content" in messages
    assert "Select a thumbnail" in messages
    assert refused.value.message == "3 items need attention."
    assert fake.sessions == {} and db.scalars(select(YouTubeUpload)).all() == []


@pytest.mark.parametrize("answer", [True, False])
def test_audience_answer_is_sent_and_read_back(db, settings, store, fake, answer):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(made_for_kids=answer))
    body = next(payload for name, payload in fake.calls if name == "start_upload")
    assert body["status"]["selfDeclaredMadeForKids"] is answer
    assert row.made_for_kids is answer and row.made_for_kids_confirmed is answer
    assert uploads.serialize_upload(row)["audience"] == {"made_for_kids": answer, "confirmed_by_youtube": answer, "answered": True}


def test_a_row_without_an_audience_answer_is_never_sent(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row, source, _run = uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options())
    row.upload_settings = {"body": {"snippet": {"title": "x"}, "status": {"privacyStatus": "private"}}}
    db.commit()
    uploads.run_upload(db, row.id, source.path, settings, store, fake)
    db.refresh(row)
    assert row.last_error_code == "settings_missing" and fake.sessions == {}


def test_saved_default_prefills_the_audience_but_is_still_shown(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    client = api_client(db, settings, store, fake)
    draft = client.get(f"/api/youtube/projects/{PID}/draft").json()
    assert draft["options"]["made_for_kids"] is None  # never silently pre-answered
    assert draft["options"]["contains_synthetic_media"] is None
    saved = client.put("/api/youtube/defaults", json={"made_for_kids": False, "contains_synthetic_media": False, "timezone": "Europe/Berlin"})
    assert saved.status_code == 200
    draft = client.get(f"/api/youtube/projects/{PID}/draft").json()
    assert draft["options"]["made_for_kids"] is False and draft["defaults"]["made_for_kids"] is False
    assert draft["defaults"]["timezone"] == "Europe/Berlin"
    assert project.id == PID


def test_existing_video_without_answer_can_be_answered(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    fake.videos[row.youtube_video_id]["status"].pop("selfDeclaredMadeForKids")  # a V1 upload
    row.made_for_kids = row.made_for_kids_confirmed = None
    db.commit()
    uploads.set_audience(db, row, False, settings, store, fake)
    update = [payload for name, payload in fake.calls if name == "update_video"][-1]
    assert update["status"]["selfDeclaredMadeForKids"] is False
    assert update["status"]["privacyStatus"] == "private"  # other status fields preserved
    assert row.made_for_kids_confirmed is False


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def test_generated_youtube_cover_is_the_default_and_is_set_after_video_id(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    choices = publishing.thumbnail_choices(project.revisions[0].state, PID, settings)
    assert choices[0]["platform"] == "youtube" and all(item["valid"] for item in choices)
    assert publishing.default_thumbnail(choices)["asset"] == "youtube-cover-3"
    row = upload(db, project, settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    names = [name for name, _ in fake.calls]
    assert names.index("set_thumbnail") > names.index("start_upload")
    assert fake.thumbnails_set == [(row.youtube_video_id, len((settings.render_root / PID / "thumbnails" / "youtube-cover-3.jpg").read_bytes()), "image/jpeg")]
    assert row.thumbnail_upload_status == "applied" and row.thumbnail_source == "generated" and row.thumbnail_asset == "youtube-cover-3"
    assert row.thumbnail_sha256


def test_thumbnail_failure_keeps_the_video_and_can_be_retried(db, settings, store, fake):
    connect(db, settings, store, fake)
    fake.thumbnail_error = YouTubeApiError("thumbnail_not_allowed", "YouTube does not allow custom thumbnails for this channel yet.")
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    assert row.youtube_video_id and row.state in {"uploaded", "processing"}
    assert row.last_error_code is None
    serialized = uploads.serialize_upload(row)
    assert serialized["video_status"] == "processing"
    assert serialized["thumbnail"]["status"] == "failed"
    assert "custom thumbnails" in serialized["thumbnail"]["failure_reason"]
    fake.thumbnail_error = None
    uploads.apply_thumbnail(db, row, settings, store, fake)
    assert row.thumbnail_upload_status == "applied" and row.thumbnail_failure_reason is None


def test_oversized_png_is_reencoded_below_the_limit_and_small_images_are_refused(db, settings, store, fake):
    big = _image(1280, 2200, "PNG", noise=True)
    assert len(big) > publishing.THUMBNAIL_MAX_BYTES
    project = build_project(db, settings, youtube_cover=big)
    prepared = publishing.prepare_thumbnail(publishing.ThumbnailChoice(**generated_thumbnail()), project.revisions[0].state, PID, settings)
    assert prepared.content_type == "image/jpeg" and len(prepared.data) <= publishing.THUMBNAIL_MAX_BYTES
    with pytest.raises(ValueError):
        publishing.save_custom_thumbnail(PID, _image(320, 180), settings)
    connect(db, settings, store, fake)
    client = api_client(db, settings, store, fake)
    response = client.post(f"/api/youtube/projects/{PID}/thumbnails", json={"data_base64": base64.b64encode(_image(1280, 720)).decode()})
    assert response.status_code == 200
    custom = [item for item in response.json()["thumbnails"] if item["source"] == "custom"]
    assert custom and custom[0]["valid"] and custom[0]["width"] == 1280


def test_explicit_youtube_frame_choice_is_recorded_not_silent(db, settings, store, fake):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail={"source": "youtube_auto"}))
    assert row.thumbnail_upload_status == "not_requested" and "set_thumbnail" not in [name for name, _ in fake.calls]


# ---------------------------------------------------------------------------
# Scheduling, time zones, DST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("day", "clock", "status", "offset", "abbreviation", "utc"), [
    ("2026-09-28", "20:30", "ok", "UTC+02:00", "CEST", "2026-09-28T18:30:00Z"),
    ("2026-12-01", "20:30", "ok", "UTC+01:00", "CET", "2026-12-01T19:30:00Z"),
    ("2027-03-28", "02:30", "nonexistent", None, None, None),  # clocks jump 02:00 -> 03:00
    ("2026-10-25", "02:30", "ambiguous", None, None, None),  # 02:00-03:00 happens twice
    ("2026-10-25", "03:30", "ok", "UTC+01:00", "CET", "2026-10-25T02:30:00Z"),
])
def test_berlin_schedule_resolution_is_dst_safe(day, clock, status, offset, abbreviation, utc):
    resolution = publishing.resolve_schedule(ScheduleChoice(date=day, time=clock, timezone="Europe/Berlin"), now=NOW)
    assert resolution.status == status
    assert resolution.utc_offset == offset and resolution.abbreviation == abbreviation
    assert resolution.as_dict()["publish_at"] == utc
    if status != "ok":
        assert "Choose another time" in resolution.message


def test_other_zones_and_invalid_input():
    ny = publishing.resolve_schedule(ScheduleChoice(date="2026-11-01", time="09:00", timezone="America/New_York"), now=NOW)
    assert (ny.status, ny.abbreviation, ny.as_dict()["publish_at"]) == ("ok", "EST", "2026-11-01T14:00:00Z")
    assert publishing.resolve_schedule(ScheduleChoice(date="2026-09-10", time="12:05", timezone="UTC"), now=NOW).status == "past"
    assert publishing.resolve_schedule(ScheduleChoice(date="2026-02-30", time="12:00", timezone="UTC"), now=NOW).status == "invalid"
    assert publishing.resolve_schedule(ScheduleChoice(date="2026-10-01", time="12:00", timezone="Nowhere/City"), now=NOW).status == "invalid_timezone"


def test_upload_and_schedule_sends_private_publish_at_and_persists_the_zone(db, settings, store, fake):
    connect(db, settings, store, fake)
    options = publish_options(visibility="schedule", schedule={"date": "2026-09-28", "time": "20:30", "timezone": "Europe/Berlin"})
    row = upload(db, build_project(db, settings), settings, store, fake, options, now=NOW)
    body = next(payload for name, payload in fake.calls if name == "start_upload")
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"] == "2026-09-28T18:30:00Z"
    assert (row.schedule_local_time, row.schedule_timezone) == ("2026-09-28T20:30", "Europe/Berlin")
    assert uploads.aware(row.publish_at) == datetime(2026, 9, 28, 18, 30, tzinfo=UTC)
    assert uploads.serialize_upload(row)["schedule"]["timezone"] == "Europe/Berlin"
    assert uploads.lifecycle(row, now=NOW) == "scheduled"


def test_schedule_resolve_endpoint(db, settings, store, fake):
    client = api_client(db, settings, store, fake)
    body = client.post("/api/youtube/schedule/resolve", json={"date": "2099-07-01", "time": "20:30", "timezone": "Europe/Berlin"}).json()
    assert body["status"] == "too_far"
    body = client.post("/api/youtube/schedule/resolve", json={"date": "2027-03-28", "time": "02:15", "timezone": "Europe/Berlin"}).json()
    assert body["status"] == "nonexistent" and body["publish_at"] is None


# ---------------------------------------------------------------------------
# Settings mapping against Google's discovery document
# ---------------------------------------------------------------------------

SCHEMA_FOR_PART = {
    "snippet": "VideoSnippet",
    "status": "VideoStatus",
    "recordingDetails": "VideoRecordingDetails",
    "paidProductPlacementDetails": "VideoPaidProductPlacementDetails",
}


def _exists(api: str) -> bool:
    if api == "thumbnails.set":
        return "videoId" in FIXTURE["methods"]["thumbnails.set"]["parameters"]
    if api.startswith("videos.insert:"):
        return api.split(":")[1] in FIXTURE["methods"]["videos.insert"]["parameters"]
    part, prop = api.split(".", 1)
    return part in FIXTURE["schemas"]["Video"] and prop in FIXTURE["schemas"][SCHEMA_FOR_PART[part]]


def test_every_exposed_setting_maps_to_a_real_api_property():
    catalog = publishing.settings_catalog()
    for item in catalog["api_writable"]:
        assert _exists(item["api"]), item
    assert set(FIXTURE["enums"]["VideoStatus.license"]) == {"youtube", "creativeCommon"}
    assert "madeForKids" in FIXTURE["schemas"]["VideoStatus"]  # read-only effective value
    assert {item["api"] for item in catalog["api_read_only"]} >= {"status.madeForKids", "status.uploadStatus"}


def test_insert_body_only_contains_catalog_properties_and_no_studio_only_setting():
    options = publish_options(
        visibility="schedule", schedule={"date": "2026-09-28", "time": "20:30", "timezone": "Europe/Berlin"},
        category_id="27", license="creativeCommon", embeddable=False, public_stats_viewable=False,
        recording_date="2026-09-01", paid_product_placement=True,
    )
    resolution = publishing.resolve_schedule(options.schedule, now=NOW)
    body = publishing.insert_body(options, resolution)
    written = {f"{part}.{key}" for part, values in body.items() for key in values}
    catalog = {item["api"] for item in publishing.settings_catalog()["api_writable"]}
    assert written <= catalog
    assert body["status"]["license"] == "creativeCommon" and body["status"]["embeddable"] is False
    assert body["recordingDetails"] == {"recordingDate": "2026-09-01T00:00:00Z"}
    assert body["paidProductPlacementDetails"] == {"hasPaidProductPlacement": True}
    studio = {item["key"] for item in publishing.settings_catalog()["studio_only"]}
    assert {"automatic_chapters", "shorts_remixing", "comments", "featured_places", "caption_certification"} <= studio
    assert not studio & {key.split(".")[-1] for key in written}
    for key in PublishOptions.model_fields:
        assert key not in studio


def test_notify_subscribers_is_the_insert_query_parameter(db, settings, store, fake):
    connect(db, settings, store, fake)
    upload(db, build_project(db, settings), settings, store, fake, publish_options(notify_subscribers=False))
    assert ("notify_subscribers", False) in fake.calls


def test_public_needs_an_audited_api_project(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options(visibility="public"))
    assert "unverified" in refused.value.issues[0]["message"]
    publishing.save_defaults(db, UploadDefaults(api_project_audited=True))
    row = upload(db, project, settings, store, fake, publish_options(visibility="public"))
    body = next(payload for name, payload in fake.calls if name == "start_upload")
    assert body["status"]["privacyStatus"] == "public"
    fake.videos[row.youtube_video_id]["status"].update(privacyStatus="private", uploadStatus="processed")
    uploads.sync_status(db, row, settings, store, fake)
    assert row.visibility_restricted is True  # YouTube kept it private


def test_metadata_is_validated_not_silently_truncated(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    long_title = "A" * 101
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options(title=long_title, tags=["x" * 300, "y" * 300]))
    fields = {item["field"] for item in refused.value.issues}
    assert fields == {"title", "tags"}
    row = upload(db, project, settings, store, fake, publish_options(title="  Exactly my title  ", description="Mine.", tags=["a b", "c"]))
    body = next(payload for name, payload in fake.calls if name == "start_upload")
    assert body["snippet"]["title"] == "Exactly my title" and body["snippet"]["description"] == "Mine."
    assert (row.title, row.tags) == ("Exactly my title", ["a b", "c"])
    assert row.upload_settings["body"] == body


def test_categories_come_from_youtube_and_suggestion_is_only_a_hint(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    client = api_client(db, settings, store, fake)
    draft = client.get(f"/api/youtube/projects/{PID}/draft", params={"region": "DE", "language": "de"}).json()
    assert [item["id"] for item in draft["categories"]] == ["27", "28"]  # non-assignable dropped
    assert ("list_categories", ("DE", "de")) in fake.calls
    assert draft["suggested_category"]["id"] == "27" and draft["options"]["category_id"] is None
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options(category_id="18"), categories=draft["categories"])
    assert refused.value.issues[0]["field"] == "category_id"
    preflight = client.post(f"/api/youtube/projects/{PID}/preflight", json={"options": publish_options(made_for_kids=None).model_dump()}).json()
    assert preflight["ready"] is False and preflight["issues"][0]["field"] == "made_for_kids"


def test_synthetic_suggestion_is_explained_and_never_applied():
    fingerprint = {"visual": {"generated_scene_count": 0}}
    assert publishing.synthetic_suggestion(fingerprint)["value"] is False
    mixed = publishing.synthetic_suggestion({"visual": {"generated_scene_count": 2}})
    assert mixed["value"] is None and "2 scenes use AI-generated images" in mixed["why"]
    assert publishing.options_with_defaults({"title": "t"}, UploadDefaults())["contains_synthetic_media"] is None


# ---------------------------------------------------------------------------
# Upload-aware deletion and the Learning Archive
# ---------------------------------------------------------------------------


def _published_with_analytics(db, settings, store, fake, project):
    row = upload(db, project, settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    fake.publish(row.youtube_video_id, "2026-09-10T11:00:00Z")
    uploads.sync_status(db, row, settings, store, fake)
    fake.analytics_handler = analytics_payload(METRICS, curve=CURVE)
    analytics.refresh_analytics(db, row, settings, store, fake, now=NOW)
    return row


def test_never_uploaded_project_is_deleted_completely(db, settings, store, fake):
    connect(db, settings, store, fake)
    build_project(db, settings)
    plan = lifecycle.plan_deletion(db, PID, settings, store, fake)
    assert plan.mode == "full" and plan.reclaimable_bytes and plan.reclaimable_bytes > 0
    result = lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert result["mode"] == "full" and result["retained_bytes"] == 0
    assert db.get(Project, PID) is None and not (settings.render_root / PID).exists()
    assert db.scalars(select(YouTubeLearningArchive)).all() == []


def test_failed_or_rejected_never_successful_upload_leaves_no_archive(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row = upload(db, project, settings, store, fake)
    fake.videos[row.youtube_video_id]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    plan = lifecycle.plan_deletion(db, PID, settings, store, fake)
    assert plan.mode == "full" and plan.uploads[0]["classification"] == "never_uploaded"
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert db.scalars(select(YouTubeUpload)).all() == [] and db.scalars(select(ProductionFingerprint)).all() == []
    assert db.scalars(select(YouTubeLearningArchive)).all() == []


def test_uploaded_project_keeps_only_the_learning_archive(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row = _published_with_analytics(db, settings, store, fake, project)
    video_id = row.youtube_video_id
    heavy = files_under(settings.render_root / PID)
    assert any(path.suffix == ".mp4" for path in heavy) and any("thumbnails" in str(path) for path in heavy)
    client = api_client(db, settings, store, fake)
    plan = client.get(f"/api/projects/{PID}/delete-plan").json()
    assert plan["mode"] == "archive" and plan["verification"] == "live"
    assert plan["reclaimable_bytes"] > 0 and 0 < plan["retained_bytes"] < 200_000
    response = client.delete(f"/api/projects/{PID}")
    assert response.status_code == 200 and response.json()["mode"] == "archive"
    # heavy media and the editable project are gone
    assert not (settings.render_root / PID).exists()
    assert db.get(Project, PID) is None
    assert client.get(f"/api/projects/{PID}").status_code == 404
    # the YouTube video was never touched
    assert video_id in fake.videos and not [name for name, _ in fake.calls if name in {"delete_video", "videos.delete"}]
    # the learning record survived and still works
    kept = db.scalar(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id == video_id))
    assert kept is not None and db.get(ProductionFingerprint, kept.fingerprint_id) is not None
    assert db.scalars(select(YouTubeRetentionPoint)).all()
    report = learning.performance_report(db, kept, min_sample=5)
    assert report["status"] == "ready" and report["scene_retention"] and report["opening_retention"]["status"] == "ok"
    assert learning.learning_table(db, "UC_fake_channel_01")["video_count"] == 1
    archive = db.scalar(select(YouTubeLearningArchive))
    assert archive.project_id == PID and archive.title == "Why are airplane windows round?" and archive.upload_ids == [kept.id]
    entries = client.get("/api/youtube/archive").json()["entries"]
    assert entries[0]["upload_id"] == kept.id and entries[0]["editable"] is False
    detail = client.get(f"/api/youtube/archive/{kept.id}").json()
    assert detail["performance"]["status"] == "ready" and detail["fingerprint"]["hook"]["strategy"]
    assert "scenes" not in detail["fingerprint"]  # compact summary for the UI


def test_offline_verification_fails_closed_for_unknown_outcome(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row, _source, _run = uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options())
    row.state, row.bytes_uploaded = "failed", row.render_file_size
    row.upload_session_uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=lost"
    db.commit()
    connection.reset_youtube_auth_cache()
    fake.refresh_error = YouTubeApiError("network_timeout", "The YouTube token refresh request timed out.", retryable=True)
    client = api_client(db, settings, store, fake)
    plan = client.get(f"/api/projects/{PID}/delete-plan").json()
    assert plan["mode"] == "unverified" and plan["verification"] == "offline"
    refused = client.delete(f"/api/projects/{PID}")
    assert refused.status_code == 409 and refused.json()["detail"]["status"] == "upload_unverified"
    assert db.get(Project, PID) is not None and (settings.render_root / PID).exists()  # nothing deleted
    confirmed = client.delete(f"/api/projects/{PID}", params={"confirm_unverified": "true"})
    assert confirmed.status_code == 200 and confirmed.json()["mode"] == "full"
    assert db.get(Project, PID) is None


def test_known_upload_is_archived_even_when_youtube_is_unreachable(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row = upload(db, project, settings, store, fake)
    connection.reset_youtube_auth_cache()
    fake.refresh_error = YouTubeApiError("network_error", "YouTube could not be reached for token refresh.", retryable=True)
    plan = lifecycle.plan_deletion(db, PID, settings, store, fake)
    assert plan.mode == "archive" and plan.verification == "offline"
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert db.get(YouTubeUpload, row.id) is not None


def test_historical_analytics_survive_a_later_youtube_deletion(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row = _published_with_analytics(db, settings, store, fake, project)
    del fake.videos[row.youtube_video_id]
    uploads.sync_status(db, row, settings, store, fake)
    assert row.deleted_on_youtube
    assert lifecycle.plan_deletion(db, PID, settings, store, fake).mode == "archive"
    lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert db.scalars(select(YouTubeAnalyticsSnapshot)).all()
    assert db.scalar(select(YouTubeLearningArchive)) is not None


def test_active_upload_blocks_deletion(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = build_project(db, settings)
    row, _source, _run = uploads.request_upload(db, project, settings, channel_id="UC_fake_channel_01", options=publish_options())
    assert uploads.claim_upload(db, row.id)
    with pytest.raises(lifecycle.ProjectDeletionBusy):
        lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert db.get(Project, PID) is not None


def test_bulk_delete_uses_the_same_lifecycle(db, settings, store, fake):
    connect(db, settings, store, fake)
    uploaded = build_project(db, settings)
    _published_with_analytics(db, settings, store, fake, uploaded)
    build_project(db, settings, project_id="33333333-3333-4333-8333-333333333333")
    client = api_client(db, settings, store, fake)
    assert client.get("/api/projects/delete-plan").json()["projects_keeping_learning_record"] == 1
    result = client.delete("/api/projects").json()
    assert result["deleted_projects"] == 2 and result["archived_projects"] == 1
    assert len(client.get("/api/youtube/archive").json()["entries"]) == 1


def test_archive_is_sufficient_for_the_channel_baseline(db, settings, store, fake):
    connect(db, settings, store, fake)
    for index in range(6):
        project_id = f"{index + 4:08d}-1111-4111-8111-111111111111"
        project = build_project(db, settings, project_id=project_id, content=bytes([index + 1]) * 20_000)
        _published_with_analytics(db, settings, store, fake, project)
        lifecycle.delete_project_lifecycle(db, project_id, settings, store, fake)
    assert db.scalars(select(Project)).all() == []
    baseline = learning.channel_baseline(db, "UC_fake_channel_01", min_sample=5)
    assert baseline["sample_size"] == 6 and baseline["sufficient"]


def test_exported_v1_project_still_uploads_from_its_canonical_export(db, settings, store, fake):
    connect(db, settings, store, fake)
    project = Project(id=PID, original_prompt="p", title="Exported", status="exported", current_revision=1)
    db.add(project)
    db.flush()
    add_export_revision(db, project, settings, b"E" * 25_000, rendered_state(10.0))
    row = upload(db, project, settings, store, fake)
    assert row.source_kind == "export" and bytes(next(iter(fake.sessions.values()))["data"]) == b"E" * 25_000


def test_v1_database_gets_the_new_columns_at_startup(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    from clipforge import database

    engine = create_engine(f"sqlite:///{tmp_path / 'v1.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR PRIMARY KEY, original_prompt TEXT, active_tip_revision INTEGER)"))
        conn.execute(text("CREATE TABLE project_revisions (id VARCHAR PRIMARY KEY, project_id VARCHAR, number INTEGER, kind VARCHAR)"))
        conn.execute(text("CREATE TABLE youtube_uploads (id VARCHAR PRIMARY KEY, project_id VARCHAR)"))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.settings, "database_url", f"sqlite:///{tmp_path / 'v1.db'}")
    database.ensure_runtime_schema()
    columns = {column["name"] for column in inspect(engine).get_columns("youtube_uploads")}
    assert {"made_for_kids", "thumbnail_upload_status", "schedule_timezone", "upload_settings", "remote_privacy_status", "remote_status_checked_at", "remote_view_count", "library_thumbnail"} <= columns


def test_suggested_defaults_are_clean_but_user_edits_are_validated_not_rewritten():
    state = rendered_state(10.0)  # social title contains "<round>"
    draft = publishing.default_metadata(state, "fallback")
    assert draft["title"] == "Why are airplane windows round?" and "<" not in draft["description"]
    issues, _ = publishing.validate_options(publish_options(title="Why <round>?"), defaults=UploadDefaults(), thumbnails=[])
    assert [item["field"] for item in issues] == ["title"]
