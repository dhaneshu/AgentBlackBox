from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event as sa_event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .artifacts import ArtifactStore
from .errors import Problem
from .models import Artifact, Event, Job, Run

SENSITIVE = re.compile(r"(authorization|api[-_]?key|password|secret|token)", re.I)


def redact(value: Any) -> tuple[Any, bool]:
    changed = False
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if SENSITIVE.search(str(key)):
                result[key] = "[REDACTED]"
                changed = True
            else:
                result[key], child_changed = redact(child)
                changed |= child_changed
        return result, changed
    if isinstance(value, list):
        result = []
        for child in value:
            redacted, child_changed = redact(child)
            result.append(redacted)
            changed |= child_changed
        return result, changed
    return value, False


@dataclass
class IngestionResult:
    event: Event
    duplicate: bool


def register_artifact_rollback_cleanup(
    session: Session, artifacts: ArtifactStore, key: str
) -> None:
    cleanup = session.info.setdefault("_artifact_rollback_cleanup", [])
    cleanup.append((artifacts, key))
    if session.info.get("_artifact_cleanup_listeners"):
        return
    session.info["_artifact_cleanup_listeners"] = True

    @sa_event.listens_for(session, "after_rollback")
    def cleanup_after_rollback(rolled_back_session: Session) -> None:
        pending = rolled_back_session.info.pop("_artifact_rollback_cleanup", [])
        failures = rolled_back_session.info.setdefault(
            "_artifact_rollback_cleanup_failures", []
        )
        for store, staged_key in pending:
            try:
                store.delete(staged_key)
            except Exception:
                failures.append(staged_key)

    @sa_event.listens_for(session, "after_commit")
    def clear_after_commit(committed_session: Session) -> None:
        committed_session.info.pop("_artifact_rollback_cleanup", None)


class EventIngestor:
    def __init__(
        self,
        session: Session,
        artifacts: ArtifactStore,
        *,
        max_bytes: int = 4_194_304,
        offload_bytes: int = 262_144,
    ):
        self.session = session
        self.artifacts = artifacts
        self.max_bytes = max_bytes
        self.offload_bytes = offload_bytes

    def ingest(
        self,
        *,
        workspace_id: str,
        event_id: str,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> IngestionResult:
        if not workspace_id or not event_id or not idempotency_key:
            raise Problem(422, "Invalid event", "workspace_id, event_id and idempotency key are required.")
        if run_id:
            run_query = select(Run.id).where(
                Run.workspace_id == workspace_id, Run.id == run_id
            )
            if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
                run_query = run_query.with_for_update(key_share=True)
            if self.session.scalar(run_query) is None:
                raise Problem(
                    422,
                    "Invalid parent",
                    "run_id does not belong to the event workspace.",
                )
        cleaned, was_redacted = redact(payload)
        encoded = json.dumps(cleaned, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > self.max_bytes:
            raise Problem(413, "Event too large", "Event exceeds the configured maximum size.")
        event_row_id = str(uuid.uuid4())
        values = {
            "id": event_row_id,
            "workspace_id": workspace_id,
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "event_type": event_type,
            "run_id": run_id,
            "payload": cleaned if len(encoded) <= self.offload_bytes else {"offloaded": True},
            "redacted": was_redacted,
        }
        dialect = self.session.bind.dialect.name if self.session.bind is not None else ""
        insert_factory = pg_insert if dialect == "postgresql" else sqlite_insert
        if dialect in {"postgresql", "sqlite"}:
            claimed_id = self.session.scalar(
                insert_factory(Event)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(Event.id)
            )
        else:
            claimed_id = None
            try:
                with self.session.begin_nested():
                    self.session.add(Event(**values))
                    self.session.flush()
                claimed_id = event_row_id
            except IntegrityError:
                pass
        if claimed_id is None:
            existing = self.session.scalar(
                select(Event).where(
                    Event.workspace_id == workspace_id,
                    Event.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.event_id != event_id:
                    raise Problem(
                        409,
                        "Idempotency conflict",
                        "The key was used for another event.",
                    )
                return IngestionResult(existing, True)
            raise Problem(409, "Event ID conflict", "The event ID already exists.")
        event = self.session.get(Event, claimed_id)
        assert event is not None
        artifact_id = None
        if len(encoded) > self.offload_bytes:
            key = f"{workspace_id}/events/{event_id}.json"
            created = False
            try:
                stored = self.artifacts.put(key, encoded, "application/json")
                created = stored.created
                if stored.created:
                    register_artifact_rollback_cleanup(
                        self.session, self.artifacts, key
                    )
                artifact = Artifact(
                    workspace_id=workspace_id,
                    storage_key=stored.key,
                    sha256=stored.sha256,
                    mime_type=stored.mime_type,
                    size=stored.size,
                )
                self.session.add(artifact)
                self.session.flush()
                artifact_id = artifact.id
                event.artifact_id = artifact_id
                event.payload = {"offloaded": True, "artifact_id": artifact.id}
            except Exception:
                if created:
                    self.artifacts.delete(key)
                raise
        self.session.execute(
            insert_factory(Job)
            .values(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                job_type="project_event",
                idempotency_key=f"project:{event.id}",
                payload={"event_id": event.id},
            )
            .on_conflict_do_nothing()
        )
        self.session.flush()
        return IngestionResult(event, False)
