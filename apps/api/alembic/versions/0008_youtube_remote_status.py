"""YouTube V3: remote status reconciliation (remote state separate from the request)."""

import sqlalchemy as sa

from alembic import op

revision = "0008_youtube_remote_status"
down_revision = "0007_youtube_publishing_v2"
branch_labels = None
depends_on = None

COLUMNS = (
    sa.Column("remote_privacy_status", sa.String(16), nullable=True),
    sa.Column("remote_publish_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("remote_published_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("first_observed_public_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("remote_view_count", sa.Integer(), nullable=True),
    sa.Column("remote_like_count", sa.Integer(), nullable=True),
    sa.Column("remote_comment_count", sa.Integer(), nullable=True),
    sa.Column("remote_status_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("remote_status_attempted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("remote_status_error_code", sa.String(48), nullable=True),
    sa.Column("remote_status_error", sa.Text(), nullable=True),
    sa.Column("last_analytics_attempt_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in COLUMNS:
            batch.add_column(column.copy())


def downgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        for column in reversed(COLUMNS):
            batch.drop_column(column.name)
