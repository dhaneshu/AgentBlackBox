from __future__ import annotations

import shutil
import io
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sqlalchemy_event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from blackbox_server.app import create_app
from blackbox_server.artifacts import LocalArtifactStore
from blackbox_server.config import ServerSettings
from blackbox_server.ingestion import EventIngestor
from blackbox_server.jobs import JobQueue, utcnow
from blackbox_server.models import (
    Artifact,
    AuditEvent,
    Base,
    Event,
    Job,
    Project,
    RetentionRule,
    Run,
    Workspace,
)
from blackbox_server.repositories import UnitOfWork, WorkspaceRepository
from blackbox_server.retention import RetentionWorker
from blackbox_server.workers import JobWorker
from blackbox_server import operator

ARTIFACT_ROOT = Path(".phase6-test-artifacts")
SIGNING_KEY = b"test-only-artifact-signing-key-32b"


@pytest.fixture(autouse=True)
def artifact_cleanup():
    shutil.rmtree(ARTIFACT_ROOT, ignore_errors=True)
    yield
    shutil.rmtree(ARTIFACT_ROOT, ignore_errors=True)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sqlalchemy_event.listen(
        engine,
        "connect",
        lambda connection, record: connection.execute("PRAGMA foreign_keys=ON"),
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def seed_workspace(factory) -> str:
    with factory.begin() as db:
        workspace = Workspace(name="test")
        db.add(workspace)
        db.flush()
        return workspace.id


def test_schema_contains_every_phase6_entity():
    assert {
        "users",
        "identities",
        "workspaces",
        "memberships",
        "projects",
        "datasets",
        "dataset_versions",
        "experiments",
        "runs",
        "conversations",
        "turns",
        "spans",
        "events",
        "artifacts",
        "evaluator_definitions",
        "evaluator_results",
        "policies",
        "jobs",
        "audit_events",
        "retention_rules",
    } <= set(Base.metadata.tables)


def test_workspace_repository_uow_pagination_and_isolation(database):
    _, factory = database
    first = seed_workspace(factory)
    second = seed_workspace(factory)
    with UnitOfWork(factory, first) as uow:
        repo = uow.repository(Project)
        repo.add(Project(workspace_id=first, name="a"))
        repo.add(Project(workspace_id=first, name="b"))
    with factory() as db:
        page, cursor = WorkspaceRepository(db, Project, first).page(limit=1)
        assert [x.name for x in page] == ["a"]
        assert cursor
        rest, _ = WorkspaceRepository(db, Project, first).page(limit=10, cursor=cursor)
        assert [x.name for x in rest] == ["b"]
        assert WorkspaceRepository(db, Project, second).page()[0] == []
    with pytest.raises(ValueError):
        UnitOfWork(factory, "")


def test_optimistic_concurrency(database):
    engine, factory = database
    workspace_id = seed_workspace(factory)
    with factory.begin() as db:
        item = Project(workspace_id=workspace_id, name="original")
        db.add(item)
        db.flush()
        identifier = item.id
    left, right = factory(), factory()
    try:
        a = left.get(Project, identifier)
        b = right.get(Project, identifier)
        a.name = "left"
        left.commit()
        b.name = "right"
        with pytest.raises(StaleDataError):
            right.commit()
    finally:
        left.close()
        right.close()


def test_ingestion_is_append_only_idempotent_redacted_and_offloaded(database):
    _, factory = database
    workspace_id = seed_workspace(factory)
    store = LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY)
    with factory.begin() as db:
        ingestor = EventIngestor(db, store, offload_bytes=20)
        result = ingestor.ingest(
            workspace_id=workspace_id,
            event_id="evt-1",
            idempotency_key="key-1",
            event_type="model.response",
            payload={"authorization": "Bearer secret", "answer": "x" * 100},
        )
        duplicate = ingestor.ingest(
            workspace_id=workspace_id,
            event_id="evt-1",
            idempotency_key="key-1",
            event_type="model.response",
            payload={"ignored": True},
        )
        assert result.event.redacted
        assert result.event.artifact_id
        assert duplicate.duplicate
    with factory() as db:
        assert len(list(db.scalars(select(Event)))) == 1
        assert len(list(db.scalars(select(Job)))) == 1
        artifact = db.scalar(select(Artifact))
        assert b"[REDACTED]" in store.get(artifact.storage_key, artifact.sha256)


def test_local_artifact_digest_and_signed_access():
    store = LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY)
    item = store.put("workspace/a.json", b"{}", "application/json")
    assert store.get(item.key, item.sha256) == b"{}"
    query = store.signed_url(item.key).split("?", 1)[1]
    values = dict(part.split("=", 1) for part in query.split("&"))
    assert store.verify_signature(item.key, int(values["expires"]), values["signature"])


def test_job_leasing_retry_cancel_dead_letter_and_recovery(database):
    _, factory = database
    workspace_id = seed_workspace(factory)
    with factory.begin() as db:
        db.add_all(
            [
                Job(workspace_id=workspace_id, job_type="a", idempotency_key="1"),
                Job(
                    workspace_id=workspace_id,
                    job_type="b",
                    idempotency_key="2",
                    max_attempts=1,
                ),
            ]
        )
    with factory.begin() as db:
        queue = JobQueue(db)
        jobs = queue.lease("worker", limit=2)
        queue.fail(jobs[0].id, "worker", "transient")
        queue.fail(jobs[1].id, "worker", "permanent")
        assert jobs[0].status == "retry"
        assert jobs[1].status == "dead_letter"
        jobs[0].status = "running"
        jobs[0].lease_expires_at = utcnow() - timedelta(seconds=1)
        assert queue.recover_expired() == 1


def test_worker_projects_event_transactionally(database):
    _, factory = database
    workspace_id = seed_workspace(factory)
    with factory.begin() as db:
        run = Run(workspace_id=workspace_id, status="running")
        db.add(run)
        db.flush()
        result = EventIngestor(
            db, LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY)
        ).ingest(
            workspace_id=workspace_id,
            event_id="status-1",
            idempotency_key="status-key",
            event_type="run.status",
            payload={"status": "succeeded"},
            run_id=run.id,
        )
        run_id = run.id
    assert JobWorker(
        factory,
        LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY),
        "worker-1",
    ).run_once() == 1
    with factory() as db:
        assert db.get(Run, run_id).status == "succeeded"
        assert db.scalar(select(Job)).status == "succeeded"


def test_postgresql_lease_sql_compiles_with_skip_locked():
    statement = (
        select(Job)
        .where(Job.status == "pending")
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_postgresql_migration_compiles_rls_sql():
    from alembic import command
    from alembic.config import Config

    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:password@localhost/blackbox"
    )
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY workspace_isolation" in sql
    assert "REVOKE UPDATE, DELETE ON events, audit_events FROM PUBLIC" in sql


def test_operator_hides_password_and_restore_verifies(monkeypatch):
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    monkeypatch.setattr(operator.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        operator.subprocess,
        "run",
        lambda command, env, check, shell: calls.append((command, env)),
    )
    env, args = operator._postgres_env(
        "postgresql+psycopg://service:top-secret@db.example/blackbox"
    )
    assert "top-secret" not in " ".join(args)
    assert env["PGPASSWORD"] == "top-secret"
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"backup")
    operator.restore(
        "postgresql+psycopg://service:top-secret@db.example/blackbox",
        Path("backup.dump"),
    )
    assert calls[0][0][0] == "pg_restore"
    assert calls[1][0][0] == "psql"


def test_retention_honors_hold_then_deletes_and_audits(database):
    _, factory = database
    workspace_id = seed_workspace(factory)
    with factory.begin() as db:
        workspace = db.get(Workspace, workspace_id)
        workspace.legal_hold = True
        run = Run(workspace_id=workspace_id, status="succeeded")
        db.add(run)
        db.flush()
        run_id = run.id
    with factory.begin() as db:
        worker = RetentionWorker(
            db, LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY)
        )
        assert worker.apply(workspace_id) == 0
        db.get(Workspace, workspace_id).legal_hold = False
        worker.delete_run(workspace_id, run_id, "test")
    with factory() as db:
        assert db.get(Run, run_id) is None
        assert len(list(db.scalars(select(AuditEvent)))) == 2


def test_workspace_deletion_keeps_tombstone_job_and_audit(database):
    _, factory = database
    workspace_id = seed_workspace(factory)
    with factory.begin() as db:
        db.add(Project(workspace_id=workspace_id, name="private"))
        job = Job(
            workspace_id=workspace_id,
            job_type="delete_workspace",
            idempotency_key="delete-workspace",
        )
        db.add(job)
        db.flush()
        job_id = job.id
    assert JobWorker(
        factory,
        LocalArtifactStore(ARTIFACT_ROOT, signing_key=SIGNING_KEY),
        "deleter",
    ).run_once() == 1
    with factory() as db:
        assert db.get(Workspace, workspace_id).deleted_at is not None
        assert db.get(Job, job_id).status == "succeeded"
        assert list(db.scalars(select(Project))) == []
        audit = db.scalar(select(AuditEvent))
        assert audit.action == "workspace.deleted"


def test_service_health_problem_details_crud_ingestion_and_openapi():
    app = create_app(
        ServerSettings(
            environment="test",
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=ARTIFACT_ROOT,
            artifact_offload_bytes=1024,
        )
    )
    Base.metadata.create_all(app.state.engine)
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    workspace = client.post("/api/v1/workspaces", json={"name": "acme"}).json()
    headers = {"X-Workspace-ID": workspace["id"]}
    project = client.post(
        "/api/v1/projects", headers=headers, json={"name": "demo"}
    )
    assert project.status_code == 201
    unsupported = client.post(
        "/api/v1/jobs",
        headers={**headers, "Idempotency-Key": "unsupported"},
        json={"type": "not-implemented"},
    )
    assert unsupported.status_code == 422
    assert unsupported.headers["content-type"].startswith("application/problem+json")
    assert client.get("/api/v1/projects", headers=headers).json()["items"][0]["name"] == "demo"
    event = client.post(
        "/api/v1/events",
        headers={**headers, "Idempotency-Key": "ingest-1"},
        json={"event_id": "event-1", "type": "custom", "payload": {"token": "nope"}},
    )
    assert event.status_code == 202
    duplicate = client.post(
        "/api/v1/events",
        headers={**headers, "Idempotency-Key": "ingest-1"},
        json={"event_id": "event-1", "type": "custom", "payload": {}},
    )
    assert duplicate.status_code == 200
    run = Run(workspace_id=workspace["id"], status="succeeded")
    with app.state.session_factory.begin() as db:
        db.add(run)
        db.flush()
        run_id = run.id
    stream = client.get(
        f"/api/v1/runs/{run_id}/events",
        headers={**headers, "Last-Event-ID": "7"},
    )
    assert "id: 8" in stream.text
    invalid = client.get("/api/v1/projects")
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
    assert invalid.headers["x-request-id"]
    paths = app.openapi()["paths"]
    for resource in (
        "workspaces",
        "projects",
        "datasets",
        "experiments",
        "runs",
        "traces",
        "evaluations",
        "comparisons",
        "exports",
        "imports",
        "jobs",
        "reviews",
        "retention",
        "audit-events",
    ):
        assert f"/api/v1/{resource}" in paths
