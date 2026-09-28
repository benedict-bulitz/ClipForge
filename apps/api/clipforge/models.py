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
