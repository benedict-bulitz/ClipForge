"""Project core and immutable revisions."""

import sqlalchemy as sa

from alembic import op

revision = "0001_project_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("original_prompt", sa.Text(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_index("ix_projects_status", "projects", ["status"])
    op.create_table(
        "project_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("changed_components", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "number", name="uq_project_revision_number"),
    )
    op.create_index("ix_project_revisions_project_id", "project_revisions", ["project_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER protect_project_prompt_update BEFORE UPDATE OF original_prompt ON projects "
            "WHEN NEW.original_prompt != OLD.original_prompt "
            "BEGIN SELECT RAISE(ABORT, 'original_prompt is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER protect_revision_update BEFORE UPDATE ON project_revisions "
            "BEGIN SELECT RAISE(ABORT, 'project revisions are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER protect_revision_delete BEFORE DELETE ON project_revisions "
            "BEGIN SELECT RAISE(ABORT, 'project revisions are append-only'); END"
        )
    elif dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION clipforge_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'immutable record'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER protect_project_prompt_update BEFORE UPDATE OF original_prompt ON projects "
            "FOR EACH ROW EXECUTE FUNCTION clipforge_immutable()"
        )
        op.execute(
            "CREATE TRIGGER protect_revision_update BEFORE UPDATE OR DELETE ON project_revisions "
            "FOR EACH ROW EXECUTE FUNCTION clipforge_immutable()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS clipforge_immutable() CASCADE")
    op.drop_table("project_revisions")
    op.drop_table("projects")
