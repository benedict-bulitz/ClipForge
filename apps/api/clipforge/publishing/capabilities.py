"""What each platform (and each connected account) can do.

The UI renders only the controls a provider supports; nothing is forced into
YouTube semantics.  Static platform facts live here; account-specific limits
(TikTok creator settings, an unaudited TikTok app, missing Instagram
permissions) come from the account's ``restrictions`` and, for TikTok, from
``creator_info`` at publish time (the authority for privacy/interaction options).
"""
from __future__ import annotations

from typing import Any

from ..models import PublishingAccount

CAPTION_LIMIT = 2200
INSTAGRAM_HASHTAG_LIMIT = 30


def platform_capabilities(platform: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    if platform == "youtube":
        return {
            "upload": True, "publish_now": True, "schedule": True, "schedule_mode": "native",
            "title": True, "description": True, "caption": False, "hashtags": True, "tags": True,
            "thumbnail": True, "cover": False, "cover_mode": None, "privacy": True,
            "comments": False, "duet": False, "stitch": False, "share_to_feed": False,
            "synthetic_media": True, "ai_disclosure": False, "commercial_disclosure": False,
            "analytics": True, "remote_status": True,
            "notes": [],
        }
    if platform == "instagram":
        return {
            "upload": True, "publish_now": True, "schedule": True, "schedule_mode": "clipforge",
            "title": False, "description": False, "caption": True, "caption_limit": CAPTION_LIMIT,
            "hashtags": True, "hashtag_limit": INSTAGRAM_HASHTAG_LIMIT, "tags": False,
            # A custom Reel cover image must be fetched by Meta from a public URL
            # (``cover_url``); ClipForge does not host files, so it offers a frame
            # of the video (``thumb_offset``) instead.
            "thumbnail": False, "cover": True, "cover_mode": "frame", "privacy": False,
            "comments": False, "duet": False, "stitch": False, "share_to_feed": True,
            "synthetic_media": False, "ai_disclosure": False, "commercial_disclosure": False,
            "analytics": False, "remote_status": True,
            "notes": [
                "Instagram has no separate video title: the caption (with hashtags) is the post text.",
                "A custom cover image needs a publicly hosted file; ClipForge sets the Reel cover from a video frame instead.",
                "Instagram has no future-publish time in the API: ClipForge publishes at the scheduled time while it is running.",
            ],
        }
    if platform == "tiktok":
        audited = bool(config.get("app_audited"))
        notes = [
            "TikTok's \"title\" field is the video caption; hashtags belong in it.",
            "TikTok has no future-publish time in the API: ClipForge publishes at the scheduled time while it is running.",
        ]
        if not audited:
            notes.insert(0, "Public Direct Post requires TikTok app approval. Until the app passes TikTok's audit, posts are private (Only me) and the account must be private.")
        return {
            "upload": True, "publish_now": True, "schedule": True, "schedule_mode": "clipforge",
            "title": False, "description": False, "caption": True, "caption_limit": CAPTION_LIMIT,
            "hashtags": True, "tags": False,
            "thumbnail": False, "cover": True, "cover_mode": "frame",
            # Privacy levels and interaction settings come from creator_info.
            "privacy": True, "comments": True, "duet": True, "stitch": True, "share_to_feed": False,
            "synthetic_media": False, "ai_disclosure": True, "commercial_disclosure": True,
            "analytics": False, "remote_status": True,
            "public_post": audited,
            "notes": notes,
        }
    raise ValueError(f"Unknown platform: {platform}")


def account_capabilities(account: PublishingAccount, *, config: dict[str, Any] | None = None, youtube_scopes: dict[str, bool] | None = None) -> dict[str, Any]:
    """Platform capabilities narrowed by this account's own state."""
    caps = platform_capabilities(account.platform, config=config)
    blocking = [item for item in account.restrictions or [] if item.get("blocks_publishing")]
    if account.platform == "youtube" and youtube_scopes is not None:
        caps["upload"] = caps["publish_now"] = bool(youtube_scopes.get("upload"))
        caps["schedule"] = bool(youtube_scopes.get("schedule"))
        caps["analytics"] = bool(youtube_scopes.get("analytics"))
    if account.status != "connected" or blocking:
        caps["upload"] = caps["publish_now"] = caps["schedule"] = False
    return caps
