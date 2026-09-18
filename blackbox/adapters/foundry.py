"""Optional Microsoft Foundry project-agent adapter and trace importer."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import inspect
import json
from typing import Any

from blackbox.domain.trace import (
    Conversation,
    Message,
    Span,
    Trace,
    Turn,
    Usage,
    new_ulid,
)

from .base import (
    API_VERSION,
    AdapterResponse,
    ConversationHandle,
    PermanentAdapterError,
    PreparedCase,
    TransientAdapterError,
    maybe_await,
)
from .http import redact


class FoundryAgentAdapter:
    """Drive a Foundry project agent through an injected or optional SDK client."""

    api_version = API_VERSION

    def __init__(
        self,
        project_endpoint: str,
        agent_id: str,
        *,
        name: str = "foundry-agent",
        client: Any = None,
        credential: Any = None,
        client_factory: Callable[..., Any] | None = None,
        max_retries: int = 2,
        random_seed: int | None = None,
    ) -> None:
        if not project_endpoint.startswith("https://"):
            raise ValueError("Foundry project endpoint must use HTTPS")
        if not agent_id:
            raise ValueError("Foundry agent_id is required")
        self.project_endpoint, self.agent_id, self.name = (
            project_endpoint.rstrip("/"), agent_id, name
        )
        self._client, self._credential, self._client_factory = (
            client, credential, client_factory
        )
        self.max_retries = max_retries
        self.random_seed = random_seed
        self.supports_seed = random_seed is not None
        self._owns_client = client is None

    async def prepare(self, case: PreparedCase) -> PreparedCase:
        await self._ensure_client()
        return case

    async def start_conversation(
        self, prepared: PreparedCase, *, correlation_id: str
    ) -> ConversationHandle:
        client = await self._ensure_client()
        try:
            thread = await maybe_await(_call_first(
                client,
                ("agents.threads.create", "threads.create", "conversations.create"),
                metadata={
                    "agentblackbox.correlation_id": correlation_id,
                    "agentblackbox.case_id": prepared.case_id,
                },
            ))
        except Exception as exc:
            raise _foundry_error("FOUNDRY_CONVERSATION_FAILED", exc) from exc
        identifier = str(_field(thread, "id", _field(thread, "conversation_id", "")))
        if not identifier:
            raise PermanentAdapterError(
                "INVALID_FOUNDRY_RESPONSE", "Foundry did not return a conversation ID"
            )
        return ConversationHandle(
            identifier,
            {"prepared": prepared, "client_thread": thread, "responses": []},
            {"correlation_id": correlation_id},
        )

    async def send(
        self, conversation: ConversationHandle, message: Message
    ) -> AdapterResponse:
        client = await self._ensure_client()
        kwargs = {
            "thread_id": conversation.conversation_id,
            "conversation_id": conversation.conversation_id,
            "role": message.role,
            "content": message.content,
        }
        try:
            created = await maybe_await(_call_first(
                client,
                ("agents.messages.create", "messages.create", "responses.create"),
                **kwargs,
            ))
        except Exception as exc:
            # Message creation is not safely retryable: a transport failure can
            # occur after the service accepted the prompt.
            raise PermanentAdapterError(
                "FOUNDRY_MESSAGE_AMBIGUOUS",
                f"Foundry message creation failed: {type(exc).__name__}; "
                "the prompt will not be retried",
            ) from exc
        last_error: TransientAdapterError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                run = await maybe_await(_call_first(
                    client,
                    ("agents.runs.create_and_process", "runs.create_and_process",
                     "agents.runs.create", "runs.create"),
                    thread_id=conversation.conversation_id,
                    conversation_id=conversation.conversation_id,
                    agent_id=self.agent_id,
                    seed=conversation.state["prepared"].random_seed,
                ))
                status = str(_field(run, "status", "completed")).lower()
                if status in {"failed", "cancelled", "expired"}:
                    raise PermanentAdapterError(
                        "FOUNDRY_RUN_FAILED", f"Foundry run ended with status {status}"
                    )
                output = _field(run, "output", _field(run, "content", None))
                if output is None:
                    output = _field(created, "output", _field(created, "content", ""))
                response = AdapterResponse(
                    _extract_text(output),
                    conversation_id=conversation.conversation_id,
                    trace_id=str(_field(run, "trace_id", "")),
                    metadata={
                        "foundry.run_id": str(_field(run, "id", "")),
                        "foundry.status": status,
                        "foundry.agent_id": self.agent_id,
                    },
                    raw=run,
                )
                conversation.state["responses"].append(response)
                return response
            except TransientAdapterError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise PermanentAdapterError(
                        "FOUNDRY_RUN_RETRIES_EXHAUSTED",
                        "Foundry run retries were exhausted after the prompt was "
                        "created; the prompt will not be submitted again",
                    ) from exc
            except PermanentAdapterError:
                raise
            except Exception as exc:
                converted = _foundry_error("FOUNDRY_SEND_FAILED", exc)
                if isinstance(converted, TransientAdapterError):
                    last_error = converted
                    if attempt < self.max_retries:
                        continue
                    raise PermanentAdapterError(
                        "FOUNDRY_RUN_RETRIES_EXHAUSTED",
                        "Foundry run retries were exhausted after the prompt was "
                        "created; the prompt will not be submitted again",
                    ) from exc
                raise converted from exc
        assert last_error
        raise PermanentAdapterError(
            "FOUNDRY_RUN_RETRIES_EXHAUSTED",
            "Foundry run retries were exhausted after the prompt was created",
        ) from last_error

    async def finish_conversation(
        self, conversation: ConversationHandle
    ) -> AdapterResponse | None:
        responses = conversation.state.get("responses", ())
        return responses[-1] if responses else None

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            closer = getattr(self._client, "close", None)
            if closer:
                await maybe_await(closer())
        self._client = None

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory:
            self._client = await maybe_await(self._client_factory(
                endpoint=self.project_endpoint, credential=self._credential
            ))
            return self._client
        try:
            from azure.ai.projects.aio import AIProjectClient
        except ImportError as exc:
            raise PermanentAdapterError(
                "MISSING_FOUNDRY_DEPENDENCY",
                "FoundryAgentAdapter requires the optional 'foundry' dependency",
            ) from exc
        if self._credential is None:
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError as exc:
                raise PermanentAdapterError(
                    "MISSING_FOUNDRY_DEPENDENCY",
                    "FoundryAgentAdapter requires azure-identity",
                ) from exc
            self._credential = DefaultAzureCredential()
        self._client = AIProjectClient(
            endpoint=self.project_endpoint, credential=self._credential
        )
        return self._client


class FoundryTraceImporter:
    """Map exported Foundry conversations/spans to trace schema 2.0."""

    def __init__(self, *, payload_limit_bytes: int = 1_048_576) -> None:
        self.payload_limit_bytes = payload_limit_bytes

    def import_trace(self, document: Mapping[str, Any]) -> Trace:
        size = len(json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8"))
        if size > self.payload_limit_bytes:
            raise PermanentAdapterError(
                "FOUNDRY_PAYLOAD_TOO_LARGE",
                f"Foundry trace is {size} bytes; limit is {self.payload_limit_bytes}",
            )
        conversation_id = str(
            document.get("conversation_id") or document.get("thread_id") or new_ulid()
        )
        trace_id = str(document.get("trace_id") or new_ulid())
        run_id = str(document.get("run_id") or document.get("response_id") or new_ulid())
        case_id = str(document.get("case_id") or f"foundry-{conversation_id}")
        agent = str(document.get("agent_id") or document.get("agent") or "foundry-agent")
        raw_messages = list(document.get("messages") or ())
        messages = [
            Message(
                _normalise_role(str(_field(item, "role", "user"))),
                _field(item, "content", ""),
                message_id=str(_field(item, "id", "") or new_ulid()),
                created_at=str(_field(item, "created_at", "")),
            )
            for item in raw_messages
        ]
        turn_id = str(document.get("turn_id") or new_ulid())
        spans = [
            self._span(item, trace_id, conversation_id, turn_id, agent)
            for item in document.get("spans") or ()
        ]
        turn = Turn(
            conversation_id, agent, turn_id, messages,
            [span.span_id for span in spans], "completed",
        )
        conversation = Conversation(
            conversation_id, str(document.get("title", "")), [agent], [turn],
            "completed",
        )
        answer = next(
            (str(message.content) for message in reversed(messages)
             if message.role == "assistant"),
            str(document.get("answer", "")),
        )
        question = next(
            (str(message.content) for message in reversed(messages)
             if message.role == "user"),
            str(document.get("question", "")),
        )
        usage = document.get("usage") or {}
        trace = Trace(
            run_id, case_id, agent, question, answer=answer,
            trace_id=trace_id, conversation_id=conversation_id, turn_id=turn_id,
            conversations=[conversation], spans=spans,
            usage=Usage(
                int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
                int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
            ),
            tokens_in=int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
            tokens_out=int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
            payload_limit_bytes=self.payload_limit_bytes,
            config={"foundry.metadata": _foundry_metadata(document)},
        )
        trace.validate()
        return trace

    def import_many(self, documents: list[Mapping[str, Any]]) -> list[Trace]:
        return [self.import_trace(document) for document in documents]

    def _span(
        self,
        item: Mapping[str, Any],
        trace_id: str,
        conversation_id: str,
        turn_id: str,
        agent: str,
    ) -> Span:
        attributes = redact(dict(item.get("attributes") or {}))
        namespaced = {
            key if str(key).startswith("foundry.") else f"foundry.{key}": value
            for key, value in attributes.items()
        }
        known = {
            "id", "span_id", "trace_id", "name", "parent_span_id", "kind",
            "status", "start_ns", "end_ns", "duration_ns", "attributes",
        }
        namespaced.update(redact({
            f"foundry.{key}": value for key, value in item.items() if key not in known
        }))
        start = int(item.get("start_ns", item.get("start_time_unix_nano", 0)))
        end = int(item.get("end_ns", item.get("end_time_unix_nano", start)))
        return Span(
            str(item.get("name", "foundry.operation")),
            trace_id,
            conversation_id,
            turn_id,
            str(item.get("span_id") or item.get("id") or new_ulid()),
            str(item["parent_span_id"]) if item.get("parent_span_id") else None,
            _normalise_kind(str(item.get("kind", "agent"))),
            agent,
            _normalise_status(str(item.get("status", "ok"))),
            start_ns=start,
            end_ns=end,
            duration_ns=max(0, int(item.get("duration_ns", end - start))),
            attributes=namespaced,
        )


def _call_first(client: Any, paths: tuple[str, ...], **kwargs: Any) -> Any:
    for path in paths:
        current = client
        for component in path.split("."):
            current = getattr(current, component, None)
            if current is None:
                break
        if callable(current):
            try:
                signature = inspect.signature(current)
            except (TypeError, ValueError):
                filtered = {k: v for k, v in kwargs.items() if v is not None}
            else:
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
                filtered = {
                    key: value for key, value in kwargs.items()
                    if value is not None
                    and (accepts_kwargs or key in signature.parameters)
                }
            return current(**filtered)
    raise PermanentAdapterError(
        "UNSUPPORTED_FOUNDRY_CLIENT",
        f"Foundry client implements none of: {', '.join(paths)}",
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _extract_text(value.get("text", value.get("value", value.get("content", ""))))
    if isinstance(value, (list, tuple)):
        return "".join(_extract_text(item) for item in value)
    return str(value or "")


def _foundry_error(code: str, exc: Exception):
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    if status in {408, 425, 429} or isinstance(status, int) and status >= 500:
        return TransientAdapterError(code, f"Foundry transient failure ({status})")
    return PermanentAdapterError(code, f"Foundry operation failed: {type(exc).__name__}")


def _normalise_role(role: str) -> str:
    return role if role in {"system", "user", "assistant", "tool", "developer"} else "assistant"


def _normalise_kind(kind: str) -> str:
    lowered = kind.lower()
    return lowered if lowered in {
        "agent", "model", "tool", "retrieval", "policy", "evaluator", "internal"
    } else "agent"


def _normalise_status(status: str) -> str:
    lowered = status.lower()
    if lowered in {"ok", "completed", "success", "succeeded"}:
        return "ok"
    if lowered in {"cancelled", "canceled"}:
        return "cancelled"
    if lowered in {"error", "failed"}:
        return "error"
    return "unset"


def _foundry_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "messages", "spans", "usage", "answer", "question", "trace_id",
        "conversation_id", "thread_id",
    }
    return redact({
        key if str(key).startswith("foundry.") else f"foundry.{key}": value
        for key, value in document.items() if key not in excluded
    })
