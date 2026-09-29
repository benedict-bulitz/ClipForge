from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


YOUTUBE_UPLOAD_V2_COLUMNS = (
    ("source_kind", "VARCHAR(16)"),
    ("made_for_kids", "BOOLEAN"),
    ("made_for_kids_confirmed", "BOOLEAN"),
    ("contains_synthetic_media", "BOOLEAN"),
    ("requested_visibility", "VARCHAR(16) NOT NULL DEFAULT 'private'"),
    ("visibility_restricted", "BOOLEAN NOT NULL DEFAULT 0"),
    ("notify_subscribers", "BOOLEAN"),
    ("upload_settings", "JSON NOT NULL DEFAULT '{}'"),
    ("schedule_local_time", "VARCHAR(16)"),
    ("schedule_timezone", "VARCHAR(64)"),
    ("thumbnail_source", "VARCHAR(24)"),
    ("thumbnail_asset", "VARCHAR(300)"),
    ("thumbnail_sha256", "VARCHAR(64)"),
    ("thumbnail_upload_status", "VARCHAR(16) NOT NULL DEFAULT 'none'"),
    ("thumbnail_failure_reason", "TEXT"),
    ("thumbnail_applied_at", "DATETIME"),
    # V3 remote status reconciliation
    ("remote_privacy_status", "VARCHAR(16)"),
    ("remote_publish_at", "DATETIME"),
    ("remote_published_at", "DATETIME"),
    ("first_observed_public_at", "DATETIME"),
    ("remote_view_count", "INTEGER"),
    ("remote_like_count", "INTEGER"),
    ("remote_comment_count", "INTEGER"),
    ("remote_status_checked_at", "DATETIME"),
    ("remote_status_attempted_at", "DATETIME"),
    ("remote_status_error_code", "VARCHAR(48)"),
    ("remote_status_error", "TEXT"),
    ("last_analytics_attempt_at", "DATETIME"),
    # Smart Slot Planner provenance
    ("schedule_source", "VARCHAR(16)"),
    ("schedule_slot_time", "VARCHAR(5)"),
)


def prepare_schema(bind: Engine | None = None) -> None:
    """The startup schema path: create missing tables, then upgrade existing ones.

    ``create_all`` adds tables that do not exist yet (e.g. the Smart Slot
    Planner tables on a database from before it); ``ensure_runtime_schema``
    adds columns to tables that already existed.
    """
    target = bind or engine
    Base.metadata.create_all(bind=target)
    ensure_runtime_schema(target)


def ensure_runtime_schema(bind: Engine | None = None) -> None:
    """Add protections to databases first created through metadata.create_all()."""
    target = bind or engine
    if target.dialect.name != "sqlite":
        return
    with target.begin() as connection:
        inspector = inspect(connection)
        project_columns = {column["name"] for column in inspector.get_columns("projects")}
        revision_columns = {
            column["name"] for column in inspector.get_columns("project_revisions")
        }
        generation_job_columns = {
            column["name"] for column in inspector.get_columns("generation_jobs")
        } if inspector.has_table("generation_jobs") else set()
        if generation_job_columns and "request_payload" not in generation_job_columns:
            connection.exec_driver_sql(
                "ALTER TABLE generation_jobs ADD COLUMN request_payload JSON NOT NULL DEFAULT '{}'"
            )
        if inspector.has_table("youtube_uploads"):
            # YouTube publishing V2 columns for databases created by V1.
            upload_columns = {column["name"] for column in inspector.get_columns("youtube_uploads")}
            for name, ddl in YOUTUBE_UPLOAD_V2_COLUMNS:
                if name not in upload_columns:
                    connection.exec_driver_sql(f"ALTER TABLE youtube_uploads ADD COLUMN {name} {ddl}")
        if "active_tip_revision" not in project_columns:
            connection.exec_driver_sql(
                "ALTER TABLE projects ADD COLUMN active_tip_revision INTEGER NOT NULL DEFAULT 1"
            )
            connection.exec_driver_sql(
                "UPDATE projects SET active_tip_revision = current_revision"
            )
        if "kind" not in revision_columns:
            connection.exec_driver_sql(
                "ALTER TABLE project_revisions ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'user'"
            )
            connection.exec_driver_sql(
                "UPDATE project_revisions SET kind = 'initial' WHERE parent_revision IS NULL"
            )
            connection.exec_driver_sql(
                "UPDATE project_revisions SET kind = 'system' "
                "WHERE instruction IN ('Render video', 'Export MP4', "
                "'Clean exported project media')"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_revision_number "
            "ON project_revisions (project_id, number)"
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS protect_project_prompt_update
            BEFORE UPDATE OF original_prompt ON projects
            WHEN NEW.original_prompt != OLD.original_prompt
            BEGIN SELECT RAISE(ABORT, 'original_prompt is immutable'); END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS protect_revision_update
            BEFORE UPDATE ON project_revisions
            BEGIN SELECT RAISE(ABORT, 'project revisions are append-only'); END
            """
        )
        # Direct revision deletion remains forbidden, while the foreign-key
        # cascade from deleting its parent project is deliberately allowed.
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS protect_revision_delete")
        connection.exec_driver_sql(
            """
            CREATE TRIGGER protect_revision_delete
            BEFORE DELETE ON project_revisions
            WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
            BEGIN SELECT RAISE(ABORT, 'project revisions are append-only'); END
            """
        )
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
