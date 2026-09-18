"""Lossless migration of the original single-turn documents to schema 2.0."""

from __future__ import annotations

import hashlib
from typing import Any

from blackbox.domain.schema import (
    CURRENT_SCHEMA_VERSION,
    evaluation_result_identifier,
    register_migration,
)


def _identifier(prefix: str, *parts: Any) -> str:
    value = "-".join(str(part) for part in parts if part)
    return f"{prefix}-{value}" if value else f"{prefix}-legacy"


def migrate_trace(document: dict[str, Any]) -> dict[str, Any]:
    run_id = str(document.get("run_id", ""))
    case_id = str(document.get("case_id", ""))
    document["schema_version"] = CURRENT_SCHEMA_VERSION
    document.setdefault("workspace_id", "local")
    document.setdefault("project_id", "default")
    document.setdefault("dataset_id", "legacy-suite")
    document.setdefault("experiment_id", _identifier("experiment", run_id))
    document.setdefault("conversation_id", _identifier("conversation", run_id, case_id))
    document.setdefault("turn_id", _identifier("turn", run_id, case_id, "1"))
    document.setdefault(
        "trace_id",
        hashlib.sha256(f"{run_id}\0{case_id}".encode("utf-8")).hexdigest()[:32],
    )
    document.setdefault("conversations", [])
    document.setdefault("spans", [])
    document.setdefault("handoffs", [])
    document.setdefault("artifacts", [])
    document.setdefault("usage", {
        "input_tokens": int(document.get("tokens_in", 0)),
        "output_tokens": int(document.get("tokens_out", 0)),
        "cached_input_tokens": 0,
        "tool_calls": 0,
    })
    document.setdefault("cost", {"amount": "0", "currency": "USD", "provider": ""})
    document.setdefault("payload_limit_bytes", 1_048_576)
    document.setdefault("refusal_reason", "")
    document.setdefault("steps", [])
    document.setdefault("citations", [])
    document.setdefault("violations", [])
    document.setdefault("config", {})
    document.setdefault("tokens_in", 0)
    document.setdefault("tokens_out", 0)
    document.setdefault("started_at", "")
    document.setdefault("answer", "")
    document.setdefault("refused", False)
    return document


def migrate_suite(document: dict[str, Any]) -> dict[str, Any]:
    document["schema_version"] = CURRENT_SCHEMA_VERSION
    if "messages" not in document:
        document["messages"] = [{
            "role": "user",
            "content": str(document.get("question", "")),
        }]
    document.setdefault("variables", {})
    document.setdefault("fixture_refs", [])
    document.setdefault("tags", [])
    document.setdefault("expected_outcome", document.get("expect", "answer"))
    assertions = list(document.get("assertions") or [])
    if not assertions:
        assertions.extend(
            {"type": "text_contains", "expected": value}
            for value in document.get("must_contain") or ()
        )
        assertions.extend(
            {"type": "citation", "expected": value}
            for value in document.get("must_cite") or ()
        )
        assertions.extend(
            {"type": "text_not_contains", "expected": value}
            for value in document.get("forbid") or ()
        )
        if document.get("expect") == "refuse":
            assertions.append({"type": "refusal", "expected": True})
    document["assertions"] = assertions
    document.setdefault("evaluators", [])
    document.setdefault("timeout_seconds", None)
    document.setdefault(
        "metadata", {"notes": document.get("notes", "")} if document.get("notes") else {}
    )
    for legacy_field in (
        "question", "expect", "must_contain", "must_cite", "forbid", "notes",
    ):
        document.pop(legacy_field, None)
    return document


def migrate_result(document: dict[str, Any]) -> dict[str, Any]:
    document["schema_version"] = CURRENT_SCHEMA_VERSION
    run_id = str(document.get("run_id", ""))
    migrated_results = []
    for result in document.get("results") or []:
        migrated = dict(result)
        case_id = str(migrated.get("case_id", ""))
        migrated["schema_version"] = CURRENT_SCHEMA_VERSION
        migrated["evaluation_result_id"] = evaluation_result_identifier(
            run_id, case_id
        )
        migrated.setdefault("assertion_results", [])
        migrated.setdefault("evaluator_results", [])
        migrated_results.append(migrated)
    document["results"] = migrated_results
    return document


for _kind, _migration in (
    ("trace", migrate_trace),
    ("suite", migrate_suite),
    ("result", migrate_result),
):
    register_migration(_kind, "1", CURRENT_SCHEMA_VERSION, _migration)
    register_migration(_kind, "1.0", CURRENT_SCHEMA_VERSION, _migration)
