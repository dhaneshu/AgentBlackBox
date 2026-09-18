from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from .errors import Problem
from .models import Job


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobQueue:
    def __init__(self, session: Session):
        self.session = session

    def lease(
        self, worker_id: str, *, limit: int = 1, lease_seconds: int = 60
    ) -> list[Job]:
        now = utcnow()
        statement = (
            select(Job)
            .where(
                Job.status.in_(("pending", "retry")),
                Job.available_at <= now,
                Job.cancel_requested.is_(False),
            )
            .order_by(Job.available_at, Job.created_at, Job.id)
            .limit(max(1, min(limit, 100)))
        )
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        jobs = list(self.session.scalars(statement))
        for job in jobs:
            job.status = "running"
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.attempts += 1
        self.session.flush()
        return jobs

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 60) -> None:
        job = self._owned(job_id, worker_id)
        if job.cancel_requested:
            raise Problem(409, "Cancellation requested", "The job must stop.")
        job.heartbeat_at = utcnow()
        job.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)

    def heartbeat_atomic(
        self, job_id: str, worker_id: str, lease_seconds: int = 60
    ) -> bool:
        now = utcnow()
        result = self.session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.lease_owner == worker_id,
                Job.cancel_requested.is_(False),
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        return bool(result.rowcount)

    def complete(self, job_id: str, worker_id: str) -> None:
        job = self._owned(job_id, worker_id)
        job.status = "cancelled" if job.cancel_requested else "succeeded"
        job.lease_owner = None
        job.lease_expires_at = None

    def fail(self, job_id: str, worker_id: str, error: str, base_delay: int = 5) -> None:
        job = self._owned(job_id, worker_id)
        job.last_error = error[:4000]
        job.lease_owner = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.status = "dead_letter"
        else:
            job.status = "retry"
            job.available_at = utcnow() + timedelta(
                seconds=min(base_delay * (2 ** (job.attempts - 1)), 3600)
            )

    def request_cancel(self, workspace_id: str, job_id: str) -> None:
        job = self.session.scalar(
            select(Job).where(Job.id == job_id, Job.workspace_id == workspace_id)
        )
        if not job:
            raise Problem(404, "Not found", "The job was not found.")
        job.cancel_requested = True
        if job.status in ("pending", "retry"):
            job.status = "cancelled"

    def recover_expired(self) -> int:
        now = utcnow()
        result = self.session.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.lease_expires_at < now,
            )
            .values(
                status=case(
                    (Job.cancel_requested.is_(True), "cancelled"),
                    (Job.attempts >= Job.max_attempts, "dead_letter"),
                    else_="retry",
                ),
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                updated_at=now,
                version=Job.version + 1,
            )
        )
        return int(result.rowcount or 0)

    def _owned(self, job_id: str, worker_id: str) -> Job:
        job = self.session.scalar(
            select(Job).where(
                Job.id == job_id,
                Job.status == "running",
                Job.lease_owner == worker_id,
            )
        )
        if not job:
            raise Problem(409, "Lease lost", "The worker no longer owns this job.")
        return job
