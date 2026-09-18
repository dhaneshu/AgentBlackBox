"""Schema-validated webhook and JSONL trace imports with declarative mapping."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.domain.trace import DEFAULT_PAYLOAD_LIMIT, Trace, canonical_json

from .base import PermanentAdapterError
from .http import redact


class ImportAdapterError(PermanentAdapterError):
    pass


@dataclass(frozen=True)
class DeclarativeMapping:
    """Map source paths to destination paths and optional literal defaults."""

    fields: Mapping[str, str] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)

    def apply(self, source: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for destination, source_path in self.fields.items():
            try:
                value = _get(source, source_path)
            except (KeyError, IndexError, TypeError) as exc:
                if destination in self.defaults:
                    value = self.defaults[destination]
                else:
                    raise ImportAdapterError(
                        "IMPORT_MAPPING_FAILED",
                        f"cannot map {source_path!r} to {destination!r}",
                        details={"source": source_path, "destination": destination},
                    ) from exc
            _set(result, destination, value)
        for destination, value in self.defaults.items():
            if not _has(result, destination):
                _set(result, destination, value)
        return result


class TraceImportAdapter:
    def __init__(
        self,
        mapping: DeclarativeMapping | Mapping[str, str] | None = None,
        *,
        schema: Mapping[str, Any] | None = None,
        payload_limit_bytes: int = DEFAULT_PAYLOAD_LIMIT,
    ) -> None:
        self.mapping = (
            mapping if isinstance(mapping, DeclarativeMapping)
            else DeclarativeMapping(mapping or {})
        )
        self.schema = dict(schema or {})
        self.payload_limit_bytes = payload_limit_bytes
        if payload_limit_bytes <= 0:
            raise ValueError("payload_limit_bytes must be positive")

    def import_document(self, source: Mapping[str, Any]) -> Trace:
        document = (
            self.mapping.apply(source) if self.mapping.fields or self.mapping.defaults
            else dict(source)
        )
        size = len(canonical_json(document).encode("utf-8"))
        if size > self.payload_limit_bytes:
            raise ImportAdapterError(
                "IMPORT_TOO_LARGE",
                f"import payload is {size} bytes; limit is {self.payload_limit_bytes}",
            )
        self._validate_schema(document)
        document.setdefault("payload_limit_bytes", self.payload_limit_bytes)
        try:
            return Trace.from_dict(document)
        except Exception as exc:
            raise ImportAdapterError(
                "INVALID_TRACE", f"mapped import is not a valid trace: {exc}",
                details={"document": redact(document)},
            ) from exc

    def _validate_schema(self, document: Mapping[str, Any]) -> None:
        if not self.schema:
            return
        try:
            import jsonschema
        except ImportError as exc:
            raise ImportAdapterError(
                "MISSING_SCHEMA_DEPENDENCY",
                "custom import schema validation requires the optional SDK dependency",
            ) from exc
        try:
            jsonschema.Draft202012Validator(self.schema).validate(document)
        except jsonschema.ValidationError as exc:
            path = ".".join(str(value) for value in exc.absolute_path)
            raise ImportAdapterError(
                "IMPORT_SCHEMA_INVALID",
                f"import schema validation failed at {path or '<root>'}: {exc.message}",
            ) from exc


class WebhookTraceImporter(TraceImportAdapter):
    """Import a decoded webhook body; HTTP serving is intentionally Phase 6."""

    def import_payload(
        self, body: bytes | str | Mapping[str, Any], *, content_type: str = "application/json"
    ) -> Trace:
        if isinstance(body, Mapping):
            document = body
        else:
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            if len(raw) > self.payload_limit_bytes:
                raise ImportAdapterError(
                    "IMPORT_TOO_LARGE",
                    f"webhook payload exceeds {self.payload_limit_bytes} bytes",
                )
            if content_type.split(";", 1)[0].strip().lower() not in {
                "application/json", "application/cloudevents+json"
            }:
                raise ImportAdapterError(
                    "UNSUPPORTED_CONTENT_TYPE",
                    f"unsupported webhook content type {content_type!r}",
                )
            try:
                document = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ImportAdapterError(
                    "INVALID_JSON", "webhook body is not valid UTF-8 JSON"
                ) from exc
        if not isinstance(document, Mapping):
            raise ImportAdapterError("INVALID_JSON", "webhook body must be a JSON object")
        return self.import_document(document)


class JSONLTraceImporter(TraceImportAdapter):
    def iter_lines(self, lines: Iterable[str], *, source: str = "<stream>"):
        for line_number, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(stripped.encode("utf-8")) > self.payload_limit_bytes:
                raise ImportAdapterError(
                    "IMPORT_TOO_LARGE",
                    f"{source}:{line_number}: line exceeds {self.payload_limit_bytes} bytes",
                )
            try:
                document = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ImportAdapterError(
                    "INVALID_JSON",
                    f"{source}:{line_number}: invalid JSON ({exc.msg})",
                ) from exc
            if not isinstance(document, Mapping):
                raise ImportAdapterError(
                    "INVALID_JSON", f"{source}:{line_number}: expected a JSON object"
                )
            try:
                yield self.import_document(document)
            except ImportAdapterError as exc:
                raise ImportAdapterError(
                    exc.code, f"{source}:{line_number}: {exc}",
                    details=exc.details,
                ) from exc

    def import_file(self, path: str | Path) -> list[Trace]:
        target = Path(path)
        with target.open("r", encoding="utf-8") as handle:
            return list(self.iter_lines(handle, source=str(target)))


def _get(value: Any, path: str) -> Any:
    current = value
    for component in path.split(".") if path else ():
        if isinstance(current, Mapping):
            current = current[component]
        elif isinstance(current, (list, tuple)):
            current = current[int(component)]
        else:
            raise TypeError(f"cannot traverse {component!r}")
    return current


def _set(target: dict[str, Any], path: str, value: Any) -> None:
    components = path.split(".")
    current = target
    for component in components[:-1]:
        child = current.setdefault(component, {})
        if not isinstance(child, dict):
            raise ImportAdapterError(
                "IMPORT_MAPPING_FAILED", f"mapping destination collision at {component!r}"
            )
        current = child
    current[components[-1]] = value


def _has(target: Mapping[str, Any], path: str) -> bool:
    try:
        _get(target, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False
