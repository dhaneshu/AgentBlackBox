---
goal: Production-ready Agent Black Box evaluation platform
version: 1.0
date_created: 2026-09-18
last_updated: 2026-09-18
owner: Agent Black Box maintainers
status: 'Planned'
tags: [architecture, platform, evaluation, agents, observability, azure]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan evolves Agent Black Box from a single-process, single-turn RAG
evaluation prototype into an open-source Python SDK and CLI with an optional
self-hosted multi-tenant web platform. The target release supports multi-turn
and multi-agent traces, tool and side-effect assertions, latency and cost
metrics, pluggable deterministic and model-based evaluators, remote trace
ingestion, durable PostgreSQL storage, OIDC authentication with Microsoft Entra
as the reference provider, a TypeScript web UI, and a production Azure
deployment.

The existing offline workflow remains supported. `python -m blackbox demo`,
file-based runs, deterministic sample agents, and current JSONL suites must
continue to work without network access or service dependencies.

## 1. Requirements & Constraints

- **REQ-001**: Preserve the existing offline SDK and CLI workflows, current
  five verdicts, policy behavior, JSONL trace loading, and deterministic demo.
- **REQ-002**: Publish a versioned Python package named `agent-black-box` that
  supports Python 3.11, 3.12, and 3.13 and exposes stable public modules under
  `blackbox`.
- **REQ-003**: Introduce trace schema version `2.0` with workspace, project,
  dataset, experiment, run, conversation, turn, span, event, artifact, metric,
  and evaluation-result identifiers.
- **REQ-004**: Represent multi-turn conversations as ordered turns and represent
  nested agent, model, retrieval, tool, policy, and evaluator activity as W3C
  trace-compatible spans with parent-child relationships.
- **REQ-005**: Represent multi-agent delegation using span links plus explicit
  `source_agent`, `target_agent`, `handoff_id`, and `handoff_payload` fields.
- **REQ-006**: Extend evaluation cases to support ordered conversation messages,
  variables, setup and teardown fixtures, expected final outcomes, expected
  intermediate tool calls, expected span sequences, and evaluator-specific
  assertions.
- **REQ-007**: Support deterministic assertions for exact text, substring,
  regular expression, JSON Schema, numeric tolerance, citations, refusal,
  ordered and unordered tool calls, tool arguments, tool results, span order,
  latency, tokens, cost, and side effects.
- **REQ-008**: Add a pluggable evaluator protocol with deterministic evaluators
  enabled by default and optional model-based evaluators for groundedness,
  relevance, coherence, task completion, safety, and custom rubrics.
- **REQ-009**: Record evaluator identity, implementation version, configuration,
  judge deployment, prompt hash, input hash, output, score, rationale, latency,
  token usage, and cost for every evaluator result.
- **REQ-010**: Add side-effect verification through pluggable fixture providers
  that capture before and after snapshots and evaluate declared assertions
  without granting the core engine direct production-system credentials.
- **REQ-011**: Add latency metrics for every span and aggregate p50, p95, p99,
  mean, minimum, maximum, timeout count, and error count per run.
- **REQ-012**: Add cost accounting using versioned price catalogs and explicit
  per-model overrides; unknown pricing must produce an `unknown` cost state
  rather than a zero-cost result.
- **REQ-013**: Add remote trace ingestion for native Agent Black Box events and
  OpenTelemetry Protocol HTTP traces while preserving unknown attributes.
- **REQ-014**: Add adapters for in-process Python agents, HTTP agents, Microsoft
  Foundry agents/traces, and generic OpenTelemetry-instrumented applications.
- **REQ-015**: Add durable service storage in PostgreSQL with Alembic migrations,
  immutable raw trace events, normalized query tables, and object storage for
  large artifacts.
- **REQ-016**: Add a FastAPI service with OpenAPI documentation for workspaces,
  projects, datasets, dataset versions, experiments, runs, traces, evaluators,
  policies, comparisons, exports, and administration.
- **REQ-017**: Add asynchronous evaluation execution with idempotent jobs,
  bounded retries, cancellation, heartbeat, lease recovery, and PostgreSQL
  `FOR UPDATE SKIP LOCKED` worker coordination.
- **REQ-018**: Add a React and TypeScript web application for dataset authoring,
  run submission, live status, trace waterfall inspection, failure triage,
  metric dashboards, before/after comparison, evaluator configuration, and
  workspace administration.
- **REQ-019**: Support OIDC authentication with Microsoft Entra as the tested
  reference provider and isolate data by multi-tenant workspace.
- **REQ-020**: Implement workspace roles `owner`, `admin`, `editor`, `runner`,
  and `viewer`, with authorization enforced server-side for every API request.
- **REQ-021**: Provide CLI profiles for `local` and `remote` execution and add
  `login`, `workspace`, `project`, `dataset`, `run`, `watch`, `export`, and
  `import` commands without breaking current commands.
- **REQ-022**: Provide run gates that return a nonzero CLI exit code and a
  machine-readable result when configured thresholds fail, including maximum
  regression count, minimum accuracy, maximum hallucination rate, maximum
  over-refusal rate, maximum p95 latency, maximum cost, and maximum violations.
- **REQ-023**: Version datasets immutably and record the exact dataset version,
  agent version, configuration hash, evaluator versions, policy versions, code
  revision, environment, and dependency lock hash on every experiment run.
- **REQ-024**: Support trace and result export in versioned JSONL and JSON, JUnit
  XML for CI, and CSV for analysis.
- **REQ-025**: Provide retention policies, legal-hold metadata, workspace export,
  workspace deletion, and auditable deletion jobs.
- **REQ-026**: Provide structured logs, OpenTelemetry metrics and traces, health
  endpoints, readiness endpoints, worker health, database migration status, and
  deployment version information.
- **REQ-027**: Supply Docker images and Azure Bicep modules for Azure Container
  Apps, Azure Database for PostgreSQL Flexible Server, Blob Storage, Key Vault,
  Container Registry, Application Insights, Log Analytics, and managed identity.
- **REQ-028**: Provide local development through Docker Compose with API,
  worker, web, PostgreSQL, and Azurite, while retaining a dependency-free offline
  CLI mode.
- **REQ-029**: Provide extension interfaces for evaluator plugins, policy
  plugins, agent adapters, trace exporters, artifact stores, price providers,
  and fixture providers using Python entry points.
- **REQ-030**: Document stable APIs, extension contracts, deployment procedures,
  upgrade procedures, backup and restore, incident response, and contribution
  workflows.
- **SEC-001**: Never persist access tokens, API keys, passwords, connection
  strings, or OIDC refresh tokens in traces, evaluation inputs, logs, or
  configuration fingerprints.
- **SEC-002**: Encrypt network traffic with TLS and use managed identity for
  Azure service-to-service authentication.
- **SEC-003**: Encrypt PostgreSQL and Blob Storage at rest and store service
  secrets only in Key Vault or an equivalent external secret provider.
- **SEC-004**: Apply workspace isolation in every repository query; repository
  methods that access tenant-owned entities must require `workspace_id`.
- **SEC-005**: Validate OIDC issuer, audience, signature, expiry, not-before,
  tenant allowlist, and nonce or state where applicable.
- **SEC-006**: Redact configured sensitive keys and detected secrets before
  persistence, and retain a redaction audit event without retaining the secret.
- **SEC-007**: Limit trace event, artifact, request, and evaluator payload sizes;
  reject oversized payloads with explicit errors.
- **SEC-008**: Treat imported traces, evaluator prompts, tool outputs, artifact
  names, Markdown, and HTML as untrusted data and prevent script execution,
  path traversal, server-side request forgery, and unsafe deserialization.
- **SEC-009**: Protect state-changing API endpoints with authorization, request
  validation, rate limits, idempotency keys, and immutable audit events.
- **SEC-010**: Run dependency, secret, container, and static security scanning in
  CI and block releases on critical or high unresolved findings.
- **CON-001**: Existing schema version `1` JSONL files must remain readable and
  migrate in memory to schema version `2.0`.
- **CON-002**: The default evaluator set must run offline and deterministically;
  model-based evaluators are opt-in and visibly marked nondeterministic.
- **CON-003**: The service must not require Azure; Azure is the reference
  deployment and all service interfaces must permit non-Azure implementations.
- **CON-004**: Initial production database support is PostgreSQL 16; SQLite may
  be used only for unit tests and local SDK metadata, not the hosted service.
- **CON-005**: Large artifacts above 256 KiB must be stored through the artifact
  store and referenced by URI plus SHA-256 digest rather than embedded in rows.
- **CON-006**: Raw trace events are append-only. Corrections and human reviews
  must create new events and must not mutate historical evidence.
- **CON-007**: Public REST endpoints must be versioned under `/api/v1`; breaking
  changes require a new API major version.
- **CON-008**: All persisted timestamps use UTC RFC 3339 and all durations use
  integer nanoseconds.
- **GUD-001**: Implement vertical slices in the phase order below and keep each
  phase deployable, migrated, documented, and tested.
- **GUD-002**: Prefer deterministic evaluators for release gates and use
  model-based evaluators as supplemental signals with recorded confidence and
  judge metadata.
- **GUD-003**: Preserve raw evidence and derive normalized projections so future
  evaluators can rescore historical runs without rerunning the application.
- **GUD-004**: Use generated OpenAPI clients for the CLI remote backend and web
  application; do not hand-maintain duplicate request types.
- **GUD-005**: Emit explicit errors for invalid schemas, unavailable evaluators,
  missing prices, failed fixtures, incomplete traces, and authorization denial.
- **PAT-001**: Use ports-and-adapters architecture: domain models and evaluation
  logic must not import FastAPI, SQLAlchemy, Azure SDKs, or UI code.
- **PAT-002**: Use immutable dataclasses or Pydantic models for domain events and
  discriminated unions for event and assertion types.
- **PAT-003**: Use repositories and units of work for persistence, with explicit
  transaction boundaries in application services.
- **PAT-004**: Use OpenTelemetry semantic conventions where applicable and place
  Agent Black Box-specific attributes under the `agentblackbox.*` namespace.
- **PAT-005**: Use an append-only event ingestion path followed by idempotent
  projection and evaluation jobs.

## 2. Implementation Steps

### Implementation Phase 1: Package, contracts, and compatibility

- **GOAL-001**: Establish stable package boundaries and versioned contracts
  without changing existing offline behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `pyproject.toml` using Hatchling, package name `agent-black-box`, Python range `>=3.11,<3.14`, dependency groups `sdk`, `server`, `azure`, `dev`, and console script `blackbox=blackbox.cli:main`; replace `requirements.txt` with a documented compatibility input generated from the project metadata. | Yes | 2026-09-18 |
| TASK-002 | Create `blackbox/domain/` and move domain-only trace, suite, score, policy, and comparison types behind compatibility re-exports from the current modules so existing imports remain valid. | Yes | 2026-09-18 |
| TASK-003 | Create `blackbox/domain/schema.py` with `CURRENT_SCHEMA_VERSION = "2.0"`, schema identifiers, validation errors, and a migration registry; implement version `1` trace migration in `blackbox/domain/migrations/v1_to_v2.py`. | Yes | 2026-09-18 |
| TASK-004 | Create `schemas/trace-2.0.schema.json`, `schemas/suite-2.0.schema.json`, `schemas/result-2.0.schema.json`, and golden examples under `tests/fixtures/schemas/`; validate serialized output against these schemas. | Yes | 2026-09-18 |
| TASK-005 | Add `blackbox/config.py` using Pydantic Settings with `.env` support, typed local and remote profiles, secret-field exclusion, environment overrides, and explicit validation errors; remove direct environment access from `blackbox/azure_agent.py`. | Yes | 2026-09-18 |
| TASK-006 | Add `blackbox/plugins.py` with typed entry-point protocols and discovery for adapters, evaluators, policies, artifact stores, fixture providers, price providers, and exporters; include duplicate-name and incompatible-version errors. | Yes | 2026-09-18 |
| TASK-007 | Add deprecation warnings and a `docs/upgrading/schema-v2.md` migration guide for current `Trace`, `Case`, `Recorder`, CLI, and JSONL consumers. | Yes | 2026-09-18 |

### Implementation Phase 2: Conversation and distributed trace model

- **GOAL-002**: Model real multi-turn, multi-agent, tool-using executions without
  losing compatibility with single-turn traces.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Implement `Conversation`, `Turn`, `Message`, `Span`, `SpanLink`, `Event`, `ArtifactRef`, `Usage`, `Cost`, and `ErrorInfo` in `blackbox/domain/trace.py`; use ULIDs for platform-generated IDs and accept W3C trace and span IDs during ingestion. | Yes | 2026-09-18 |
| TASK-009 | Replace positional `STEP_KINDS` with discriminated event types for agent handoff, retrieval, model request, model response, tool request, tool response, policy decision, evaluator result, human review, error, and custom events. | Yes | 2026-09-18 |
| TASK-010 | Extend `blackbox/recorder.py` with context-managed `conversation()`, `turn()`, and `span()` APIs that measure time, capture exceptions, propagate context safely across async tasks, and preserve current helper methods. | Yes | 2026-09-18 |
| TASK-011 | Add async recorder methods and `contextvars` propagation for concurrent tools and subagents; enforce parent ownership and reject orphan or cyclic spans. | Yes | 2026-09-18 |
| TASK-012 | Add multi-agent handoff recording with linked source and target spans, payload artifact references, and status transitions `requested`, `accepted`, `completed`, `failed`, and `cancelled`. | Yes | 2026-09-18 |
| TASK-013 | Implement deterministic canonical serialization, SHA-256 content digests, streaming JSONL readers and writers, payload-size validation, and corruption diagnostics with line and event identifiers. | Yes | 2026-09-18 |

### Implementation Phase 3: Dataset and assertion engine

- **GOAL-003**: Make test suites expressive enough for multi-turn workflows,
  tool behavior, structured output, and side effects.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Replace the fixed `Case` fields with a versioned `EvaluationCase` containing messages, variables, fixture references, tags, expected outcome, assertion list, evaluator list, timeout, and metadata; provide a migration adapter for current JSONL cases. | Yes | 2026-09-18 |
| TASK-015 | Implement assertion types in `blackbox/assertions/`: text, regex, refusal, JSON Schema, numeric tolerance, citation, span existence, ordered span sequence, tool call count, tool arguments, tool result, latency, tokens, cost, policy violation, and artifact digest. | Yes | 2026-09-18 |
| TASK-016 | Implement JSONPath selection for structured tool and model outputs and produce assertion results containing status, expected value, actual value, evidence references, and machine-readable failure codes. | Yes | 2026-09-18 |
| TASK-017 | Implement `FixtureProvider` lifecycle `prepare`, `snapshot`, `verify`, and `cleanup`; add built-in HTTP fixture, SQL read-only fixture, filesystem sandbox fixture, and in-memory fixture for tests. | Yes | 2026-09-18 |
| TASK-018 | Require fixtures that can mutate external systems to declare `destructive=true`, require an explicit CLI or API approval flag, and record before/after snapshots as artifact references. | Yes | 2026-09-18 |
| TASK-019 | Add YAML and JSON suite support, JSON Schema validation, immutable dataset version hashes, suite composition, parameterized cases, tags, case filtering, and validation that every answer expectation has at least one outcome assertion. | Yes | 2026-09-18 |
| TASK-020 | Add deterministic dataset split and sampling by seed, with recorded sampling configuration and selected case IDs. | Yes | 2026-09-18 |

### Implementation Phase 4: Evaluator and policy platform

- **GOAL-004**: Support trustworthy deterministic scoring and auditable optional
  model-based evaluation.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-021 | Define `Evaluator`, `EvaluatorContext`, `EvaluatorResult`, `Score`, and `EvaluationError` protocols in `blackbox/evaluators/base.py`, including sync and async execution and declared input requirements. | Yes | 2026-09-18 |
| TASK-022 | Convert current verdict and groundedness logic into built-in deterministic evaluator plugins while preserving existing score output and five verdict names. | Yes | 2026-09-18 |
| TASK-023 | Implement built-in evaluators for assertions, task completion from assertions, tool correctness, trajectory conformance, latency, token usage, cost, policy compliance, and deterministic stability. | Yes | 2026-09-18 |
| TASK-024 | Implement an opt-in Azure OpenAI judge adapter with rubric templates, structured JSON output, retries limited to transient errors, prompt-injection delimiters, judge metadata, calibration datasets, and no success-shaped fallback. | Yes | 2026-09-18 |
| TASK-025 | Add evaluator ensembles with explicit aggregation methods `all`, `any`, `weighted_mean`, and `majority`; reject incompatible score ranges and missing required evaluator outputs. | Yes | 2026-09-18 |
| TASK-026 | Add human review events, reviewer labels, adjudication status, comments, and evaluator calibration reports comparing automated results to adjudicated labels. | Yes | 2026-09-18 |
| TASK-027 | Refactor policies into versioned plugins with configuration schemas, severity, remediation text, applicability predicates, and deterministic result IDs. | Yes | 2026-09-18 |
| TASK-028 | Implement evaluation cache keys from trace digest, case digest, evaluator version, evaluator configuration, and judge deployment; never reuse model-judge results across a changed component. | Yes | 2026-09-18 |

### Implementation Phase 5: Execution engine and adapters

- **GOAL-005**: Execute local and remote agents reliably and ingest traces from
  common production instrumentation.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-029 | Create `blackbox/runtime/runner.py` with async bounded concurrency, per-case timeout, cancellation, retries restricted to declared transient adapter errors, deterministic ordering of persisted results, and partial-run status. | Yes | 2026-09-18 |
| TASK-030 | Define `AgentAdapter` lifecycle `prepare`, `start_conversation`, `send`, `finish_conversation`, and `close`; wrap current agents in `InProcessAgentAdapter`. | Yes | 2026-09-18 |
| TASK-031 | Add `HttpAgentAdapter` with configurable request and response mappings, OIDC or API-key authentication, retry policy, correlation IDs, streaming response support, and redacted request logging. | Yes | 2026-09-18 |
| TASK-032 | Add `FoundryAgentAdapter` for Microsoft Foundry project agents and `FoundryTraceImporter` for Foundry conversation and W3C trace identifiers; preserve Foundry-specific attributes as namespaced metadata. | Yes | 2026-09-18 |
| TASK-033 | Add OTLP HTTP ingestion and import in `blackbox/otel/`, mapping OpenTelemetry GenAI spans into the version `2.0` trace model while preserving unmapped attributes. | Yes | 2026-09-18 |
| TASK-034 | Add generic webhook and JSONL import adapters with declarative field mapping and schema validation. | Yes | 2026-09-18 |
| TASK-035 | Implement rescore mode that applies new policies and evaluators to stored traces without invoking the agent again. | Yes | 2026-09-18 |
| TASK-036 | Replace the current determinism check with configurable repeated trials, exact and semantic agreement metrics, confidence intervals, and a recorded random seed where the adapter supports one. | Yes | 2026-09-18 |

### Implementation Phase 6: Storage and service

- **GOAL-006**: Provide a secure, transactional, multi-tenant API and durable job
  execution layer.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-037 | Create `blackbox_server/` with FastAPI application factory, dependency injection, RFC 9457 problem responses, request IDs, structured logging, health endpoints, and `/api/v1` routing. | Yes | 2026-09-18 |
| TASK-038 | Create SQLAlchemy 2 models and Alembic migrations for users, identities, workspaces, memberships, projects, datasets, dataset versions, experiments, runs, conversations, turns, spans, events, artifacts, evaluator definitions, evaluator results, policies, jobs, audit events, and retention rules. | Yes | 2026-09-18 |
| TASK-039 | Implement workspace-scoped repositories requiring `workspace_id`, transaction-scoped units of work, optimistic concurrency, pagination, stable sorting, and PostgreSQL row-level security as defense in depth. | Yes | 2026-09-18 |
| TASK-040 | Implement raw event ingestion with idempotency keys, unique event IDs, append-only storage, validation, redaction, artifact offload, and transactional projection job creation. | Yes | 2026-09-18 |
| TASK-041 | Implement artifact store interfaces with local filesystem and Azure Blob providers, SHA-256 verification, signed short-lived downloads, MIME allowlists, and 256 KiB database offload threshold. | Yes | 2026-09-18 |
| TASK-042 | Implement PostgreSQL-backed worker leases using `FOR UPDATE SKIP LOCKED`, heartbeats, cancellation, retry schedules, dead-letter status, and recovery of expired leases. | Yes | 2026-09-18 |
| TASK-043 | Implement REST resources for workspaces, projects, datasets, experiments, runs, traces, evaluations, comparisons, exports, imports, jobs, reviews, retention, and audit events with generated OpenAPI examples. | Yes | 2026-09-18 |
| TASK-044 | Implement server-sent events for run and job status; clients must reconnect using `Last-Event-ID` and fall back to bounded polling. | Yes | 2026-09-18 |
| TASK-045 | Implement retention and deletion workers that honor legal hold, delete normalized and raw data consistently, remove artifacts, and write immutable audit outcomes. | Yes | 2026-09-18 |
| TASK-046 | Add PostgreSQL backup, restore, schema migration, and disaster-recovery commands with operator documentation and automated restore verification. | Yes | 2026-09-18 |

### Implementation Phase 7: Identity, authorization, and security

- **GOAL-007**: Enforce standards-based identity, workspace isolation, and
  auditable least privilege.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-047 | Implement OIDC discovery and JWT validation in `blackbox_server/auth/` with issuer and audience allowlists, JWKS caching and rotation, clock-skew bounds, and explicit authentication errors. | | |
| TASK-048 | Implement Microsoft Entra configuration and group or app-role mapping as the reference provider while keeping provider-specific claims behind an identity mapping interface. | | |
| TASK-049 | Implement workspace RBAC for `owner`, `admin`, `editor`, `runner`, and `viewer`; define an authorization matrix and enforce it in application services rather than UI controls. | | |
| TASK-050 | Add immutable audit events for login, membership change, role change, dataset publication, run execution, evaluator change, export, import, retention change, deletion, and administrative actions. | | |
| TASK-051 | Implement configurable redaction rules for headers, JSON paths, attributes, environment variables, credentials, and detected secrets before persistence and before log emission. | | |
| TASK-052 | Add API and ingestion rate limits per identity and workspace, payload limits, decompression limits, outbound URL allowlists, and private-address SSRF blocking for HTTP adapters and webhooks. | | |
| TASK-053 | Add Content Security Policy, secure cookies, CSRF protection for cookie-based web sessions, strict CORS configuration, and safe Markdown rendering. | | |
| TASK-054 | Produce a threat model covering identity, tenant isolation, trace ingestion, evaluator prompt injection, artifact delivery, fixture credentials, side effects, and supply chain. | | |

### Implementation Phase 8: CLI, SDK, and web experience

- **GOAL-008**: Provide professional local and collaborative workflows over one
  generated API contract.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-055 | Refactor `blackbox/cli.py` into command modules using Typer while preserving current command syntax through compatibility aliases and exit codes. | | |
| TASK-056 | Add CLI profile management, OIDC device-code login, secure OS credential storage, workspace and project selection, remote dataset upload, remote run submission, live watch, cancellation, export, import, and CI gate commands. | | |
| TASK-057 | Generate a typed Python API client from OpenAPI and use it in remote CLI commands; retain direct domain calls for local mode. | | |
| TASK-058 | Create `web/` using React, TypeScript, Vite, TanStack Router, TanStack Query, and generated OpenAPI types; configure accessible design tokens and error boundaries. | | |
| TASK-059 | Implement workspace and project navigation, dataset editor with schema validation, immutable version publication, evaluator and policy configuration, and run submission. | | |
| TASK-060 | Implement live run dashboard, case table, filtering, metric cards, distributions, latency and cost charts, and downloadable result formats. | | |
| TASK-061 | Implement trace waterfall with nested spans, events, tool arguments and results, citations, artifacts, redaction indicators, errors, evaluator evidence, and linked agent handoffs. | | |
| TASK-062 | Implement comparison view for fixed, regressed, unchanged, newly added, and removed cases, including metric confidence intervals and configuration differences. | | |
| TASK-063 | Implement failure triage and human review queues with assignment, labels, comments, adjudication, deep links, and audit history. | | |
| TASK-064 | Meet WCAG 2.2 AA for keyboard navigation, focus management, color contrast, semantic landmarks, charts, and screen-reader labels. | | |

### Implementation Phase 9: Packaging, CI, and quality gates

- **GOAL-009**: Make releases reproducible, secure, supportable, and consumable
  by Python, container, and CI users.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-065 | Add Ruff, mypy strict mode for public modules, Pyright, pytest coverage, Hypothesis, pip-audit, Bandit, detect-secrets, and pre-commit with pinned tool versions. | | |
| TASK-066 | Add GitHub Actions for Python 3.11-3.13 on Windows and Linux, PostgreSQL integration tests, web lint and tests, Playwright tests, migration tests, OpenAPI drift, schema compatibility, package build, container build, SBOM, vulnerability scan, and signed provenance. | | |
| TASK-067 | Add unit, contract, property, integration, end-to-end, performance, chaos, upgrade, backup-restore, and tenant-isolation test suites with documented runtime tiers. | | |
| TASK-068 | Add deterministic golden trace fixtures for single-turn, multi-turn, concurrent tools, failed tools, multi-agent handoff, partial run, imported OTLP, side effects, and model-judge results. | | |
| TASK-069 | Add performance budgets: ingest at least 500 events per second per API replica, query a 10,000-span trace within 2 seconds at p95, and evaluate 1,000 deterministic cases within 5 minutes on the documented reference machine. | | |
| TASK-070 | Add semantic versioning, Conventional Commits, generated changelog, release notes, migration notes, Python wheel and source distribution publication, multi-architecture container publication, and rollback instructions. | | |
| TASK-071 | Add `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, support policy, compatibility matrix, architecture decision record template, and maintainer release checklist. | | |

### Implementation Phase 10: Azure reference deployment and operations

- **GOAL-010**: Deliver a secure, observable, repeatable Azure production
  deployment without making Azure mandatory for the product.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-072 | Create modular Bicep under `infra/azure/` for resource group-scoped deployment of Container Apps environment, API app, worker app, web app, PostgreSQL Flexible Server, Blob Storage, Key Vault, ACR, Log Analytics, Application Insights, managed identities, private DNS, and optional virtual network integration. | | |
| TASK-073 | Configure Entra app registrations through documented scripts and federated credentials, expose OIDC settings as deployment outputs, and assign minimal data-plane roles to managed identities. | | |
| TASK-074 | Configure Container Apps revisions, startup and readiness probes, autoscaling, workload profiles, resource limits, deployment labels, migration job, and zero-downtime API and worker rollout sequence. | | |
| TASK-075 | Configure private endpoints and public-access switches for PostgreSQL, Storage, Key Vault, and ACR; default the production parameter file to private networking and deny public data-plane access. | | |
| TASK-076 | Configure Application Insights dashboards and alerts for availability, API errors, ingestion failures, worker backlog, lease expiry, evaluation failures, p95 latency, database capacity, storage errors, and authentication failures. | | |
| TASK-077 | Add `docker-compose.yml` and `.env.example` for local API, worker, web, PostgreSQL, and Azurite; use non-secret development defaults and persistent named volumes. | | |
| TASK-078 | Add deployment validation, Bicep lint, what-if, policy checks, integration smoke tests, rollback tests, and post-deployment health verification. | | |
| TASK-079 | Write operator runbooks for deployment, scaling, failed migrations, queue backlog, database failover, backup restore, identity failures, certificate rotation, incident containment, and workspace data export or deletion. | | |
| TASK-080 | Run a production-readiness review and block version `1.0.0` until all release acceptance criteria and security findings are closed or explicitly risk-accepted by the owner. | | |

## 3. Alternatives

- **ALT-001**: Build only a hosted web platform. Rejected because it would
  remove the current offline and CI-friendly value proposition and force users
  to upload sensitive traces.
- **ALT-002**: Keep JSONL as the hosted service database. Rejected because it
  cannot provide safe concurrent writes, tenant isolation, indexed queries,
  retention jobs, or transactional evaluation scheduling.
- **ALT-003**: Use an LLM judge for every verdict. Rejected because judge
  nondeterminism, cost, latency, and circular evaluation would make release
  gates less trustworthy; model judges remain optional plugins.
- **ALT-004**: Implement Microsoft Entra-specific authentication throughout the
  service. Rejected because the product is open source; OIDC is the contract and
  Entra is the tested provider.
- **ALT-005**: Use Azure Service Bus as a mandatory job queue. Rejected for the
  first release because it would make non-Azure self-hosting harder;
  PostgreSQL-backed leases satisfy initial reliability requirements behind a
  queue abstraction.
- **ALT-006**: Replace the existing CLI and schemas without compatibility
  adapters. Rejected because current traces and automated workflows are
  valuable evidence and must remain usable.

## 4. Dependencies

- **DEP-001**: Python 3.11-3.13, Hatchling, Pydantic 2, Pydantic Settings,
  Typer, Rich, and `python-dotenv`.
- **DEP-002**: FastAPI, Uvicorn, SQLAlchemy 2, Alembic, psycopg 3, and PostgreSQL
  16.
- **DEP-003**: OpenTelemetry API, SDK, OTLP protobuf definitions, and GenAI
  semantic conventions supported by the selected OpenTelemetry version.
- **DEP-004**: `jsonschema`, `jsonpath-ng`, PyYAML, ULID implementation, and
  `httpx`.
- **DEP-005**: Azure Identity, Azure Blob Storage, Azure Monitor OpenTelemetry,
  and existing OpenAI SDK dependencies behind optional `azure` extras.
- **DEP-006**: React, TypeScript, Vite, TanStack Router, TanStack Query, a
  maintained accessible component library, Vitest, and Playwright.
- **DEP-007**: Docker, Docker Compose, Bicep CLI, Azure CLI, and GitHub Actions
  for reference deployment and release automation.
- **DEP-008**: OIDC provider metadata and application registration; Microsoft
  Entra is required only for the reference deployment.

## 5. Files

- **FILE-001**: `pyproject.toml` defines package metadata, dependency extras,
  scripts, and tool configuration.
- **FILE-002**: `blackbox/domain/` contains platform-independent versioned
  domain models, migrations, assertions, scores, and policies.
- **FILE-003**: `blackbox/recorder.py` provides compatible sync and async
  conversation, turn, span, event, tool, citation, answer, and refusal APIs.
- **FILE-004**: `blackbox/assertions/` contains deterministic assertion
  implementations and evidence-rich results.
- **FILE-005**: `blackbox/evaluators/` contains evaluator protocols, built-ins,
  aggregation, calibration, caching, and optional model judges.
- **FILE-006**: `blackbox/adapters/` contains in-process, HTTP, Foundry, webhook,
  and import adapters.
- **FILE-007**: `blackbox/runtime/` contains local execution, rescore,
  concurrency, cancellation, and repeat-trial logic.
- **FILE-008**: `blackbox/otel/` contains OTLP ingestion and semantic mapping.
- **FILE-009**: `blackbox/client/` contains the generated remote API client and
  local or remote backend facade.
- **FILE-010**: `blackbox/cli/` contains Typer command modules and compatibility
  aliases.
- **FILE-011**: `blackbox_server/` contains the FastAPI service, application
  services, persistence, workers, authentication, authorization, ingestion,
  redaction, artifact storage, and API routes.
- **FILE-012**: `alembic/` contains immutable PostgreSQL migration revisions.
- **FILE-013**: `schemas/` contains versioned JSON Schemas for traces, suites,
  results, imports, exports, and plugin configuration.
- **FILE-014**: `web/` contains the React and TypeScript web application and
  generated API types.
- **FILE-015**: `infra/azure/` contains modular Bicep and environment parameter
  files.
- **FILE-016**: `docker-compose.yml` defines the complete local service stack.
- **FILE-017**: `.github/workflows/` contains CI, security, package, container,
  release, and Azure deployment workflows.
- **FILE-018**: `tests/` contains unit, contract, schema, integration,
  end-to-end, security, performance, migration, and golden fixture tests.
- **FILE-019**: `docs/` contains user, SDK, extension, API, operator, security,
  deployment, upgrade, and contribution documentation.
- **FILE-020**: `README.md` presents local quick start, service quick start,
  screenshots, supported integrations, security posture, and compatibility.

## 6. Testing

- **TEST-001**: Existing offline tests and documented demo metrics pass without
  PostgreSQL, Docker, Azure, network access, or optional evaluators.
- **TEST-002**: Version `1` traces and suites migrate to version `2.0` and
  preserve answers, refusals, citations, violations, tokens, configuration
  hashes, and verdicts.
- **TEST-003**: Schema round-trip and property tests cover every event, span,
  assertion, evaluator result, artifact reference, and error variant.
- **TEST-004**: Multi-turn tests verify ordered messages, per-turn assertions,
  conversation outcomes, timeouts, partial conversations, and replay.
- **TEST-005**: Multi-agent tests verify parent-child spans, handoff links,
  concurrent subagents, failures, cancellation, and cycle rejection.
- **TEST-006**: Tool assertion tests verify ordered and unordered calls,
  argument JSONPath, results, errors, retries, parallel calls, and prohibited
  tools.
- **TEST-007**: Side-effect tests verify fixture lifecycle, snapshots, cleanup
  after failure, explicit destructive approval, artifact integrity, and
  credential non-persistence.
- **TEST-008**: Evaluator contract tests run every plugin against golden traces,
  validate score ranges, verify cache isolation, and surface unavailable judges.
- **TEST-009**: OTLP and Foundry import tests map complete, partial, unknown, and
  malformed traces without losing unmapped attributes.
- **TEST-010**: API integration tests cover CRUD, ingestion idempotency,
  pagination, concurrency conflicts, cancellation, job recovery, exports,
  retention, and RFC 9457 errors.
- **TEST-011**: Tenant-isolation tests attempt cross-workspace reads, writes,
  guessed IDs, artifact downloads, exports, worker jobs, and admin operations
  for every role and require denial.
- **TEST-012**: OIDC tests cover valid tokens, issuer mismatch, audience
  mismatch, expiry, future not-before, unknown key, rotation, disabled tenant,
  missing role, and replayed authorization code state.
- **TEST-013**: Security tests cover secret redaction, stored and reflected XSS,
  SSRF, path traversal, unsafe Markdown, decompression bombs, oversized payloads,
  malicious JSON, SQL injection, and artifact content disposition.
- **TEST-014**: Web tests cover dataset authoring, run execution, live updates,
  trace inspection, comparison, review, RBAC visibility, errors, and WCAG 2.2 AA.
- **TEST-015**: Performance tests enforce the budgets in TASK-069 on a versioned
  reference environment and store trend results.
- **TEST-016**: Upgrade tests restore the previous release database and artifacts,
  apply migrations, run smoke evaluations, and verify rollback documentation.
- **TEST-017**: Backup-restore tests create tenant data, back up PostgreSQL and
  artifacts, restore into an empty environment, and verify digests and access.
- **TEST-018**: Azure deployment tests run Bicep validation and what-if, deploy
  an ephemeral environment, verify identity and private connectivity, execute a
  sample evaluation, and delete the environment.
- **TEST-019**: Release-gate tests verify JUnit and JSON outputs and nonzero exit
  codes for every configured threshold.
- **TEST-020**: Full acceptance tests evaluate the same agent locally and
  remotely and require equivalent case verdicts, assertion results, and trace
  digests after normalization.

## 7. Risks & Assumptions

- **RISK-001**: Building the complete platform in one milestone can delay usable
  feedback. Mitigation: release each implementation phase behind stable schemas
  and deployable vertical slices even though version `1.0.0` requires all phases.
- **RISK-002**: Model-based judges can be biased, nondeterministic, expensive,
  or vulnerable to prompt injection. Mitigation: keep them opt-in, record all
  judge metadata, calibrate against human labels, and exclude them from default
  hard gates.
- **RISK-003**: Traces can contain regulated or customer-sensitive data.
  Mitigation: pre-persistence redaction, workspace retention, export and
  deletion, external artifact storage, least privilege, and deployment guidance.
- **RISK-004**: Side-effect evaluation can modify real systems. Mitigation:
  fixture isolation, dry-run support, explicit destructive approval, scoped
  credentials, snapshots, cleanup, and visible audit records.
- **RISK-005**: OpenTelemetry GenAI conventions can evolve. Mitigation: isolate
  semantic mapping, preserve raw attributes, and version import mappings.
- **RISK-006**: PostgreSQL job queues can become a scaling bottleneck.
  Mitigation: abstract queue operations, index lease queries, measure backlog,
  and permit a future Service Bus or other queue plugin.
- **RISK-007**: Schema expansion can break current users. Mitigation:
  compatibility re-exports, migration readers, golden fixtures, and explicit
  deprecation windows.
- **RISK-008**: Multi-tenant authorization defects can expose trace data.
  Mitigation: mandatory workspace-scoped repositories, server-side authorization,
  PostgreSQL row-level security, and adversarial isolation tests.
- **RISK-009**: A custom web UI can consume disproportionate effort.
  Mitigation: generated clients, a maintained accessible component library,
  strict feature boundaries, and no business logic in the UI.
- **RISK-010**: Azure reference infrastructure can make the project appear
  Azure-only. Mitigation: keep cloud interfaces abstract, provide Docker Compose,
  and test the service without Azure SDKs.
- **ASSUMPTION-001**: PostgreSQL 16 is acceptable for all hosted deployments and
  users needing only local evaluation continue using file storage.
- **ASSUMPTION-002**: Initial users accept an English-only UI and documentation;
  UI strings are centralized to permit later localization.
- **ASSUMPTION-003**: One API and worker deployment can serve multiple
  workspaces when authorization and data isolation are enforced.
- **ASSUMPTION-004**: The initial web release requires desktop and tablet
  support; mobile authoring is not a release criterion.
- **ASSUMPTION-005**: Microsoft Entra is available for validating the reference
  OIDC implementation and Azure deployment.
- **ASSUMPTION-006**: Existing deterministic lexical groundedness remains
  available as a transparent baseline after optional semantic evaluators are
  added.

## 8. Related Specifications / Further Reading

- [Current feature specification](../specs/001-agent-black-box/spec.md)
- [Current implementation plan](../specs/001-agent-black-box/plan.md)
- [Current task breakdown](../specs/001-agent-black-box/tasks.md)
- [Project README](../README.md)
- [OpenTelemetry Generative AI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)
- [FastAPI documentation](https://fastapi.tiangolo.com/)
- [PostgreSQL row security policies](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)
- [Azure Container Apps documentation](https://learn.microsoft.com/azure/container-apps/)
- [Microsoft identity platform OIDC](https://learn.microsoft.com/entra/identity-platform/v2-protocols-oidc)
