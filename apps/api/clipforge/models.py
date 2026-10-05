import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    original_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False, index=True)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active_tip_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    revisions: Mapped[list["ProjectRevision"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectRevision.number"
    )
    chat_messages: Mapped[list["ProjectChatMessage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectChatMessage.created_at"
    )


@event.listens_for(Project, "before_update")
def protect_original_prompt(_mapper: Any, _connection: Any, target: Project) -> None:
    if inspect(target).attrs.original_prompt.history.has_changes():
        raise ValueError("original_prompt is immutable")


class ProjectRevision(Base):
    __tablename__ = "project_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_project_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    changed_components: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    project: Mapped[Project] = relationship(back_populates="revisions")


class ProjectChatMessage(Base):
    __tablename__ = "project_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    project: Mapped[Project] = relationship(back_populates="chat_messages")


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The queued request must survive an API restart; workers load this rather
    # than relying on an in-memory FastAPI BackgroundTask payload.
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(96), nullable=True, unique=True)
    base_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing")
    stage_label: Mapped[str] = mapped_column(String(160), nullable=False, default="Preparing")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    completed_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_plan: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    completed_stages: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    stage_timings: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_remaining_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Audit only: the backend code identity that ran this job (None for older jobs).
    runtime_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class GenerationTimingStat(Base):
    __tablename__ = "generation_timing_stats"

    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    ema_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    ema_seconds_per_unit: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


@event.listens_for(ProjectRevision, "before_update")
def protect_revision_update(_mapper: Any, _connection: Any, _target: ProjectRevision) -> None:
    raise ValueError("project revisions are append-only")


@event.listens_for(ProjectRevision, "before_delete")
def protect_revision_delete(_mapper: Any, _connection: Any, _target: ProjectRevision) -> None:
    raise ValueError("project revisions are append-only")


# ---------------------------------------------------------------------------
# YouTube Learning Loop V1
#
# One connection authority (``youtube_connections.slot == "primary"``), one
# project<->video mapping authority (``youtube_uploads``) and one analytics
# store (snapshots + metric values + raw retention points).  Tokens are never
# stored here: the refresh token lives only in the OS keyring.
# ---------------------------------------------------------------------------


class YouTubeConnection(Base):
    __tablename__ = "youtube_connections"

    # Exactly one connected channel identity per ClipForge installation.
    slot: Mapped[str] = mapped_column(String(16), primary_key=True, default="primary")
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="connected")
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class YouTubeUpload(Base):
    """The mapping of one exact rendered project revision to one YouTube video."""

    __tablename__ = "youtube_uploads"
    __table_args__ = (
        Index("ix_youtube_uploads_project_revision", "project_id", "render_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Deliberately no FK: the YouTube video (and what it teaches) outlives a
    # locally deleted project.
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    render_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    render_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    render_file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    render_content_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "<channel>:<project>:<render sha>" while this row is the active mapping
    # of that render; cleared only when the user explicitly re-uploads after
    # YouTube deleted/rejected the video.  Prevents accidental duplicates.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    fingerprint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    privacy_status: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    # The *requested* publication time (immutable provenance, never YouTube's answer).
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_status: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    schedule_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # The resumable session URI is an upload capability: stored for resuming,
    # never serialized to clients or written to logs.
    upload_session_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_uploaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upload_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    processing_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    deleted_on_youtube: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Analytics failures (quota, disabled API, expired auth) stay visible
    # without being confused with the upload's own state.
    analytics_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    analytics_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # V2 publishing settings: the exact values sent to YouTube for this video.
    source_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    made_for_kids: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # YouTube's read-back of the audience answer after upload (None = not yet read).
    made_for_kids_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    contains_synthetic_media: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    requested_visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    visibility_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_subscribers: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    upload_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    schedule_local_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    schedule_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thumbnail_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    thumbnail_asset: Mapped[str | None] = mapped_column(String(300), nullable=True)
    thumbnail_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # none | pending | applied | failed | not_requested
    thumbnail_upload_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    thumbnail_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # V3 remote state: what YouTube last reported (current authority).
    # ``privacy_status``/``publish_at`` above are the values at upload time /
    # the requested schedule (provenance); ``upload_status``,
    # ``processing_status`` and the reasons are YouTube's own values.
    remote_privacy_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    remote_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_observed_public_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_status_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_status_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    remote_status_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_analytics_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Smart Slot Planner provenance: "auto" (the planner's slot) | "manual"
    # (the user's own time) | None (not scheduled); the preferred slot it filled.
    schedule_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    schedule_slot_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Video Library: file name of the small retained preview (render_root /
    # "video-library"); it outlives the project's own media.
    library_thumbnail: Mapped[str | None] = mapped_column(String(120), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_analytics_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class YouTubeAnalyticsSnapshot(Base):
    """One fetch of a video's analytics at some age; history is never overwritten."""

    __tablename__ = "youtube_analytics_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    upload_id: Mapped[str] = mapped_column(
        ForeignKey("youtube_uploads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_video_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    render_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # youtube_analytics_api | manual_studio_import
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube_analytics_api")
    # 1h | 6h | 24h | 72h | 7d | manual
    age_bucket: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    published_age_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ok | no_data_yet | partial | error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    retention_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_requested")
    content_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    date_range: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Raw API responses (column headers + rows) exactly as returned.
    raw_responses: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    metrics: Mapped[list["YouTubeMetricValue"]] = relationship(
        cascade="all, delete-orphan", order_by="YouTubeMetricValue.name"
    )
    retention_points: Mapped[list["YouTubeRetentionPoint"]] = relationship(
        cascade="all, delete-orphan", order_by="YouTubeRetentionPoint.elapsed_video_ratio"
    )


class YouTubeMetricValue(Base):
    """A single named metric with provenance; unavailable metrics are explicit rows."""

    __tablename__ = "youtube_metric_values"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("youtube_analytics_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_video_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # available | unavailable | no_data_yet
    availability: Mapped[str] = mapped_column(String(16), nullable=False, default="available")
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # youtube_analytics_api | youtube_data_api | manual_studio_import
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class YouTubeRetentionPoint(Base):
    """A raw audience-retention bucket exactly as returned (never smoothed)."""

    __tablename__ = "youtube_retention_points"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("youtube_analytics_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_video_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    elapsed_video_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    video_second: Mapped[float] = mapped_column(Float, nullable=False)
    audience_watch_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_retention_performance: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_watching: Mapped[float | None] = mapped_column(Float, nullable=True)
    stopped_watching: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_segment_impressions: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ProductionFingerprint(Base):
    """Compact, immutable record of the decisions behind one uploaded render."""

    __tablename__ = "production_fingerprints"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "render_revision", "render_sha256", name="uq_production_fingerprint_render"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    render_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    render_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fingerprint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(ProductionFingerprint, "before_update")
def protect_fingerprint_update(_mapper: Any, _connection: Any, _target: ProductionFingerprint) -> None:
    raise ValueError("production fingerprints are immutable")


class YouTubeUploadDefaults(Base):
    """User-chosen upload defaults (never silently pre-filled compliance answers)."""

    __tablename__ = "youtube_upload_defaults"

    slot: Mapped[str] = mapped_column(String(16), primary_key=True, default="primary")
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class YouTubeLearningArchive(Base):
    """What remains of a deleted, successfully uploaded project besides its
    upload mappings, fingerprints and analytics (which are kept as they are)."""

    __tablename__ = "youtube_learning_archives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic: Mapped[str | None] = mapped_column(String(300), nullable=True)
    upload_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    bytes_freed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retained_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# ---------------------------------------------------------------------------
# YouTube Smart Slot Planner
#
# One publishing-schedule authority per channel (cadence, zone, preferred
# slots), one bounded cache of what YouTube itself reports as scheduled or
# published (``youtube_schedule_entries``) and short-lived slot reservations
# that keep two concurrent ClipForge uploads from claiming the same slot.
# ---------------------------------------------------------------------------


class YouTubePublishingSchedule(Base):
    """The channel's publishing cadence; a channel setting, never per project."""

    __tablename__ = "youtube_publishing_schedules"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    videos_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # seed (starter preset) | manual (user-edited) | learned (user-approved proposal)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="seed")
    # When on, the planner's slot is pre-selected in the publishing sheet.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occupancy_tolerance_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    min_lead_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    learned_sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learned_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Remote schedule sync state (the cache itself is youtube_schedule_entries).
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_sync_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_sync_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    remote_sync_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    remote_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class YouTubeScheduleSlot(Base):
    """One preferred local wall-clock slot; ``weekday`` NULL = every day.

    Weekday-specific rows (0 = Monday) override the every-day rows for that
    day, so per-weekday schedules need no schema change later.
    """

    __tablename__ = "youtube_schedule_slots"
    __table_args__ = (
        Index("ix_youtube_schedule_slots_channel_weekday", "channel_id", "weekday"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("youtube_publishing_schedules.channel_id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    # seed | manual | learned
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="seed")


class YouTubeScheduleEntry(Base):
    """A video YouTube reports as scheduled or published (bounded cache).

    Filled only from the channel's uploads playlist + videos.list, so videos
    scheduled in YouTube Studio or by other tools count exactly like ClipForge's.
    """

    __tablename__ = "youtube_schedule_entries"
    __table_args__ = (
        UniqueConstraint("channel_id", "video_id", name="uq_youtube_schedule_entry_video"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # scheduled (private + publishAt) | published (public)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    privacy_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    upload_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The instant this video occupies on the channel's calendar.
    occupies_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="youtube")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class YouTubeSlotReservation(Base):
    """A ClipForge upload's claim on a slot until YouTube confirms (or refuses) it.

    ``active_key`` ("<channel>:<UTC instant>") is unique while an automatic
    reservation is reserved/confirmed, so two uploads cannot both claim it;
    it is cleared on release.
    """

    __tablename__ = "youtube_slot_reservations"
    __table_args__ = (
        Index("ix_youtube_slot_reservations_channel_slot", "channel_id", "slot_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_time: Mapped[str] = mapped_column(String(16), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # auto | manual
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # reserved | confirmed | released
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved", index=True)
    active_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    upload_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


# ---------------------------------------------------------------------------
# Topic Intelligence V1 ("Generate Next Video")
#
# One candidate store (``topic_candidates``, keyed by a stable candidate id so
# "Try another" and novelty memory survive a pool refresh), one discovery-run
# log (freshness, per-source status, quota spent) and one provider cache of
# *normalized* source results (never raw API payloads) with a TTL.  Scores are
# written only by ``topic_intelligence.scoring``.
# ---------------------------------------------------------------------------


class TopicDiscoveryRun(Base):
    __tablename__ = "topic_discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Monotonic order of refreshes (timestamps can tie within one request).
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    # ok | partial | unavailable
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok", index=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="de")
    region: Mapped[str] = mapped_column(String(8), nullable=False, default="DE")
    score_version: Mapped[str] = mapped_column(String(48), nullable=False)
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # [{name, status, error, cached, fetched_at, calls, quota_units, items}]
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    transformation: Mapped[str] = mapped_column(String(16), nullable=False, default="template")
    ranked_candidate_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    raw_topic_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    youtube_quota_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class TopicCandidateRecord(Base):
    """The canonical TopicCandidate (see ``topic_intelligence.candidate``)."""

    __tablename__ = "topic_candidates"

    candidate_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    question: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="de")
    region: Mapped[str] = mapped_column(String(8), nullable=False, default="DE")
    niche: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    # {trend|outlier|competition|novelty|channel_fit|suitability|visual|
    #  researchability|own_performance: {value, confidence, evidence, sources}}
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score_version: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pooled | proposed | skipped | used | rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pooled", index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TopicSourceCache(Base):
    """Normalized, compact provider results with a TTL (quota protection)."""

    __tablename__ = "topic_source_cache"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quota_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
