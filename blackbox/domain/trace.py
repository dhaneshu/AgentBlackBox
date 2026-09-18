"""Versioned conversation and distributed-trace domain contracts."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

from blackbox.domain.schema import CURRENT_SCHEMA_VERSION, migrate_document

DEFAULT_PAYLOAD_LIMIT = 1_048_576
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Kept for source compatibility. New integrations should use Event subclasses.
STEP_KINDS: tuple[str, ...] = (
    "receive", "retrieve", "tool", "generate", "refuse", "policy"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a cryptographically random, lexicographically sortable ULID."""
    value = ((timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000)
             << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically, without insignificant whitespace."""
    return json.dumps(
        _json_value(value), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _payload_bytes(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _check_payload(value: Any, limit: int, identifier: str) -> None:
    size = _payload_bytes(value)
    if size > limit:
        raise PayloadTooLargeError(identifier, size, limit)


class TraceValidationError(ValueError):
    """A trace graph or payload violates an ingestion invariant."""


class PayloadTooLargeError(TraceValidationError):
    def __init__(self, identifier: str, actual_bytes: int, limit_bytes: int):
        self.identifier = identifier
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"payload {identifier!r} is {actual_bytes} bytes; limit is {limit_bytes}"
        )


class TraceCorruptionError(TraceValidationError):
    def __init__(
        self, path: Path, line: int, message: str, event_id: str | None = None
    ):
        self.path, self.line, self.event_id = path, line, event_id
        suffix = f" (event {event_id})" if event_id else ""
        super().__init__(f"{path}:{line}: {message}{suffix}")


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    digest: str
    media_type: str = "application/octet-stream"
    size_bytes: int | None = None
    name: str = ""
    artifact_id: str = field(default_factory=new_ulid)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            uri=str(data.get("uri", "")), digest=str(data.get("digest", "")),
            media_type=str(data.get("media_type", "application/octet-stream")),
            size_bytes=(int(data["size_bytes"]) if data.get("size_bytes") is not None else None),
            name=str(data.get("name", "")),
            artifact_id=str(data.get("artifact_id") or new_ulid()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate_payload(self, limit: int = DEFAULT_PAYLOAD_LIMIT) -> None:
        _check_payload(self.to_dict(), limit, self.artifact_id)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_calls: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cached_input_tokens,
               self.tool_calls) < 0:
            raise TraceValidationError("usage values cannot be negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Usage":
        return cls(**{f.name: int(data.get(f.name, 0)) for f in fields(cls)})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Cost:
    amount: Decimal = Decimal("0")
    currency: str = "USD"
    provider: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", self.currency.upper())
        if not self.amount.is_finite() or self.amount < 0:
            raise TraceValidationError("cost must be finite and cannot be negative")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Cost":
        return cls(data.get("amount", "0"), str(data.get("currency", "USD")),
                   str(data.get("provider", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"amount": format(self.amount, "f"), "currency": self.currency,
                "provider": self.provider}


@dataclass(frozen=True)
class ErrorInfo:
    type: str
    message: str
    stack: str = ""
    retryable: bool = False
    code: str = ""

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorInfo":
        import traceback
        return cls(type(exc).__name__, str(exc), "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ErrorInfo":
        return cls(str(data.get("type", "")), str(data.get("message", "")),
                   str(data.get("stack", "")), bool(data.get("retryable", False)),
                   str(data.get("code", "")))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpanLink:
    trace_id: str
    span_id: str
    relationship: Literal["follows_from", "handoff", "related"] = "related"
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.relationship not in {"follows_from", "handoff", "related"}:
            raise TraceValidationError(
                f"unknown span link relationship {self.relationship!r}"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpanLink":
        return cls(str(data.get("trace_id", "")), str(data.get("span_id", "")),
                   str(data.get("relationship", "related")),  # type: ignore[arg-type]
                   dict(data.get("attributes") or {}))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: Any
    message_id: str = field(default_factory=new_ulid)
    name: str = ""
    created_at: str = field(default_factory=now)
    artifact_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool", "developer"}:
            raise TraceValidationError(f"unknown message role {self.role!r}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        return cls(
            str(data.get("role", "user")), data.get("content"),  # type: ignore[arg-type]
            str(data.get("message_id") or new_ulid()), str(data.get("name", "")),
            str(data.get("created_at") or now()),
            tuple(ArtifactRef.from_dict(a) for a in data.get("artifact_refs") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id, "role": self.role, "content": self.content,
            "name": self.name, "created_at": self.created_at,
            "artifact_refs": [a.to_dict() for a in self.artifact_refs],
        }


@dataclass(frozen=True)
class Event:
    """Base class for discriminated span events."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=new_ulid)
    timestamp: str = field(default_factory=now)
    timestamp_ns: int = field(default_factory=time.time_ns)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    type: ClassVar[str] = "custom"

    def validate_payload(self, limit: int = DEFAULT_PAYLOAD_LIMIT) -> None:
        _check_payload(self.payload, limit, self.event_id)
        for artifact in self.artifact_refs:
            artifact.validate_payload(limit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "event_id": self.event_id, "name": self.name,
            "timestamp": self.timestamp, "timestamp_ns": self.timestamp_ns,
            "payload": self.payload,
            "artifact_refs": [a.to_dict() for a in self.artifact_refs],
        }


@dataclass(frozen=True)
class AgentHandoffEvent(Event):
    type: ClassVar[str] = "agent_handoff"


@dataclass(frozen=True)
class RetrievalEvent(Event):
    type: ClassVar[str] = "retrieval"


@dataclass(frozen=True)
class ModelRequestEvent(Event):
    type: ClassVar[str] = "model_request"


@dataclass(frozen=True)
class ModelResponseEvent(Event):
    type: ClassVar[str] = "model_response"


@dataclass(frozen=True)
class ToolRequestEvent(Event):
    type: ClassVar[str] = "tool_request"


@dataclass(frozen=True)
class ToolResponseEvent(Event):
    type: ClassVar[str] = "tool_response"


@dataclass(frozen=True)
class PolicyDecisionEvent(Event):
    type: ClassVar[str] = "policy_decision"


@dataclass(frozen=True)
class EvaluatorResultEvent(Event):
    type: ClassVar[str] = "evaluator_result"


@dataclass(frozen=True)
class HumanReviewEvent(Event):
    type: ClassVar[str] = "human_review"


@dataclass(frozen=True)
class ErrorEvent(Event):
    type: ClassVar[str] = "error"


@dataclass(frozen=True)
class CustomEvent(Event):
    type: ClassVar[str] = "custom"


EVENT_TYPES: dict[str, type[Event]] = {
    cls.type: cls for cls in (
        AgentHandoffEvent, RetrievalEvent, ModelRequestEvent, ModelResponseEvent,
        ToolRequestEvent, ToolResponseEvent, PolicyDecisionEvent,
        EvaluatorResultEvent, HumanReviewEvent, ErrorEvent, CustomEvent,
    )
}


def event_from_dict(data: Mapping[str, Any]) -> Event:
    event_type = str(data.get("type", "custom"))
    try:
        cls = EVENT_TYPES[event_type]
    except KeyError as exc:
        raise TraceValidationError(f"unknown event type {event_type!r}") from exc
    return cls(
        name=str(data.get("name", "")), payload=dict(data.get("payload") or {}),
        event_id=str(data.get("event_id") or new_ulid()),
        timestamp=str(data.get("timestamp") or now()),
        timestamp_ns=int(data.get("timestamp_ns", 0)),
        artifact_refs=tuple(ArtifactRef.from_dict(a)
                            for a in data.get("artifact_refs") or []),
    )


@dataclass
class Span:
    name: str
    trace_id: str
    conversation_id: str
    turn_id: str
    span_id: str = field(default_factory=new_ulid)
    parent_span_id: str | None = None
    kind: Literal["agent", "model", "tool", "retrieval", "policy", "evaluator",
                  "internal"] = "internal"
    agent_id: str = ""
    status: Literal["unset", "ok", "error", "cancelled"] = "unset"
    started_at: str = field(default_factory=now)
    ended_at: str = ""
    start_ns: int = 0
    end_ns: int = 0
    duration_ns: int = 0
    links: list[SpanLink] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    cost: Cost = field(default_factory=Cost)
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "agent", "model", "tool", "retrieval", "policy", "evaluator", "internal"
        }:
            raise TraceValidationError(f"unknown span kind {self.kind!r}")
        if self.status not in {"unset", "ok", "error", "cancelled"}:
            raise TraceValidationError(f"unknown span status {self.status!r}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Span":
        return cls(
            name=str(data.get("name", "")), trace_id=str(data.get("trace_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
            turn_id=str(data.get("turn_id", "")),
            span_id=str(data.get("span_id") or new_ulid()),
            parent_span_id=(str(data["parent_span_id"]) if data.get("parent_span_id") else None),
            kind=str(data.get("kind", "internal")),  # type: ignore[arg-type]
            agent_id=str(data.get("agent_id", "")),
            status=str(data.get("status", "unset")),  # type: ignore[arg-type]
            started_at=str(data.get("started_at", "")), ended_at=str(data.get("ended_at", "")),
            start_ns=int(data.get("start_ns", 0)), end_ns=int(data.get("end_ns", 0)),
            duration_ns=int(data.get("duration_ns", 0)),
            links=[SpanLink.from_dict(v) for v in data.get("links") or []],
            events=[event_from_dict(v) for v in data.get("events") or []],
            attributes=dict(data.get("attributes") or {}),
            usage=Usage.from_dict(data.get("usage") or {}),
            cost=Cost.from_dict(data.get("cost") or {}),
            error=(ErrorInfo.from_dict(data["error"]) if data.get("error") else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id, "trace_id": self.trace_id, "name": self.name,
            "conversation_id": self.conversation_id, "turn_id": self.turn_id,
            "parent_span_id": self.parent_span_id, "kind": self.kind,
            "agent_id": self.agent_id, "status": self.status,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "start_ns": self.start_ns, "end_ns": self.end_ns,
            "duration_ns": self.duration_ns,
            "links": [v.to_dict() for v in self.links],
            "events": [v.to_dict() for v in self.events],
            "attributes": self.attributes, "usage": self.usage.to_dict(),
            "cost": self.cost.to_dict(),
            "error": self.error.to_dict() if self.error else None,
        }


HANDOFF_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"accepted", "failed", "cancelled"},
    "accepted": {"completed", "failed", "cancelled"},
    "completed": set(), "failed": set(), "cancelled": set(),
}


@dataclass
class AgentHandoff:
    source_span_id: str
    target_span_id: str | None
    source_agent_id: str
    target_agent_id: str
    status: Literal["requested", "accepted", "completed", "failed",
                    "cancelled"] = "requested"
    handoff_id: str = field(default_factory=new_ulid)
    payload_ref: ArtifactRef | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status not in HANDOFF_TRANSITIONS:
            raise TraceValidationError(f"unknown handoff status {self.status!r}")
        if self.status != "requested" and not self.target_span_id:
            raise TraceValidationError(
                f"handoff {self.handoff_id!r} status {self.status!r} requires a target span"
            )

    def transition(self, status: str, error: ErrorInfo | None = None) -> None:
        if status not in HANDOFF_TRANSITIONS.get(self.status, set()):
            raise TraceValidationError(
                f"invalid handoff transition {self.status!r} -> {status!r}"
            )
        if status == "accepted" and not self.target_span_id:
            raise TraceValidationError(
                f"handoff {self.handoff_id!r} cannot be accepted without a target span"
            )
        self.status = status  # type: ignore[assignment]
        self.updated_at = now()
        self.error = error

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentHandoff":
        return cls(
            str(data.get("source_span_id", "")),
            (str(data["target_span_id"]) if data.get("target_span_id") else None),
            str(data.get("source_agent_id", "")), str(data.get("target_agent_id", "")),
            str(data.get("status", "requested")),  # type: ignore[arg-type]
            str(data.get("handoff_id") or new_ulid()),
            ArtifactRef.from_dict(data["payload_ref"]) if data.get("payload_ref") else None,
            str(data.get("created_at") or now()), str(data.get("updated_at") or now()),
            ErrorInfo.from_dict(data["error"]) if data.get("error") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id, "source_span_id": self.source_span_id,
            "target_span_id": self.target_span_id,
            "source_agent_id": self.source_agent_id,
            "target_agent_id": self.target_agent_id, "status": self.status,
            "payload_ref": self.payload_ref.to_dict() if self.payload_ref else None,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass
class Turn:
    conversation_id: str
    agent_id: str
    turn_id: str = field(default_factory=new_ulid)
    messages: list[Message] = field(default_factory=list)
    span_ids: list[str] = field(default_factory=list)
    status: Literal["active", "completed", "failed", "cancelled"] = "active"
    started_at: str = field(default_factory=now)
    ended_at: str = ""
    start_ns: int = 0
    end_ns: int = 0
    duration_ns: int = 0
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status not in {"active", "completed", "failed", "cancelled"}:
            raise TraceValidationError(f"unknown turn status {self.status!r}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Turn":
        return cls(
            str(data.get("conversation_id", "")), str(data.get("agent_id", "")),
            str(data.get("turn_id") or new_ulid()),
            [Message.from_dict(v) for v in data.get("messages") or []],
            [str(v) for v in data.get("span_ids") or []],
            str(data.get("status", "active")),  # type: ignore[arg-type]
            str(data.get("started_at", "")), str(data.get("ended_at", "")),
            int(data.get("start_ns", 0)), int(data.get("end_ns", 0)),
            int(data.get("duration_ns", 0)),
            ErrorInfo.from_dict(data["error"]) if data.get("error") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["messages"] = [v.to_dict() for v in self.messages]
        result["error"] = self.error.to_dict() if self.error else None
        return result


@dataclass
class Conversation:
    conversation_id: str = field(default_factory=new_ulid)
    title: str = ""
    participants: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    status: Literal["active", "completed", "failed", "cancelled"] = "active"
    started_at: str = field(default_factory=now)
    ended_at: str = ""
    start_ns: int = 0
    end_ns: int = 0
    duration_ns: int = 0
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status not in {"active", "completed", "failed", "cancelled"}:
            raise TraceValidationError(
                f"unknown conversation status {self.status!r}"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Conversation":
        return cls(
            str(data.get("conversation_id") or new_ulid()), str(data.get("title", "")),
            [str(v) for v in data.get("participants") or []],
            [Turn.from_dict(v) for v in data.get("turns") or []],
            str(data.get("status", "active")),  # type: ignore[arg-type]
            str(data.get("started_at", "")), str(data.get("ended_at", "")),
            int(data.get("start_ns", 0)), int(data.get("end_ns", 0)),
            int(data.get("duration_ns", 0)),
            ErrorInfo.from_dict(data["error"]) if data.get("error") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["turns"] = [v.to_dict() for v in self.turns]
        result["error"] = self.error.to_dict() if self.error else None
        return result


@dataclass(frozen=True)
class Citation:
    source: str
    lines: tuple[int, int]
    quote: str = ""
    claim: str = ""
    support: float = 0.0

    @property
    def supports(self) -> bool:
        return self.support >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "lines": list(self.lines), "quote": self.quote,
                "claim": self.claim, "support": round(self.support, 4)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Citation":
        lines = data.get("lines") or [0, 0]
        return cls(str(data.get("source", "")), (int(lines[0]), int(lines[-1])),
                   str(data.get("quote", "")), str(data.get("claim", "")),
                   float(data.get("support", 0.0)))

    def __str__(self) -> str:
        lo, hi = self.lines
        return f"{self.source}:{lo}" if lo == hi else f"{self.source}:{lo}-{hi}"


@dataclass(frozen=True)
class Violation:
    policy: str
    severity: str
    detail: str
    step_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Violation":
        return cls(str(data.get("policy", "")), str(data.get("severity", "low")),
                   str(data.get("detail", "")), int(data.get("step_index", -1)))


@dataclass(frozen=True)
class Step:
    index: int
    kind: str
    name: str
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Step":
        return cls(int(data.get("index", 0)), str(data.get("kind", "")),
                   str(data.get("name", "")), str(data.get("summary", "")),
                   dict(data.get("detail") or {}))


@dataclass
class Trace:
    """A backward-compatible single-turn result plus its distributed graph."""

    run_id: str
    case_id: str
    agent: str
    question: str
    answer: str = ""
    refused: bool = False
    refusal_reason: str = ""
    steps: list[Step] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: str = ""
    schema_version: str = CURRENT_SCHEMA_VERSION
    workspace_id: str = "local"
    project_id: str = "default"
    dataset_id: str = "legacy-suite"
    experiment_id: str = ""
    conversation_id: str = ""
    turn_id: str = ""
    trace_id: str = field(default_factory=new_ulid)
    conversations: list[Conversation] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    handoffs: list[AgentHandoff] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: Cost = field(default_factory=Cost)
    payload_limit_bytes: int = DEFAULT_PAYLOAD_LIMIT

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False,
                                   compare=False)

    @property
    def config_hash(self) -> str:
        return content_digest(self.config)[:12]

    @property
    def digest(self) -> str:
        document = self.to_dict()
        return content_digest(document)

    @property
    def grounded(self) -> bool:
        return self.refused or bool(self.citations) and all(c.supports for c in self.citations)

    @property
    def unsupported_claims(self) -> list[Citation]:
        return [c for c in self.citations if not c.supports]

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def worst_violation(self) -> str:
        order = ("low", "medium", "high", "critical")
        return (max(self.violations, key=lambda v: order.index(v.severity)).severity
                if self.violations else "")

    def validate(self) -> None:
        with self._lock:
            conversations = {v.conversation_id: v for v in self.conversations}
            turns = {t.turn_id: t for c in self.conversations for t in c.turns}
            spans = {v.span_id: v for v in self.spans}
            if len(conversations) != len(self.conversations):
                raise TraceValidationError("duplicate conversation_id")
            turn_count = sum(len(c.turns) for c in self.conversations)
            if len(turns) != turn_count:
                raise TraceValidationError("duplicate turn_id")
            if len(spans) != len(self.spans):
                raise TraceValidationError("duplicate span_id")
            for conversation in self.conversations:
                if conversation.status not in {
                    "active", "completed", "failed", "cancelled"
                }:
                    raise TraceValidationError(
                        f"unknown conversation status {conversation.status!r}"
                    )
                for turn in conversation.turns:
                    if turn.status not in {
                        "active", "completed", "failed", "cancelled"
                    }:
                        raise TraceValidationError(
                            f"unknown turn status {turn.status!r}"
                        )
                    if turn.conversation_id != conversation.conversation_id:
                        raise TraceValidationError(
                            f"turn {turn.turn_id!r} belongs to another conversation"
                        )
                    for message in turn.messages:
                        if message.role not in {
                            "system", "user", "assistant", "tool", "developer"
                        }:
                            raise TraceValidationError(
                                f"unknown message role {message.role!r}"
                            )
                        _check_payload(
                            message.content, self.payload_limit_bytes,
                            message.message_id,
                        )
                        for artifact in message.artifact_refs:
                            artifact.validate_payload(self.payload_limit_bytes)
            owned_span_ids: dict[str, list[str]] = {turn_id: [] for turn_id in turns}
            for span in self.spans:
                if span.kind not in {
                    "agent", "model", "tool", "retrieval", "policy",
                    "evaluator", "internal",
                }:
                    raise TraceValidationError(f"unknown span kind {span.kind!r}")
                if span.status not in {"unset", "ok", "error", "cancelled"}:
                    raise TraceValidationError(
                        f"unknown span status {span.status!r}"
                    )
                if span.trace_id != self.trace_id:
                    raise TraceValidationError(
                        f"span {span.span_id!r} belongs to another trace"
                    )
                if span.conversation_id not in conversations:
                    raise TraceValidationError(
                        f"orphan span {span.span_id!r}: conversation not found"
                    )
                turn = turns.get(span.turn_id)
                if turn is None or turn.conversation_id != span.conversation_id:
                    raise TraceValidationError(
                        f"orphan span {span.span_id!r}: turn not found"
                    )
                owned_span_ids[span.turn_id].append(span.span_id)
                if span.parent_span_id and span.parent_span_id not in spans:
                    raise TraceValidationError(
                        f"orphan span {span.span_id!r}: parent not found"
                    )
                if span.parent_span_id:
                    parent = spans[span.parent_span_id]
                    if (parent.conversation_id, parent.turn_id) != (
                        span.conversation_id, span.turn_id
                    ):
                        raise TraceValidationError(
                            f"span {span.span_id!r} parent belongs to another turn"
                        )
                _check_payload(
                    span.attributes, self.payload_limit_bytes, span.span_id
                )
                for link in span.links:
                    if link.relationship not in {
                        "follows_from", "handoff", "related"
                    }:
                        raise TraceValidationError(
                            f"unknown span link relationship {link.relationship!r}"
                        )
                for event in span.events:
                    if event.type not in EVENT_TYPES:
                        raise TraceValidationError(
                            f"unknown event type {event.type!r}"
                        )
                    event.validate_payload(self.payload_limit_bytes)
            for turn_id, turn in turns.items():
                declared = turn.span_ids
                owned = owned_span_ids[turn_id]
                if len(declared) != len(set(declared)):
                    raise TraceValidationError(
                        f"turn {turn_id!r} contains duplicate span_ids"
                    )
                if set(declared) != set(owned):
                    raise TraceValidationError(
                        f"turn {turn_id!r} span_ids do not exactly match owned spans"
                    )
            for handoff in self.handoffs:
                if handoff.status not in HANDOFF_TRANSITIONS:
                    raise TraceValidationError(
                        f"unknown handoff status {handoff.status!r}"
                    )
                if handoff.source_span_id not in spans:
                    raise TraceValidationError(
                        f"handoff {handoff.handoff_id!r} references an orphan source span"
                    )
                source = spans[handoff.source_span_id]
                if source.agent_id != handoff.source_agent_id:
                    raise TraceValidationError(
                        f"handoff {handoff.handoff_id!r} source agent does not own source span"
                    )
                if handoff.payload_ref:
                    handoff.payload_ref.validate_payload(self.payload_limit_bytes)
                if handoff.target_span_id is None:
                    if handoff.status != "requested":
                        raise TraceValidationError(
                            f"handoff {handoff.handoff_id!r} has no target span"
                        )
                    continue
                if handoff.target_span_id not in spans:
                    raise TraceValidationError(
                        f"handoff {handoff.handoff_id!r} references an orphan target span"
                    )
                target = spans[handoff.target_span_id]
                if target.agent_id != handoff.target_agent_id:
                    raise TraceValidationError(
                        f"handoff {handoff.handoff_id!r} target agent does not own target span"
                    )
                if not any(link.span_id == source.span_id and link.relationship == "handoff"
                           for link in target.links):
                    raise TraceValidationError(
                        f"handoff {handoff.handoff_id!r} is missing its target span link"
                    )
            for artifact in self.artifacts:
                artifact.validate_payload(self.payload_limit_bytes)
            for span in self.spans:
                seen: set[str] = set()
                cursor: Span | None = span
                while cursor and cursor.parent_span_id:
                    if cursor.span_id in seen:
                        raise TraceValidationError(
                            f"cyclic span ancestry at {cursor.span_id!r}"
                        )
                    seen.add(cursor.span_id)
                    cursor = spans.get(cursor.parent_span_id)

    def to_dict(self) -> dict[str, Any]:
        conversation_id = self.conversation_id or f"conversation-{self.run_id}-{self.case_id}"
        turn_id = self.turn_id or f"turn-{self.run_id}-{self.case_id}-1"
        return {
            "schema_version": self.schema_version, "workspace_id": self.workspace_id,
            "project_id": self.project_id, "dataset_id": self.dataset_id,
            "experiment_id": self.experiment_id or f"experiment-{self.run_id}",
            "run_id": self.run_id, "conversation_id": conversation_id,
            "turn_id": turn_id, "case_id": self.case_id, "agent": self.agent,
            "question": self.question, "answer": self.answer, "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "steps": [v.to_dict() for v in self.steps],
            "citations": [v.to_dict() for v in self.citations],
            "violations": [v.to_dict() for v in self.violations],
            "config": self.config, "config_hash": self.config_hash,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "started_at": self.started_at, "trace_id": self.trace_id,
            "conversations": [v.to_dict() for v in self.conversations],
            "spans": [v.to_dict() for v in self.spans],
            "handoffs": [v.to_dict() for v in self.handoffs],
            "artifacts": [v.to_dict() for v in self.artifacts],
            "usage": self.usage.to_dict(), "cost": self.cost.to_dict(),
            "payload_limit_bytes": self.payload_limit_bytes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Trace":
        data = migrate_document("trace", raw)
        trace = cls(
            run_id=str(data.get("run_id", "")), case_id=str(data.get("case_id", "")),
            agent=str(data.get("agent", "")), question=str(data.get("question", "")),
            answer=str(data.get("answer", "")), refused=bool(data.get("refused", False)),
            refusal_reason=str(data.get("refusal_reason", "")),
            steps=[Step.from_dict(v) for v in data.get("steps") or []],
            citations=[Citation.from_dict(v) for v in data.get("citations") or []],
            violations=[Violation.from_dict(v) for v in data.get("violations") or []],
            config=dict(data.get("config") or {}), tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
            started_at=str(data.get("started_at", "")),
            schema_version=str(data["schema_version"]),
            workspace_id=str(data.get("workspace_id", "local")),
            project_id=str(data.get("project_id", "default")),
            dataset_id=str(data.get("dataset_id", "legacy-suite")),
            experiment_id=str(data.get("experiment_id", "")),
            conversation_id=str(data.get("conversation_id", "")),
            turn_id=str(data.get("turn_id", "")),
            trace_id=str(data.get("trace_id") or new_ulid()),
            conversations=[Conversation.from_dict(v) for v in data.get("conversations") or []],
            spans=[Span.from_dict(v) for v in data.get("spans") or []],
            handoffs=[AgentHandoff.from_dict(v) for v in data.get("handoffs") or []],
            artifacts=[ArtifactRef.from_dict(v) for v in data.get("artifacts") or []],
            usage=Usage.from_dict(data.get("usage") or {
                "input_tokens": data.get("tokens_in", 0),
                "output_tokens": data.get("tokens_out", 0),
            }),
            cost=Cost.from_dict(data.get("cost") or {}),
            payload_limit_bytes=int(data.get("payload_limit_bytes", DEFAULT_PAYLOAD_LIMIT)),
        )
        trace.validate()
        return trace


T = TypeVar("T", bound=Trace)


class JSONLWriter:
    def __init__(self, path: str | Path, *, append: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a" if append else "w", encoding="utf-8", newline="\n")

    def write(self, trace: Trace) -> None:
        trace.validate()
        self._handle.write(canonical_json(trace.to_dict()) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "JSONLWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def iter_traces(path: str | Path) -> Iterator[Trace]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Trace file not found: {target}")
    with target.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            event_id: str | None = None
            try:
                document = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TraceCorruptionError(
                    target, line_no, f"invalid JSON ({exc.msg})"
                ) from exc
            event_id = _recover_event_id(document)
            try:
                _validate_trace_shape(document)
                yield Trace.from_dict(document)
            except (TraceValidationError, TypeError, ValueError, KeyError,
                    IndexError, InvalidOperation) as exc:
                raise TraceCorruptionError(
                    target, line_no, str(exc), event_id
                ) from exc


def _recover_event_id(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    direct = document.get("event_id")
    if isinstance(direct, str) and direct:
        return direct
    spans = document.get("spans")
    if not isinstance(spans, list):
        return None
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("events"), list):
            continue
        for event in span["events"]:
            if isinstance(event, dict) and isinstance(event.get("event_id"), str):
                return event["event_id"] or None
    return None


def _validate_trace_shape(document: Any) -> None:
    if not isinstance(document, dict):
        raise TraceValidationError("trace must be a JSON object")
    for key in ("steps", "citations", "violations", "conversations", "spans",
                "handoffs", "artifacts"):
        value = document.get(key, [])
        if not isinstance(value, list):
            raise TraceValidationError(f"{key} must be an array")
        if not all(isinstance(item, dict) for item in value):
            raise TraceValidationError(f"{key} entries must be objects")
    for key in ("config", "usage", "cost"):
        if key in document and not isinstance(document[key], dict):
            raise TraceValidationError(f"{key} must be an object")
    for step in document.get("steps", []):
        if not isinstance(step.get("detail", {}), dict):
            raise TraceValidationError("step detail must be an object")
    _validate_artifact_array(document.get("artifacts", []), "artifacts")
    for conversation in document.get("conversations", []):
        _validate_optional_object(conversation.get("error"), "conversation error")
        turns = conversation.get("turns", [])
        if not isinstance(turns, list) or not all(isinstance(v, dict) for v in turns):
            raise TraceValidationError("conversation turns must be an array of objects")
        for turn in turns:
            _validate_optional_object(turn.get("error"), "turn error")
            for key in ("messages", "span_ids"):
                if not isinstance(turn.get(key, []), list):
                    raise TraceValidationError(f"turn {key} must be an array")
            if not all(isinstance(v, dict) for v in turn.get("messages", [])):
                raise TraceValidationError("turn messages must contain objects")
            for message in turn.get("messages", []):
                _validate_artifact_array(
                    message.get("artifact_refs", []), "message artifact_refs"
                )
    for span in document.get("spans", []):
        for key in ("attributes", "usage", "cost"):
            if key in span and not isinstance(span[key], dict):
                raise TraceValidationError(f"span {key} must be an object")
        _validate_optional_object(span.get("error"), "span error")
        for key in ("links", "events"):
            value = span.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                raise TraceValidationError(f"span {key} must be an array of objects")
        for link in span.get("links", []):
            if not isinstance(link.get("attributes", {}), dict):
                raise TraceValidationError("span link attributes must be an object")
        for event in span.get("events", []):
            if not isinstance(event.get("payload", {}), dict):
                raise TraceValidationError("event payload must be an object")
            _validate_artifact_array(
                event.get("artifact_refs", []), "event artifact_refs"
            )
    for handoff in document.get("handoffs", []):
        _validate_optional_object(handoff.get("payload_ref"), "handoff payload_ref")
        _validate_optional_object(handoff.get("error"), "handoff error")


def _validate_optional_object(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise TraceValidationError(f"{name} must be an object or null")


def _validate_artifact_array(value: Any, name: str) -> None:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, dict) for item in value
    ):
        raise TraceValidationError(f"{name} must be an array of objects")


def save_traces(traces: Iterable[Trace], path: str | Path) -> Path:
    with JSONLWriter(path) as writer:
        for trace in traces:
            writer.write(trace)
    return Path(path)


def load_traces(path: str | Path) -> list[Trace]:
    return list(iter_traces(path))
