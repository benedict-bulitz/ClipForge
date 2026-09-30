"""Topic Intelligence V1: candidate pool, discovery runs and source cache."""

import sqlalchemy as sa

from alembic import op

revision = "0011_topic_intelligence"
down_revision = "0010_youtube_video_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topic_discovery_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("language", sa.String(8), nullable=False, server_default="de"),
        sa.Column("region", sa.String(8), nullable=False, server_default="DE"),
        sa.Column("score_version", sa.String(48), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("transformation", sa.String(16), nullable=False, server_default="template"),
        sa.Column("ranked_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("raw_topic_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("youtube_quota_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topic_discovery_runs_sequence", "topic_discovery_runs", ["sequence"])
    op.create_index("ix_topic_discovery_runs_status", "topic_discovery_runs", ["status"])
    op.create_index("ix_topic_discovery_runs_expires_at", "topic_discovery_runs", ["expires_at"])
    op.create_table(
        "topic_candidates",
        sa.Column("candidate_id", sa.String(40), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("topic", sa.String(300), nullable=False),
        sa.Column("question", sa.String(300), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.String(8), nullable=False, server_default="de"),
        sa.Column("region", sa.String(8), nullable=False, server_default="DE"),
        sa.Column("niche", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("source_signals", sa.JSON(), nullable=False),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("score_version", sa.String(48), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="low"),
        sa.Column("rejection_reasons", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("freshness_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pooled"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_project_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_topic_candidates_run_id", "topic_candidates", ["run_id"])
    op.create_index("ix_topic_candidates_status", "topic_candidates", ["status"])
    op.create_index("ix_topic_candidates_used_project_id", "topic_candidates", ["used_project_id"])
    op.create_table(
        "topic_source_cache",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quota_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topic_source_cache_provider", "topic_source_cache", ["provider"])
    op.create_index("ix_topic_source_cache_expires_at", "topic_source_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_table("topic_source_cache")
    op.drop_table("topic_candidates")
    op.drop_table("topic_discovery_runs")
