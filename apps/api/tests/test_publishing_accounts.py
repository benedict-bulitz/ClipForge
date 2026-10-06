"""Multi-account publishing: account model, per-account secrets, YouTube migration."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from publishing_support import (
    FakeInstagram,
    FakeTikTok,
    MultiChannelYouTube,
    apis,
    connect_instagram,
    connect_tiktok,
    publishing_settings,
)
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from youtube_support import exported_project, publish_options

from alembic import command
from clipforge.config import get_settings
from clipforge.database import get_db
from clipforge.integrations import get_secret_store
from clipforge.main import app
from clipforge.models import PublishingAccount, YouTubeConnection, YouTubeUpload
from clipforge.publishing import accounts, connections
from clipforge.publishing.routes import get_publishing_apis
from clipforge.security.secrets import SecretStore, account_secret_name
from clipforge.youtube import analytics, connection, uploads
from clipforge.youtube.provider import REQUESTED_SCOPES, ChannelIdentity, YouTubeApiError
from clipforge.youtube.routes import get_youtube_provider

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset():
    connection.reset_youtube_auth_cache()
    connections.reset_cache()
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
def youtube():
    return MultiChannelYouTube()


def connect_youtube(db, settings, store, fake, channel_id: str, title: str):
    fake.channel = ChannelIdentity(channel_id, title)
    url = connection.begin_authorization(settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    return connection.complete_authorization(db, settings, store, fake, code="auth-code", state=state)


def all_database_text(db) -> str:
    rows = []
    for table in ("publishing_accounts", "social_publications", "publishing_platform_configs", "youtube_connections", "youtube_uploads"):
        rows.extend(str(tuple(row)) for row in db.execute(text(f"SELECT * FROM {table}")).all())
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Account model
# ---------------------------------------------------------------------------


def test_multiple_youtube_channels_each_with_their_own_keyring_entry(db, settings, store, youtube, test_keyring):
    a = connect_youtube(db, settings, store, youtube, "UC_channel_A", "Rank Frame Shorts")
    b = connect_youtube(db, settings, store, youtube, "UC_channel_B", "Second Channel")
    c = connect_youtube(db, settings, store, youtube, "UC_channel_C", "Third Channel")
    assert len({a.id, b.id, c.id}) == 3
    assert [item.channel_id for item in connection.list_channels(db)] == ["UC_channel_A", "UC_channel_B", "UC_channel_C"]
    for account in (a, b, c):
        assert test_keyring.secrets[("ClipForge", f"YOUTUBE_REFRESH_TOKEN:{account.id}")] == f"1//rt-{account.channel_id}"
    assert ("ClipForge", "YOUTUBE_REFRESH_TOKEN") not in test_keyring.secrets
    assert a.is_default and not b.is_default and not c.is_default


def test_multiple_instagram_and_tiktok_accounts_without_a_cap(db, settings, store, test_keyring):
    tiktok = FakeTikTok()
    for index in range(25):  # no ClipForge account limit
        tiktok.add_user(f"open-{index:02d}", f"creator{index:02d}")
        connect_tiktok(db, settings, store, tiktok, f"open-{index:02d}")
    instagram = FakeInstagram()
    instagram.add_account("17841400000000001", "brand_one")
    instagram.add_account("17841400000000002", "brand_two")
    connected = connect_instagram(db, settings, store, instagram)
    assert len(connected) == 2
    assert len(accounts.list_accounts(db, "tiktok")) == 25
    assert len(accounts.list_accounts(db, "instagram")) == 2
    keys = {name for (_service, name) in test_keyring.secrets}
    assert sum(1 for name in keys if name.startswith("TIKTOK_REFRESH_TOKEN:")) == 25
    assert sum(1 for name in keys if name.startswith("INSTAGRAM_TOKEN:")) == 2
    assert {item.handle for item in connected} == {"brand_one", "brand_two"}


def test_duplicate_external_account_keeps_one_stable_row(db, settings, store):
    tiktok = FakeTikTok()
    tiktok.add_user("open-1", "creator")
    first = connect_tiktok(db, settings, store, tiktok, "open-1")
    again = connect_tiktok(db, settings, store, tiktok, "open-1")
    assert first.id == again.id
    assert db.scalar(select(PublishingAccount.id).where(PublishingAccount.platform == "tiktok").limit(2)) == first.id
    assert len(db.scalars(select(PublishingAccount).where(PublishingAccount.platform == "tiktok")).all()) == 1
    # the database refuses a second row for the same (platform, external id)
    db.add(PublishingAccount(platform="tiktok", external_account_id="open-1", display_name="dup"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_disconnect_one_account_leaves_the_others_untouched(db, settings, store, test_keyring):
    tiktok = FakeTikTok()
    tiktok.add_user("open-a", "alpha")
    tiktok.add_user("open-b", "beta")
    a = connect_tiktok(db, settings, store, tiktok, "open-a")
    b = connect_tiktok(db, settings, store, tiktok, "open-b")
    connections.disconnect(db, settings, store, a, tiktok_api=tiktok)
    assert ("ClipForge", f"TIKTOK_REFRESH_TOKEN:{a.id}") not in test_keyring.secrets
    assert test_keyring.secrets[("ClipForge", f"TIKTOK_REFRESH_TOKEN:{b.id}")] == "rt-open-b"
    assert ("revoke", "at-open-a") in tiktok.calls and ("revoke", "at-open-b") not in tiktok.calls
    db.refresh(b)
    assert b.status == "connected" and accounts.default_account(db, "tiktok").id == b.id
    connections.reset_cache()
    assert connections.access_token(db, settings, store, account=b, tiktok_api=tiktok) == "at-open-b"
    with pytest.raises(Exception) as error:
        connections.access_token(db, settings, store, account=a, tiktok_api=tiktok)
    assert error.value.code == "not_connected"


def test_instagram_disconnect_does_not_revoke_a_shared_facebook_authorization(db, settings, store, test_keyring):
    instagram = FakeInstagram()
    instagram.add_account("ig-1", "brand_one")
    instagram.add_account("ig-2", "brand_two")
    one, two = connect_instagram(db, settings, store, instagram)
    connections.disconnect(db, settings, store, one, instagram_api=instagram)
    assert instagram.revoked == []  # brand_two still uses the same Facebook authorization
    assert ("ClipForge", f"INSTAGRAM_TOKEN:{two.id}") in test_keyring.secrets
    connections.disconnect(db, settings, store, two, instagram_api=instagram)
    assert instagram.revoked == ["ll-fb-user-1"]


def test_per_account_tokens_are_never_crossed(db, settings, store):
    tiktok = FakeTikTok()
    tiktok.add_user("open-a", "alpha")
    tiktok.add_user("open-b", "beta")
    a = connect_tiktok(db, settings, store, tiktok, "open-a")
    b = connect_tiktok(db, settings, store, tiktok, "open-b")
    connections.reset_cache()
    assert connections.access_token(db, settings, store, account=b, tiktok_api=tiktok) == "at-open-b"
    assert connections.access_token(db, settings, store, account=a, tiktok_api=tiktok) == "at-open-a"
    assert [call[1] for call in tiktok.calls if call[0] == "refresh"] == ["rt-open-b", "rt-open-a"]


def test_secret_names_are_namespaced_and_validated(store):
    assert account_secret_name("TIKTOK_REFRESH_TOKEN", "3f0b6a1e-0000-4000-8000-000000000001") == "TIKTOK_REFRESH_TOKEN:3f0b6a1e-0000-4000-8000-000000000001"
    with pytest.raises(ValueError):
        account_secret_name("TIKTOK_REFRESH_TOKEN", "../other")
    with pytest.raises(ValueError):
        store.get_secret("OPENAI_API_KEY:3f0b6a1e-0000-4000-8000-000000000001")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.get_secret("SOMETHING_ELSE")  # type: ignore[arg-type]


def test_default_account_is_only_an_initial_selection(db, settings, store):
    tiktok = FakeTikTok()
    tiktok.add_user("open-a", "alpha")
    tiktok.add_user("open-b", "beta")
    a = connect_tiktok(db, settings, store, tiktok, "open-a")
    b = connect_tiktok(db, settings, store, tiktok, "open-b")
    assert accounts.default_account(db, "tiktok").id == a.id
    accounts.set_default(db, b)
    db.refresh(a)
    assert accounts.default_account(db, "tiktok").id == b.id and not a.is_default
    connections.disconnect(db, settings, store, b, tiktok_api=tiktok)
    assert accounts.default_account(db, "tiktok").id == a.id  # falls back, never a dangling default


def test_accounts_api_never_exposes_credentials(db, settings, store, youtube, test_keyring):
    tiktok, instagram = FakeTikTok(), FakeInstagram()
    tiktok.add_user("open-a", "alpha")
    instagram.add_account("ig-1", "brand_one")
    connect_youtube(db, settings, store, youtube, "UC_channel_A", "Rank Frame Shorts")
    connect_tiktok(db, settings, store, tiktok, "open-a")
    connect_instagram(db, settings, store, instagram)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_publishing_apis] = lambda: apis(tiktok, instagram)
    app.dependency_overrides[get_youtube_provider] = lambda: youtube
    client = TestClient(app)
    body = client.get("/api/publishing/accounts").json()
    dump = json.dumps(body)
    for secret in [*test_keyring.secrets.values(), "tiktok-client-secret-never-logged", "meta-app-secret-never-logged", "fake-client-secret"]:
        assert secret not in dump
    assert dump.count('"configured": true') >= 3
    platforms = body["platforms"]
    assert [item["display_name"] for item in platforms["youtube"]["accounts"]] == ["Rank Frame Shorts"]
    assert platforms["tiktok"]["accounts"][0]["handle"] == "alpha"
    assert platforms["tiktok"]["accounts"][0]["restrictions"][0]["code"] == "unaudited_client"
    assert platforms["instagram"]["accounts"][0]["capabilities"]["cover_mode"] == "frame"
    assert platforms["instagram"]["capabilities"]["title"] is False
    dump_db = all_database_text(db)
    for secret in test_keyring.secrets.values():
        assert secret not in dump_db


def test_add_account_and_reconnect_urls(db, settings, store):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    tiktok_url = client.post("/api/publishing/tiktok/authorize").json()["authorization_url"]
    query = parse_qs(urlparse(tiktok_url).query)
    assert query["client_key"] == ["tiktok-client-key"] and query["scope"] == ["user.info.basic,video.publish"]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 64  # hex SHA-256
    assert "client_secret" not in tiktok_url
    meta_url = client.post("/api/publishing/instagram/authorize").json()["authorization_url"]
    assert meta_url.startswith("https://www.facebook.com/v25.0/dialog/oauth?")
    assert "instagram_content_publish" in parse_qs(urlparse(meta_url).query)["scope"][0]
    assert "meta-app-secret" not in meta_url


def test_tiktok_callback_connects_and_redirects_without_secrets(db, settings, store):
    tiktok = FakeTikTok()
    tiktok.add_user("open-a", "alpha")
    tiktok.signing_in = "open-a"
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_publishing_apis] = lambda: apis(tiktok)
    client = TestClient(app)
    url = client.post("/api/publishing/tiktok/authorize").json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    response = client.get("/api/publishing/tiktok/oauth/callback", params={"code": "one-time", "state": state}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert "platform=tiktok" in location and "result=connected" in location and "one-time" not in location
    replay = client.get("/api/publishing/tiktok/oauth/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert "reason=invalid_state" in replay.headers["location"]


def test_instagram_without_a_professional_account_is_refused(db, settings, store):
    instagram = FakeInstagram()  # the Facebook user manages no IG professional account
    with pytest.raises(Exception) as error:
        connect_instagram(db, settings, store, instagram)
    assert error.value.code == "no_professional_account"
    assert accounts.list_accounts(db, "instagram") == []


def test_instagram_without_publish_permission_is_restricted(db, settings, store):
    instagram = FakeInstagram(permissions_granted=("instagram_basic", "pages_show_list"))
    instagram.add_account("ig-1", "brand_one")
    (account,) = connect_instagram(db, settings, store, instagram)
    assert account.restrictions[0]["code"] == "insufficient_scope" and account.restrictions[0]["blocks_publishing"]
    from clipforge.publishing.capabilities import account_capabilities

    assert account_capabilities(account)["upload"] is False


# ---------------------------------------------------------------------------
# YouTube regression + migration
# ---------------------------------------------------------------------------


def seed_legacy_installation(db, settings, test_keyring) -> YouTubeUpload:
    """What a pre-multi-account ClipForge stored: one row, one global token, uploads."""
    db.add(YouTubeConnection(slot="primary", channel_id="UC_fake_channel_01", channel_title="Knowledge Lab", status="connected", granted_scopes=list(REQUESTED_SCOPES)))
    db.commit()
    test_keyring.secrets[("ClipForge", "YOUTUBE_REFRESH_TOKEN")] = "1//rt-UC_fake_channel_01"
    project = exported_project(db, settings)
    upload, _target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id="UC_fake_channel_01")
    return upload


def test_existing_single_youtube_connection_migrates_without_losing_anything(db, settings, store, youtube, test_keyring):
    upload = seed_legacy_installation(db, settings, test_keyring)
    record = connection.active_connection(db)
    assert record is not None and record.platform == "youtube" and record.channel_id == "UC_fake_channel_01"
    assert record.is_default and record.details["legacy_secret"] is True
    assert db.get(YouTubeConnection, "primary") is not None  # the legacy row is never deleted
    # The first token use adopts the old global token into the account's own entry.
    account, token = connection.access_token(db, settings, store, youtube, capability="upload", channel_id=upload.channel_id)
    assert account.id == record.id and token == "ya29.UC_fake_channel_01"
    assert test_keyring.secrets[("ClipForge", f"YOUTUBE_REFRESH_TOKEN:{record.id}")] == "1//rt-UC_fake_channel_01"
    assert test_keyring.secrets[("ClipForge", "YOUTUBE_REFRESH_TOKEN")] == "1//rt-UC_fake_channel_01"  # kept (downgrade-safe)
    # Old uploads still resolve to their channel's account and upload with its token.
    target = uploads.resolve_upload_source(db.get(__import__("clipforge.models", fromlist=["Project"]).Project, upload.project_id), settings)
    uploads.run_upload(db, upload.id, target.path, settings, store, youtube)
    db.refresh(upload)
    assert upload.youtube_video_id and ("upload", "ya29.UC_fake_channel_01") in youtube.used_tokens
    # Migration is idempotent and never resurrects a disconnected account.
    accounts.migrate_legacy_youtube(db)
    assert len(accounts.list_accounts(db, "youtube", include_disconnected=True)) == 1
    connection.disconnect(db, store, youtube, record.id)
    assert ("ClipForge", "YOUTUBE_REFRESH_TOKEN") not in test_keyring.secrets
    assert ("ClipForge", f"YOUTUBE_REFRESH_TOKEN:{record.id}") not in test_keyring.secrets
    assert db.get(YouTubeConnection, "primary").status == "disconnected"
    accounts.migrate_legacy_youtube(db)
    assert connection.active_connection(db) is None


def test_a_second_channel_never_reads_the_legacy_token(db, settings, store, youtube, test_keyring):
    seed_legacy_installation(db, settings, test_keyring)
    second = connect_youtube(db, settings, store, youtube, "UC_channel_B", "Second")
    test_keyring.secrets.pop(("ClipForge", f"YOUTUBE_REFRESH_TOKEN:{second.id}"))
    connection.reset_youtube_auth_cache()
    with pytest.raises(YouTubeApiError) as error:
        connection.access_token(db, settings, store, youtube, capability="upload", account_id=second.id)
    assert error.value.code == "auth_expired"  # no fallback to another account's credential


def test_new_upload_is_bound_to_the_selected_channel_and_analytics_use_its_token(db, settings, store, youtube):
    connect_youtube(db, settings, store, youtube, "UC_channel_A", "Channel A")
    b = connect_youtube(db, settings, store, youtube, "UC_channel_B", "Channel B")
    connection.reset_youtube_auth_cache()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_secret_store] = lambda: store
    app.dependency_overrides[get_youtube_provider] = lambda: youtube
    from clipforge.youtube.routes import get_upload_dispatcher

    dispatched: list[str] = []
    app.dependency_overrides[get_upload_dispatcher] = lambda: (lambda upload_id, path: dispatched.append(upload_id))
    project = exported_project(db, settings)
    client = TestClient(app)
    response = client.post(f"/api/youtube/projects/{project.id}/uploads", json={
        "base_revision": project.current_revision, "account_id": b.id, "options": publish_options().model_dump(),
    })
    assert response.status_code == 202, response.text
    upload = db.get(YouTubeUpload, response.json()["upload"]["id"])
    assert upload.channel_id == "UC_channel_B"
    target = uploads.resolve_upload_source(project, settings)
    uploads.run_upload(db, upload.id, target.path, settings, store, youtube)
    db.refresh(upload)
    assert ("upload", "ya29.UC_channel_B") in youtube.used_tokens and ("upload", "ya29.UC_channel_A") not in youtube.used_tokens
    youtube.videos[upload.youtube_video_id]["status"]["privacyStatus"] = "public"
    youtube.videos[upload.youtube_video_id]["snippet"]["publishedAt"] = "2026-09-01T10:00:00Z"
    youtube.used_tokens.clear()
    analytics.refresh_analytics(db, upload, settings, store, youtube)
    assert youtube.used_tokens and {token for _kind, token in youtube.used_tokens} == {"ya29.UC_channel_B"}
    # the project's YouTube state for channel A says "not uploaded yet" for A
    a_state = client.get(f"/api/youtube/projects/{project.id}").json()
    assert a_state["current_render"]["uploadable"] is True and a_state["connection"]["channel_id"] == "UC_channel_A"
    b_state = client.get(f"/api/youtube/projects/{project.id}", params={"account_id": b.id}).json()
    assert b_state["current_render"]["code"] == "already_uploaded"


def test_youtube_scheduling_still_uses_the_uploads_own_channel(db, settings, store, youtube):
    from clipforge.youtube.publishing import ScheduleChoice

    connect_youtube(db, settings, store, youtube, "UC_channel_A", "Channel A")
    b = connect_youtube(db, settings, store, youtube, "UC_channel_B", "Channel B")
    project = exported_project(db, settings)
    upload, target, _run = uploads.request_upload(db, project, settings, options=publish_options(), channel_id=b.channel_id)
    uploads.run_upload(db, upload.id, target.path, settings, store, youtube)
    db.refresh(upload)
    youtube.used_tokens.clear()
    from datetime import UTC, datetime, timedelta

    day = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    uploads.schedule_publication(db, upload, ScheduleChoice(date=day, time="18:00", timezone="Europe/Berlin"), settings, store, youtube)
    db.refresh(upload)
    assert upload.schedule_status == "scheduled"
    assert {token for _kind, token in youtube.used_tokens} == {"ya29.UC_channel_B"}


def test_alembic_upgrade_adopts_the_legacy_connection(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'clipforge.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(config, "0012_job_runtime_identity")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO youtube_connections (slot, channel_id, channel_title, status, granted_scopes, connected_at, updated_at) "
            "VALUES ('primary', 'UC_legacy', 'Legacy Channel', 'connected', '[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
    command.upgrade(config, "head")
    get_settings.cache_clear()
    inspector = inspect(engine)
    assert {"publishing_accounts", "social_publications", "publishing_platform_configs"} <= set(inspector.get_table_names())
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT platform, external_account_id, display_name, is_default, details FROM publishing_accounts")).all()
        legacy = conn.execute(text("SELECT channel_id FROM youtube_connections")).all()
    assert len(rows) == 1 and rows[0][:4] == ("youtube", "UC_legacy", "Legacy Channel", 1)
    assert json.loads(rows[0][4])["legacy_secret"] is True
    assert legacy == [("UC_legacy",)]  # untouched
    command.downgrade(config, "0012_job_runtime_identity")
    assert "publishing_accounts" not in inspect(create_engine(url)).get_table_names()
