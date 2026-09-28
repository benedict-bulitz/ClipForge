"""YouTube Learning Loop V1: channel connection, revision-safe uploads, analytics history."""

import sqlalchemy as sa

from alembic import op

revision = "0006_youtube_learning_loop"
down_revision = "0005_generation_job_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "youtube_connections",
        sa.Column("slot", sa.String(16), primary_key=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("channel_title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_youtube_connections_channel_id", "youtube_connections", ["channel_id"])

    op.create_table(
        "youtube_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("render_revision", sa.Integer(), nullable=False),
        sa.Column("render_sha256", sa.String(64), nullable=False),
        sa.Column("render_file_size", sa.Integer(), nullable=False),
        sa.Column("render_content_hash", sa.String(32), nullable=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("fingerprint_id", sa.String(36), nullable=True),
        sa.Column("youtube_video_id", sa.String(32), nullable=True, unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("privacy_status", sa.String(16), nullable=False),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schedule_status", sa.String(24), nullable=False),
        sa.Column("schedule_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_source", sa.String(32), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("upload_session_uri", sa.Text(), nullable=True),
        sa.Column("bytes_uploaded", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("upload_status", sa.String(24), nullable=True),
        sa.Column("processing_status", sa.String(24), nullable=True),
        sa.Column("failure_reason", sa.String(64), nullable=True),
        sa.Column("rejection_reason", sa.String(64), nullable=True),
        sa.Column("content_type", sa.String(24), nullable=True),
        sa.Column("deleted_on_youtube", sa.Boolean(), nullable=False),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("analytics_error_code", sa.String(48), nullable=True),
        sa.Column("analytics_error_message", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_analytics_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_youtube_uploads_project_id", "youtube_uploads", ["project_id"])
    op.create_index("ix_youtube_uploads_channel_id", "youtube_uploads", ["channel_id"])
    op.create_index("ix_youtube_uploads_render_sha256", "youtube_uploads", ["render_sha256"])
    op.create_index("ix_youtube_uploads_state", "youtube_uploads", ["state"])
    op.create_index(
        "ix_youtube_uploads_project_revision", "youtube_uploads", ["project_id", "render_revision"]
    )

    op.create_table(
        "youtube_analytics_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "upload_id",
            sa.String(36),
            sa.ForeignKey("youtube_uploads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("youtube_video_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("render_revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("age_bucket", sa.String(16), nullable=False),
        sa.Column("published_age_hours", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("retention_status", sa.String(24), nullable=False),
        sa.Column("content_type", sa.String(24), nullable=True),
        sa.Column("date_range", sa.JSON(), nullable=False),
        sa.Column("raw_responses", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("upload_id", "youtube_video_id", "channel_id", "project_id", "fetched_at"):
        op.create_index(
            f"ix_youtube_analytics_snapshots_{column}", "youtube_analytics_snapshots", [column]
        )

    op.create_table(
        "youtube_metric_values",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("youtube_analytics_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("youtube_video_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("availability", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("snapshot_id", "youtube_video_id", "fetched_at"):
        op.create_index(f"ix_youtube_metric_values_{column}", "youtube_metric_values", [column])

    op.create_table(
        "youtube_retention_points",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("youtube_analytics_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("youtube_video_id", sa.String(32), nullable=False),
        sa.Column("elapsed_video_ratio", sa.Float(), nullable=False),
        sa.Column("video_second", sa.Float(), nullable=False),
        sa.Column("audience_watch_ratio", sa.Float(), nullable=True),
        sa.Column("relative_retention_performance", sa.Float(), nullable=True),
        sa.Column("started_watching", sa.Float(), nullable=True),
        sa.Column("stopped_watching", sa.Float(), nullable=True),
        sa.Column("total_segment_impressions", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("snapshot_id", "youtube_video_id", "fetched_at"):
        op.create_index(
            f"ix_youtube_retention_points_{column}", "youtube_retention_points", [column]
        )

    op.create_table(
        "production_fingerprints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("render_revision", sa.Integer(), nullable=False),
        sa.Column("render_sha256", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "render_revision", "render_sha256", name="uq_production_fingerprint_render"
        ),
    )
    op.create_index("ix_production_fingerprints_project_id", "production_fingerprints", ["project_id"])


def downgrade() -> None:
    op.drop_table("production_fingerprints")
    op.drop_table("youtube_retention_points")
    op.drop_table("youtube_metric_values")
    op.drop_table("youtube_analytics_snapshots")
    op.drop_table("youtube_uploads")
    op.drop_table("youtube_connections")
