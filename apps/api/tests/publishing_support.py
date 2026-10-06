"""Fakes for multi-platform publishing tests: nothing here reaches TikTok or Meta.

Every fake issues *account-specific* tokens (``at-<account>``) and records the
token each call used, so tests can prove that account A's token is never used
for account B.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from youtube_support import FakeYouTube, exported_project, rendered_state, youtube_settings

from clipforge.config import Settings
from clipforge.publishing import connections
from clipforge.publishing.errors import PublishingApiError
from clipforge.publishing.instagram import MetaApp, MetaToken
from clipforge.publishing.publications import Apis
from clipforge.publishing.tiktok import CreatorInfo, TikTokClient, TikTokGrant
from clipforge.youtube.provider import ChannelIdentity, OAuthClient, TokenGrant

TIKTOK_KEY = "tiktok-client-key"
TIKTOK_SECRET = "tiktok-client-secret-never-logged"
META_APP_ID = "1234567890"
META_SECRET = "meta-app-secret-never-logged"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def publishing_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = youtube_settings(tmp_path)
    return base.model_copy(update={
        "tiktok_client_key": TIKTOK_KEY,
        "tiktok_client_secret": TIKTOK_SECRET,
        "meta_app_id": META_APP_ID,
        "meta_app_secret": META_SECRET,
        **overrides,
    })


def social_state(duration: float = 10.0) -> dict[str, Any]:
    state = rendered_state(duration)
    state["social_metadata"] = {
        "status": "available",
        "platforms": {
            **state["social_metadata"]["platforms"],
            "instagram": {"title": "Round windows – explained", "description": "Why airplane windows are round. Save it for later.", "hashtags": ["#aviation", "#LearnSomething"]},
            "tiktok": {"title": "Why are airplane windows round?", "description": "The comet disaster explained in seconds.", "hashtags": ["#aviation", "#QuickLearn", "#Learn"]},
        },
    }
    return state


def social_project(db, settings: Settings, content: bytes = b"\x00mp4" * 6000, *, project_id: str = PROJECT_ID, duration: float = 10.0):
    from youtube_support import add_export_revision

    from clipforge.models import Project

    project = Project(id=project_id, original_prompt="Why are airplane windows round?", title="Why are airplane windows round?", status="exported", current_revision=1)
    db.add(project)
    db.flush()
    add_export_revision(db, project, settings, content, social_state(duration))
    return project


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


@dataclass
class TikTokUser:
    open_id: str
    username: str
    display_name: str
    privacy_options: tuple[str, ...] = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
    comment_disabled: bool = False
    duet_disabled: bool = False
    stitch_disabled: bool = False
    max_duration: int = 600


@dataclass
class FakeTikTok:
    users: dict[str, TikTokUser] = field(default_factory=dict)
    signing_in: str | None = None  # open_id the next OAuth code belongs to
    scopes: tuple[str, ...] = ("user.info.basic", "video.publish")
    init_errors: list[PublishingApiError] = field(default_factory=list)
    chunk_errors: list[PublishingApiError] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)  # status responses, in order
    refresh_error: PublishingApiError | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def add_user(self, open_id: str, username: str, **kwargs: Any) -> TikTokUser:
        self.users[open_id] = TikTokUser(open_id=open_id, username=username, display_name=username.title(), **kwargs)
        return self.users[open_id]

    def _user_for(self, access_token: str) -> TikTokUser:
        assert access_token.startswith("at-"), "unexpected token"
        return self.users[access_token[3:]]

    def exchange_code(self, client: TikTokClient, code: str, code_verifier: str) -> TikTokGrant:
        self.calls.append(("exchange_code", {"code": code, "verifier_len": len(code_verifier), "redirect_uri": client.redirect_uri}))
        open_id = self.signing_in or next(iter(self.users))
        return TikTokGrant(f"at-{open_id}", 86400, open_id, f"rt-{open_id}", 31536000, self.scopes)

    def refresh(self, client: TikTokClient, refresh_token: str) -> TikTokGrant:
        self.calls.append(("refresh", refresh_token))
        if self.refresh_error:
            raise self.refresh_error
        open_id = refresh_token[3:]
        return TikTokGrant(f"at-{open_id}", 86400, open_id, refresh_token, 31536000, self.scopes)

    def revoke(self, client: TikTokClient, access_token: str) -> None:
        self.calls.append(("revoke", access_token))

    def user_info(self, access_token: str) -> dict[str, Any]:
        user = self._user_for(access_token)
        return {"open_id": user.open_id, "display_name": user.display_name, "avatar_url": f"https://p16.tiktokcdn.com/{user.open_id}.jpg"}

    def creator_info(self, access_token: str) -> CreatorInfo:
        self.calls.append(("creator_info", access_token))
        user = self._user_for(access_token)
        return CreatorInfo(
            username=user.username, nickname=user.display_name, avatar_url=None,
            privacy_level_options=user.privacy_options, comment_disabled=user.comment_disabled,
            duet_disabled=user.duet_disabled, stitch_disabled=user.stitch_disabled,
            max_video_post_duration_sec=user.max_duration,
        )

    def init_direct_post(self, access_token: str, post_info: dict[str, Any], source_info: dict[str, Any]) -> tuple[str, str]:
        self.calls.append(("init", {"token": access_token, "post_info": post_info, "source_info": source_info}))
        if self.init_errors:
            raise self.init_errors.pop(0)
        publish_id = f"v_pub_file~v2-{next(self._ids)}"
        self.posts[publish_id] = {"token": access_token, "post_info": post_info, "source_info": source_info, "data": bytearray()}
        return publish_id, f"https://open-upload.tiktokapis.com/video/?upload_id={publish_id}&upload_token=secret"

    def upload_chunk(self, upload_url: str, data: bytes, first: int, last: int, total: int) -> int:
        if self.chunk_errors:
            raise self.chunk_errors.pop(0)
        publish_id = parse_qs(urlparse(upload_url).query)["upload_id"][0]
        post = self.posts[publish_id]
        assert first == len(post["data"]), "chunks must be sent in order"
        assert last - first + 1 == len(data)
        post["data"].extend(data)
        self.chunks.append({"publish_id": publish_id, "first": first, "last": last, "total": total})
        return 201 if last + 1 == total else 206

    def fetch_status(self, access_token: str, publish_id: str) -> dict[str, Any]:
        self.calls.append(("status", {"token": access_token, "publish_id": publish_id}))
        assert self.posts[publish_id]["token"] == access_token, "status must be read with the posting account's token"
        if self.statuses:
            return self.statuses.pop(0)
        return {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001], "uploaded_bytes": len(self.posts[publish_id]["data"])}


# ---------------------------------------------------------------------------
# Instagram (Facebook Login for Business)
# ---------------------------------------------------------------------------


@dataclass
class FakeInstagram:
    """One Facebook user (``fb_user``) can expose several IG professional accounts."""

    fb_user: str = "fb-user-1"
    accounts_by_user: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    permissions_granted: tuple[str, ...] = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")
    status_sequence: list[str] = field(default_factory=list)
    container_errors: list[PublishingApiError] = field(default_factory=list)
    upload_errors: list[PublishingApiError] = field(default_factory=list)
    publish_errors: list[PublishingApiError] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    containers: dict[str, dict[str, Any]] = field(default_factory=dict)
    published: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def add_account(self, ig_id: str, username: str, *, fb_user: str | None = None) -> None:
        self.accounts_by_user.setdefault(fb_user or self.fb_user, []).append(
            {"id": ig_id, "username": username, "name": username.title(), "profile_picture_url": f"https://scontent.cdninstagram.com/{ig_id}.jpg", "page_id": f"page-{ig_id}", "page_name": f"{username} page"}
        )

    def exchange_code(self, app: MetaApp, code: str) -> MetaToken:
        self.calls.append(("exchange_code", code))
        return MetaToken(f"short-{self.fb_user}", 3600)

    def long_lived_token(self, app: MetaApp, short_lived: str) -> MetaToken:
        return MetaToken(f"ll-{short_lived[6:]}", 60 * 24 * 3600)

    def facebook_user(self, app: MetaApp, token: str) -> dict[str, Any]:
        return {"id": token[3:], "name": "Test User"}

    def permissions(self, app: MetaApp, token: str) -> list[str]:
        return list(self.permissions_granted)

    def instagram_accounts(self, app: MetaApp, token: str) -> list[dict[str, Any]]:
        return list(self.accounts_by_user.get(token[3:], []))

    def revoke(self, app: MetaApp, token: str) -> None:
        self.revoked.append(token)

    def _owner(self, token: str, ig_user_id: str) -> None:
        owned = {item["id"] for item in self.accounts_by_user.get(token[3:], [])}
        assert ig_user_id in owned, "a token may only act on the Instagram accounts its Facebook user manages"

    def create_reel_container(self, app: MetaApp, token: str, ig_user_id: str, params: dict[str, Any]) -> tuple[str, str]:
        self.calls.append(("container", {"token": token, "ig_user_id": ig_user_id, "params": params}))
        self._owner(token, ig_user_id)
        if self.container_errors:
            raise self.container_errors.pop(0)
        container = f"1790{next(self._ids):010d}"
        self.containers[container] = {"ig_user_id": ig_user_id, "params": params, "bytes": 0, "published": False, "token": token}
        return container, f"https://rupload.facebook.com/ig-api-upload/{app.graph_version}/{container}"

    def upload_video(self, app: MetaApp, token: str, upload_uri: str, path: Path, size: int) -> dict[str, Any]:
        self.calls.append(("upload", {"token": token, "uri": upload_uri, "size": size}))
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        container = upload_uri.rsplit("/", 1)[-1]
        assert self.containers[container]["token"] == token
        data = path.read_bytes()
        assert len(data) == size
        self.containers[container]["bytes"] = size
        return {"success": True}

    def container_status(self, app: MetaApp, token: str, container_id: str) -> tuple[str, str | None]:
        self.calls.append(("status", {"token": token, "container": container_id}))
        if self.containers[container_id]["published"]:
            return "PUBLISHED", None
        if self.status_sequence:
            return self.status_sequence.pop(0), None
        return "FINISHED", None

    def publish_container(self, app: MetaApp, token: str, ig_user_id: str, container_id: str) -> str:
        self.calls.append(("publish", {"token": token, "ig_user_id": ig_user_id, "container": container_id}))
        self._owner(token, ig_user_id)
        if self.publish_errors:
            raise self.publish_errors.pop(0)
        container = self.containers[container_id]
        assert not container["published"], "a container can be published only once"
        assert container["bytes"] > 0
        container["published"] = True
        media = f"1800{next(self._ids):010d}"
        self.published.append({"media_id": media, "container": container_id, "ig_user_id": ig_user_id})
        return media

    def media_permalink(self, app: MetaApp, token: str, media_id: str) -> str | None:
        return f"https://www.instagram.com/reel/{media_id[-6:]}/"

    def publishing_limit(self, app: MetaApp, token: str, ig_user_id: str) -> dict[str, Any]:
        self._owner(token, ig_user_id)
        return {"quota_usage": 1, "config": {"quota_total": 100, "quota_duration": 86400}}


# ---------------------------------------------------------------------------
# YouTube with per-channel tokens
# ---------------------------------------------------------------------------


@dataclass
class MultiChannelYouTube(FakeYouTube):
    """FakeYouTube whose tokens identify the channel they belong to."""

    used_tokens: list[tuple[str, str]] = field(default_factory=list)

    def exchange_code(self, client: OAuthClient, code: str, code_verifier: str) -> TokenGrant:
        super().exchange_code(client, code, code_verifier)
        return TokenGrant(f"ya29.{self.channel.channel_id}", 3600, f"1//rt-{self.channel.channel_id}", self.scopes)

    def refresh_access_token(self, client: OAuthClient, refresh_token: str) -> TokenGrant:
        super().refresh_access_token(client, refresh_token)
        return TokenGrant(f"ya29.{refresh_token.split('rt-', 1)[1]}", 3600, None, ())

    def get_my_channel(self, access_token: str) -> ChannelIdentity:
        if access_token.startswith("ya29.UC"):
            channel_id = access_token[5:]
            return ChannelIdentity(channel_id, self.channel.title if channel_id == self.channel.channel_id else channel_id)
        return self.channel

    def start_resumable_upload(self, access_token, body, size, content_type, *, notify_subscribers=True):
        self.used_tokens.append(("upload", access_token))
        return super().start_resumable_upload(access_token, body, size, content_type, notify_subscribers=notify_subscribers)

    def list_videos(self, access_token, video_ids, parts):
        self.used_tokens.append(("list_videos", access_token))
        return super().list_videos(access_token, video_ids, parts)

    def analytics_report(self, access_token, params):
        self.used_tokens.append(("analytics", access_token))
        return super().analytics_report(access_token, params)


def apis(tiktok: FakeTikTok | None = None, instagram: FakeInstagram | None = None) -> Apis:
    return Apis(tiktok=tiktok or FakeTikTok(), instagram=instagram or FakeInstagram())


def connect_tiktok(db, settings, store, fake: FakeTikTok, open_id: str):
    url = connections.begin_tiktok(settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    fake.signing_in = open_id
    return connections.complete_tiktok(db, settings, store, fake, code=f"code-{open_id}", state=state)


def connect_instagram(db, settings, store, fake: FakeInstagram, fb_user: str | None = None):
    url = connections.begin_instagram(db, settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    if fb_user:
        fake.fb_user = fb_user
    return connections.complete_instagram(db, settings, store, fake, code="meta-code", state=state)


__all__ = [
    "FakeInstagram", "FakeTikTok", "MultiChannelYouTube", "apis", "connect_instagram", "connect_tiktok",
    "exported_project", "publishing_settings", "social_project",
]
