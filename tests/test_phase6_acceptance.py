from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import blackbox_server.api as api_module
from blackbox_server.api import status_stream
from blackbox_server.app import create_app
from blackbox_server.artifacts import LocalArtifactStore
from blackbox_server.config import ServerSettings
from blackbox_server.ingestion import EventIngestor
from blackbox_server.jobs import JobQueue, utcnow
from blackbox_server.models import (
    Artifact,
    ArtifactDeletion,
    AuditEvent,
    Base,
    Conversation,
    Dataset,
    DatasetVersion,
    EvaluatorDefinition,
    EvaluatorResult,
    Event,
    Experiment,
    Job,
    Project,
    Review,
    Run,
    Span,
    Turn,
    Workspace,
)
from blackbox_server.retention import RetentionWorker
from blackbox_server.workers import JobWorker, MAX_JOB_ITEMS


def test_local_artifact_store_reports_link_race_as_existing(tmp_path, monkeypatch):
    artifact_store = LocalArtifactStore(tmp_path, signing_key=b"x" * 32)
    key = "workspace/exports/raced.json"
    data = b'{"schema_version":"2.0"}'
    original_link = os.link

    def competing_link(source, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        raise FileExistsError

    monkeypatch.setattr(os, "link", competing_link)
    stored = artifact_store.put(key, data, "application/json")
    monkeypatch.setattr(os, "link", original_link)

    assert stored.created is False
    assert artifact_store.get(key) == data

ROOT = Path(".phase6-acceptance-artifacts")
DB_PATH = Path(".phase6-acceptance.db")
KEY = b"acceptance-only-signing-key-over-32-bytes"


@pytest.fixture(autouse=True)
def cleanup():
    shutil.rmtree(ROOT, ignore_errors=True)
    DB_PATH.unlink(missing_ok=True)
    yield
    shutil.rmtree(ROOT, ignore_errors=True)
    DB_PATH.unlink(missing_ok=True)


@pytest.fixture
def database():
    engine = create_engine(
        f"sqlite+pysqlite:///{DB_PATH.resolve()}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    sqlalchemy_event.listen(
        engine,
        "connect",
        lambda connection, record: connection.execute("PRAGMA foreign_keys=ON"),
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def seed(factory):
    with factory.begin() as db:
        first, second = Workspace(name="first"), Workspace(name="second")
        db.add_all([first, second])
        db.flush()
        return first.id, second.id


def store() -> LocalArtifactStore:
    return LocalArtifactStore(ROOT, signing_key=KEY)


def test_production_signing_configuration_and_rotation():
    with pytest.raises(ValueError, match="required"):
        ServerSettings(environment="production")
    secret = "do-not-log-" + "x" * 32
    settings = ServerSettings(
        environment="production", local_signing_secret=secret
    )
    assert secret not in repr(settings)
    with pytest.raises(ValueError, match="at least 32"):
        ServerSettings(environment="production", local_signing_secret="short")
    old_key = b"old-signing-key-for-rotation-32-bytes"
    old = LocalArtifactStore(ROOT, old_key)
    signed = old.signed_url("workspace/object")
    query = dict(
        item.split("=", 1) for item in signed.split("?", 1)[1].split("&")
    )
    rotated = LocalArtifactStore(ROOT, KEY, (old_key,))
    assert rotated.verify_signature(
        "workspace/object", int(query["expires"]), query["signature"]
    )
    assert create_app(ServerSettings(environment="test"))


def test_composite_foreign_keys_reject_every_cross_workspace_parent(database):
    _, factory = database
    first, second = seed(factory)
    with factory.begin() as db:
        project = Project(workspace_id=first, name="project")
        dataset = Dataset(workspace_id=first, name="dataset")
        experiment = Experiment(workspace_id=first, name="experiment")
        run = Run(workspace_id=first)
        second_run = Run(workspace_id=second)
        evaluator = EvaluatorDefinition(
            workspace_id=first, name="evaluator", implementation="module:function"
        )
        artifact = Artifact(
            workspace_id=first,
            storage_key=f"{first}/artifact",
            sha256="a" * 64,
            mime_type="application/json",
            size=2,
        )
        db.add_all([project, dataset, experiment, run, second_run, evaluator, artifact])
        db.flush()
        conversation = Conversation(workspace_id=first, run_id=run.id)
        parent_span = Span(
            workspace_id=first, run_id=run.id, trace_id="trace", kind="root"
        )
        db.add_all([conversation, parent_span])
        db.flush()
        ids = {
            "project": project.id,
            "dataset": dataset.id,
            "experiment": experiment.id,
            "run": run.id,
            "second_run": second_run.id,
            "conversation": conversation.id,
            "span": parent_span.id,
            "evaluator": evaluator.id,
            "artifact": artifact.id,
        }

    children = [
        lambda: Experiment(workspace_id=second, name="x", project_id=ids["project"]),
        lambda: DatasetVersion(
            workspace_id=second,
            dataset_id=ids["dataset"],
            digest="b" * 64,
            document={},
        ),
        lambda: Run(workspace_id=second, experiment_id=ids["experiment"]),
        lambda: Conversation(workspace_id=second, run_id=ids["run"]),
        lambda: Turn(
            workspace_id=second,
            conversation_id=ids["conversation"],
            sequence=1,
            payload={},
        ),
        lambda: Span(
            workspace_id=second,
            run_id=ids["run"],
            trace_id="other",
            kind="child",
        ),
        lambda: Span(
            workspace_id=second,
            run_id=ids["second_run"],
            trace_id="other",
            kind="child",
            parent_span_id=ids["span"],
        ),
        lambda: Event(
            workspace_id=second,
            event_id="foreign-run",
            idempotency_key="foreign-run",
            event_type="custom",
            run_id=ids["run"],
        ),
        lambda: Event(
            workspace_id=second,
            event_id="foreign-artifact",
            idempotency_key="foreign-artifact",
            event_type="custom",
            artifact_id=ids["artifact"],
        ),
        lambda: EvaluatorResult(
            workspace_id=second,
            run_id=ids["run"],
            evaluator_id=ids["evaluator"],
            result={},
        ),
        lambda: Review(workspace_id=second, run_id=ids["run"]),
    ]
    for make_child in children:
        with pytest.raises(IntegrityError):
            with factory.begin() as db:
                db.add(make_child())


def test_ingestion_rejects_foreign_run_and_cleans_rollback(database):
    _, factory = database
    first, second = seed(factory)
    with factory.begin() as db:
        run = Run(workspace_id=first)
        db.add(run)
        db.flush()
        run_id = run.id
    with factory() as db:
        with pytest.raises(Exception, match="does not belong"):
            EventIngestor(db, store()).ingest(
                workspace_id=second,
                event_id="bad",
                idempotency_key="bad",
                event_type="custom",
                payload={},
                run_id=run_id,
            )
    db = factory()
    try:
        EventIngestor(db, store(), offload_bytes=1).ingest(
            workspace_id=first,
            event_id="rollback",
            idempotency_key="rollback",
            event_type="custom",
            payload={"large": "payload"},
        )
        key = f"{first}/events/rollback.json"
        assert store().get(key)
        db.rollback()
        with pytest.raises(FileNotFoundError):
            store().get(key)
    finally:
        db.close()


def test_concurrent_ingestion_creates_one_event_job_and_artifact(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    errors: list[Exception] = []

    def ingest() -> None:
        try:
            with factory() as db:
                barrier.wait()
                result = EventIngestor(db, store(), offload_bytes=1).ingest(
                    workspace_id=workspace_id,
                    event_id="concurrent",
                    idempotency_key="concurrent",
                    event_type="custom",
                    payload={"value": "large"},
                )
                db.commit()
                outcomes.append(result.duplicate)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ingest) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert sorted(outcomes) == [False, True]
    with factory() as db:
        assert len(list(db.scalars(select(Event)))) == 1
        assert len(list(db.scalars(select(Job)))) == 1
        assert len(list(db.scalars(select(Artifact)))) == 1
        artifact = db.scalar(select(Artifact))
        assert store().get(artifact.storage_key, artifact.sha256)


def test_all_advertised_jobs_have_bounded_working_handlers(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        before = Run(workspace_id=workspace_id, status="failed")
        after = Run(workspace_id=workspace_id, status="succeeded")
        execute = Run(workspace_id=workspace_id)
        db.add_all([before, after, execute])
        db.flush()
        jobs = [
            Job(
                workspace_id=workspace_id,
                job_type="execute_run",
                idempotency_key="execute",
                payload={"run_id": execute.id, "events": []},
            ),
            Job(
                workspace_id=workspace_id,
                job_type="comparison",
                idempotency_key="compare",
                payload={"before_run_id": before.id, "after_run_id": after.id},
            ),
            Job(
                workspace_id=workspace_id,
                job_type="export",
                idempotency_key="export",
                payload={},
            ),
            Job(
                workspace_id=workspace_id,
                job_type="import",
                idempotency_key="import",
                payload={
                    "events": [
                        {"event_id": "imported", "type": "custom", "payload": {}}
                    ]
                },
            ),
        ]
        db.add_all(jobs)
        db.flush()
        job_ids = [job.id for job in jobs]
    assert JobWorker(factory, store(), "worker").run_once() == 4
    with factory() as db:
        completed = [db.get(Job, identifier) for identifier in job_ids]
        assert {job.status for job in completed} == {"succeeded"}
        assert completed[1].payload["result"]["status_changed"]
        assert completed[2].payload["artifact_id"]
        assert completed[3].payload["imported"] == 1
    with factory.begin() as db:
        db.add(
            Job(
                workspace_id=workspace_id,
                job_type="import",
                idempotency_key="too-large",
                payload={"events": [{}] * (MAX_JOB_ITEMS + 1)},
                max_attempts=1,
            )
        )
    JobWorker(factory, store(), "bounded").run_once()
    with factory() as db:
        bounded = db.scalar(select(Job).where(Job.idempotency_key == "too-large"))
        assert bounded.status == "dead_letter"


def test_offloaded_run_status_projection_uses_resolved_payload(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        run = Run(workspace_id=workspace_id)
        db.add(run)
        db.flush()
        EventIngestor(db, store(), offload_bytes=1).ingest(
            workspace_id=workspace_id,
            event_id="status",
            idempotency_key="status",
            event_type="run.status",
            payload={"status": "succeeded"},
            run_id=run.id,
        )
        run_id = run.id
    JobWorker(factory, store(), "projector").run_once()
    with factory() as db:
        assert db.get(Run, run_id).status == "succeeded"


def test_long_handler_heartbeats_prevent_second_worker_lease(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        job = Job(
            workspace_id=workspace_id, job_type="slow", idempotency_key="slow"
        )
        db.add(job)
        db.flush()
        job_id = job.id
    started = threading.Event()
    first = JobWorker(
        factory, store(), "first", lease_seconds=1, heartbeat_interval=0.05
    )

    def slow(db, job):
        db.add(Project(workspace_id=workspace_id, name="heartbeat-result"))
        started.set()
        time.sleep(1.2)

    first.handlers["slow"] = slow
    thread = threading.Thread(target=first.run_once)
    thread.start()
    assert started.wait(2)
    time.sleep(1.05)
    assert JobWorker(factory, store(), "second", lease_seconds=1).run_once() == 0
    thread.join(3)
    assert not thread.is_alive()
    with factory() as db:
        assert db.get(Job, job_id).status == "succeeded"
        assert db.scalar(select(Project).where(Project.name == "heartbeat-result"))


def test_concurrent_expired_recovery_is_atomic_and_worker_continues(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        job = Job(
            workspace_id=workspace_id,
            job_type="export",
            idempotency_key="expired",
            status="running",
            attempts=1,
            lease_owner="dead-worker",
            lease_expires_at=utcnow() - timedelta(seconds=5),
        )
        db.add(job)
        db.flush()
        job_id = job.id
    barrier = threading.Barrier(2)
    recovered: list[int] = []
    errors: list[Exception] = []

    def recover() -> None:
        try:
            with factory.begin() as db:
                barrier.wait()
                recovered.append(JobQueue(db).recover_expired())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=recover) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert sorted(recovered) == [0, 1]
    assert JobWorker(factory, store(), "replacement").run_once() == 1
    with factory() as db:
        assert db.get(Job, job_id).status == "succeeded"


def test_export_lease_loss_cleans_only_new_artifact(database, monkeypatch):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        new_job = Job(
            workspace_id=workspace_id,
            job_type="export",
            idempotency_key="new-export",
        )
        existing_job = Job(
            workspace_id=workspace_id,
            job_type="export",
            idempotency_key="existing-export",
        )
        db.add_all([new_job, existing_job])
        db.flush()
        new_key = f"{workspace_id}/exports/{new_job.id}.json"
        existing_key = f"{workspace_id}/exports/{existing_job.id}.json"
        existing_data = json.dumps(
            {"schema_version": "2.0", "runs": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        stored = store().put(existing_key, existing_data, "application/json")
        db.add(
            Artifact(
                workspace_id=workspace_id,
                storage_key=existing_key,
                sha256=stored.sha256,
                mime_type=stored.mime_type,
                size=stored.size,
            )
        )
    monkeypatch.setattr(
        JobQueue, "heartbeat_atomic", lambda *args, **kwargs: False
    )
    JobWorker(factory, store(), "lease-loser", heartbeat_interval=0.01).run_once()
    with pytest.raises(FileNotFoundError):
        store().get(new_key)
    assert store().get(existing_key) == existing_data
    with factory() as db:
        assert db.scalar(
            select(Artifact).where(Artifact.storage_key == new_key)
        ) is None
        assert db.scalar(
            select(Artifact).where(Artifact.storage_key == existing_key)
        )


def test_export_commit_failure_removes_new_artifact(database):
    engine, regular_factory = database
    workspace_id, _ = seed(regular_factory)

    class FailingCommitSession(Session):
        fail_next_commit = False

        def commit(self) -> None:
            if type(self).fail_next_commit:
                type(self).fail_next_commit = False
                raise RuntimeError("injected commit failure")
            super().commit()

    factory = sessionmaker(
        engine, class_=FailingCommitSession, expire_on_commit=False
    )
    with factory.begin() as db:
        job = Job(
            workspace_id=workspace_id,
            job_type="export",
            idempotency_key="commit-failure",
        )
        db.add(job)
        db.flush()
        job_id = job.id
        key = f"{workspace_id}/exports/{job.id}.json"
    FailingCommitSession.fail_next_commit = True
    assert JobWorker(factory, store(), "failing-commit").run_once() == 1
    with pytest.raises(FileNotFoundError):
        store().get(key)
    with regular_factory() as db:
        assert db.scalar(select(Artifact).where(Artifact.storage_key == key)) is None
        assert db.get(Job, job_id).status == "retry"


def test_lease_loss_rolls_back_and_never_completes(database, monkeypatch):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        job = Job(
            workspace_id=workspace_id, job_type="loses", idempotency_key="loses"
        )
        db.add(job)
        db.flush()
        job_id = job.id
    monkeypatch.setattr(JobQueue, "heartbeat_atomic", lambda *args, **kwargs: False)
    worker = JobWorker(factory, store(), "worker", heartbeat_interval=0.01)

    def handler(db, job):
        db.add(Project(workspace_id=workspace_id, name="must-rollback"))
        time.sleep(0.05)

    worker.handlers["loses"] = handler
    worker.run_once()
    with factory() as db:
        assert db.get(Job, job_id).status == "running"
        assert db.scalar(select(Project).where(Project.name == "must-rollback")) is None


def test_sse_session_sets_workspace_context(database, monkeypatch):
    _, factory = database
    workspace_id, _ = seed(factory)
    with factory.begin() as db:
        run = Run(workspace_id=workspace_id, status="succeeded")
        db.add(run)
        db.flush()
        run_id = run.id
    contexts = []
    monkeypatch.setattr(
        api_module,
        "set_workspace_context",
        lambda db, workspace: contexts.append(workspace),
    )

    class Request:
        class App:
            state = type("State", (), {"session_factory": factory})()

        app = App()

        async def is_disconnected(self):
            return False

    async def consume():
        generator = status_stream(Request(), Run, workspace_id, run_id, 0)
        return await anext(generator)

    assert "succeeded" in asyncio.run(consume())
    assert contexts == [workspace_id]


def test_retention_outbox_is_transactional_and_recovers(database):
    _, factory = database
    workspace_id, _ = seed(factory)
    local = store()
    stored = local.put(f"{workspace_id}/run.json", b"{}", "application/json")
    with factory.begin() as db:
        run = Run(workspace_id=workspace_id)
        artifact = Artifact(
            workspace_id=workspace_id,
            storage_key=stored.key,
            sha256=stored.sha256,
            mime_type=stored.mime_type,
            size=stored.size,
        )
        db.add_all([run, artifact])
        db.flush()
        db.add(
            Event(
                workspace_id=workspace_id,
                event_id="retained",
                idempotency_key="retained",
                event_type="custom",
                run_id=run.id,
                artifact_id=artifact.id,
            )
        )
        run_id = run.id
    db = factory()
    RetentionWorker(db, local).delete_run(workspace_id, run_id, "test")
    db.rollback()
    db.close()
    assert local.get(stored.key) == b"{}"
    with factory.begin() as db:
        RetentionWorker(db, local).delete_run(workspace_id, run_id, "test")
    with factory() as db:
        intent = db.scalar(select(ArtifactDeletion))
        assert intent.status == "pending"
        deletion_job = db.scalar(select(Job).where(Job.job_type == "artifact_delete"))
        deletion_job_id = deletion_job.id

    class FlakyStore:
        def __init__(self):
            self.fail = True

        def delete(self, key):
            if self.fail:
                self.fail = False
                raise OSError("injected")
            local.delete(key)

        def __getattr__(self, name):
            return getattr(local, name)

    flaky = FlakyStore()
    JobWorker(factory, flaky, "deletion-worker").run_once()
    with factory.begin() as db:
        intent = db.scalar(select(ArtifactDeletion))
        assert intent.status == "retry"
        assert intent.attempts == 1
        job = db.get(Job, deletion_job_id)
        job.available_at = utcnow()
    JobWorker(factory, flaky, "deletion-worker").run_once()
    with factory() as db:
        assert db.scalar(select(ArtifactDeletion)).status == "succeeded"
        assert db.get(Job, deletion_job_id).status == "succeeded"
        outcomes = list(
            db.scalars(
                select(AuditEvent.outcome).where(
                    AuditEvent.action.in_(
                        ["artifact.delete_failed", "artifact.deleted"]
                    )
                )
            )
        )
        assert outcomes == ["retry", "succeeded"]
    with pytest.raises(FileNotFoundError):
        local.get(stored.key)


def test_initial_revision_is_frozen_and_forces_rls():
    migration = Path("alembic/versions/0001_phase6.py").read_text()
    assert "blackbox_server.models" not in migration
    assert "create_all" not in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "blackbox_api" in migration
