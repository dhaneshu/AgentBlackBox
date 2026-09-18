"""Stable lifecycle and error contracts for agents under evaluation."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from blackbox.domain.trace import Message, Trace
from blackbox.plugins import PLUGIN_API_VERSION


class AdapterError(RuntimeError):
    """An explicit adapter failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        transient: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.transient = transient
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "transient": self.transient,
            "details": self.details,
        }


class TransientAdapterError(AdapterError):
    """A declared retryable transport or service failure."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(code, message, transient=True, details=details)


class PermanentAdapterError(AdapterError):
    """A non-retryable mapping, validation, authentication, or agent failure."""


@dataclass(frozen=True)
class PreparedCase:
    case_id: str
    run_id: str
    messages: tuple[Message, ...]
    variables: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    random_seed: int | None = None


@dataclass
class ConversationHandle:
    conversation_id: str
    state: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterResponse:
    content: Any
    refused: bool = False
    refusal_reason: str = ""
    conversation_id: str = ""
    trace_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Any = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Lifecycle implemented by local and remote agent integrations."""

    name: str
    api_version: str
    supports_seed: bool

    async def prepare(self, case: PreparedCase) -> Any: ...

    async def start_conversation(
        self, prepared: Any, *, correlation_id: str
    ) -> ConversationHandle: ...

    async def send(
        self, conversation: ConversationHandle, message: Message
    ) -> AdapterResponse | Trace | None: ...

    async def finish_conversation(
        self, conversation: ConversationHandle
    ) -> Trace | AdapterResponse | None: ...

    async def close(self) -> None: ...


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


API_VERSION = PLUGIN_API_VERSION
