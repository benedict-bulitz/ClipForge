"""Video Library removal: ``library_removed_at`` on both publication tables.

Removing a video from the Videos tab only hides its library entry; the
publication record (and its analytics, history and remote post) stays, so
the marker is a nullable timestamp and nothing existing changes.
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_video_library_removal"
down_revision = "0013_multiplatform_publishing"
branch_labels = None
depends_on = None


TABLES = ("youtube_uploads", "social_publications")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("library_removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("library_removed_at")
