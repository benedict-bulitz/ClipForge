"""Removing one video from the Video Library (Videos tab): local entry + its own
preview only; publication records, analytics, projects and remote posts stay."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from publishing_support import FakeTikTok, apis, connect_tiktok, publishing_settings, social_project
from sqlalchemy import create_engine, func, inspect, select
from test_publishing_scheduler import schedule_tiktok
from test_youtube_learning_loop import connect
from test_youtube_publishing_v2 import (
    PID,
    _published_with_analytics,
    build_project,
    files_under,
    generated_thumbnail,
    upload,
)
from youtube_support import FakeYouTube, publish_options

from alembic import command
from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import (
    ProductionFingerprint,
    Project,
    SocialPublication,
    YouTubeAnalyticsSnapshot,
    YouTubeUpload,
)
from clipforge.publishing import connections, publications, read_model, scheduler
from clipforge.security.secrets import SecretStore
from clipforge.youtube import connection, library, library_removal, lifecycle, publishing, uploads
from clipforge.youtube.routes import get_youtube_provider

API_ROOT = Path(__file__).resolve().parents[1]
OTHER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    connections.reset_cache()
    publishing.reset_category_cache()
    uploads._SHA_CACHE.clear()
    yield
    connection.reset_youtube_auth_cache()
    connections.reset_cache()
    app.dependency_overrides.clear()


@pytest.fixture()
def settings(tmp_path):
    return publishing_settings(tmp_path)


@pytest.fixture()
def store():
    return SecretStore()


@pytest.fixture()
def fake():
    return FakeYouTube()


@pytest.fixture()
def tiktok():
    fake = FakeTikTok()
    fake.add_user("open-a", "alpha")
    return fake


@pytest.fixture()
def client(db, settings, store, fake):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    return TestClient(app)


def library_ids(client) -> list[str]:
    response = client.get("/api/videos")
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def youtube_video(db, settings, store, fake) -> YouTubeUpload:
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake, publish_options(thumbnail=generated_thumbnail()))
    library.ensure_library_thumbnail(db, row, settings)
    assert library.thumbnail_path(row, settings) is not None
    return row


def render_file(settings) -> Path:
    return settings.render_root / PID / "renders" / "v1" / "clipforge.mp4"


# ---------------------------------------------------------------------------
# YouTube entries
# ---------------------------------------------------------------------------


def test_ordinary_deletion_removes_the_entry_and_only_its_own_preview(db, settings, store, fake, client):
    row = youtube_video(db, settings, store, fake)
    preview = library.thumbnail_path(row, settings)
    calls_before = len(fake.calls)
    assert library_ids(client) == [row.id]

    response = client.delete(f"/api/videos/{row.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "removed" and body["platform"] == "youtube" and body["id"] == row.id
    assert body["removed_media"] == [{"kind": "library_preview", "name": preview.name, "bytes": body["freed_bytes"]}]
    assert body["freed_bytes"] > 0 and body["cleanup_complete"] is True and body["cancelled_schedule"] is False
    assert body["retained"] == {"publication_record": True, "remote_post": True, "project": True}
    assert not preview.exists()
    # gone from the Videos tab (index, counts and detail) ...
    assert library_ids(client) == []
    assert client.get("/api/videos").json()["summary"]["total"] == 0
    assert client.get(f"/api/videos/{row.id}").status_code == 404
    assert client.get(f"/api/videos/{row.youtube_video_id}").status_code == 404
    # ... but the publication record, the project and its canonical render stay
    db.expire_all()
    kept = db.get(YouTubeUpload, row.id)
    assert kept is not None and kept.youtube_video_id == row.youtube_video_id and kept.library_removed_at is not None
    assert db.get(Project, PID) is not None and render_file(settings).is_file()
    # no provider call: the YouTube video is never touched
    assert len(fake.calls) == calls_before


def test_deletion_by_youtube_video_id(db, settings, store, fake, client):
    row = youtube_video(db, settings, store, fake)
    response = client.delete(f"/api/videos/{row.youtube_video_id}")
    assert response.status_code == 200 and response.json()["id"] == row.id
    assert library_ids(client) == []


def test_published_video_keeps_its_analytics_and_learning_record(db, settings, store, fake, client):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    snapshots = db.scalar(select(func.count()).select_from(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == row.id))
    assert snapshots and row.fingerprint_id
    performance_before = client.get("/api/videos/performance").json()
    publications_before = read_model.project_publications(db, PID)

    response = client.delete(f"/api/videos/{row.id}")

    assert response.status_code == 200 and response.json()["retained"]["remote_post"] is True
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(YouTubeAnalyticsSnapshot).where(YouTubeAnalyticsSnapshot.upload_id == row.id)) == snapshots
    assert db.get(ProductionFingerprint, row.fingerprint_id) is not None
    # analytics/learning keep the video in their cohort: nothing becomes inconsistent
    assert client.get("/api/videos/performance").json() == performance_before
    assert row.id in set(db.scalars(select(YouTubeUpload.id).where(library.library_condition())).all())
    # the project still knows this YouTube post exists
    assert [item["id"] for item in read_model.project_publications(db, PID)] == [item["id"] for item in publications_before]
    assert read_model.project_publications(db, PID)[0]["remote_url"].endswith(row.youtube_video_id)


def test_missing_preview_file_is_not_an_error(db, settings, store, fake, client):
    row = youtube_video(db, settings, store, fake)
    library.thumbnail_path(row, settings).unlink()
    response = client.delete(f"/api/videos/{row.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "removed" and body["removed_media"] == [] and body["freed_bytes"] == 0 and body["cleanup_complete"] is True
    assert library_ids(client) == []


def test_video_without_any_preview_can_be_removed(db, settings, store, fake, client):
    connect(db, settings, store, fake)
    row = upload(db, build_project(db, settings), settings, store, fake)
    row.library_thumbnail = None
    db.commit()
    response = client.delete(f"/api/videos/{row.id}")
    assert response.status_code == 200 and response.json()["removed_media"] == []


def test_repeated_deletion_is_idempotent(db, settings, store, fake, client):
    row = youtube_video(db, settings, store, fake)
    first = client.delete(f"/api/videos/{row.id}")
    assert first.status_code == 200 and first.json()["status"] == "removed"
    stamp = db.get(YouTubeUpload, row.id).library_removed_at
    for _ in range(2):
        again = client.delete(f"/api/videos/{row.id}")
        assert again.status_code == 200
        assert again.json()["status"] == "already_removed" and again.json()["removed_media"] == [] and again.json()["cleanup_complete"] is True
    db.expire_all()
    assert db.get(YouTubeUpload, row.id).library_removed_at == stamp


def test_a_removed_video_rejected_later_stays_already_removed(db, settings, store, fake, client):
    row = youtube_video(db, settings, store, fake)
    assert client.delete(f"/api/videos/{row.id}").json()["status"] == "removed"
    row.upload_status = "rejected"  # YouTube's status changed after the removal
    db.commit()
    again = client.delete(f"/api/videos/{row.id}")
    assert again.status_code == 200 and again.json()["status"] == "already_removed"


def test_unknown_and_non_library_targets_are_not_found(db, settings, store, fake, client):
    from clipforge.youtube.provider import YouTubeApiError

    connect(db, settings, store, fake)
    project = build_project(db, settings)
    fake.start_error = YouTubeApiError("quota_exceeded", "The YouTube quota is exhausted.")
    failed = upload(db, project, settings, store, fake)
    assert failed.youtube_video_id is None
    for identifier in (failed.id, "no-such-video", "a" * 65, "..", "%2E%2E%2Fsecret", "x y"):
        response = client.delete(f"/api/videos/{identifier}")
        assert response.status_code in (404, 405), (identifier, response.status_code)
    assert client.delete(f"/api/videos/{failed.id}").json()["detail"]["status"] == "not_found"
    db.expire_all()
    assert db.get(YouTubeUpload, failed.id) is not None  # a failed attempt is never removed by this endpoint


def test_identifier_validation_rejects_paths(db, settings):
    for identifier in ("../etc/passwd", "a/b", "..", "", "x" * 65, "id.webp", "C:\\x"):
        with pytest.raises(LookupError):
            library_removal.remove_from_library(db, identifier, settings)


def test_only_files_clipforge_named_for_this_entry_are_deleted(db, settings, store, fake, client, tmp_path):
    row = youtube_video(db, settings, store, fake)
    own = library.thumbnail_path(row, settings)
    directory = library.library_directory(settings)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    neighbour = directory / f"{OTHER}.webp"
    neighbour.write_bytes(b"another entry's preview")
    for stored in (f"../../{outside.name}", str(outside), f"{OTHER}.webp", f"{row.id}.webp/../{OTHER}.webp", "", f"{row.id}.png"):
        row.library_thumbnail = stored
        row.library_removed_at = None
        db.commit()
        assert library_removal.owned_preview(row, settings) is None, stored
        response = client.delete(f"/api/videos/{row.id}")
        assert response.status_code == 200 and response.json()["removed_media"] == []
    assert outside.read_text() == "keep me" and neighbour.is_file() and own.is_file()


def test_a_symlinked_preview_is_unlinked_without_touching_its_target(db, settings, store, fake, client, tmp_path):
    row = youtube_video(db, settings, store, fake)
    own = library.thumbnail_path(row, settings)
    own.unlink()
    target = tmp_path / "precious.bin"
    target.write_bytes(b"precious")
    os.symlink(target, own)
    response = client.delete(f"/api/videos/{row.id}")
    assert response.status_code == 200 and response.json()["cleanup_complete"] is True
    assert not os.path.lexists(own) and target.read_bytes() == b"precious"


def test_partially_cleaned_media_is_retried_by_a_repeated_delete(db, settings, store, fake, client, monkeypatch):
    row = youtube_video(db, settings, store, fake)
    preview = library.thumbnail_path(row, settings)
    real_unlink = Path.unlink

    def failing_unlink(self, missing_ok=False):
        if self == preview:
            raise PermissionError("locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    first = client.delete(f"/api/videos/{row.id}")
    assert first.status_code == 200
    assert first.json()["status"] == "removed" and first.json()["cleanup_complete"] is False and first.json()["removed_media"] == []
    assert preview.is_file() and library_ids(client) == []  # hidden even though the file is still there
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = client.delete(f"/api/videos/{row.id}")
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "already_removed" and body["cleanup_complete"] is True
    assert [item["name"] for item in body["removed_media"]] == [preview.name] and not preview.exists()


def test_later_project_deletion_never_recreates_a_removed_preview(db, settings, store, fake, client):
    connect(db, settings, store, fake)
    row = _published_with_analytics(db, settings, store, fake, build_project(db, settings))
    library.ensure_library_thumbnail(db, row, settings)
    assert client.delete(f"/api/videos/{row.id}").status_code == 200
    result = lifecycle.delete_project_lifecycle(db, PID, settings, store, fake)
    assert result["mode"] == "archive"
    assert files_under(library.library_directory(settings)) == set()
    db.expire_all()
    kept = db.get(YouTubeUpload, row.id)
    assert kept is not None and kept.library_removed_at is not None
    assert library_ids(client) == []


def test_removing_one_video_leaves_the_others_alone(db, settings, store, fake, client):
    first = youtube_video(db, settings, store, fake)
    second_project = build_project(db, settings, project_id=OTHER, content=b"O" * 20_000)
    second = upload(db, second_project, settings, store, fake, publish_options(title="Second", thumbnail=generated_thumbnail()))
    library.ensure_library_thumbnail(db, second, settings)
    assert set(library_ids(client)) == {first.id, second.id}
    assert client.delete(f"/api/videos/{first.id}").status_code == 200
    assert library_ids(client) == [second.id]
    assert library.thumbnail_path(second, settings) is not None


# ---------------------------------------------------------------------------
# Instagram/TikTok entries
# ---------------------------------------------------------------------------


def published_tiktok(db, settings, store, tiktok) -> SocialPublication:
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at)
    scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(seconds=5))
    scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(seconds=30))
    db.refresh(row)
    assert row.state == "published" and row.remote_post_id
    return row


def test_published_social_post_keeps_its_publication_record(db, settings, store, tiktok, client):
    row = published_tiktok(db, settings, store, tiktok)
    key, remote_id, events = row.idempotency_key, row.remote_post_id, len(row.events)
    calls = len(tiktok.calls)
    assert library_ids(client) == [row.id]

    response = client.delete(f"/api/videos/{row.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform"] == "tiktok" and body["status"] == "removed" and body["removed_media"] == []
    assert body["cancelled_schedule"] is False and body["retained"]["remote_post"] is True and body["retained"]["project"] is True
    assert library_ids(client) == []
    db.expire_all()
    kept = db.get(SocialPublication, row.id)
    assert kept.state == "published" and kept.remote_post_id == remote_id and len(kept.events) == events
    # still the active publication of that render: the same post is not re-published by accident
    assert kept.idempotency_key == key
    assert [item["id"] for item in read_model.project_publications(db, kept.project_id)] == [row.id]
    assert len(tiktok.calls) == calls  # no TikTok call, the post itself is never deleted
    assert client.delete(f"/api/videos/{row.id}").json()["status"] == "already_removed"


def test_scheduled_social_post_is_cancelled_so_it_never_publishes(db, settings, store, tiktok, client):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at)
    response = client.delete(f"/api/videos/{row.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "removed" and body["cancelled_schedule"] is True and body["retained"]["remote_post"] is False
    db.expire_all()
    kept = db.get(SocialPublication, row.id)
    assert kept.state == "cancelled" and kept.idempotency_key is None and kept.library_removed_at is not None
    assert kept.events[-1]["state"] == "cancelled" and "Video Library" in kept.events[-1]["note"]
    result = scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(seconds=5))
    assert result["ran"] == [] and not [call for call in tiktok.calls if call[0] == "init"]
    again = client.delete(f"/api/videos/{row.id}")
    assert again.status_code == 200 and again.json()["status"] == "already_removed"


def test_a_post_being_published_right_now_is_refused(db, settings, store, tiktok, client):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at)
    assert publications.claim(db, row.id, now=due + timedelta(seconds=1))  # the scheduler took it
    response = client.delete(f"/api/videos/{row.id}")
    assert response.status_code == 409 and response.json()["detail"]["status"] == "publication_in_progress"
    db.expire_all()
    kept = db.get(SocialPublication, row.id)
    assert kept.state == "uploading" and kept.library_removed_at is None and kept.idempotency_key


def test_cancel_race_with_the_scheduler_never_cancels_a_started_upload(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at)
    # The remover read "scheduled"; the scheduler claims before the cancel lands.
    assert row.state == "scheduled"
    from sqlalchemy.orm import Session

    with Session(db.get_bind()) as other:
        assert publications.claim(other, row.id, now=due + timedelta(seconds=1))
    assert publications.cancel_unstarted(db, row, now=due + timedelta(seconds=2), note="x") is False
    db.rollback()
    assert db.get(SocialPublication, row.id).state == "uploading"


def test_a_cancelled_post_that_is_not_in_the_library_is_not_found(db, settings, store, tiktok, client):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    publications.cancel(db, row)
    assert client.delete(f"/api/videos/{row.id}").status_code == 404


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_migration_adds_the_removal_marker_to_both_publication_tables(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'clipforge.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    for table in ("youtube_uploads", "social_publications"):
        assert "library_removed_at" in {column["name"] for column in inspector.get_columns(table)}
    command.downgrade(config, "0013_multiplatform_publishing")
    inspector = inspect(create_engine(url))
    for table in ("youtube_uploads", "social_publications"):
        assert "library_removed_at" not in {column["name"] for column in inspector.get_columns(table)}
    get_settings.cache_clear()


def test_startup_schema_path_adds_the_marker_to_existing_tables(tmp_path):
    from clipforge.database import Base, ensure_runtime_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # a database from before this change
        for table in ("youtube_uploads", "social_publications"):
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN library_removed_at")
    ensure_runtime_schema(engine)
    for table in ("youtube_uploads", "social_publications"):
        assert "library_removed_at" in {column["name"] for column in inspect(engine).get_columns(table)}
