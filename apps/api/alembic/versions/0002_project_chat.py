"""Persist project editor conversations."""

import sqlalchemy as sa

from alembic import op

revision = "0002_project_chat"
down_revision = "0001_project_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_chat_messages_project_id",
        "project_chat_messages",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_table("project_chat_messages")
