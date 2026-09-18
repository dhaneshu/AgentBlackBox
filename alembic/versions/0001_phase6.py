"""Phase 6 durable service schema.

This revision is intentionally self-contained. Never import application models
from a historical migration.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_phase6"
down_revision = None
branch_labels = None
depends_on = None

_OWNED = (
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
    "reviews",
    "artifact_deletions",
)
_ALL_APPLICATION_TABLES = ("users", "identities", "workspaces", *_OWNED)


def _id() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True, nullable=False)


def _workspace() -> sa.Column:
    return sa.Column(
        "workspace_id",
        sa.String(36),
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )


def _timestamps() -> tuple[sa.Column, sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )


def _owned_indexes(table: str) -> None:
    op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def upgrade() -> None:
    op.create_table(
        "users",
        _id(),
        sa.Column("display_name", sa.String(200), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "workspaces",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("legal_hold", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_table(
        "identities",
        _id(),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("issuer", "subject"),
    )
    op.create_table(
        "memberships",
        _id(),
        _workspace(),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "user_id"),
    )
    _owned_indexes("memberships")

    for table in ("projects", "datasets", "policies"):
        op.create_table(
            table,
            _id(),
            _workspace(),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            *_timestamps(),
            sa.UniqueConstraint("workspace_id", "id"),
        )
        _owned_indexes(table)

    op.create_table(
        "evaluator_definitions",
        _id(),
        _workspace(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("implementation", sa.String(300), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("evaluator_definitions")
    op.create_table(
        "artifacts",
        _id(),
        _workspace(),
        sa.Column("storage_key", sa.String(1000), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("inline_data", sa.LargeBinary(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("artifacts")
    op.create_table(
        "experiments",
        _id(),
        _workspace(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("experiments")
    op.create_table(
        "dataset_versions",
        _id(),
        _workspace(),
        sa.Column("dataset_id", sa.String(36), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["datasets.workspace_id", "datasets.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "dataset_id", "digest"),
    )
    _owned_indexes("dataset_versions")
    op.create_table(
        "runs",
        _id(),
        _workspace(),
        sa.Column("experiment_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("runs")
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "conversations",
        _id(),
        _workspace(),
        sa.Column("run_id", sa.String(36), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("conversations")
    op.create_index("ix_conversations_run_id", "conversations", ["run_id"])
    op.create_table(
        "turns",
        _id(),
        _workspace(),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "conversation_id", "sequence"),
    )
    _owned_indexes("turns")
    op.create_table(
        "spans",
        _id(),
        _workspace(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("parent_span_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_span_id"],
            ["spans.workspace_id", "spans.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    _owned_indexes("spans")
    op.create_index("ix_spans_run_id", "spans", ["run_id"])
    op.create_index("ix_spans_trace_id", "spans", ["trace_id"])
    op.create_table(
        "events",
        _id(),
        _workspace(),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "artifact_id"],
            ["artifacts.workspace_id", "artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "event_id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key"),
    )
    _owned_indexes("events")
    op.create_index(
        "ix_events_workspace_created_id",
        "events",
        ["workspace_id", "created_at", "id"],
    )
    op.create_table(
        "evaluator_results",
        _id(),
        _workspace(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("evaluator_id", sa.String(36), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluator_id"],
            ["evaluator_definitions.workspace_id", "evaluator_definitions.id"],
            ondelete="RESTRICT",
        ),
    )
    _owned_indexes("evaluator_results")
    op.create_table(
        "reviews",
        _id(),
        _workspace(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
    )
    _owned_indexes("reviews")
    op.create_table(
        "jobs",
        _id(),
        _workspace(),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "idempotency_key"),
    )
    _owned_indexes("jobs")
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index(
        "ix_jobs_lease", "jobs", ["status", "available_at", "lease_expires_at"]
    )
    op.create_table(
        "audit_events",
        _id(),
        _workspace(),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(300), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _owned_indexes("audit_events")
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_table(
        "retention_rules",
        _id(),
        _workspace(),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("retain_days", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    _owned_indexes("retention_rules")
    op.create_table(
        "artifact_deletions",
        _id(),
        _workspace(),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("workspace_id", "storage_key"),
    )
    _owned_indexes("artifact_deletions")
    op.create_index(
        "ix_artifact_deletions_status", "artifact_deletions", ["status"]
    )

    if op.get_bind().dialect.name == "postgresql":
        for table in _OWNED:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY workspace_isolation ON "{table}" '
                "USING (workspace_id = current_setting('app.workspace_id', true)) "
                "WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"
            )
        mutable = ", ".join(
            f'"{table}"'
            for table in _OWNED
            if table not in {"events", "audit_events"}
        )
        worker_tables = ", ".join(
            f'"{table}"' for table in _ALL_APPLICATION_TABLES
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'blackbox_api') THEN "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {mutable} TO blackbox_api; "
            "GRANT SELECT, INSERT, UPDATE ON workspaces TO blackbox_api; "
            "GRANT SELECT, INSERT ON events, audit_events TO blackbox_api; "
            "END IF; END $$"
        )
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'blackbox_worker') THEN "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {worker_tables} "
            "TO blackbox_worker; END IF; END $$"
        )
        op.execute("REVOKE UPDATE, DELETE ON events, audit_events FROM PUBLIC")


def downgrade() -> None:
    for table in (
        "artifact_deletions",
        "retention_rules",
        "audit_events",
        "jobs",
        "reviews",
        "evaluator_results",
        "events",
        "spans",
        "turns",
        "conversations",
        "runs",
        "dataset_versions",
        "experiments",
        "artifacts",
        "evaluator_definitions",
        "policies",
        "datasets",
        "projects",
        "memberships",
        "identities",
        "workspaces",
        "users",
    ):
        op.drop_table(table)
