"""The one YouTube upload-settings authority.

Every control the publishing UI shows is listed in ``SETTINGS_CATALOG`` with
the exact YouTube Data API v3 property it writes (checked against Google's
discovery document, revision 20260924).  Anything YouTube Studio offers but the
public API cannot write is listed in ``STUDIO_ONLY`` and is never rendered as a
control.  Compliance answers (made for kids, altered/synthetic content) are
never pre-filled: they come from the user, per video or from a default the
user saved themselves.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import YouTubeUploadDefaults
from .provider import YouTubeProvider

TITLE_LIMIT = 100
DESCRIPTION_LIMIT_BYTES = 5000
TAGS_LIMIT_CHARS = 500
MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_SCHEDULE_AHEAD = timedelta(days=365)
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # YouTube's custom-thumbnail limit
THUMBNAIL_MIN_WIDTH = 640
CUSTOM_THUMBNAIL_DIR = "youtube-thumbnails"
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")

# ---------------------------------------------------------------------------
# A / B / C classification
# ---------------------------------------------------------------------------

API_WRITABLE = "api_writable"
API_READ_ONLY = "api_read_only"
STUDIO_ONLY = "studio_only"

SETTINGS_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "title", "label": "Title", "api": "snippet.title", "section": "video", "limit": TITLE_LIMIT},
    {"key": "description", "label": "Description", "api": "snippet.description", "section": "video", "limit_bytes": DESCRIPTION_LIMIT_BYTES},
    {"key": "tags", "label": "Tags", "api": "snippet.tags", "section": "video", "limit": TAGS_LIMIT_CHARS},
    {"key": "thumbnail", "label": "Thumbnail", "api": "thumbnails.set", "section": "video"},
    {"key": "made_for_kids", "label": "Made for kids", "api": "status.selfDeclaredMadeForKids", "section": "audience", "required": True},
    {"key": "contains_synthetic_media", "label": "Altered or synthetic content", "api": "status.containsSyntheticMedia", "section": "audience", "required": True},
    {"key": "visibility", "label": "Visibility", "api": "status.privacyStatus", "section": "visibility"},
    {"key": "schedule", "label": "Scheduled publication", "api": "status.publishAt", "section": "visibility"},
    {"key": "category_id", "label": "Category", "api": "snippet.categoryId", "section": "advanced"},
    {"key": "default_language", "label": "Video language", "api": "snippet.defaultLanguage", "section": "advanced"},
    {"key": "default_audio_language", "label": "Audio language", "api": "snippet.defaultAudioLanguage", "section": "advanced"},
    {"key": "license", "label": "License", "api": "status.license", "section": "advanced", "values": ["youtube", "creativeCommon"]},
    {"key": "embeddable", "label": "Allow embedding", "api": "status.embeddable", "section": "advanced"},
    {"key": "public_stats_viewable", "label": "Show public statistics", "api": "status.publicStatsViewable", "section": "advanced"},
    {"key": "notify_subscribers", "label": "Notify subscribers", "api": "videos.insert:notifySubscribers", "section": "advanced"},
    {"key": "recording_date", "label": "Recording date", "api": "recordingDetails.recordingDate", "section": "advanced"},
    {"key": "paid_product_placement", "label": "Paid promotion", "api": "paidProductPlacementDetails.hasPaidProductPlacement", "section": "advanced"},
)
API_WRITABLE_NOT_EXPOSED = (
    {"key": "localizations", "label": "Translated titles and descriptions", "api": "localizations", "reason": "Supported by the API; not offered in this version."},
    {"key": "recording_location", "label": "Recording location", "api": "recordingDetails.location", "reason": "Supported by the API; not offered in this version."},
)
API_READ_ONLY_FIELDS = (
    {"key": "made_for_kids_effective", "label": "YouTube's audience designation", "api": "status.madeForKids"},
    {"key": "upload_status", "label": "Processing status", "api": "status.uploadStatus"},
    {"key": "failure_reason", "label": "Processing failure", "api": "status.failureReason"},
    {"key": "rejection_reason", "label": "Rejection reason", "api": "status.rejectionReason"},
    {"key": "age_restriction", "label": "Age restriction", "api": "contentDetails.contentRating.ytRating"},
)
STUDIO_ONLY_SETTINGS = (
    {"key": "automatic_chapters", "label": "Automatic chapters"},
    {"key": "featured_places", "label": "Featured places"},
    {"key": "shorts_remixing", "label": "Shorts remixing"},
    {"key": "comments", "label": "Comments and moderation"},
    {"key": "related_video", "label": "Related video (Shorts)"},
    {"key": "age_restriction", "label": "Age restriction"},
    {"key": "caption_certification", "label": "Caption certification"},
    {"key": "monetization", "label": "Monetization and ad suitability"},
    {"key": "education_metadata", "label": "Education details"},
    {"key": "end_screens_cards", "label": "End screens and cards"},
)


def settings_catalog() -> dict[str, Any]:
    return {
        API_WRITABLE: list(SETTINGS_CATALOG),
        "api_writable_not_exposed": list(API_WRITABLE_NOT_EXPOSED),
        API_READ_ONLY: list(API_READ_ONLY_FIELDS),
        STUDIO_ONLY: list(STUDIO_ONLY_SETTINGS),
        "api_revision": "20260924",
    }


# ---------------------------------------------------------------------------
# Options, defaults
# ---------------------------------------------------------------------------

Visibility = Literal["private", "schedule", "unlisted", "public"]


class ScheduleChoice(BaseModel):
    date: str = Field(max_length=10)
    time: str = Field(max_length=5)
    timezone: str = Field(max_length=64)


class ThumbnailChoice(BaseModel):
    source: Literal["generated", "custom", "youtube_auto"]
    asset: str | None = Field(default=None, max_length=300)


class PublishOptions(BaseModel):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=200)
    thumbnail: ThumbnailChoice | None = None
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    visibility: Visibility = "private"
    schedule: ScheduleChoice | None = None
    category_id: str | None = Field(default=None, max_length=8)
    default_language: str | None = Field(default=None, max_length=20)
    default_audio_language: str | None = Field(default=None, max_length=20)
    license: Literal["youtube", "creativeCommon"] = "youtube"
    embeddable: bool = True
    public_stats_viewable: bool = True
    notify_subscribers: bool = True
    recording_date: str | None = Field(default=None, max_length=10)
    paid_product_placement: bool = False


class UploadDefaults(BaseModel):
    """Only what the user saved; ``None`` means "ask me every time"."""

    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    category_id: str | None = Field(default=None, max_length=8)
    default_language: str | None = Field(default=None, max_length=20)
    license: Literal["youtube", "creativeCommon"] | None = None
    embeddable: bool | None = None
    public_stats_viewable: bool | None = None
    notify_subscribers: bool | None = None
    visibility: Literal["private", "schedule", "unlisted", "public"] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    # The user's declaration that their Google API project passed YouTube's
    # audit; until then YouTube keeps API uploads private.
    api_project_audited: bool = False


def load_defaults(db: Session) -> UploadDefaults:
    record = db.get(YouTubeUploadDefaults, "primary")
    return UploadDefaults.model_validate(record.values if record else {})


def save_defaults(db: Session, defaults: UploadDefaults) -> UploadDefaults:
    if defaults.timezone and not valid_timezone(defaults.timezone):
        raise ValueError("Unknown time zone.")
    if defaults.default_language and not _LANGUAGE.match(defaults.default_language):
        raise ValueError("Use a language code such as en or de.")
    record = db.get(YouTubeUploadDefaults, "primary")
    if record is None:
        record = YouTubeUploadDefaults(slot="primary", values={})
        db.add(record)
    record.values = defaults.model_dump()
    record.updated_at = datetime.now(UTC)
    db.commit()
    return defaults


def allowed_visibilities(defaults: UploadDefaults) -> list[str]:
    # Unverified Google API projects: YouTube forces API uploads to private.
    return ["private", "schedule", "unlisted", "public"] if defaults.api_project_audited else ["private", "schedule"]


# ---------------------------------------------------------------------------
# Schedule: local wall time + IANA zone -> exact instant, DST-safe
# ---------------------------------------------------------------------------


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class ScheduleResolution:
    status: str  # ok | invalid | invalid_timezone | nonexistent | ambiguous | past | too_far
    message: str | None = None
    publish_at: datetime | None = None
    local_time: str | None = None
    timezone: str | None = None
    abbreviation: str | None = None
    utc_offset: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "publish_at": self.publish_at.strftime("%Y-%m-%dT%H:%M:%SZ") if self.publish_at else None,
            "local_time": self.local_time,
            "timezone": self.timezone,
            "abbreviation": self.abbreviation,
            "utc_offset": self.utc_offset,
        }


def _offset(value: timedelta | None) -> str:
    minutes = int((value or timedelta()).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def resolve_schedule(choice: ScheduleChoice, *, now: datetime | None = None) -> ScheduleResolution:
    """Resolve with the tz database; never compute offsets by hand.

    A wall time skipped by a DST jump (nonexistent) or occurring twice
    (ambiguous) is rejected instead of guessed.
    """
    now = now or datetime.now(UTC)
    if not valid_timezone(choice.timezone):
        return ScheduleResolution("invalid_timezone", "Unknown time zone.")
    if not _DATE.match(choice.date) or not _TIME.match(choice.time):
        return ScheduleResolution("invalid", "Choose a date and a time.")
    try:
        naive = datetime.combine(date.fromisoformat(choice.date), clock.fromisoformat(choice.time))
    except ValueError:
        return ScheduleResolution("invalid", "Choose a valid date and time.")
    zone = ZoneInfo(choice.timezone)
    first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    base = {"local_time": naive.strftime("%Y-%m-%dT%H:%M"), "timezone": choice.timezone}
    if first.utcoffset() != second.utcoffset():
        # Either the clock jumps over this time or passes it twice.
        roundtrip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if roundtrip != naive:
            return ScheduleResolution("nonexistent", f"{choice.time} does not exist on {choice.date} in {choice.timezone} (the clocks jump forward). Choose another time.", **base)
        return ScheduleResolution("ambiguous", f"{choice.time} happens twice on {choice.date} in {choice.timezone} (the clocks go back). Choose another time.", **base)
    instant = first.astimezone(UTC)
    details = {**base, "abbreviation": first.tzname(), "utc_offset": _offset(first.utcoffset())}
    if instant < now + MIN_SCHEDULE_LEAD:
        return ScheduleResolution("past", "Choose a time at least 15 minutes from now.", instant, **details)
    if instant > now + MAX_SCHEDULE_AHEAD:
        return ScheduleResolution("too_far", "Choose a time within the next year.", instant, **details)
    return ScheduleResolution("ok", None, instant, **details)


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedThumbnail:
    data: bytes
    content_type: str
    sha256: str
    source: str
    asset: str


def _inspect_image(path: Path) -> tuple[str | None, int, int, str | None]:
    """(format, width, height, problem) for a candidate thumbnail file."""
    try:
        with Image.open(path) as image:
            fmt, (width, height) = image.format, image.size
    except (OSError, UnidentifiedImageError):
        return None, 0, 0, "The file is not a readable image."
    if fmt not in {"JPEG", "PNG"}:
        return fmt, width, height, "Use a JPEG or PNG image."
    if width < THUMBNAIL_MIN_WIDTH:
        return fmt, width, height, f"The image must be at least {THUMBNAIL_MIN_WIDTH} px wide."
    return fmt, width, height, None


def _safe_child(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(root.resolve()) and candidate.is_file() else None


def thumbnail_choices(state: dict[str, Any], project_id: str, settings: Settings) -> list[dict[str, Any]]:
    """Generated covers (YouTube first) and custom images, each validated."""
    root = settings.render_root.resolve()
    thumbnails = state.get("thumbnails") if isinstance(state.get("thumbnails"), dict) else {}
    choices = []
    variants = [item for item in thumbnails.get("variants") or [] if isinstance(item, dict)]
    for variant in sorted(variants, key=lambda item: item.get("platform") != "youtube"):
        url = str(variant.get("url") or "")
        path = _safe_child(root, url.removeprefix("/media/")) if url.startswith(f"/media/{project_id}/") else None
        _fmt, width, height, problem = _inspect_image(path) if path else (None, 0, 0, "The cover file is missing.")
        choices.append({
            "source": "generated", "asset": str(variant.get("id")), "platform": variant.get("platform"),
            "label": "YouTube cover" if variant.get("platform") == "youtube" else f"{str(variant.get('platform')).capitalize()} cover",
            "url": url, "width": width, "height": height, "valid": problem is None, "problem": problem,
        })
    custom_dir = root / project_id / CUSTOM_THUMBNAIL_DIR
    if custom_dir.is_dir():
        for path in sorted(custom_dir.glob("custom-*")):
            _fmt, width, height, problem = _inspect_image(path)
            choices.append({
                "source": "custom", "asset": path.name, "platform": None, "label": "Your image",
                "url": f"/media/{project_id}/{CUSTOM_THUMBNAIL_DIR}/{path.name}",
                "width": width, "height": height, "valid": problem is None, "problem": problem,
            })
    return choices


def default_thumbnail(choices: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in choices if item["valid"] and item["platform"] == "youtube"), None) or next(
        (item for item in choices if item["valid"]), None
    )


def save_custom_thumbnail(project_id: str, data: bytes, settings: Settings) -> str:
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("The image is too large.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = image.format
            width = image.size[0]
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("The file is not a readable image.") from exc
    if fmt not in {"JPEG", "PNG"}:
        raise ValueError("Use a JPEG or PNG image.")
    if width < THUMBNAIL_MIN_WIDTH:
        raise ValueError(f"The image must be at least {THUMBNAIL_MIN_WIDTH} px wide.")
    directory = settings.render_root.resolve() / project_id / CUSTOM_THUMBNAIL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    name = f"custom-{hashlib.sha256(data).hexdigest()[:16]}.{'jpg' if fmt == 'JPEG' else 'png'}"
    (directory / name).write_bytes(data)
    return name


def prepare_thumbnail(choice: ThumbnailChoice, state: dict[str, Any], project_id: str, settings: Settings) -> PreparedThumbnail | None:
    """Bytes to send to thumbnails.set; re-encoded only if over YouTube's size limit."""
    if choice.source == "youtube_auto":
        return None
    match = next(
        (item for item in thumbnail_choices(state, project_id, settings) if item["source"] == choice.source and item["asset"] == choice.asset),
        None,
    )
    if match is None or not match["valid"]:
        raise ValueError((match or {}).get("problem") or "The selected thumbnail is no longer available.")
    path = _safe_child(settings.render_root.resolve(), match["url"].removeprefix("/media/"))
    assert path is not None
    data = path.read_bytes()
    content_type = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
    if len(data) > THUMBNAIL_MAX_BYTES:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            for quality in (88, 80, 72, 64, 56):
                buffer = io.BytesIO()
                rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
                if buffer.tell() <= THUMBNAIL_MAX_BYTES:
                    data, content_type = buffer.getvalue(), "image/jpeg"
                    break
            else:
                raise ValueError("The thumbnail cannot be made smaller than 2 MB.")
    return PreparedThumbnail(data, content_type, hashlib.sha256(data).hexdigest(), choice.source, str(choice.asset))


# ---------------------------------------------------------------------------
# Categories (fetched from YouTube, cached; never hardcoded)
# ---------------------------------------------------------------------------

_CATEGORY_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CATEGORY_LOCK = Lock()
CATEGORY_TTL_SECONDS = 24 * 3600


def list_categories(provider: YouTubeProvider, token: str, region: str, language: str) -> list[dict[str, Any]]:
    key = (region.upper(), language)
    with _CATEGORY_LOCK:
        cached = _CATEGORY_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CATEGORY_TTL_SECONDS:
        return cached[1]
    items = provider.list_categories(token, key[0], language)
    categories = [
        {"id": str(item.get("id")), "title": str((item.get("snippet") or {}).get("title") or item.get("id"))}
        for item in items
        if (item.get("snippet") or {}).get("assignable")
    ]
    with _CATEGORY_LOCK:
        _CATEGORY_CACHE[key] = (time.monotonic(), categories)
    return categories


def reset_category_cache() -> None:
    with _CATEGORY_LOCK:
        _CATEGORY_CACHE.clear()


def suggest_category(categories: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
    """A hint for explainer Shorts; the user decides."""
    content_type = str((state.get("intent") or {}).get("content_type") or "")
    if "explainer" not in content_type and "fact" not in content_type:
        return None
    wanted = ("education", "bildung", "éducation", "educación", "istruzione", "onderwijs")
    match = next((item for item in categories if any(word in item["title"].casefold() for word in wanted)), None)
    return {"id": match["id"], "title": match["title"], "reason": "ClipForge Knowledge Shorts explain a topic."} if match else None


# ---------------------------------------------------------------------------
# Altered / synthetic content suggestion (never an automatic answer)
# ---------------------------------------------------------------------------


def synthetic_suggestion(fingerprint: dict[str, Any]) -> dict[str, Any]:
    visual = fingerprint.get("visual") or {}
    generated = int(visual.get("generated_scene_count") or 0)
    if generated == 0:
        return {
            "value": False,
            "why": "Every scene uses real stock or archive media or a simple graphic. A synthetic narration voice reading your script is not realistic altered content on its own.",
        }
    return {
        "value": None,
        "why": (
            f"{generated} scene{'s' if generated != 1 else ''} use{'s' if generated == 1 else ''} AI-generated images. "
            "Choose Yes only if one could be mistaken for a real person, place or event."
        ),
    }


# ---------------------------------------------------------------------------
# Draft + validation (the preflight)
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def default_metadata(state: dict[str, Any], title: str) -> dict[str, Any]:
    platforms = ((state.get("social_metadata") or {}).get("platforms") or {})
    youtube = platforms.get("youtube") if isinstance(platforms.get("youtube"), dict) else {}
    tags = []
    for tag in youtube.get("hashtags") or []:
        clean = re.sub(r"[^\w]", "", str(tag).lstrip("#"), flags=re.UNICODE)
        if clean and clean.casefold() not in {item.casefold() for item in tags}:
            tags.append(clean)
    description = re.sub(r"[<>]", "", _clean(youtube.get("description")))
    hashtags = " ".join(f"#{tag}" for tag in tags[:15] if f"#{tag}".casefold() not in description.casefold())
    return {
        # Suggested defaults drop the < > YouTube rejects; the user's own edits are validated, not rewritten.
        "title": " ".join(re.sub(r"[<>]", "", _clean(youtube.get("title")) or _clean(title)).split()),
        "description": f"{description}\n\n{hashtags}".strip() if hashtags else description,
        "tags": tags,
        "language": str((state.get("intent") or {}).get("language") or "") or None,
    }


def tags_length(tags: list[str]) -> int:
    return sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1)


def validate_options(
    options: PublishOptions,
    *,
    defaults: UploadDefaults,
    thumbnails: list[dict[str, Any]],
    categories: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, str]], ScheduleResolution | None]:
    """Everything that must be fixed before any byte is sent, in plain words."""
    issues: list[dict[str, str]] = []

    def need(field: str, message: str) -> None:
        issues.append({"field": field, "message": message})

    title = _clean(options.title)
    if not title:
        need("title", "Add a title")
    elif len(title) > TITLE_LIMIT:
        need("title", f"Shorten the title to {TITLE_LIMIT} characters ({len(title)} now)")
    if re.search(r"[<>]", options.title) or re.search(r"[<>]", options.description):
        need("title" if re.search(r"[<>]", options.title) else "description", "Remove the < and > characters (YouTube does not allow them)")
    if len(options.description.encode("utf-8")) > DESCRIPTION_LIMIT_BYTES:
        need("description", f"Shorten the description to {DESCRIPTION_LIMIT_BYTES} bytes ({len(options.description.encode('utf-8'))} now)")
    tags = [tag.strip() for tag in options.tags]
    if any(not tag or re.search(r"[<>,]", tag) for tag in tags):
        need("tags", "Remove empty tags and tags containing < > or commas")
    elif tags_length(tags) > TAGS_LIMIT_CHARS:
        need("tags", f"Shorten the tags to {TAGS_LIMIT_CHARS} characters in total ({tags_length(tags)} now)")
    if options.made_for_kids is None:
        need("made_for_kids", "Choose whether this video is made for kids")
    if options.contains_synthetic_media is None:
        need("contains_synthetic_media", "Choose whether the video contains realistic altered or synthetic content")
    if options.thumbnail is None:
        need("thumbnail", "Select a thumbnail")
    elif options.thumbnail.source != "youtube_auto":
        match = next((item for item in thumbnails if item["source"] == options.thumbnail.source and item["asset"] == options.thumbnail.asset), None)
        if match is None:
            need("thumbnail", "Select a thumbnail")
        elif not match["valid"]:
            need("thumbnail", f"Choose another thumbnail: {match['problem']}")
    if options.visibility not in allowed_visibilities(defaults):
        need("visibility", "YouTube keeps uploads from unverified API projects private; choose Private or Schedule")
    resolution = None
    if options.visibility == "schedule":
        if options.schedule is None:
            need("schedule", "Choose the publication date and time")
        else:
            resolution = resolve_schedule(options.schedule, now=now)
            if resolution.status != "ok":
                need("schedule", resolution.message or "Choose a valid publication time")
    if options.category_id is not None and categories is not None and options.category_id not in {item["id"] for item in categories}:
        need("category_id", "Choose a category YouTube offers for your region")
    for field in ("default_language", "default_audio_language"):
        value = getattr(options, field)
        if value and not _LANGUAGE.match(value):
            need(field, "Use a language code such as en or de")
    if options.recording_date:
        if not _DATE.match(options.recording_date):
            need("recording_date", "Use a recording date like 2026-09-28")
        else:
            try:
                recorded = date.fromisoformat(options.recording_date)
            except ValueError:
                need("recording_date", "Use a valid recording date")
            else:
                if recorded > (now or datetime.now(UTC)).date():
                    need("recording_date", "The recording date cannot be in the future")
    return issues, resolution


def insert_body(options: PublishOptions, resolution: ScheduleResolution | None) -> dict[str, Any]:
    """videos.insert body; every key is a documented, writable API property."""
    snippet: dict[str, Any] = {"title": _clean(options.title), "description": options.description.strip()}
    tags = [tag.strip() for tag in options.tags if tag.strip()]
    if tags:
        snippet["tags"] = tags
    if options.category_id:
        snippet["categoryId"] = options.category_id
    if options.default_language:
        snippet["defaultLanguage"] = options.default_language
    if options.default_audio_language:
        snippet["defaultAudioLanguage"] = options.default_audio_language
    if options.made_for_kids is None or options.contains_synthetic_media is None:
        raise ValueError("Audience and synthetic-content answers are required.")
    status: dict[str, Any] = {
        "privacyStatus": "private" if options.visibility in {"private", "schedule"} else options.visibility,
        "selfDeclaredMadeForKids": bool(options.made_for_kids),
        "containsSyntheticMedia": bool(options.contains_synthetic_media),
        "license": options.license,
        "embeddable": bool(options.embeddable),
        "publicStatsViewable": bool(options.public_stats_viewable),
    }
    if options.visibility == "schedule":
        if resolution is None or resolution.status != "ok" or resolution.publish_at is None:
            raise ValueError("A valid publication time is required.")
        status["publishAt"] = resolution.as_dict()["publish_at"]
    body: dict[str, Any] = {
        "snippet": snippet,
        "status": status,
        "paidProductPlacementDetails": {"hasPaidProductPlacement": bool(options.paid_product_placement)},
    }
    if options.recording_date:
        body["recordingDetails"] = {"recordingDate": f"{options.recording_date}T00:00:00Z"}
    return body


def options_with_defaults(draft: dict[str, Any], defaults: UploadDefaults) -> dict[str, Any]:
    """Initial form state: project metadata plus only the defaults the user saved."""
    return {
        **draft,
        "made_for_kids": defaults.made_for_kids,
        "contains_synthetic_media": defaults.contains_synthetic_media,
        "visibility": defaults.visibility if defaults.visibility in allowed_visibilities(defaults) else "private",
        "category_id": defaults.category_id,
        "default_language": defaults.default_language or draft.get("language"),
        "default_audio_language": defaults.default_language or draft.get("language"),
        "license": defaults.license or "youtube",
        "embeddable": True if defaults.embeddable is None else defaults.embeddable,
        "public_stats_viewable": True if defaults.public_stats_viewable is None else defaults.public_stats_viewable,
        "notify_subscribers": True if defaults.notify_subscribers is None else defaults.notify_subscribers,
        "paid_product_placement": False,
        "recording_date": None,
    }
