"""persist queued generation requests."""

import sqlalchemy as sa

from alembic import op

revision = "0005_generation_job_payload"
down_revision = "0004_generation_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("request_payload", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("generation_jobs", "request_payload")
