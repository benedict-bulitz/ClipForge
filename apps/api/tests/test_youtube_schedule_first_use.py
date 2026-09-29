"""Regression: Settings -> Integrations -> YouTube on a database from before the Smart Scheduler.

Real bug: the publishing schedule showed "could not be loaded".  The dev
page mounts twice (React Strict Mode), so two first-use GET /schedule
requests ran the YouTube sync at the same time and both inserted the same
cache rows; the loser failed with an IntegrityError -> a bare 500 without
CORS headers -> the browser's fetch rejected.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from youtube_support import FakeYouTube, youtube_settings

from alembic import command
from clipforge import database
from clipforge.config import get_settings
from clipforge.database import get_db, prepare_schema
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import YouTubeScheduleEntry
from clipforge.security.secrets import SecretStore
from clipforge.youtube import connection, publishing
from clipforge.youtube.routes import get_youtube_provider

API_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_TABLES = {"youtube_publishing_schedules", "youtube_schedule_slots", "youtube_schedule_entries", "youtube_slot_reservations"}
BERLIN = "Europe/Berlin"


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    publishing.reset_category_cache()
    yield
    connection.reset_youtube_auth_cache()
    app.dependency_overrides.clear()


class SlowYouTube(FakeYouTube):
    """Google answers take time, so two page-load requests really overlap."""

    delay: float = 0.3

    def list_videos(self, access_token, video_ids, parts):
        time.sleep(self.delay)
        return super().list_videos(access_token, video_ids, parts)


def pre_scheduler_database(tmp_path: Path, monkeypatch) -> str:
    """A dev database at the previous YouTube migration (0008), before the scheduler."""
    url = f"sqlite:///{tmp_path / 'clipforge.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config()  # no ini file: alembic must not reconfigure the test process' logging
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "0008_youtube_remote_status")
    get_settings.cache_clear()
    return url


def start_like_the_app(url: str, monkeypatch):
    """The canonical startup schema path (lifespan -> prepare_schema) on that database."""
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", engine)
    prepare_schema()
    return engine


def seed_existing_setup(engine, settings, store, fake) -> None:
    """What the real database already had: a connected channel and saved upload defaults."""
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        url = connection.begin_authorization(settings)
        state = parse_qs(urlparse(url).query)["state"][0]
        connection.complete_authorization(db, settings, store, fake, code="auth-code", state=state)
        publishing.save_defaults(db, publishing.UploadDefaults(made_for_kids=False, visibility="private", timezone=BERLIN))


def client_for(engine, settings, store, fake) -> TestClient:
    Session = sessionmaker(bind=engine, autoflush=False)

    def session_per_request():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = session_per_request
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: fake
    return TestClient(app, raise_server_exceptions=False)


def concurrently(requests: int, call):
    results: list = [None] * requests
    barrier = threading.Barrier(requests)

    def work(index: int) -> None:
        barrier.wait()
        results[index] = call()

    threads = [threading.Thread(target=work, args=(index,)) for index in range(requests)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.fixture()
def real_setup(tmp_path, monkeypatch):
    settings = youtube_settings(tmp_path)
    store = SecretStore()
    fake = SlowYouTube()
    for index in range(6):
        fake.add_studio_video(f"studio{index}", publish_at=f"2026-10-{10 + index}T18:00:00Z", published_at="2026-09-20T10:00:00Z")
    engine = start_like_the_app(pre_scheduler_database(tmp_path, monkeypatch), monkeypatch)
    seed_existing_setup(engine, settings, store, fake)
    return engine, settings, store, fake


def test_startup_path_upgrades_a_pre_scheduler_database(tmp_path, monkeypatch):
    url = pre_scheduler_database(tmp_path, monkeypatch)
    before = create_engine(url)
    assert not SCHEDULER_TABLES & set(inspect(before).get_table_names())
    assert not {"schedule_source", "schedule_slot_time"} & {column["name"] for column in inspect(before).get_columns("youtube_uploads")}
    engine = start_like_the_app(url, monkeypatch)
    inspector = inspect(engine)
    assert SCHEDULER_TABLES <= set(inspector.get_table_names())
    assert {"schedule_source", "schedule_slot_time"} <= {column["name"] for column in inspector.get_columns("youtube_uploads")}
    assert {"enabled", "videos_per_day", "timezone", "uploads_playlist_id", "remote_synced_at"} <= {column["name"] for column in inspector.get_columns("youtube_publishing_schedules")}
    prepare_schema()  # idempotent on every later start
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0008_youtube_remote_status"


def test_alembic_upgrades_the_previous_version_to_the_scheduler_schema(tmp_path, monkeypatch):
    url = pre_scheduler_database(tmp_path, monkeypatch)
    config = Config()
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert SCHEDULER_TABLES <= set(inspector.get_table_names())
    assert {"schedule_source", "schedule_slot_time"} <= {column["name"] for column in inspector.get_columns("youtube_uploads")}


def test_integrations_page_loads_the_first_use_schedule(real_setup):
    engine, settings, store, fake = real_setup
    client = client_for(engine, settings, store, fake)
    assert client.get("/api/youtube/connection").json()["status"] == "connected"
    response = client.get("/api/youtube/schedule", params={"timezone": BERLIN})
    assert response.status_code == 200, response.text
    body = response.json()
    schedule = body["schedule"]
    assert (schedule["videos_per_day"], schedule["slots"], schedule["mode"], schedule["enabled"], schedule["timezone"]) == (1, ["20:00"], "seed", True, BERLIN)
    assert body["status"] == "verified" and body["days"] and body["learning"]["available"] is False
    # The audit declaration is untouched: still false unless the user set it.
    assert client.get("/api/youtube/defaults").json()["defaults"]["api_project_audited"] is False


def test_strict_mode_double_load_on_first_use_never_fails(real_setup):
    engine, settings, store, fake = real_setup
    client_for(engine, settings, store, fake)

    def load():
        return TestClient(app, raise_server_exceptions=False).get("/api/youtube/schedule", params={"timezone": BERLIN})

    responses = concurrently(2, load)
    assert [response.status_code for response in responses] == [200, 200], [response.text[:300] for response in responses]
    assert all(response.json()["schedule"]["slots"] == ["20:00"] for response in responses)
    with sessionmaker(bind=engine)() as db:
        assert len(db.scalars(select(YouTubeScheduleEntry)).all()) == 6
    # One page load refreshes YouTube once, not once per request (quota).
    assert sum(1 for name, _ in fake.calls if name == "playlist_items") == 1


def test_concurrent_refresh_of_a_stale_cache_with_studio_changes_never_fails(real_setup):
    engine, settings, store, fake = real_setup
    client_for(engine, settings, store, fake)
    assert TestClient(app, raise_server_exceptions=False).get("/api/youtube/schedule").status_code == 200
    del fake.videos["studio0"]  # deleted in YouTube Studio
    fake.add_studio_video("studio_new", publish_at="2026-10-20T18:00:00Z", published_at="2026-09-21T10:00:00Z")

    def refresh():
        return TestClient(app, raise_server_exceptions=False).post("/api/youtube/schedule/refresh")

    responses = concurrently(3, refresh)
    assert [response.status_code for response in responses] == [200, 200, 200], [response.text[:300] for response in responses]
    with sessionmaker(bind=engine)() as db:
        ids = {row.video_id for row in db.scalars(select(YouTubeScheduleEntry)).all()}
    assert "studio0" not in ids and "studio_new" in ids and len(ids) == 6


def test_a_genuine_load_failure_is_a_structured_error_not_a_bare_500(real_setup, monkeypatch):
    engine, settings, store, fake = real_setup
    client = client_for(engine, settings, store, fake)
    from sqlalchemy.exc import OperationalError

    from clipforge.youtube import schedule_learning

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(schedule_learning, "analyze", broken)
    response = client.get("/api/youtube/schedule", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 503
    assert response.json()["detail"] == {"status": "schedule_unavailable", "message": "Could not load publishing schedule."}
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"  # the browser can read it
