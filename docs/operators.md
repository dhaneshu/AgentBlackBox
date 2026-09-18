# Self-hosted service operations

Install `agent-black-box[server]`, configure
`BLACKBOX_SERVER_DATABASE_URL=postgresql+psycopg://...`, run
`blackbox-operator migrate`, and start `blackbox-server`. Use TLS and an
external secret provider; never put credentials in command history or files.
The Phase 6 identity header is only a dependency-injection seam, not
authentication. Do not expose the service until Phase 7 authentication is
configured.

For local artifact storage in production,
`BLACKBOX_SERVER_LOCAL_SIGNING_SECRET` is mandatory and must contain at least
32 random bytes. Generate it with a cryptographic secret manager, inject it at
process start, and never persist it in source or logs. To rotate, put the new
key in `BLACKBOX_SERVER_LOCAL_SIGNING_SECRET` and supply the retiring keys as a
JSON list in `BLACKBOX_SERVER_LOCAL_PREVIOUS_SIGNING_SECRETS`; remove old keys
after the maximum signed-URL lifetime. Ephemeral signing keys are available
only when `BLACKBOX_SERVER_ENVIRONMENT` is explicitly `development` or `test`.

Create separate PostgreSQL roles before the first migration. The migration
grants `blackbox_api` access only when that role already exists:

```sql
CREATE ROLE blackbox_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE blackbox_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT CONNECT ON DATABASE blackbox TO blackbox_api, blackbox_worker;
GRANT USAGE ON SCHEMA public TO blackbox_api, blackbox_worker;
```

Use externally managed credentials or workload identity for role login. Never
run the API as the database owner: every workspace table has both `ENABLE` and
`FORCE ROW LEVEL SECURITY`, and the restricted `blackbox_api` role has no
`BYPASSRLS`. Run `blackbox-worker` separately with `blackbox_worker`; its
cross-workspace leasing requires `BYPASSRLS`, but handlers still use explicit
workspace predicates. Grant that role only the required tables, not schema
ownership or DDL. Events and audit events are append-only for the API role.

Artifact retention uses a transactional deletion outbox. Database references
are removed and an `artifact_delete` job is committed first; the worker then
deletes storage idempotently and records success or retry in the audit log.
Monitor retry/dead-letter jobs and `artifact_deletions.status`, then retry
failed jobs after restoring storage connectivity. Never manually remove an
outbox row before its object deletion is verified.

## Backup and restore

`blackbox-operator backup --file backups/blackbox.dump` calls `pg_dump` in
custom format and writes a SHA-256 sidecar. Copy both files to encrypted,
access-controlled storage and test restores on a schedule.

Restore into an empty PostgreSQL 16 database with
`blackbox-operator restore --file backups/blackbox.dump`, then run
`blackbox-operator migrate`. The restore command verifies the sidecar before
invoking `pg_restore --exit-on-error` and automatically checks the restored
revision and critical tables. `blackbox-operator verify` repeats that check.
Use `--clean` only for an isolated recovery database.

## Disaster recovery

Provision a replacement database and artifact store, restore the newest tested
backup, migrate, verify the Alembic revision, sample artifact SHA-256 digests,
and check `/ready` plus `/migration-status` before changing traffic. Preserve
the old environment until run/event/artifact counts and legal holds are
verified. Record recovery point and recovery time in the incident audit trail.
