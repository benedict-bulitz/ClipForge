"""YouTube publishing V2: explicit upload settings, thumbnails, defaults, learning archive."""

import sqlalchemy as sa

from alembic import op

revision = "0007_youtube_publishing_v2"
down_revision = "0006_youtube_learning_loop"
branch_labels = None
depends_on = None

UPLOAD_COLUMNS = (
    sa.Column("source_kind", sa.String(16), nullable=True),
    sa.Column("made_for_kids", sa.Boolean(), nullable=True),
    sa.Column("made_for_kids_confirmed", sa.Boolean(), nullable=True),
    sa.Column("contains_synthetic_media", sa.Boolean(), nullable=True),
    sa.Column("requested_visibility", sa.String(16), nullable=False, server_default="private"),
    sa.Column("visibility_restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("notify_subscribers", sa.Boolean(), nullable=True),
    sa.Column("upload_settings", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("schedule_local_time", sa.String(16), nullable=True),
    sa.Column("schedule_timezone", sa.String(64), nullable=True),
    sa.Column("thumbnail_source", sa.String(24), nullable=True),
    sa.Column("thumbnail_asset", sa.String(300), nullable=True),
    sa.Column("thumbnail_sha256", sa.String(64), nullable=True),
    sa.Column("thumbnail_upload_status", sa.String(16), nullable=False, server_default="none"),
    sa.Column("thumbnail_failure_reason", sa.Text(), nullable=True),
    sa.Column("thumbnail_applied_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in UPLOAD_COLUMNS:
            batch.add_column(column.copy())
    op.create_table(
        "youtube_upload_defaults",
        sa.Column("slot", sa.String(16), primary_key=True),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "youtube_learning_archives",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(300), nullable=True),
        sa.Column("upload_ids", sa.JSON(), nullable=False),
        sa.Column("bytes_freed", sa.Integer(), nullable=False),
        sa.Column("retained_bytes", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_youtube_learning_archives_project_id", "youtube_learning_archives", ["project_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("youtube_learning_archives")
    op.drop_table("youtube_upload_defaults")
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in reversed(UPLOAD_COLUMNS):
            batch.drop_column(column.name)
