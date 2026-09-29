"""YouTube Video Library: a small retained preview per uploaded video."""

import sqlalchemy as sa

from alembic import op

revision = "0010_youtube_video_library"
down_revision = "0009_youtube_smart_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        batch.add_column(sa.Column("library_thumbnail", sa.String(120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("youtube_uploads") as batch:
        batch.drop_column("library_thumbnail")
