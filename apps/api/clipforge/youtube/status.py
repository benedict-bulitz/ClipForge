"""The one current-status authority for uploaded videos.

Two kinds of facts are kept strictly apart:

* **Requested / historical** - what ClipForge asked for: ``requested_visibility``,
  ``publish_at`` (the requested publication time), ``schedule_local_time`` and
  ``schedule_timezone``.  Immutable provenance; never drives the current UI
  once YouTube has answered.
* **Remote** - what YouTube last reported via ``videos.list``: ``remote_*``
  fields, ``upload_status``, ``processing_status``, rejection/failure reasons
  and live statistics, stamped with ``remote_status_checked_at``.

``current_status`` derives the state users see from the remote facts; a
failed refresh never changes them (a confirmed Published video stays
Published, marked stale).  ``needs_reconcile`` / ``poll_interval`` bound how
often YouTube is asked, so page opens and the Results page's existing polling
cannot hammer the API.

Publication time semantics: ``remote_published_at`` is YouTube's
``snippet.publishedAt`` (per the videos reference, the time a private video was
made public, but the upload time while it is private/unlisted), so it is only
treated as the publication time while YouTube reports the video as public.
``first_observed_public_at`` is when ClipForge first saw it public/unlisted.
``published_at`` is the effective time analytics use: the snippet time while
public, otherwise the first observation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import YouTubeUpload

ACTIVE_STATES = ("pending", "uploading")
LIVE_STATES = {"published", "unlisted"}
AUTH_ERRORS = {"auth_expired", "insufficient_scope", "not_connected", "client_not_configured", "wrong_channel"}
MIN_ATTEMPT_GAP = timedelta(seconds=20)
STALE_AFTER = timedelta(hours=6)
POLL_WINDOW_AFTER_PUBLISH = timedelta(minutes=60)
POLL_WINDOW_BEFORE_PUBLISH = timedelta(minutes=15)
PROCESSING_POLL_WINDOW = timedelta(minutes=60)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Applying YouTube's answer (the only writer of remote facts)
# ---------------------------------------------------------------------------


def apply_remote_video(upload: YouTubeUpload, item: dict[str, Any], *, now: datetime) -> None:
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    processing = item.get("processingDetails") if isinstance(item.get("processingDetails"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else None
    upload.deleted_on_youtube = False
    upload.remote_privacy_status = status.get("privacyStatus") or upload.remote_privacy_status
    upload.upload_status = status.get("uploadStatus") or upload.upload_status  # remote uploadStatus
    upload.processing_status = processing.get("processingStatus") or upload.processing_status
    # Present only while a private video is scheduled; absent after publication.
    upload.remote_publish_at = _parse(status.get("publishAt"))
    upload.remote_published_at = _parse(snippet.get("publishedAt")) or upload.remote_published_at
    if statistics is not None:
        upload.remote_view_count = _count(statistics.get("viewCount"))
        upload.remote_like_count = _count(statistics.get("likeCount"))
        upload.remote_comment_count = _count(statistics.get("commentCount"))
    upload.remote_status_checked_at = now
    upload.remote_status_attempted_at = now
    upload.remote_status_error_code = None
    upload.remote_status_error = None
    upload_status = upload.upload_status
    if upload_status == "processed":
        upload.state = "ready"
        upload.failure_reason = None
    elif upload_status == "uploaded":
        upload.state = "processing"
    elif upload_status == "failed":
        upload.state = "failed"
        upload.failure_reason = status.get("failureReason")
        upload.last_error_code = "processing_failed"
        upload.last_error_message = f"YouTube could not process the video ({upload.failure_reason or 'unknown reason'})."
    elif upload_status == "rejected":
        upload.state = "failed"
        upload.rejection_reason = status.get("rejectionReason")
        upload.last_error_code = "rejected"
        upload.last_error_message = f"YouTube rejected the video ({upload.rejection_reason or 'no reason given'})."
    elif upload_status == "deleted":
        upload.deleted_on_youtube = True
    declared = status.get("selfDeclaredMadeForKids", status.get("madeForKids"))
    if isinstance(declared, bool):
        upload.made_for_kids_confirmed = declared
    placement = item.get("paidProductPlacementDetails") if isinstance(item.get("paidProductPlacementDetails"), dict) else None
    if placement is not None:
        record = dict(upload.upload_settings or {})
        record["paid_product_placement_confirmed"] = placement.get("hasPaidProductPlacement")
        upload.upload_settings = record
    privacy = upload.remote_privacy_status
    upload.visibility_restricted = bool(
        upload.requested_visibility in {"public", "unlisted"} and privacy == "private" and upload_status == "processed"
    )
    if privacy in {"public", "unlisted"}:
        if upload.first_observed_public_at is None:
            upload.first_observed_public_at = now
        if privacy == "public" and upload.remote_published_at is not None:
            upload.published_at, upload.published_source = upload.remote_published_at, "youtube_snippet_published_at"
        elif upload.published_at is None:
            upload.published_at, upload.published_source = upload.first_observed_public_at, "first_observed_public"
        if upload.schedule_status == "scheduled":
            upload.schedule_status = "published"
    # A video that went back to private keeps its (historical) publication time.


def apply_remote_missing(upload: YouTubeUpload, *, now: datetime) -> None:
    """videos.list answered without the video: deleted or removed on YouTube."""
    upload.deleted_on_youtube = True
    upload.remote_status_checked_at = now
    upload.remote_status_attempted_at = now
    upload.remote_status_error_code = None
    upload.remote_status_error = None
    upload.last_error_code = "deleted_on_youtube"
    upload.last_error_message = "This video no longer exists on YouTube (deleted or removed)."


def record_refresh_failure(upload: YouTubeUpload, code: str, message: str, *, now: datetime) -> None:
    """Remember the failure; never touch the last confirmed remote facts."""
    upload.remote_status_error_code = code
    upload.remote_status_error = message[:500]
    upload.remote_status_attempted_at = now


# ---------------------------------------------------------------------------
# Current state (what the UI shows) and freshness
# ---------------------------------------------------------------------------

LABELS = {
    "uploading": "Uploading",
    "upload_failed": "Upload failed",
    "deleted": "Deleted on YouTube",
    "rejected": "Rejected by YouTube",
    "processing_failed": "Processing failed on YouTube",
    "published": "Published",
    "unlisted": "Unlisted",
    "scheduled": "Scheduled",
    "publish_pending": "Still private after the scheduled time",
    "private": "Private",
}


def scheduled_time(upload: YouTubeUpload) -> datetime | None:
    """YouTube's scheduled time once checked; the request only before any check."""
    if upload.remote_status_checked_at is not None:
        return _utc(upload.remote_publish_at)
    return _utc(upload.publish_at) if upload.schedule_status == "scheduled" else None


def current_state(upload: YouTubeUpload, now: datetime) -> str:
    if upload.state in ACTIVE_STATES:
        return "uploading"
    if not upload.youtube_video_id:
        return "upload_failed"
    if upload.deleted_on_youtube:
        return "deleted"
    if upload.upload_status == "rejected":
        return "rejected"
    if upload.upload_status == "failed":
        return "processing_failed"
    privacy = upload.remote_privacy_status or upload.privacy_status
    if privacy == "public":
        return "published"
    if privacy == "unlisted":
        return "unlisted"
    scheduled = scheduled_time(upload)
    if scheduled is not None:
        return "scheduled" if now < scheduled else "publish_pending"
    requested = _utc(upload.publish_at)
    if upload.remote_status_checked_at is not None and requested is not None and now >= requested and upload.first_observed_public_at is None:
        # YouTube no longer reports a schedule and the video is still private.
        return "publish_pending"
    return "private"


def current_status(upload: YouTubeUpload, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state = current_state(upload, now)
    checked = _utc(upload.remote_status_checked_at)
    attempted = _utc(upload.remote_status_attempted_at)
    if not upload.youtube_video_id:
        stale, reason = False, None
    elif checked is None:
        stale, reason = True, "never_checked"
    elif upload.remote_status_error_code and attempted is not None and attempted >= checked:
        stale, reason = True, "refresh_failed"
    elif now - checked > STALE_AFTER:
        stale, reason = True, "old"
    else:
        stale, reason = False, None
    history = list((upload.upload_settings or {}).get("schedule_history") or [])
    return {
        "state": state,
        "label": LABELS[state],
        "processing": upload.upload_status == "uploaded" and state not in {"deleted", "rejected", "processing_failed"},
        "stale": stale,
        "stale_reason": reason,
        "last_checked_at": checked,
        "last_attempt_at": attempted,
        "refresh_error": {"code": upload.remote_status_error_code, "message": upload.remote_status_error} if upload.remote_status_error_code else None,
        "scheduled_for": scheduled_time(upload) if state in {"scheduled", "publish_pending"} else None,
        "published_at": _utc(upload.published_at) if state in LIVE_STATES or upload.published_at else None,
        "published_time_source": upload.published_source,
        "first_observed_public_at": _utc(upload.first_observed_public_at),
        "remote": {
            "privacy_status": upload.remote_privacy_status,
            "upload_status": upload.upload_status,
            "processing_status": upload.processing_status,
            "publish_at": _utc(upload.remote_publish_at),
            "published_at": _utc(upload.remote_published_at),
            "rejection_reason": upload.rejection_reason,
            "failure_reason": upload.failure_reason,
        },
        "live_stats": {
            "views": upload.remote_view_count,
            "likes": upload.remote_like_count,
            "comments": upload.remote_comment_count,
            "checked_at": checked,
            "source": "youtube_data_api_videos_list",
        } if checked is not None and upload.remote_view_count is not None else None,
        "requested": {
            "visibility": upload.requested_visibility,
            "publish_at": _utc(upload.publish_at),
            "local_time": upload.schedule_local_time,
            "timezone": upload.schedule_timezone,
            "history": history,
        },
    }


def _publish_window_interval(upload: YouTubeUpload, now: datetime) -> timedelta | None:
    """Cadence near the publish time; None once the window is over."""
    state = current_state(upload, now)
    target = scheduled_time(upload) or (_utc(upload.publish_at) if state == "publish_pending" else None)
    if target is None:
        return None
    if state == "scheduled":
        return timedelta(seconds=60) if target - now <= POLL_WINDOW_BEFORE_PUBLISH else None
    if state == "publish_pending":
        elapsed = now - target
        if elapsed < timedelta(minutes=2):
            return timedelta(seconds=30)
        if elapsed < timedelta(minutes=10):
            return timedelta(seconds=60)
        if elapsed < POLL_WINDOW_AFTER_PUBLISH:
            return timedelta(seconds=120)
    return None


def _processing_interval(upload: YouTubeUpload, now: datetime) -> timedelta | None:
    uploaded = _utc(upload.uploaded_at)
    if upload.upload_status == "uploaded" and uploaded is not None and timedelta(0) <= now - uploaded < PROCESSING_POLL_WINDOW:
        return timedelta(seconds=60)
    return None


def needs_reconcile(upload: YouTubeUpload, *, now: datetime | None = None) -> bool:
    """Whether a page open / poll should ask YouTube now (freshness-gated)."""
    now = now or datetime.now(UTC)
    if not upload.youtube_video_id or upload.state in ACTIVE_STATES:
        return False
    attempted = _utc(upload.remote_status_attempted_at)
    if attempted is not None and now - attempted < MIN_ATTEMPT_GAP:
        return False
    checked = _utc(upload.remote_status_checked_at)
    if checked is None:
        return True
    if upload.deleted_on_youtube:
        return now - checked > timedelta(hours=24)
    interval = min(
        (item for item in (_publish_window_interval(upload, now), _processing_interval(upload, now)) if item is not None),
        default=timedelta(minutes=10) if current_state(upload, now) in LIVE_STATES else timedelta(minutes=30),
    )
    if upload.remote_status_error_code and attempted is not None and attempted >= checked:
        interval = max(interval, timedelta(seconds=60))
        return now - attempted >= interval
    return now - checked >= interval


def poll_interval(upload: YouTubeUpload, *, now: datetime | None = None) -> int | None:
    """Seconds until the Results page should ask again; None = stop polling."""
    now = now or datetime.now(UTC)
    if upload.state in ACTIVE_STATES or upload.thumbnail_upload_status == "pending":
        return 3  # local progress only; no YouTube call
    interval = min(
        (item for item in (_publish_window_interval(upload, now), _processing_interval(upload, now)) if item is not None),
        default=None,
    )
    return int(interval.total_seconds()) if interval is not None else None


def analytics_state(upload: YouTubeUpload, *, latest_status: str | None, retention_ok: bool, now: datetime | None = None) -> str:
    """not_published | processing | partial | available | failed | auth_error."""
    now = now or datetime.now(UTC)
    if upload.published_at is None and current_state(upload, now) not in LIVE_STATES:
        return "not_published"
    if upload.analytics_error_code:
        if upload.analytics_error_code in AUTH_ERRORS:
            return "auth_error"
        if latest_status is None:
            return "failed"
    if latest_status is None or latest_status == "no_data_yet":
        return "processing"
    if latest_status == "partial" or not retention_ok:
        return "partial"
    return "available"
