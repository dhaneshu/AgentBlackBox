from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import ServerSettings


def create_database(settings: ServerSettings) -> tuple[Engine, sessionmaker[Session]]:
    kwargs = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if settings.database_url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, factory


def session_dependency(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


def set_workspace_context(session: Session, workspace_id: str) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.connection().exec_driver_sql(
            "SELECT set_config('app.workspace_id', %s, true)", (workspace_id,)
        )
