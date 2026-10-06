"""multi-platform publishing: accounts, platform config, social publications.

Adopts the legacy single YouTube connection (``youtube_connections``) as a
``publishing_accounts`` row without touching the legacy row, any upload,
schedule or analytics record (they keep referring to the channel id).  The
runtime performs the same adoption lazily (``publishing.accounts``) for
databases that are created/upgraded through ``prepare_schema``.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0013_multiplatform_publishing"
down_revision = "0012_job_runtime_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publishing_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("external_account_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("handle", sa.String(200), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="connected"),
        sa.Column("granted_scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("restrictions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("platform", "external_account_id", name="uq_publishing_account_external"),
    )
    op.create_index("ix_publishing_accounts_platform", "publishing_accounts", ["platform"])
    op.create_index(
        "uq_publishing_account_default", "publishing_accounts", ["platform"], unique=True,
        sqlite_where=sa.text("is_default = 1"), postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "publishing_platform_configs",
        sa.Column("platform", sa.String(16), primary_key=True),
        sa.Column("values", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "social_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("external_account_id", sa.String(128), nullable=False),
        sa.Column("account_label", sa.String(200), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_title", sa.String(200), nullable=False, server_default=""),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("render_revision", sa.Integer(), nullable=False),
        sa.Column("render_sha256", sa.String(64), nullable=False),
        sa.Column("render_file_size", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=True),
        sa.Column("idempotency_key", sa.String(300), nullable=True, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="now"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schedule_timezone", sa.String(64), nullable=True),
        sa.Column("schedule_local_time", sa.String(32), nullable=True),
        sa.Column("metadata_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("remote_container_id", sa.String(128), nullable=True),
        sa.Column("remote_post_id", sa.String(128), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("remote_status", sa.String(48), nullable=True),
        sa.Column("bytes_uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upload_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("publish_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("events", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, columns in (
        ("ix_social_publications_platform", ["platform"]),
        ("ix_social_publications_account_id", ["account_id"]),
        ("ix_social_publications_project_id", ["project_id"]),
        ("ix_social_publications_render_sha256", ["render_sha256"]),
        ("ix_social_publications_state", ["state"]),
        ("ix_social_publications_due", ["state", "scheduled_at"]),
        ("ix_social_publications_project", ["project_id", "platform"]),
    ):
        op.create_index(name, "social_publications", columns)

    # Adopt the legacy YouTube connection (non-destructive copy).
    bind = op.get_bind()
    legacy = sa.table(
        "youtube_connections",
        sa.column("slot"), sa.column("channel_id"), sa.column("channel_title"), sa.column("status"),
        sa.column("granted_scopes", sa.JSON()), sa.column("last_error_code"), sa.column("last_error_message"),
        sa.column("connected_at"),
    )
    accounts = sa.table(
        "publishing_accounts",
        sa.column("id"), sa.column("platform"), sa.column("external_account_id"), sa.column("display_name"),
        sa.column("status"), sa.column("granted_scopes", sa.JSON()), sa.column("restrictions", sa.JSON()),
        sa.column("details", sa.JSON()), sa.column("is_default"), sa.column("last_error_code"),
        sa.column("last_error_message"), sa.column("connected_at"), sa.column("updated_at"),
    )
    if not sa.inspect(bind).has_table("youtube_connections"):
        return
    now = datetime.now(UTC)
    default_taken = False
    for row in bind.execute(sa.select(legacy)).mappings().all():
        if not row["channel_id"]:
            continue
        primary = row["slot"] == "primary"
        active = (row["status"] or "connected") != "disconnected"
        bind.execute(accounts.insert().values(
            id=str(uuid.uuid4()),
            platform="youtube",
            external_account_id=row["channel_id"],
            display_name=(row["channel_title"] or "")[:200],
            status=row["status"] or "connected",
            granted_scopes=row["granted_scopes"] or [],
            restrictions=[],
            details={"migrated_from": "youtube_connections", "legacy_slot": row["slot"], "legacy_secret": primary},
            is_default=bool(primary and active and not default_taken),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            connected_at=row["connected_at"] or now,
            updated_at=now,
        ))
        default_taken = default_taken or (primary and active)


def downgrade() -> None:
    # The legacy youtube_connections row was never modified by the upgrade.
    op.drop_table("social_publications")
    op.drop_table("publishing_platform_configs")
    op.drop_index("uq_publishing_account_default", table_name="publishing_accounts")
    op.drop_index("ix_publishing_accounts_platform", table_name="publishing_accounts")
    op.drop_table("publishing_accounts")
