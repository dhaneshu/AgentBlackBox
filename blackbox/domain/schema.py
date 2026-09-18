"""Schema identifiers, validation errors, and in-memory migrations."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import Any, Final
import warnings

CURRENT_SCHEMA_VERSION: Final = "2.0"
TRACE_SCHEMA_ID: Final = "https://agentblackbox.dev/schemas/trace-2.0.schema.json"
SUITE_SCHEMA_ID: Final = "https://agentblackbox.dev/schemas/suite-2.0.schema.json"
RESULT_SCHEMA_ID: Final = "https://agentblackbox.dev/schemas/result-2.0.schema.json"
EVALUATION_RESULT_ID_PATTERN: Final = r"^evaluation-[0-9a-f]{24}$"


class SchemaError(ValueError):
    """Base error for versioned domain documents."""


class UnsupportedSchemaVersionError(SchemaError):
    """Raised when no safe migration exists for a document."""


Migration = Callable[[dict[str, Any]], dict[str, Any]]
MIGRATIONS: dict[tuple[str, str, str], Migration] = {}
_WARNED_LEGACY_KINDS: set[str] = set()


def evaluation_result_identifier(run_id: str, case_id: str) -> str:
    """Return the canonical deterministic identity for one run/case evaluation."""
    digest = sha256(f"{run_id}\0{case_id}".encode("utf-8")).hexdigest()[:24]
    return f"evaluation-{digest}"


def register_migration(kind: str, source: str, target: str, migration: Migration) -> None:
    MIGRATIONS[(kind, source, target)] = migration


def migrate_document(kind: str, document: dict[str, Any]) -> dict[str, Any]:
    """Return a current-version copy without mutating caller-owned data."""
    source = str(document.get("schema_version", "1"))
    if source == CURRENT_SCHEMA_VERSION:
        return dict(document)
    migration = MIGRATIONS.get((kind, source, CURRENT_SCHEMA_VERSION))
    if migration is None:
        raise UnsupportedSchemaVersionError(
            f"Unsupported {kind} schema version {source!r}; "
            f"expected {CURRENT_SCHEMA_VERSION!r}"
        )
    if kind not in _WARNED_LEGACY_KINDS:
        warnings.warn(
            f"{kind} schema {source} is deprecated and was migrated in memory to "
            f"{CURRENT_SCHEMA_VERSION}; persist schema-v2 output",
            DeprecationWarning,
            stacklevel=2,
        )
        _WARNED_LEGACY_KINDS.add(kind)
    return migration(dict(document))


from blackbox.domain.migrations import v1_to_v2 as _v1_to_v2  # noqa: E402,F401
