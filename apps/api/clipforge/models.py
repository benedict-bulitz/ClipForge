import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
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
