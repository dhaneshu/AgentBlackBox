from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .app import create_app
from .artifacts import ArtifactStore
from .config import ServerSettings
from .errors import Problem
from .ingestion import EventIngestor, redact, register_artifact_rollback_cleanup
from .jobs import JobQueue
from .models import (
    Artifact,
    ArtifactDeletion,
    AuditEvent,
    Event,
    Job,
    Project,
    Run,
    Span,
)
from .retention import RetentionWorker

MAX_JOB_ITEMS = 1000


class LeaseLost(RuntimeError):
    pass


class _Heartbeat:
    def __init__(
        self,
        factory: sessionmaker[Session],
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        interval: float,
    ):
        self.factory = factory
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval = interval
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(self.interval * 2, 1))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.factory.begin() as db:
                    if not JobQueue(db).heartbeat_atomic(
                        self.job_id, self.worker_id, self.lease_seconds
                    ):
                        self.lost_event.set()
                        return
            except Exception:
                self.lost_event.set()
                return
            if self.stop_event.wait(self.interval):
                return


class JobWorker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        artifacts: ArtifactStore,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        heartbeat_interval: float | None = None,
    ):
        self.factory = factory
        self.artifacts = artifacts
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval or max(1.0, lease_seconds / 3)
        self.handlers: dict[str, Callable[[Session, Job], None]] = {
            "project_event": self._project_event,
            "projection": self._project_event,
            "execute_run": self._execute_run,
            "comparison": self._comparison,
            "export": self._export,
            "import": self._import,
            "retention": self._retention,
            "delete_run": self._delete_run,
            "delete_workspace": self._delete_workspace,
            "artifact_delete": self._artifact_delete,
        }

    def run_once(self, limit: int = 10) -> int:
        with self.factory.begin() as db:
            queue = JobQueue(db)
            queue.recover_expired()
            job_ids = [
                job.id
                for job in queue.lease(
                    self.worker_id, limit=limit, lease_seconds=self.lease_seconds
                )
            ]
        heartbeats = {
            job_id: _Heartbeat(
                self.factory,
                job_id,
                self.worker_id,
                self.lease_seconds,
                self.heartbeat_interval,
            )
            for job_id in job_ids
        }
        for heartbeat in heartbeats.values():
            heartbeat.start()
        for job_id in job_ids:
            heartbeat = heartbeats[job_id]
            try:
                self._process(job_id, heartbeat)
            finally:
                heartbeat.stop()
        return len(job_ids)

    def _process(self, job_id: str, heartbeat: _Heartbeat) -> None:
        db = self.factory()
        workspace_id: str | None = None
        try:
            job = JobQueue(db)._owned(job_id, self.worker_id)
            workspace_id = job.workspace_id
            if job.cancel_requested:
                JobQueue(db).complete(job.id, self.worker_id)
                db.commit()
                return
            handler = self.handlers.get(job.job_type)
            if handler is None:
                raise ValueError(f"unsupported job type: {job.job_type}")
            handler(db, job)
            if heartbeat.lost_event.is_set():
                raise LeaseLost("lease heartbeat was lost")
            JobQueue(db).complete(job.id, self.worker_id)
            db.commit()
        except LeaseLost:
            db.rollback()
            cleanup_failures = db.info.pop(
                "_artifact_rollback_cleanup_failures", []
            )
            with self.factory.begin() as cancelled_db:
                self._persist_cleanup_failures(
                    cancelled_db, workspace_id, cleanup_failures
                )
                cancelled = cancelled_db.scalar(
                    select(Job).where(
                        Job.id == job_id,
                        Job.status == "running",
                        Job.lease_owner == self.worker_id,
                        Job.cancel_requested.is_(True),
                    )
                )
                if cancelled is not None:
                    JobQueue(cancelled_db).complete(job_id, self.worker_id)
        except Exception as exc:
            job_type = None
            db.rollback()
            cleanup_failures = db.info.pop(
                "_artifact_rollback_cleanup_failures", []
            )
            with self.factory.begin() as failure_db:
                self._persist_cleanup_failures(
                    failure_db, workspace_id, cleanup_failures
                )
                queue = JobQueue(failure_db)
                try:
                    failed_job = queue._owned(job_id, self.worker_id)
                    job_type = failed_job.job_type
                    queue.fail(
                        job_id,
                        self.worker_id,
                        f"{type(exc).__name__}: job handler failed",
                    )
                    if job_type == "artifact_delete":
                        self._record_artifact_failure(failure_db, failed_job)
                except Problem:
                    pass
        finally:
            db.close()

    def _project_event(self, db: Session, job: Job) -> None:
        event = db.scalar(
            select(Event).where(
                Event.id == job.payload["event_id"],
                Event.workspace_id == job.workspace_id,
            )
        )
        if event is None:
            raise ValueError("event no longer exists")
        payload: dict[str, Any] = event.payload
        if event.artifact_id:
            artifact = db.scalar(
                select(Artifact).where(
                    Artifact.id == event.artifact_id,
                    Artifact.workspace_id == job.workspace_id,
                )
            )
            if artifact is None:
                raise ValueError("offloaded event artifact no longer exists")
            payload = json.loads(self.artifacts.get(artifact.storage_key, artifact.sha256))
        if event.event_type == "span":
            db.add(
                Span(
                    id=payload.get("id"),
                    workspace_id=job.workspace_id,
                    run_id=event.run_id or payload["run_id"],
                    trace_id=payload["trace_id"],
                    parent_span_id=payload.get("parent_span_id"),
                    kind=payload.get("kind", "custom"),
                    attributes=payload.get("attributes", {}),
                )
            )
        elif event.event_type == "run.status" and event.run_id:
            run = db.scalar(
                select(Run).where(
                    Run.id == event.run_id, Run.workspace_id == job.workspace_id
                )
            )
            if run:
                run.status = str(payload["status"])

    def _execute_run(self, db: Session, job: Job) -> None:
        run_id = str(job.payload["run_id"])
        run = db.scalar(
            select(Run).where(Run.id == run_id, Run.workspace_id == job.workspace_id)
        )
        if run is None:
            raise ValueError("run does not belong to job workspace")
        events = job.payload.get("events", [])
        if not isinstance(events, list) or len(events) > MAX_JOB_ITEMS:
            raise ValueError("run events exceed bounded job limit")
        ingestor = EventIngestor(db, self.artifacts)
        for index, event in enumerate(events):
            ingestor.ingest(
                workspace_id=job.workspace_id,
                event_id=str(event["event_id"]),
                idempotency_key=str(event.get("idempotency_key", f"{job.id}:{index}")),
                event_type=str(event["type"]),
                payload=event.get("payload", {}),
                run_id=run.id,
            )
        run.status = "succeeded"
        run.metadata_json = {
            **run.metadata_json,
            "execution": {"event_count": len(events), "job_id": job.id},
        }

    def _comparison(self, db: Session, job: Job) -> None:
        before_id = str(job.payload["before_run_id"])
        after_id = str(job.payload["after_run_id"])
        runs = list(
            db.scalars(
                select(Run).where(
                    Run.workspace_id == job.workspace_id,
                    Run.id.in_([before_id, after_id]),
                )
            )
        )
        if len(runs) != 2:
            raise ValueError("comparison runs must belong to the job workspace")
        by_id = {run.id: run for run in runs}
        job.payload = {
            **job.payload,
            "result": {
                "before_status": by_id[before_id].status,
                "after_status": by_id[after_id].status,
                "status_changed": by_id[before_id].status != by_id[after_id].status,
            },
        }

    def _export(self, db: Session, job: Job) -> None:
        if artifact_id := job.payload.get("artifact_id"):
            if db.scalar(
                select(Artifact.id).where(
                    Artifact.id == artifact_id,
                    Artifact.workspace_id == job.workspace_id,
                )
            ):
                return
        runs = list(
            db.scalars(
                select(Run)
                .where(Run.workspace_id == job.workspace_id)
                .order_by(Run.created_at, Run.id)
                .limit(MAX_JOB_ITEMS + 1)
            )
        )
        if len(runs) > MAX_JOB_ITEMS:
            raise ValueError("workspace export exceeds bounded job limit")
        data = json.dumps(
            {
                "schema_version": "2.0",
                "runs": [
                    {"id": run.id, "status": run.status, "metadata": run.metadata_json}
                    for run in runs
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        key = f"{job.workspace_id}/exports/{job.id}.json"
        artifact = db.scalar(
            select(Artifact).where(
                Artifact.workspace_id == job.workspace_id, Artifact.storage_key == key
            )
        )
        if artifact is None:
            artifact = Artifact(
                workspace_id=job.workspace_id,
                storage_key=key,
                sha256=hashlib.sha256(data).hexdigest(),
                mime_type="application/json",
                size=len(data),
            )
            db.add(artifact)
            db.flush()
            stored = self.artifacts.put(key, data, "application/json")
            if stored.created:
                register_artifact_rollback_cleanup(db, self.artifacts, key)
        else:
            stored = self.artifacts.put(key, data, "application/json")
            if (
                artifact.sha256 != stored.sha256
                or artifact.mime_type != stored.mime_type
                or artifact.size != stored.size
            ):
                raise ValueError("existing export artifact metadata does not match")
        job.payload = {**job.payload, "artifact_id": artifact.id}

    def _persist_cleanup_failures(
        self, db: Session, workspace_id: str | None, keys: list[str]
    ) -> None:
        if workspace_id is None:
            return
        for key in keys:
            intent = db.scalar(
                select(ArtifactDeletion).where(
                    ArtifactDeletion.workspace_id == workspace_id,
                    ArtifactDeletion.storage_key == key,
                )
            )
            if intent is None:
                intent = ArtifactDeletion(
                    workspace_id=workspace_id,
                    storage_key=key,
                    sha256="unknown",
                    status="retry",
                    last_error="rollback artifact cleanup failed",
                )
                db.add(intent)
                db.flush()
            if db.scalar(
                select(Job.id).where(
                    Job.workspace_id == workspace_id,
                    Job.idempotency_key == f"artifact-delete:{intent.id}",
                )
            ) is None:
                db.add(
                    Job(
                        workspace_id=workspace_id,
                        job_type="artifact_delete",
                        idempotency_key=f"artifact-delete:{intent.id}",
                        payload={"intent_id": intent.id},
                    )
                )

    def _import(self, db: Session, job: Job) -> None:
        events = job.payload.get("events")
        if not isinstance(events, list) or len(events) > MAX_JOB_ITEMS:
            raise ValueError("import events are required and bounded")
        ingestor = EventIngestor(db, self.artifacts)
        imported = 0
        for index, event in enumerate(events):
            result = ingestor.ingest(
                workspace_id=job.workspace_id,
                event_id=str(event["event_id"]),
                idempotency_key=str(event.get("idempotency_key", f"{job.id}:{index}")),
                event_type=str(event["type"]),
                payload=event.get("payload", {}),
                run_id=event.get("run_id"),
            )
            imported += not result.duplicate
        job.payload = {**job.payload, "imported": imported}

    def _retention(self, db: Session, job: Job) -> None:
        RetentionWorker(db, self.artifacts).apply(job.workspace_id)

    def _delete_run(self, db: Session, job: Job) -> None:
        RetentionWorker(db, self.artifacts).delete_run(
            job.workspace_id,
            str(job.payload["run_id"]),
            str(job.payload.get("actor", self.worker_id)),
        )

    def _delete_workspace(self, db: Session, job: Job) -> None:
        RetentionWorker(db, self.artifacts).delete_workspace(
            job.workspace_id,
            str(job.payload.get("actor", self.worker_id)),
            preserve_job_id=job.id,
        )

    def _artifact_delete(self, db: Session, job: Job) -> None:
        intent = db.scalar(
            select(ArtifactDeletion).where(
                ArtifactDeletion.id == job.payload["intent_id"],
                ArtifactDeletion.workspace_id == job.workspace_id,
            )
        )
        if intent is None or intent.status == "succeeded":
            return
        self.artifacts.delete(intent.storage_key)
        intent.status = "succeeded"
        intent.attempts += 1
        intent.last_error = None
        db.add(
            AuditEvent(
                workspace_id=job.workspace_id,
                action="artifact.deleted",
                actor=self.worker_id,
                target_type="artifact",
                target_id=intent.id,
                outcome="succeeded",
            )
        )

    def _record_artifact_failure(self, db: Session, job: Job) -> None:
        intent = db.scalar(
            select(ArtifactDeletion).where(
                ArtifactDeletion.id == job.payload.get("intent_id"),
                ArtifactDeletion.workspace_id == job.workspace_id,
            )
        )
        if intent:
            intent.status = "retry"
            intent.attempts += 1
            intent.last_error = "artifact store deletion failed"
            db.add(
                AuditEvent(
                    workspace_id=job.workspace_id,
                    action="artifact.delete_failed",
                    actor=self.worker_id,
                    target_type="artifact",
                    target_id=intent.id,
                    outcome="retry",
                )
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blackbox-worker")
    parser.add_argument("--worker-id", default=f"worker-{uuid.uuid4()}")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args(argv)
    app = create_app(ServerSettings())
    worker = JobWorker(app.state.session_factory, app.state.artifact_store, args.worker_id)
    while True:
        processed = worker.run_once(args.batch_size)
        if args.once:
            return 0
        if not processed:
            time.sleep(max(0.1, args.poll_seconds))
