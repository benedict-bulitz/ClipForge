"""ClipForge-owned scheduling for Instagram/TikTok: persistence, safety, recovery."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from publishing_support import (
    FakeInstagram,
    FakeTikTok,
    apis,
    connect_instagram,
    connect_tiktok,
    publishing_settings,
    social_project,
)
from sqlalchemy import select

from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.exporter import resolve_final_master
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import SocialPublication
from clipforge.publishing import accounts, connections, publications, read_model, scheduler
from clipforge.publishing.errors import PublishingApiError
from clipforge.publishing.publications import PublicationRefused, PublicationRequest
from clipforge.publishing.routes import get_publication_dispatcher, get_publishing_apis
from clipforge.security.secrets import SecretStore
from clipforge.services import effective_revision_state
from clipforge.youtube import library, uploads
from clipforge.youtube.publishing import ScheduleChoice


@pytest.fixture(autouse=True)
def _reset():
    connections.reset_cache()
    uploads._SHA_CACHE.clear()
    yield
    connections.reset_cache()
    app.dependency_overrides.clear()


@pytest.fixture()
def settings(tmp_path):
    return publishing_settings(tmp_path)


@pytest.fixture()
def store():
    return SecretStore()


@pytest.fixture()
def tiktok():
    fake = FakeTikTok()
    fake.add_user("open-a", "alpha")
    fake.add_user("open-b", "beta")
    return fake


@pytest.fixture()
def instagram():
    fake = FakeInstagram()
    fake.add_account("ig-1", "brand_one")
    return fake


def tiktok_request(account, project, *, mode="schedule", when: datetime | None = None, timezone="Europe/Berlin") -> PublicationRequest:
    schedule = None
    if mode == "schedule":
        local = (when or datetime.now(UTC) + timedelta(days=1)).astimezone(__import__("zoneinfo").ZoneInfo(timezone))
        schedule = {"date": local.date().isoformat(), "time": local.strftime("%H:%M"), "timezone": timezone}
    return PublicationRequest(
        account_id=account.id, base_revision=project.current_revision, mode=mode, schedule=schedule,
        tiktok={"caption": "Round windows.", "hashtags": ["#aviation"], "privacy_level": "SELF_ONLY", "is_aigc": False, "music_usage_confirmed": True},
    )


def schedule_tiktok(db, settings, store, tiktok, account, project, when: datetime | None = None, timezone="Europe/Berlin") -> SocialPublication:
    row, run_now = publications.request_publication(db, project, settings, store, apis(tiktok), tiktok_request(account, project, when=when, timezone=timezone))
    assert not run_now
    return row


def final_path(project, settings):
    db_state = effective_revision_state(project)
    return resolve_final_master(project.id, project.title, db_state, settings).path


def test_future_schedule_is_persisted_with_its_exact_binding(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-b")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project, when=datetime(2026, 12, 1, 18, 30, tzinfo=UTC) if datetime.now(UTC) < datetime(2026, 11, 20, tzinfo=UTC) else None)
    db.expire_all()
    stored = db.get(SocialPublication, row.id)
    assert stored.state == "scheduled" and stored.mode == "schedule"
    assert stored.account_id == account.id and stored.external_account_id == "open-b" and stored.platform == "tiktok"
    assert stored.project_id == project.id and stored.project_revision == project.current_revision
    assert stored.render_revision == 1
    assert stored.render_sha256 == uploads.file_sha256(final_path(project, settings))
    assert stored.render_file_size == final_path(project, settings).stat().st_size
    assert stored.metadata_snapshot["caption"] == "Round windows.\n\n#aviation"
    assert stored.schedule_timezone == "Europe/Berlin" and stored.scheduled_at is not None
    assert not [call for call in tiktok.calls if call[0] == "init"]  # nothing uploaded before it is due


def test_timezone_is_resolved_with_the_tz_database(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    now = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    summer = PublicationRequest(
        account_id=account.id, base_revision=project.current_revision, mode="schedule",
        schedule={"date": "2026-07-02", "time": "20:30", "timezone": "Europe/Berlin"},
        tiktok={"caption": "x", "privacy_level": "SELF_ONLY", "is_aigc": False, "music_usage_confirmed": True},
    )
    _issues, resolution, _account, _creator = publications.preflight(db, project, settings, store, apis(tiktok), summer, now=now)
    assert resolution.publish_at == datetime(2026, 7, 2, 18, 30, tzinfo=UTC)  # CEST = UTC+2
    winter = summer.model_copy(update={"schedule": ScheduleChoice(date="2026-12-02", time="20:30", timezone="Europe/Berlin")})
    _issues, resolution, _account, _creator = publications.preflight(db, project, settings, store, apis(tiktok), winter, now=now)
    assert resolution.publish_at == datetime(2026, 12, 2, 19, 30, tzinfo=UTC)  # CET = UTC+1
    gap = summer.model_copy(update={"schedule": ScheduleChoice(date="2027-03-28", time="02:30", timezone="Europe/Berlin")})
    issues, _resolution, _account, _creator = publications.preflight(db, project, settings, store, apis(tiktok), gap, now=now)
    assert any("does not exist" in item["message"] for item in issues)  # DST gap is refused, never guessed


def test_due_schedule_runs_once_on_the_bound_account(db, settings, store, tiktok):
    connect_tiktok(db, settings, store, tiktok, "open-a")
    b = connect_tiktok(db, settings, store, tiktok, "open-b")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, b, project)
    due = publications.aware(row.scheduled_at)
    early = scheduler.tick(db, settings, store, apis(tiktok), now=due - timedelta(minutes=1))
    assert early["ran"] == [] and db.get(SocialPublication, row.id).state == "scheduled"
    result = scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(seconds=5))
    assert [item["id"] for item in result["ran"]] == [row.id]
    db.refresh(row)
    assert row.state == "processing"
    init = next(detail for name, detail in tiktok.calls if name == "init")
    assert init["token"] == "at-open-b"
    # a second pass never runs it again
    again = scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(seconds=30))
    assert again["ran"] == [] and len([call for call in tiktok.calls if call[0] == "init"]) == 1
    assert again["polled"] == [{"id": row.id, "state": "published"}]
    final = scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(minutes=2))
    assert final == {"recovered": [], "missed": [], "ran": [], "polled": []}


def test_duplicate_execution_is_prevented(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at) + timedelta(seconds=1)
    assert publications.claim(db, row.id, now=due) is True
    assert publications.claim(db, row.id, now=due) is False  # the lease makes execution exclusive
    with pytest.raises(PublicationRefused) as error:
        schedule_tiktok(db, settings, store, tiktok, account, project)
    assert error.value.code == "already_scheduled"  # same render + account cannot be queued twice


def test_changed_render_is_rejected_at_execution(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    final_path(project, settings).write_bytes(b"re-rendered" * 3000)  # a new final master replaced the bound one
    uploads._SHA_CACHE.clear()
    scheduler.tick(db, settings, store, apis(tiktok), now=publications.aware(row.scheduled_at) + timedelta(seconds=1))
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code == "render_changed"
    assert "will not upload a different video" in row.last_error_message
    assert not [call for call in tiktok.calls if call[0] == "init"]


def test_missing_final_master_is_rejected(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    final_path(project, settings).unlink()
    scheduler.tick(db, settings, store, apis(tiktok), now=publications.aware(row.scheduled_at) + timedelta(seconds=1))
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code in {"render_missing", "render_changed"}
    assert not [call for call in tiktok.calls if call[0] == "init"]


def test_transient_failures_retry_with_bounded_backoff(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    tiktok.init_errors = [PublishingApiError("provider_error", "TikTok 503") for _ in range(10)]
    now = publications.aware(row.scheduled_at) + timedelta(seconds=1)
    waits = []
    for _ in range(settings.publishing_max_attempts):
        scheduler.tick(db, settings, store, apis(tiktok), now=now)
        db.refresh(row)
        if row.state == "failed":
            break
        assert row.state == "scheduled" and row.last_error_code == "provider_error"
        waits.append(publications.aware(row.next_attempt_at) - now)
        now = publications.aware(row.next_attempt_at) + timedelta(seconds=1)
    assert row.state == "failed" and row.attempt_count == settings.publishing_max_attempts
    assert waits == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=8)]
    assert "gave up" in row.last_error_message


def test_permanent_failure_stops_retrying(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    connections.reset_cache()
    tiktok.refresh_error = PublishingApiError("auth_expired", "TikTok access expired or was revoked.", retryable=False)
    due = publications.aware(row.scheduled_at) + timedelta(seconds=1)
    scheduler.tick(db, settings, store, apis(tiktok), now=due)
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code == "auth_expired" and row.attempt_count == 1
    db.refresh(account)
    assert account.status == "auth_expired"
    scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(hours=1))
    db.refresh(row)
    assert row.attempt_count == 1


def test_offline_past_schedule_becomes_missed_and_is_never_published_late(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    # ClipForge was not running at the scheduled time; it starts 3 hours later.
    later = publications.aware(row.scheduled_at) + timedelta(hours=3)
    recovery = scheduler.startup(db, settings, now=later)
    assert recovery["missed"] == [row.id]
    scheduler.tick(db, settings, store, apis(tiktok), now=later + timedelta(seconds=30))
    db.refresh(row)
    assert row.state == "missed" and row.last_error_code == "missed_schedule"
    assert not [call for call in tiktok.calls if call[0] == "init"]
    assert publications.serialize(row)["actions"] == {"cancel": True, "reschedule": True, "publish_now": True, "retry": False}
    # The user decides: publish now.
    publications.publish_missed_now(db, row, now=later + timedelta(minutes=1))
    scheduler.tick(db, settings, store, apis(tiktok), now=later + timedelta(minutes=1, seconds=1))
    db.refresh(row)
    assert row.state == "processing"


def test_slightly_late_start_within_grace_still_publishes(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    restart = publications.aware(row.scheduled_at) + timedelta(minutes=5)
    assert scheduler.startup(db, settings, now=restart)["missed"] == []
    scheduler.tick(db, settings, store, apis(tiktok), now=restart)
    db.refresh(row)
    assert row.state == "processing"


def test_restart_recovery_never_reuploads_a_delivered_video(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    due = publications.aware(row.scheduled_at) + timedelta(seconds=1)
    scheduler.tick(db, settings, store, apis(tiktok), now=due)
    db.refresh(row)
    # Simulate a crash right after the last byte: the row still says uploading.
    row.state = "uploading"
    row.lease_until = due + timedelta(minutes=15)
    db.commit()
    recovered = scheduler.startup(db, settings, now=due + timedelta(minutes=1))
    assert recovered["recovered"] == [row.id]
    db.refresh(row)
    assert row.state == "processing"
    scheduler.tick(db, settings, store, apis(tiktok), now=due + timedelta(minutes=2))
    db.refresh(row)
    assert row.state == "published" and len([call for call in tiktok.calls if call[0] == "init"]) == 1


def test_restart_recovery_restarts_an_interrupted_partial_upload(db, settings, store, instagram):
    (account,) = connect_instagram(db, settings, store, instagram)
    project = social_project(db, settings)
    row, _run = publications.request_publication(
        db, project, settings, store, apis(instagram=instagram),
        PublicationRequest(account_id=account.id, base_revision=project.current_revision, instagram={"caption": "x"}),
    )
    row.state, row.remote_container_id, row.upload_complete = "uploading", "17900000099", False
    row.lease_until = datetime.now(UTC) + timedelta(minutes=10)
    db.commit()
    scheduler.startup(db, settings, now=datetime.now(UTC))
    db.refresh(row)
    assert row.state == "pending" and row.remote_container_id is None
    scheduler.tick(db, settings, store, apis(instagram=instagram), now=datetime.now(UTC) + timedelta(seconds=1))
    db.refresh(row)
    assert row.state == "processing" and row.remote_container_id in instagram.containers


def test_cancel_and_reschedule(db, settings, store, tiktok):
    account = connect_tiktok(db, settings, store, tiktok, "open-a")
    project = social_project(db, settings)
    row = schedule_tiktok(db, settings, store, tiktok, account, project)
    when = datetime.now(UTC) + timedelta(days=3)
    publications.reschedule(db, row, ScheduleChoice(date=when.date().isoformat(), time="09:15", timezone="America/New_York"))
    db.refresh(row)
    assert row.schedule_timezone == "America/New_York" and row.schedule_local_time.endswith("09:15")
    publications.cancel(db, row)
    db.refresh(row)
    assert row.state == "cancelled" and row.idempotency_key is None
    scheduler.tick(db, settings, store, apis(tiktok), now=when + timedelta(days=1))
    assert not [call for call in tiktok.calls if call[0] == "init"]
    # history is kept; a new publication of the same render is allowed again
    again = schedule_tiktok(db, settings, store, tiktok, account, project)
    assert again.id != row.id and db.get(SocialPublication, row.id).state == "cancelled"


def test_failed_attempt_is_kept_and_retry_is_a_new_record(db, settings, store, instagram):
    (account,) = connect_instagram(db, settings, store, instagram)
    project = social_project(db, settings)
    instagram.container_errors = [PublishingApiError("provider_rejected", "Instagram refused.", retryable=False)]
    row, _run = publications.request_publication(
        db, project, settings, store, apis(instagram=instagram),
        PublicationRequest(account_id=account.id, base_revision=project.current_revision, instagram={"caption": "x"}),
    )
    publications.run(db, row.id, settings, store, apis(instagram=instagram))
    db.refresh(row)
    assert row.state == "failed"
    clone = publications.retry_as_new(db, row)
    assert clone.id != row.id and clone.render_sha256 == row.render_sha256 and clone.account_id == row.account_id
    db.refresh(row)
    assert row.state == "failed" and row.last_error_code == "provider_rejected"  # never overwritten


def test_project_can_have_publications_on_several_platforms_and_accounts(db, settings, store, tiktok, instagram):
    a = connect_tiktok(db, settings, store, tiktok, "open-a")
    connect_tiktok(db, settings, store, tiktok, "open-b")
    (ig,) = connect_instagram(db, settings, store, instagram)
    project = social_project(db, settings)
    schedule_tiktok(db, settings, store, tiktok, a, project)
    row, _run = publications.request_publication(
        db, project, settings, store, apis(instagram=instagram),
        PublicationRequest(account_id=ig.id, base_revision=project.current_revision, instagram={"caption": "x"}),
    )
    publications.run(db, row.id, settings, store, apis(instagram=instagram))
    items = read_model.project_publications(db, project.id)
    assert {(item["platform"], item["account_id"]) for item in items} == {("tiktok", a.id), ("instagram", ig.id)}
    page = library.list_videos(db, settings)
    assert page["summary"]["platforms"] == {"youtube": 0, "instagram": 1, "tiktok": 1}
    assert {item["platform"] for item in page["items"]} == {"tiktok", "instagram"}
    only_tiktok = library.list_videos(db, settings, platform="tiktok")
    assert [item["account"]["id"] for item in only_tiktok["items"]] == [a.id]
    by_account = library.list_videos(db, settings, account=ig.id)
    assert [item["platform"] for item in by_account["items"]] == ["instagram"]
    scheduled = library.list_videos(db, settings, status="scheduled")
    assert [item["platform"] for item in scheduled["items"]] == ["tiktok"]
    assert library.list_videos(db, settings, platform="youtube")["items"] == []
    assert {item["id"] for item in page["accounts"]} >= {a.id, ig.id}


def test_publishing_api_flow_with_the_unified_targets(db, settings, store, tiktok, instagram):
    connect_tiktok(db, settings, store, tiktok, "open-a")
    connect_instagram(db, settings, store, instagram)
    project = social_project(db, settings)
    dispatched: list[str] = []
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_publishing_apis] = lambda: apis(tiktok, instagram)
    app.dependency_overrides[get_publication_dispatcher] = lambda: dispatched.append
    client = TestClient(app)
    targets = client.get(f"/api/publishing/projects/{project.id}/targets").json()
    labels = [item["label"] for item in targets["targets"]]
    assert labels == ["Instagram · @brand_one", "TikTok · @alpha"]
    tiktok_target = next(item for item in targets["targets"] if item["platform"] == "tiktok")
    draft = client.get(f"/api/publishing/projects/{project.id}/draft", params={"account_id": tiktok_target["id"]}).json()
    assert draft["creator_info"]["privacy_level_options"][-1] == "SELF_ONLY"
    assert draft["options"]["caption"] == "The comet disaster explained in seconds."
    body = {
        "account_id": tiktok_target["id"], "base_revision": project.current_revision, "mode": "now",
        "tiktok": {**draft["options"], "privacy_level": "SELF_ONLY", "is_aigc": True, "music_usage_confirmed": True},
    }
    preflight = client.post(f"/api/publishing/projects/{project.id}/preflight", json=body).json()
    assert preflight["ready"] is True
    created = client.post(f"/api/publishing/projects/{project.id}/publications", json=body)
    assert created.status_code == 202 and created.json()["started"] is True
    assert dispatched == [created.json()["publication"]["id"]]
    duplicate = client.post(f"/api/publishing/projects/{project.id}/publications", json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["status"] == "already_scheduled"
    listed = client.get("/api/publishing/publications", params={"project_id": project.id}).json()
    assert len(listed["publications"]) == 1 and listed["scheduler"]["notice"]
    videos = client.get("/api/videos", params={"platform": "tiktok"}).json()
    assert videos["items"][0]["state"] == "pending" and videos["items"][0]["platform"] == "tiktok"


def test_scheduler_loop_survives_and_ticks(db, settings, store):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db.get_bind())
    thread = scheduler.SchedulerThread(Session, lambda: settings.model_copy(update={"publishing_scheduler_interval_seconds": 5}), lambda: store, lambda: apis())
    thread.start()
    try:
        for _ in range(100):
            if thread.last_tick_at is not None:
                break
            __import__("time").sleep(0.02)
        assert thread.last_tick_at is not None and thread.running
    finally:
        thread.stop()
    assert db.scalars(select(SocialPublication)).all() == []
    assert accounts.list_accounts(db) == []
