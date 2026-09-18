"""Asynchronous bounded-concurrency execution over the AgentAdapter lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from blackbox.adapters import (
    AdapterResponse,
    AgentAdapter,
    PermanentAdapterError,
    PreparedCase,
    TransientAdapterError,
)
from blackbox.adapters.http import redact
from blackbox.domain.suite import EvaluationCase
from blackbox.domain.trace import (
    Conversation,
    ErrorInfo,
    Message,
    Span,
    Step,
    Trace,
    Turn,
    JSONLWriter,
    new_ulid,
    now,
)


@dataclass(frozen=True)
class RunnerConfig:
    concurrency: int = 4
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_delay_seconds: float = 0.1
    payload_limit_bytes: int = 1_048_576
    random_seed: int | None = None

    def __post_init__(self) -> None:
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0 or self.retry_delay_seconds < 0:
            raise ValueError("retry settings cannot be negative")
        if self.payload_limit_bytes <= 0:
            raise ValueError("payload_limit_bytes must be positive")


@dataclass(frozen=True)
class CaseExecution:
    index: int
    case_id: str
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    trace: Trace
    attempts: int = 1
    error: dict[str, Any] | None = None


@dataclass
class ExecutionRun:
    run_id: str
    agent: str
    cases: list[CaseExecution] = field(default_factory=list)
    status: Literal["completed", "partial", "failed", "cancelled"] = "completed"
    output_path: Path | None = None
    random_seed: int | None = None

    @property
    def traces(self) -> list[Trace]:
        return [item.trace for item in self.cases]

    @property
    def config(self) -> dict[str, Any]:
        return {
            "runtime_status": self.status,
            "random_seed": self.random_seed,
        }

    def by_case(self) -> dict[str, Trace]:
        return {trace.case_id: trace for trace in self.traces}

    def save(self, directory: str | Path) -> Path:
        target = Path(directory) / f"{self.run_id}.jsonl"
        _persist(self.cases, target)
        self.output_path = target
        return target


class AsyncRunner:
    def __init__(
        self,
        adapter: AgentAdapter,
        config: RunnerConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.config = config or RunnerConfig()
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        self._cancel.set()

    async def run(
        self,
        cases: list[EvaluationCase],
        *,
        run_id: str | None = None,
        output_path: str | Path | None = None,
    ) -> ExecutionRun:
        run_id = run_id or _run_id(self.adapter.name)
        semaphore = asyncio.Semaphore(self.config.concurrency)

        async def bounded(index: int, case: EvaluationCase) -> CaseExecution:
            async with semaphore:
                return await self._execute(index, case, run_id)

        tasks = [
            asyncio.create_task(bounded(index, case), name=f"blackbox:{case.id}")
            for index, case in enumerate(cases)
        ]
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self._cancel.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            await self.adapter.close()
        results.sort(key=lambda result: result.index)
        statuses = {result.status for result in results}
        if statuses == {"completed"}:
            status = "completed"
        elif statuses <= {"failed", "timed_out"}:
            status = "failed"
        elif statuses == {"cancelled"}:
            status = "cancelled"
        else:
            status = "partial"
        run = ExecutionRun(
            run_id, self.adapter.name, results, status,
            random_seed=self.config.random_seed,
        )
        if output_path is not None:
            target = Path(output_path)
            _persist(results, target)
            run.output_path = target
        return run

    async def _execute(
        self, index: int, case: EvaluationCase, run_id: str
    ) -> CaseExecution:
        if self._cancel.is_set():
            return self._failure(index, case, run_id, "cancelled", 0, None)
        timeout = case.timeout_seconds or self.config.timeout_seconds
        seed = (
            _case_seed(self.config.random_seed, case.id)
            if self.config.random_seed is not None and self.adapter.supports_seed
            else None
        )
        prepared = PreparedCase(
            case.id, run_id, case.messages, case.variables, case.metadata, seed
        )
        attempts = 0
        try:
            async with asyncio.timeout(timeout):
                while attempts <= self.config.max_retries:
                    attempts += 1
                    conversation = None
                    try:
                        if self._cancel.is_set():
                            raise asyncio.CancelledError()
                        state = await self.adapter.prepare(prepared)
                        correlation_id = _correlation_id(run_id, case.id, attempts)
                        conversation = await self.adapter.start_conversation(
                            state, correlation_id=correlation_id
                        )
                        last: AdapterResponse | Trace | None = None
                        for message in case.messages:
                            if self._cancel.is_set():
                                raise asyncio.CancelledError()
                            last = await self.adapter.send(conversation, message)
                        finished = await self.adapter.finish_conversation(conversation)
                        trace = _coerce_trace(
                            finished or last, case, run_id, self.adapter.name,
                            conversation.conversation_id, correlation_id,
                            self.config.payload_limit_bytes, seed,
                        )
                        return CaseExecution(
                            index, case.id, "completed", trace, attempts
                        )
                    except TransientAdapterError as exc:
                        if attempts > self.config.max_retries:
                            return self._failure(
                                index, case, run_id, "failed", attempts, exc
                            )
                        await asyncio.sleep(
                            self.config.retry_delay_seconds * attempts
                        )
        except TimeoutError:
            return self._failure(
                index, case, run_id, "timed_out", attempts,
                PermanentAdapterError(
                    "CASE_TIMEOUT", f"case exceeded timeout of {timeout:g} seconds"
                ),
            )
        except asyncio.CancelledError:
            if self._cancel.is_set():
                return self._failure(
                    index, case, run_id, "cancelled", attempts, None
                )
            raise
        except Exception as exc:
            error = (
                exc if isinstance(exc, PermanentAdapterError)
                else PermanentAdapterError(
                    "ADAPTER_FAILED",
                    f"adapter failed with {type(exc).__name__}: {exc}",
                )
            )
            return self._failure(
                index, case, run_id, "failed", attempts, error
            )
        raise AssertionError("unreachable")

    def _failure(
        self,
        index: int,
        case: EvaluationCase,
        run_id: str,
        status: Literal["failed", "timed_out", "cancelled"],
        attempts: int,
        error: Exception | None,
    ) -> CaseExecution:
        code = (
            getattr(error, "code", "")
            if error else ("CASE_CANCELLED" if status == "cancelled" else status.upper())
        )
        message = redact(str(error)) if error else "case execution was cancelled"
        safe_error = {
            "code": code,
            "message": message,
            "transient": bool(getattr(error, "transient", False)),
            "details": redact(getattr(error, "details", {})),
        }
        seed = (
            _case_seed(self.config.random_seed, case.id)
            if self.config.random_seed is not None and self.adapter.supports_seed
            else None
        )
        trace = Trace(
            run_id, case.id, self.adapter.name, case.question,
            steps=[Step(0, "error", "runtime.failure", message, safe_error)],
            config={
                "runtime.status": status,
                "runtime.attempts": attempts,
                "runtime.error": safe_error,
                **(
                    {"agentblackbox.random_seed": seed}
                    if seed is not None else {}
                ),
            },
            payload_limit_bytes=self.config.payload_limit_bytes,
        )
        return CaseExecution(index, case.id, status, trace, attempts, safe_error)


def _coerce_trace(
    value: Any,
    case: EvaluationCase,
    run_id: str,
    agent: str,
    conversation_id: str,
    correlation_id: str,
    payload_limit: int,
    seed: int | None,
) -> Trace:
    if isinstance(value, Trace):
        value.run_id = run_id
        value.case_id = case.id
        value.config.setdefault("agentblackbox.correlation_id", correlation_id)
        if seed is not None:
            value.config["agentblackbox.random_seed"] = seed
        value.validate()
        return value
    response = value if isinstance(value, AdapterResponse) else AdapterResponse(value or "")
    messages = list(case.messages)
    messages.append(Message("assistant", response.content))
    turn_id = new_ulid()
    span = Span(
        "agent.send",
        response.trace_id or new_ulid(),
        response.conversation_id or conversation_id,
        turn_id,
        kind="agent",
        agent_id=agent,
        status="ok",
        attributes={
            "agentblackbox.correlation_id": correlation_id,
            "adapter.metadata": redact(dict(response.metadata)),
        },
    )
    turn = Turn(
        span.conversation_id, agent, turn_id, messages, [span.span_id], "completed"
    )
    conversation = Conversation(
        span.conversation_id, case.id, [agent], [turn], "completed"
    )
    trace = Trace(
        run_id, case.id, agent, case.question,
        answer=str(response.content),
        refused=response.refused,
        refusal_reason=response.refusal_reason,
        trace_id=span.trace_id,
        conversation_id=span.conversation_id,
        turn_id=turn_id,
        conversations=[conversation],
        spans=[span],
        config={
            "agentblackbox.correlation_id": correlation_id,
            **({"agentblackbox.random_seed": seed} if seed is not None else {}),
        },
        payload_limit_bytes=payload_limit,
    )
    trace.validate()
    return trace


def _persist(results: list[CaseExecution], path: Path) -> None:
    with JSONLWriter(path) as writer:
        for result in sorted(results, key=lambda item: item.index):
            writer.write(result.trace)


def _correlation_id(run_id: str, case_id: str, attempt: int) -> str:
    return hashlib.sha256(
        f"{run_id}\0{case_id}\0{attempt}".encode("utf-8")
    ).hexdigest()[:32]


def _case_seed(seed: int | None, case_id: str) -> int | None:
    if seed is None:
        return None
    return int.from_bytes(
        hashlib.sha256(f"{seed}\0{case_id}".encode()).digest()[:8], "big"
    )


def _run_id(agent: str) -> str:
    return f"{now().replace(':', '').replace('-', '')[:15]}-{agent}-{new_ulid()[-6:]}"
