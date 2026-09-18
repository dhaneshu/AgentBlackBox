"""Synchronous and asynchronous recording contexts."""

from __future__ import annotations

import asyncio
import contextvars
import sys
import time
import warnings
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Generic, TypeVar

from blackbox.domain.trace import (
    AgentHandoff,
    AgentHandoffEvent,
    ArtifactRef,
    Citation,
    Conversation,
    ErrorEvent,
    ErrorInfo,
    Event,
    Message,
    Span,
    SpanLink,
    Step,
    Trace,
    TraceValidationError,
    Turn,
    Violation,
    now,
)

_warned_legacy_recorder = False
T = TypeVar("T", Conversation, Turn, Span)


class _RecordingContext(
    AbstractContextManager[T], AbstractAsyncContextManager[T], Generic[T]
):
    def __init__(
        self,
        recorder: "Recorder",
        value: T,
        variable: contextvars.ContextVar[T | None],
    ):
        self.recorder, self.value, self.variable = recorder, value, variable
        self._token: contextvars.Token[T | None] | None = None

    def __enter__(self) -> T:
        self._token = self.variable.set(self.value)
        self.value.start_ns = time.perf_counter_ns()
        self.value.started_at = now()
        try:
            self.recorder._attach(self.value)
        except BaseException:
            self.variable.reset(self._token)
            self._token = None
            raise
        return self.value

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        self._finish(exc)
        return False

    async def __aenter__(self) -> T:
        return self.__enter__()

    async def __aexit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def _finish(self, exc: BaseException | None) -> None:
        self.value.end_ns = time.perf_counter_ns()
        self.value.duration_ns = max(0, self.value.end_ns - self.value.start_ns)
        self.value.ended_at = now()
        if exc is None:
            if isinstance(self.value, Span):
                if self.value.status == "unset":
                    self.value.status = "ok"
            elif self.value.status == "active":
                self.value.status = "completed"
        elif isinstance(exc, asyncio.CancelledError):
            self.value.status = "cancelled"
            self.value.error = ErrorInfo.from_exception(exc)
        else:
            self.value.status = "error" if isinstance(self.value, Span) else "failed"
            self.value.error = ErrorInfo.from_exception(exc)
            if isinstance(self.value, Span):
                self.value.events.append(ErrorEvent(
                    name="exception",
                    payload={"error_type": self.value.error.type, "captured": True},
                ))
        if self._token is not None:
            self.variable.reset(self._token)
        self.recorder.trace.validate()


class _HandoffTargetContext(
    AbstractContextManager[Span], AbstractAsyncContextManager[Span]
):
    def __init__(
        self, recorder: "Recorder", handoff: AgentHandoff,
        span_context: _RecordingContext[Span],
    ):
        self.recorder, self.handoff, self.span_context = (
            recorder, handoff, span_context
        )

    def __enter__(self) -> Span:
        target = self.span_context.__enter__()
        try:
            self.recorder._bind_handoff(self.handoff, target)
            self.recorder.transition_handoff(self.handoff, "accepted")
        except BaseException:
            self.span_context.__exit__(*sys.exc_info())
            raise
        return target

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        result = self.span_context.__exit__(exc_type, exc, tb)
        status = (
            "cancelled" if isinstance(exc, asyncio.CancelledError)
            else "failed" if exc is not None
            else "completed"
        )
        self.recorder.transition_handoff(self.handoff, status, exc)
        return result

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)


class Recorder:
    """Accumulates a backward-compatible trace and a distributed span graph."""

    def __init__(
        self,
        run_id: str,
        case_id: str,
        agent: str,
        question: str,
        config: dict[str, Any] | None = None,
        *,
        payload_limit_bytes: int | None = None,
    ):
        global _warned_legacy_recorder
        if not _warned_legacy_recorder:
            warnings.warn(
                "blackbox.recorder.Recorder remains supported for single-turn runs; "
                "new integrations should target the versioned domain contracts",
                DeprecationWarning,
                stacklevel=2,
            )
            _warned_legacy_recorder = True
        self.trace = Trace(
            run_id=run_id, case_id=case_id, agent=agent, question=question,
            config=dict(config or {}), started_at=now(),
        )
        if payload_limit_bytes is not None:
            if payload_limit_bytes <= 0:
                raise ValueError("payload_limit_bytes must be positive")
            self.trace.payload_limit_bytes = payload_limit_bytes
        self._conversation: contextvars.ContextVar[Conversation | None] = (
            contextvars.ContextVar(f"blackbox_conversation_{id(self)}", default=None)
        )
        self._turn: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
            f"blackbox_turn_{id(self)}", default=None
        )
        self._span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
            f"blackbox_span_{id(self)}", default=None
        )
        self.step("receive", "question", question)

    @property
    def current_conversation(self) -> Conversation | None:
        return self._conversation.get()

    @property
    def current_turn(self) -> Turn | None:
        return self._turn.get()

    @property
    def current_span(self) -> Span | None:
        return self._span.get()

    def conversation(
        self, title: str = "", *, conversation_id: str | None = None,
        participants: list[str] | None = None,
    ) -> _RecordingContext[Conversation]:
        conversation = Conversation(
            conversation_id=conversation_id or self.trace.conversation_id or "",
            title=title, participants=list(participants or [self.trace.agent]),
        )
        if not conversation.conversation_id:
            from blackbox.domain.trace import new_ulid
            conversation.conversation_id = new_ulid()
        return _RecordingContext(self, conversation, self._conversation)

    def turn(
        self, agent_id: str | None = None, *, turn_id: str | None = None,
        messages: list[Message] | None = None,
    ) -> _RecordingContext[Turn]:
        conversation = self.current_conversation
        if conversation is None:
            raise TraceValidationError("turn() requires an active conversation()")
        from blackbox.domain.trace import new_ulid
        value = Turn(
            conversation_id=conversation.conversation_id,
            agent_id=agent_id or self.trace.agent,
            turn_id=turn_id or new_ulid(), messages=list(messages or []),
        )
        return _RecordingContext(self, value, self._turn)

    def span(
        self, name: str, *, kind: str = "internal", agent_id: str | None = None,
        span_id: str | None = None, parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> _RecordingContext[Span]:
        conversation, turn = self.current_conversation, self.current_turn
        if conversation is None or turn is None:
            raise TraceValidationError(
                "span() requires active conversation() and turn() contexts"
            )
        parent = self.current_span
        from blackbox.domain.trace import new_ulid
        value = Span(
            name=name, trace_id=self.trace.trace_id,
            conversation_id=conversation.conversation_id, turn_id=turn.turn_id,
            span_id=span_id or new_ulid(),
            parent_span_id=parent_span_id if parent_span_id is not None else (
                parent.span_id if parent else None
            ),
            kind=kind,  # type: ignore[arg-type]
            agent_id=agent_id or turn.agent_id,
            attributes=dict(attributes or {}),
        )
        return _RecordingContext(self, value, self._span)

    # Explicit aliases make async instrumentation discoverable while contexts
    # themselves support both ``with`` and ``async with``.
    aconversation = conversation
    aturn = turn
    aspan = span

    def _attach(self, value: Conversation | Turn | Span) -> None:
        with self.trace._lock:
            if isinstance(value, Conversation):
                if any(v.conversation_id == value.conversation_id
                       for v in self.trace.conversations):
                    raise TraceValidationError(
                        f"duplicate conversation_id {value.conversation_id!r}"
                    )
                self.trace.conversations.append(value)
                self.trace.conversation_id = value.conversation_id
            elif isinstance(value, Turn):
                conversation = self.current_conversation
                if conversation is None:
                    raise TraceValidationError("orphan turn")
                if any(v.turn_id == value.turn_id for v in conversation.turns):
                    raise TraceValidationError(f"duplicate turn_id {value.turn_id!r}")
                conversation.turns.append(value)
                self.trace.turn_id = value.turn_id
            else:
                if any(v.span_id == value.span_id for v in self.trace.spans):
                    raise TraceValidationError(f"duplicate span_id {value.span_id!r}")
                self.trace.spans.append(value)
                turn = self.current_turn
                if turn is None:
                    raise TraceValidationError("orphan span")
                turn.span_ids.append(value.span_id)
                self.trace.validate()

    def event(self, event: Event) -> Event:
        span = self.current_span
        if span is None:
            raise TraceValidationError("event() requires an active span()")
        event.validate_payload(self.trace.payload_limit_bytes)
        with self.trace._lock:
            span.events.append(event)
        return event

    async def arecord_event(self, event: Event) -> Event:
        return self.event(event)

    def message(
        self, role: str, content: Any, *, name: str = "",
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> Message:
        turn = self.current_turn
        if turn is None:
            raise TraceValidationError("message() requires an active turn()")
        message = Message(role, content, name=name, artifact_refs=artifact_refs)  # type: ignore[arg-type]
        from blackbox.domain.trace import _check_payload
        _check_payload(message.content, self.trace.payload_limit_bytes, message.message_id)
        for artifact in message.artifact_refs:
            artifact.validate_payload(self.trace.payload_limit_bytes)
        with self.trace._lock:
            turn.messages.append(message)
        return message

    async def amessage(
        self, role: str, content: Any, *, name: str = "",
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> Message:
        return self.message(role, content, name=name, artifact_refs=artifact_refs)

    def request_handoff(
        self, target_agent_id: str, *,
        payload_ref: ArtifactRef | None = None,
    ) -> AgentHandoff:
        source = self.current_span
        if source is None:
            raise TraceValidationError(
                "request_handoff() requires an active source span()"
            )
        if payload_ref:
            payload_ref.validate_payload(self.trace.payload_limit_bytes)
        handoff = AgentHandoff(
            source.span_id, None, source.agent_id, target_agent_id,
            payload_ref=payload_ref,
        )
        self.event(AgentHandoffEvent(
            "handoff.requested",
            {"handoff_id": handoff.handoff_id, "target_agent_id": target_agent_id},
            artifact_refs=((payload_ref,) if payload_ref else ()),
        ))
        with self.trace._lock:
            self.trace.handoffs.append(handoff)
        self.trace.validate()
        return handoff

    def accept_handoff(
        self, handoff: AgentHandoff, name: str, *, kind: str = "agent",
        attributes: dict[str, Any] | None = None,
    ) -> _HandoffTargetContext:
        if handoff not in self.trace.handoffs:
            raise TraceValidationError("handoff is not owned by this recorder")
        if handoff.status != "requested" or handoff.target_span_id is not None:
            raise TraceValidationError("handoff is not awaiting a target")
        return _HandoffTargetContext(
            self, handoff,
            self.span(name, kind=kind, agent_id=handoff.target_agent_id,
                      attributes=attributes),
        )

    def _bind_handoff(self, handoff: AgentHandoff, target_span: Span) -> None:
        if target_span not in self.trace.spans:
            raise TraceValidationError("handoff target is not owned by this recorder")
        if target_span.trace_id != self.trace.trace_id:
            raise TraceValidationError("handoff target belongs to another trace")
        if target_span.agent_id != handoff.target_agent_id:
            raise TraceValidationError(
                "handoff target agent does not own target span"
            )
        source = next(
            (span for span in self.trace.spans
             if span.span_id == handoff.source_span_id), None
        )
        if source is None or source.agent_id != handoff.source_agent_id:
            raise TraceValidationError(
                "handoff source agent does not own source span"
            )
        handoff.target_span_id = target_span.span_id
        target_span.links.append(SpanLink(
            self.trace.trace_id, source.span_id, "handoff",
            {"source_agent_id": source.agent_id,
             "target_agent_id": handoff.target_agent_id},
        ))
        self.trace.validate()

    def handoff(
        self, target_span: Span, target_agent_id: str, *,
        payload_ref: ArtifactRef | None = None,
    ) -> AgentHandoff:
        """Compatibility helper for callers that already created a target span."""
        handoff = self.request_handoff(
            target_agent_id, payload_ref=payload_ref
        )
        self._bind_handoff(handoff, target_span)
        return handoff

    async def ahandoff(
        self, target_span: Span, target_agent_id: str, *,
        payload_ref: ArtifactRef | None = None,
    ) -> AgentHandoff:
        return self.handoff(target_span, target_agent_id, payload_ref=payload_ref)

    async def arequest_handoff(
        self, target_agent_id: str, *,
        payload_ref: ArtifactRef | None = None,
    ) -> AgentHandoff:
        return self.request_handoff(target_agent_id, payload_ref=payload_ref)

    def transition_handoff(
        self, handoff: AgentHandoff, status: str,
        error: BaseException | ErrorInfo | None = None,
    ) -> AgentHandoff:
        if handoff not in self.trace.handoffs:
            raise TraceValidationError("handoff is not owned by this recorder")
        info = (ErrorInfo.from_exception(error) if isinstance(error, BaseException)
                else error)
        handoff.transition(status, info)
        spans = {v.span_id: v for v in self.trace.spans}
        spans[handoff.source_span_id].events.append(AgentHandoffEvent(
            f"handoff.{status}",
            {"handoff_id": handoff.handoff_id, "status": status,
             "error": info.to_dict() if info else None},
        ))
        return handoff

    async def atransition_handoff(
        self, handoff: AgentHandoff, status: str,
        error: BaseException | ErrorInfo | None = None,
    ) -> AgentHandoff:
        return self.transition_handoff(handoff, status, error)

    # Legacy helpers -----------------------------------------------------
    def step(self, kind: str, name: str, summary: str = "", **detail: Any) -> Step:
        entry = Step(len(self.trace.steps), kind, name, summary, detail)
        with self.trace._lock:
            self.trace.steps.append(entry)
        return entry

    def retrieved(self, query: str, hits: list[Any]) -> Step:
        return self.step(
            "retrieve", "corpus.search",
            f"{len(hits)} passage(s), best {hits[0].score:.2f}" if hits else "no hits",
            query=query,
            hits=[{"citation": hit.passage.citation, "score": round(hit.score, 4)}
                  for hit in hits],
        )

    def tool(self, name: str, arguments: dict[str, Any], result: str) -> Step:
        return self.step("tool", name, str(result)[:160], arguments=arguments,
                         result=str(result))

    def cite(self, claim: str, passage: Any, support: float, quote: str = "") -> Citation:
        citation = Citation(passage.source, passage.lines, quote or passage.text[:180],
                            claim, support)
        self.trace.citations.append(citation)
        return citation

    def answered(self, answer: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.trace.answer, self.trace.refused = answer, False
        self.trace.tokens_in += tokens_in
        self.trace.tokens_out += tokens_out
        self.step("generate", "compose", answer[:160])

    def refused(self, reason: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.trace.answer, self.trace.refused = "", True
        self.trace.refusal_reason = reason
        self.trace.tokens_in += tokens_in
        self.trace.tokens_out += tokens_out
        self.step("refuse", "decline", reason)

    def violated(self, policy: str, severity: str, detail: str) -> Violation:
        value = Violation(policy, severity, detail, len(self.trace.steps) - 1)
        self.trace.violations.append(value)
        return value

    def finish(self) -> Trace:
        self.trace.validate()
        return self.trace
