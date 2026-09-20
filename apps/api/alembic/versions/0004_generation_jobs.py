"""Add durable generation progress jobs and local timing statistics."""

import sqlalchemy as sa

from alembic import op

revision = "0004_generation_jobs"
down_revision = "0003_revision_navigation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("active_key", sa.String(96), nullable=True, unique=True),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_stage", sa.String(32), nullable=False),
        sa.Column("stage_label", sa.String(160), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("completed_units", sa.Integer(), nullable=True),
        sa.Column("total_units", sa.Integer(), nullable=True),
        sa.Column("stage_plan", sa.JSON(), nullable=False),
        sa.Column("completed_stages", sa.JSON(), nullable=False),
        sa.Column("stage_timings", sa.JSON(), nullable=False),
        sa.Column("estimated_remaining_seconds", sa.Float(), nullable=True),
        sa.Column("failure_category", sa.String(64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_generation_jobs_project_id", "generation_jobs", ["project_id"])
    op.create_index("ix_generation_jobs_request_hash", "generation_jobs", ["request_hash"])
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"])
    op.create_table(
        "generation_timing_stats",
        sa.Column("stage", sa.String(32), primary_key=True),
        sa.Column("ema_seconds", sa.Float(), nullable=False),
        sa.Column("ema_seconds_per_unit", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("generation_timing_stats")
    op.drop_index("ix_generation_jobs_status", table_name="generation_jobs")
    op.drop_index("ix_generation_jobs_request_hash", table_name="generation_jobs")
    op.drop_index("ix_generation_jobs_project_id", table_name="generation_jobs")
    op.drop_table("generation_jobs")
