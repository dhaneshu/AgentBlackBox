from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from typing import Any, get_type_hints

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import Problem
from .identity import RequestIdentity, request_identity
from .ingestion import EventIngestor, redact
from .jobs import JobQueue
from .models import (
    AuditEvent,
    Artifact,
    Dataset,
    DatasetVersion,
    EvaluatorDefinition,
    EvaluatorResult,
    Experiment,
    Job,
    Policy,
    Project,
    RetentionRule,
    Review,
    Run,
    Span,
    Workspace,
)
from .artifacts import LocalArtifactStore
from .repositories import WorkspaceRepository
from .db import set_workspace_context
from .schemas import (
    DatasetVersionCreate,
    EventCreate,
    JobCreate,
    ResourceCreate,
    RetentionCreate,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(request_identity)])

RESOURCE_MODELS = {
    "projects": Project,
    "datasets": Dataset,
    "experiments": Experiment,
    "policies": Policy,
}
SUPPORTED_JOB_TYPES = {
    "execute_run",
    "comparison",
    "export",
    "import",
}


def session(request: Request):
    with request.app.state.session_factory() as db:
        if workspace_id := request.headers.get("x-workspace-id"):
            set_workspace_context(db, workspace_id)
        yield db


def workspace_header(x_workspace_id: str = Header(..., alias="X-Workspace-ID")) -> str:
    return x_workspace_id


def dump(item: Any) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.name)
        for column in item.__table__.columns
        if column.name not in {"inline_data"}
    }


@router.get("/workspaces", tags=["workspaces"])
def list_workspaces(db: Session = Depends(session)) -> list[dict[str, Any]]:
    return [dump(row) for row in db.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.id))]


@router.post("/workspaces", status_code=201, tags=["workspaces"])
def create_workspace(body: ResourceCreate, db: Session = Depends(session)) -> dict[str, Any]:
    item = Workspace(name=body.name)
    db.add(item)
    db.commit()
    return dump(item)


@router.post("/workspaces/{workspace_id}/delete", status_code=202, tags=["workspaces"])
def delete_workspace(
    workspace_id: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(session),
) -> dict[str, Any]:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise Problem(404, "Not found", "Workspace was not found.")
    if workspace.legal_hold:
        raise Problem(409, "Legal hold", "Deletion is blocked by legal hold.")
    existing = db.scalar(
        select(Job).where(
            Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key
        )
    )
    if existing:
        return dump(existing)
    job = Job(
        workspace_id=workspace_id,
        job_type="delete_workspace",
        idempotency_key=idempotency_key,
        payload={},
    )
    db.add(job)
    db.commit()
    return dump(job)


def list_resource(
    resource: str,
    workspace_id: str = Depends(workspace_header),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    db=Depends(session),
) -> dict[str, Any]:
    model = RESOURCE_MODELS[resource]
    rows, next_cursor = WorkspaceRepository(db, model, workspace_id).page(
        limit=limit, cursor=cursor
    )
    return {"items": [dump(row) for row in rows], "next_cursor": next_cursor}


def create_resource(
    resource: str,
    body: ResourceCreate,
    workspace_id: str = Depends(workspace_header),
    db=Depends(session),
) -> dict[str, Any]:
    model = RESOURCE_MODELS[resource]
    item = model(
        workspace_id=workspace_id,
        name=body.name,
        metadata_json=redact(body.metadata)[0],
    )
    WorkspaceRepository(db, model, workspace_id).add(item)
    db.commit()
    return dump(item)


for _resource in RESOURCE_MODELS:
    list_resource.__annotations__ = get_type_hints(list_resource)
    create_resource.__annotations__ = get_type_hints(create_resource)
    router.add_api_route(
        f"/{_resource}",
        partial(list_resource, _resource),
        methods=["GET"],
        tags=[_resource],
        name=f"list_{_resource}",
        response_model=None,
    )
    router.add_api_route(
        f"/{_resource}",
        partial(create_resource, _resource),
        methods=["POST"],
        tags=[_resource],
        name=f"create_{_resource}",
        status_code=201,
        response_model=None,
    )


@router.get("/runs", tags=["runs"])
def list_runs(
    workspace_id: str = Depends(workspace_header),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(session),
) -> dict[str, Any]:
    rows, next_cursor = WorkspaceRepository(db, Run, workspace_id).page(
        limit=limit, cursor=cursor
    )
    return {"items": [dump(row) for row in rows], "next_cursor": next_cursor}


@router.post("/runs", status_code=202, tags=["runs"])
def create_run(
    body: JobCreate,
    workspace_id: str = Depends(workspace_header),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(session),
) -> dict[str, Any]:
    existing = db.scalar(
        select(Job).where(
            Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.job_type != "execute_run" or not existing.payload.get("run_id"):
            raise Problem(
                409,
                "Idempotency conflict",
                "The idempotency key was used for another operation.",
            )
        existing_run = db.scalar(
            select(Run).where(
                Run.id == existing.payload.get("run_id"),
                Run.workspace_id == workspace_id,
            )
        )
        return {"run": dump(existing_run), "job": dump(existing)}
    clean_payload = redact(body.payload)[0]
    run = Run(workspace_id=workspace_id, status="pending", metadata_json=clean_payload)
    db.add(run)
    db.flush()
    job = Job(
        workspace_id=workspace_id,
        job_type="execute_run",
        idempotency_key=idempotency_key,
        payload={"run_id": run.id, **clean_payload},
        max_attempts=body.max_attempts,
    )
    db.add(job)
    db.commit()
    return {"run": dump(run), "job": dump(job)}


def polling_response(item: Any, response: Response) -> dict[str, Any]:
    if item.status not in {"succeeded", "failed", "cancelled", "dead_letter"}:
        response.headers["Retry-After"] = "5"
        response.headers["X-Poll-Max-Attempts"] = "60"
    return dump(item)


@router.get("/runs/{run_id}", tags=["runs"])
def get_run(
    run_id: str,
    response: Response,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
) -> dict[str, Any]:
    return polling_response(WorkspaceRepository(db, Run, workspace_id).get(run_id), response)


@router.get("/traces", tags=["traces"])
def traces(
    workspace_id: str = Depends(workspace_header),
    run_id: str | None = None,
    db: Session = Depends(session),
) -> list[dict[str, Any]]:
    query = select(Span).where(Span.workspace_id == workspace_id)
    if run_id:
        query = query.where(Span.run_id == run_id)
    return [dump(row) for row in db.scalars(query.order_by(Span.created_at, Span.id).limit(100))]


@router.post("/events", status_code=202, tags=["traces"])
def ingest_event(
    body: EventCreate,
    request: Request,
    workspace_id: str = Depends(workspace_header),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(session),
) -> Response:
    result = EventIngestor(
        db,
        request.app.state.artifact_store,
        max_bytes=request.app.state.settings.max_event_bytes,
        offload_bytes=request.app.state.settings.artifact_offload_bytes,
    ).ingest(
        workspace_id=workspace_id,
        event_id=body.event_id,
        idempotency_key=idempotency_key,
        event_type=body.type,
        payload=body.payload,
        run_id=body.run_id,
    )
    db.commit()
    return Response(
        json.dumps({"id": result.event.id, "duplicate": result.duplicate}),
        status_code=200 if result.duplicate else 202,
        media_type="application/json",
    )


@router.post("/jobs", status_code=202, tags=["jobs"])
def create_job(
    body: JobCreate,
    workspace_id: str = Depends(workspace_header),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(session),
) -> dict[str, Any]:
    if body.type not in SUPPORTED_JOB_TYPES:
        raise Problem(
            422,
            "Unsupported job type",
            "No worker handler is registered for the requested job type.",
        )
    existing = db.scalar(
        select(Job).where(
            Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.job_type != body.type:
            raise Problem(
                409,
                "Idempotency conflict",
                "The idempotency key was used for another operation.",
            )
        return dump(existing)
    job = Job(
        workspace_id=workspace_id,
        job_type=body.type,
        idempotency_key=idempotency_key,
        payload=redact(body.payload)[0],
        max_attempts=body.max_attempts,
    )
    db.add(job)
    db.commit()
    return dump(job)


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(
    job_id: str,
    response: Response,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
) -> dict[str, Any]:
    return polling_response(WorkspaceRepository(db, Job, workspace_id).get(job_id), response)


@router.post("/jobs/{job_id}/cancel", status_code=202, tags=["jobs"])
def cancel_job(
    job_id: str,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
) -> dict[str, bool]:
    JobQueue(db).request_cancel(workspace_id, job_id)
    db.commit()
    return {"cancel_requested": True}


@router.get("/artifacts/{artifact_id}/access", tags=["artifacts"])
def artifact_access(
    artifact_id: str,
    request: Request,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
) -> dict[str, Any]:
    artifact = db.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id, Artifact.workspace_id == workspace_id
        )
    )
    if artifact is None:
        raise Problem(404, "Not found", "The artifact was not found.")
    ttl = request.app.state.settings.artifact_url_ttl_seconds
    return {
        "url": request.app.state.artifact_store.signed_url(artifact.storage_key, ttl),
        "expires_in": ttl,
        "sha256": artifact.sha256,
        "mime_type": artifact.mime_type,
        "size": artifact.size,
    }


@router.get("/artifact-content/{key:path}", include_in_schema=False)
def local_artifact_download(
    key: str,
    request: Request,
    expires: int,
    signature: str,
    db: Session = Depends(session),
) -> Response:
    store = request.app.state.artifact_store
    if not isinstance(store, LocalArtifactStore) or not store.verify_signature(
        key, expires, signature
    ):
        raise Problem(403, "Invalid artifact signature", "The download link is invalid or expired.")
    set_workspace_context(db, key.split("/", 1)[0])
    artifact = db.scalar(select(Artifact).where(Artifact.storage_key == key))
    if artifact is None:
        raise Problem(404, "Not found", "The artifact was not found.")
    return Response(
        store.get(key, artifact.sha256),
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "X-Artifact-SHA256": artifact.sha256,
        },
    )


async def status_stream(
    request: Request, model, workspace_id: str, identifier: str, last_id: int
) -> AsyncIterator[str]:
    sequence = last_id
    previous = None
    for _ in range(60):
        if await request.is_disconnected():
            return
        with request.app.state.session_factory() as db:
            set_workspace_context(db, workspace_id)
            item = db.scalar(
                select(model).where(
                    model.id == identifier, model.workspace_id == workspace_id
                )
            )
            if item is None:
                yield "event: error\ndata: {\"status\":404}\n\n"
                return
            state = item.status
        if state != previous:
            sequence += 1
            yield f"id: {sequence}\nevent: status\ndata: {json.dumps({'id': identifier, 'status': state})}\n\n"
            previous = state
        if state in {"succeeded", "failed", "cancelled", "dead_letter"}:
            return
        await asyncio.sleep(1)
    yield f"id: {sequence + 1}\nevent: polling\ndata: {{\"retry_after\":5,\"max_attempts\":60}}\n\n"


@router.get("/runs/{identifier}/events", tags=["runs"])
@router.get("/jobs/{identifier}/events", tags=["jobs"])
async def stream_status(
    identifier: str,
    request: Request,
    workspace_id: str = Depends(workspace_header),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    model = Job if request.url.path.startswith("/api/v1/jobs/") else Run
    try:
        last_id = int(last_event_id or 0)
    except ValueError as exc:
        raise Problem(400, "Invalid Last-Event-ID", "Last-Event-ID must be an integer.") from exc
    return StreamingResponse(
        status_stream(request, model, workspace_id, identifier, last_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/evaluations", tags=["evaluations"])
def evaluations(workspace_id: str = Depends(workspace_header), db: Session = Depends(session)):
    return [dump(x) for x in db.scalars(select(EvaluatorResult).where(EvaluatorResult.workspace_id == workspace_id).limit(100))]


@router.get("/reviews", tags=["reviews"])
def reviews(workspace_id: str = Depends(workspace_header), db: Session = Depends(session)):
    return [dump(x) for x in db.scalars(select(Review).where(Review.workspace_id == workspace_id).limit(100))]


@router.get("/audit-events", tags=["audit"])
def audit_events(workspace_id: str = Depends(workspace_header), db: Session = Depends(session)):
    return [dump(x) for x in db.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id).order_by(AuditEvent.created_at, AuditEvent.id).limit(100))]


@router.post("/retention", status_code=201, tags=["retention"])
def create_retention(
    body: RetentionCreate,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
):
    item = RetentionRule(workspace_id=workspace_id, **body.model_dump())
    db.add(item)
    db.commit()
    return dump(item)


@router.get("/retention", tags=["retention"])
def list_retention(
    workspace_id: str = Depends(workspace_header), db: Session = Depends(session)
):
    return [
        dump(item)
        for item in db.scalars(
            select(RetentionRule)
            .where(RetentionRule.workspace_id == workspace_id)
            .order_by(RetentionRule.created_at, RetentionRule.id)
        )
    ]


@router.get("/datasets/{dataset_id}/versions", tags=["datasets"])
def list_dataset_versions(
    dataset_id: str,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
):
    WorkspaceRepository(db, Dataset, workspace_id).get(dataset_id)
    return [
        dump(item)
        for item in db.scalars(
            select(DatasetVersion)
            .where(
                DatasetVersion.workspace_id == workspace_id,
                DatasetVersion.dataset_id == dataset_id,
            )
            .order_by(DatasetVersion.created_at, DatasetVersion.id)
        )
    ]


@router.post("/datasets/{dataset_id}/versions", status_code=201, tags=["datasets"])
def create_dataset_version(
    dataset_id: str,
    body: DatasetVersionCreate,
    workspace_id: str = Depends(workspace_header),
    db: Session = Depends(session),
):
    WorkspaceRepository(db, Dataset, workspace_id).get(dataset_id)
    existing = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.workspace_id == workspace_id,
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.digest == body.digest,
        )
    )
    if existing:
        return dump(existing)
    item = DatasetVersion(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        digest=body.digest,
        document=redact(body.document)[0],
    )
    db.add(item)
    db.commit()
    return dump(item)


@router.get("/evaluators", tags=["evaluations"])
def evaluator_definitions(
    workspace_id: str = Depends(workspace_header), db: Session = Depends(session)
):
    return [
        dump(item)
        for item in db.scalars(
            select(EvaluatorDefinition)
            .where(EvaluatorDefinition.workspace_id == workspace_id)
            .order_by(EvaluatorDefinition.created_at, EvaluatorDefinition.id)
        )
    ]


def operation_collection(
    resource: str,
    workspace_id: str = Depends(workspace_header),
) -> dict[str, Any]:
    if resource not in {"comparisons", "exports", "imports"}:
        raise Problem(404, "Not found", "The requested resource was not found.")
    return {"items": [], "next_cursor": None, "workspace_id": workspace_id}


def operation_job(
    resource: str,
    body: JobCreate,
    workspace_id: str = Depends(workspace_header),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(session),
) -> dict[str, Any]:
    if resource not in {"comparisons", "exports", "imports"}:
        raise Problem(404, "Not found", "The requested resource was not found.")
    return create_job(
        JobCreate(type=resource.rstrip("s"), payload=body.payload, max_attempts=body.max_attempts),
        workspace_id,
        idempotency_key,
        db,
    )


operation_collection.__annotations__ = get_type_hints(operation_collection)
operation_job.__annotations__ = get_type_hints(operation_job)
for _operation in ("comparisons", "exports", "imports"):
    router.add_api_route(
        f"/{_operation}",
        partial(operation_collection, _operation),
        methods=["GET"],
        tags=[_operation],
        name=f"list_{_operation}",
    )
    router.add_api_route(
        f"/{_operation}",
        partial(operation_job, _operation),
        methods=["POST"],
        tags=[_operation],
        name=f"create_{_operation}",
        status_code=202,
    )
