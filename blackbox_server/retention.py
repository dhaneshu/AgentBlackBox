from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .artifacts import ArtifactStore
from .errors import Problem
from .jobs import utcnow
from .models import (
    Artifact,
    ArtifactDeletion,
    AuditEvent,
    Base,
    Conversation,
    EvaluatorResult,
    Event,
    Job,
    RetentionRule,
    Review,
    Run,
    Span,
    Turn,
    Workspace,
)


class RetentionWorker:
    def __init__(self, session: Session, artifact_store: ArtifactStore):
        self.session = session
        self.artifacts = artifact_store

    def apply(self, workspace_id: str, actor: str = "retention-worker") -> int:
        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise Problem(404, "Not found", "Workspace was not found.")
        if workspace.legal_hold:
            self._audit(workspace_id, actor, "retention.skipped", workspace_id, "legal_hold")
            return 0
        rules = list(
            self.session.scalars(
                select(RetentionRule).where(
                    RetentionRule.workspace_id == workspace_id,
                    RetentionRule.enabled.is_(True),
                    RetentionRule.resource_type == "runs",
                )
            )
        )
        deleted = 0
        for rule in rules:
            cutoff = utcnow() - timedelta(days=rule.retain_days)
            run_ids = list(
                self.session.scalars(
                    select(Run.id).where(
                        Run.workspace_id == workspace_id, Run.created_at < cutoff
                    )
                )
            )
            for run_id in run_ids:
                self.delete_run(workspace_id, run_id, actor)
                deleted += 1
        return deleted

    def delete_run(self, workspace_id: str, run_id: str, actor: str) -> None:
        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.legal_hold:
            raise Problem(409, "Legal hold", "Deletion is blocked by legal hold.")
        run = self.session.scalar(
            select(Run).where(Run.id == run_id, Run.workspace_id == workspace_id)
        )
        if not run:
            raise Problem(404, "Not found", "Run was not found.")
        artifact_ids = list(
            self.session.scalars(
                select(Event.artifact_id).where(
                    Event.workspace_id == workspace_id,
                    Event.run_id == run_id,
                    Event.artifact_id.is_not(None),
                )
            )
        )
        artifacts = list(
            self.session.scalars(
                select(Artifact).where(
                    Artifact.workspace_id == workspace_id, Artifact.id.in_(artifact_ids)
                )
            )
        ) if artifact_ids else []
        conversation_ids = select(Conversation.id).where(
            Conversation.workspace_id == workspace_id, Conversation.run_id == run_id
        )
        self.session.execute(
            delete(Turn).where(
                Turn.workspace_id == workspace_id,
                Turn.conversation_id.in_(conversation_ids),
            )
        )
        for model in (Review, EvaluatorResult, Span, Event, Conversation):
            self.session.execute(
                delete(model).where(
                    model.workspace_id == workspace_id, model.run_id == run_id
                )
            )
        self.session.delete(run)
        for artifact in artifacts:
            self._schedule_artifact_deletion(workspace_id, artifact)
            self.session.delete(artifact)
        self._audit(
            workspace_id, actor, "run.deleted", run_id, "scheduled_artifact_deletion"
        )

    def delete_workspace(
        self,
        workspace_id: str,
        actor: str,
        *,
        preserve_job_id: str | None = None,
    ) -> None:
        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise Problem(404, "Not found", "Workspace was not found.")
        if workspace.legal_hold:
            raise Problem(409, "Legal hold", "Deletion is blocked by legal hold.")
        artifacts = list(
            self.session.scalars(
                select(Artifact).where(Artifact.workspace_id == workspace_id)
            )
        )
        for table in reversed(Base.metadata.sorted_tables):
            if (
                "workspace_id" not in table.c
                or table.name
                in {"audit_events", "artifact_deletions", "jobs", "artifacts"}
            ):
                continue
            statement = table.delete().where(table.c.workspace_id == workspace_id)
            if table.name == "jobs" and preserve_job_id:
                statement = statement.where(table.c.id != preserve_job_id)
            self.session.execute(statement)
        jobs = Job.__table__.delete().where(Job.workspace_id == workspace_id)
        if preserve_job_id:
            jobs = jobs.where(Job.id != preserve_job_id)
        self.session.execute(jobs)
        for artifact in artifacts:
            self._schedule_artifact_deletion(workspace_id, artifact)
            self.session.delete(artifact)
        workspace.deleted_at = utcnow()
        self._audit(
            workspace_id,
            actor,
            "workspace.deleted",
            workspace_id,
            "scheduled_artifact_deletion",
        )

    def _schedule_artifact_deletion(
        self, workspace_id: str, artifact: Artifact
    ) -> ArtifactDeletion:
        intent = self.session.scalar(
            select(ArtifactDeletion).where(
                ArtifactDeletion.workspace_id == workspace_id,
                ArtifactDeletion.storage_key == artifact.storage_key,
            )
        )
        if intent is None:
            intent = ArtifactDeletion(
                workspace_id=workspace_id,
                storage_key=artifact.storage_key,
                sha256=artifact.sha256,
            )
            self.session.add(intent)
            self.session.flush()
        existing_job = self.session.scalar(
            select(Job).where(
                Job.workspace_id == workspace_id,
                Job.idempotency_key == f"artifact-delete:{intent.id}",
            )
        )
        if existing_job is None:
            self.session.add(
                Job(
                    workspace_id=workspace_id,
                    job_type="artifact_delete",
                    idempotency_key=f"artifact-delete:{intent.id}",
                    payload={"intent_id": intent.id},
                )
            )
        return intent

    def _audit(
        self, workspace_id: str, actor: str, action: str, target: str, outcome: str
    ) -> None:
        self.session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action=action,
                actor=actor,
                target_type="run" if action.startswith("run.") else "workspace",
                target_id=target,
                outcome=outcome,
            )
        )
