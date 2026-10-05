"""record the backend code identity that ran each generation job."""

import sqlalchemy as sa

from alembic import op

revision = "0012_job_runtime_identity"
down_revision = "0011_topic_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("runtime_identity", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("generation_jobs", "runtime_identity")
