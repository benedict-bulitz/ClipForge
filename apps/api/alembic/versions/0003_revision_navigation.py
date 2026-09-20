"""Add durable active-lineage revision navigation metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0003_revision_navigation"
down_revision = "0002_project_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("active_tip_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute("UPDATE projects SET active_tip_revision = current_revision")
    op.add_column(
        "project_revisions",
        sa.Column("kind", sa.String(16), nullable=False, server_default="user"),
    )
    op.execute("UPDATE project_revisions SET kind = 'initial' WHERE parent_revision IS NULL")
    op.execute(
        "UPDATE project_revisions SET kind = 'system' "
        "WHERE instruction IN ('Render video', 'Export MP4', "
        "'Clean exported project media')"
    )


def downgrade() -> None:
    op.drop_column("project_revisions", "kind")
    op.drop_column("projects", "active_tip_revision")
