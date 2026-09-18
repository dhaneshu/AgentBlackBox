from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Generic, TypeVar

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .db import set_workspace_context
from .errors import Problem
from .models import Base, WorkspaceOwned

T = TypeVar("T", bound=Base)


def encode_cursor(created_at: datetime, identifier: str) -> str:
    raw = json.dumps([created_at.isoformat(), identifier], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        timestamp, identifier = json.loads(raw)
        return datetime.fromisoformat(timestamp), str(identifier)
    except Exception as exc:
        raise Problem(400, "Invalid cursor", "The pagination cursor is malformed.") from exc


class WorkspaceRepository(Generic[T]):
    def __init__(self, session: Session, model: type[T], workspace_id: str):
        if not workspace_id:
            raise ValueError("workspace_id is mandatory")
        if not issubclass(model, WorkspaceOwned):
            raise TypeError("workspace repositories require a WorkspaceOwned model")
        self.session = session
        self.model = model
        self.workspace_id = workspace_id
        set_workspace_context(session, workspace_id)

    def get(self, identifier: str) -> T:
        item = self.session.scalar(
            select(self.model).where(
                self.model.id == identifier,
                self.model.workspace_id == self.workspace_id,
            )
        )
        if item is None:
            raise Problem(404, "Not found", "The requested resource was not found.")
        return item

    def add(self, item: T) -> T:
        if item.workspace_id != self.workspace_id:
            raise Problem(403, "Workspace mismatch", "Cross-workspace writes are forbidden.")
        self.session.add(item)
        self.session.flush()
        return item

    def delete(self, identifier: str) -> None:
        self.session.delete(self.get(identifier))

    def page(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[T], str | None]:
        limit = max(1, min(limit, 100))
        statement: Select[tuple[T]] = select(self.model).where(
            self.model.workspace_id == self.workspace_id
        )
        if cursor:
            created_at, identifier = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    self.model.created_at > created_at,
                    and_(
                        self.model.created_at == created_at,
                        self.model.id > identifier,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(self.model.created_at, self.model.id).limit(limit + 1)
            )
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(last.created_at, last.id)
            rows = rows[:limit]
        return rows, next_cursor


class UnitOfWork(AbstractContextManager["UnitOfWork"]):
    def __init__(self, factory: sessionmaker[Session], workspace_id: str):
        if not workspace_id:
            raise ValueError("workspace_id is mandatory")
        self.factory = factory
        self.workspace_id = workspace_id
        self.session: Session | None = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self.factory()
        set_workspace_context(self.session, self.workspace_id)
        return self

    def repository(self, model: type[T]) -> WorkspaceRepository[T]:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        return WorkspaceRepository(self.session, model, self.workspace_id)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        assert self.session is not None
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
            self.session = None
        return False
