from collections.abc import Generator

from sqlalchemy import create_engine, event
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


def ensure_runtime_schema() -> None:
    """Add protections to databases first created through metadata.create_all()."""
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
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
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS protect_revision_delete
            BEFORE DELETE ON project_revisions
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
