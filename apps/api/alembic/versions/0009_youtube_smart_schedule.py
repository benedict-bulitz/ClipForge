"""YouTube Smart Slot Planner: channel cadence, preferred slots, remote schedule cache, reservations."""

import sqlalchemy as sa

from alembic import op

revision = "0009_youtube_smart_schedule"
down_revision = "0008_youtube_remote_status"
branch_labels = None
depends_on = None

UPLOAD_COLUMNS = (
    sa.Column("schedule_source", sa.String(16), nullable=True),
    sa.Column("schedule_slot_time", sa.String(5), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in UPLOAD_COLUMNS:
            batch.add_column(column.copy())
    op.create_table(
        "youtube_publishing_schedules",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("videos_per_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="seed"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("occupancy_tolerance_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("min_lead_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("learned_sample_size", sa.Integer(), nullable=True),
        sa.Column("learned_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploads_playlist_id", sa.String(64), nullable=True),
        sa.Column("remote_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_sync_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_sync_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remote_sync_error_code", sa.String(48), nullable=True),
        sa.Column("remote_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "youtube_schedule_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(64), sa.ForeignKey("youtube_publishing_schedules.channel_id", ondelete="CASCADE"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("local_time", sa.String(5), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    op.create_index("ix_youtube_schedule_slots_channel_weekday", "youtube_schedule_slots", ["channel_id", "weekday"])
    op.create_table(
        "youtube_schedule_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("video_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("privacy_status", sa.String(16), nullable=True),
        sa.Column("upload_status", sa.String(24), nullable=True),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occupies_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "video_id", name="uq_youtube_schedule_entry_video"),
    )
    op.create_index("ix_youtube_schedule_entries_channel_id", "youtube_schedule_entries", ["channel_id"])
    op.create_index("ix_youtube_schedule_entries_occupies_at", "youtube_schedule_entries", ["occupies_at"])
    op.create_index("ix_youtube_schedule_entries_last_seen_at", "youtube_schedule_entries", ["last_seen_at"])
    op.create_table(
        "youtube_slot_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("slot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_time", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("active_key", sa.String(120), nullable=True, unique=True),
        sa.Column("upload_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("video_id", sa.String(32), nullable=True),
        sa.Column("release_reason", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_youtube_slot_reservations_channel_slot", "youtube_slot_reservations", ["channel_id", "slot_at"])
    op.create_index("ix_youtube_slot_reservations_state", "youtube_slot_reservations", ["state"])
    op.create_index("ix_youtube_slot_reservations_upload_id", "youtube_slot_reservations", ["upload_id"])


def downgrade() -> None:
    op.drop_table("youtube_slot_reservations")
    op.drop_table("youtube_schedule_entries")
    op.drop_index("ix_youtube_schedule_slots_channel_weekday", table_name="youtube_schedule_slots")
    op.drop_table("youtube_schedule_slots")
    op.drop_table("youtube_publishing_schedules")
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in reversed(UPLOAD_COLUMNS):
            batch.drop_column(column.name)
