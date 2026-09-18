"""Stable evaluator contracts shared by built-in and third-party plugins."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable

from blackbox.domain.trace import content_digest


@dataclass(frozen=True)
class InputRequirement:
    """An input an evaluator requires before it may run."""

    name: str
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class Score:
    """A bounded numeric score with an optional pass threshold."""

    value: float
    minimum: float = 0.0
    maximum: float = 1.0
    threshold: float | None = None
    label: str = ""

    def __post_init__(self) -> None:
        values = (self.value, self.minimum, self.maximum)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("score values must be finite")
        if self.minimum >= self.maximum:
            raise ValueError("score minimum must be less than maximum")
        if not self.minimum <= self.value <= self.maximum:
            raise ValueError("score value is outside its declared range")
        if self.threshold is not None and not self.minimum <= self.threshold <= self.maximum:
            raise ValueError("score threshold is outside its declared range")

    @property
    def passed(self) -> bool | None:
        return self.value >= self.threshold if self.threshold is not None else None

    def normalized(self) -> float:
        return (self.value - self.minimum) / (self.maximum - self.minimum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "threshold": self.threshold,
            "label": self.label,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Score":
        return cls(
            float(value["value"]),
            float(value.get("minimum", 0.0)),
            float(value.get("maximum", 1.0)),
            float(value["threshold"]) if value.get("threshold") is not None else None,
            str(value.get("label", "")),
        )


class EvaluationError(RuntimeError):
    """Explicit evaluator failure; failures are never represented as success."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        evaluator: str = "",
        transient: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.evaluator = evaluator
        self.transient = transient
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "evaluator": self.evaluator,
            "transient": self.transient,
            "details": self.details,
        }


@dataclass
class EvaluatorContext:
    """All auditable inputs supplied to an evaluator."""

    trace: Any
    case: Any
    configuration: Mapping[str, Any] = field(default_factory=dict)
    assertion_results: tuple[Any, ...] = ()
    evaluator_results: Mapping[str, "EvaluatorResult"] = field(default_factory=dict)
    policies: Any = None
    repeats: tuple[Any, ...] = ()
    judge_deployment: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def available_inputs(self) -> dict[str, Any]:
        return {
            "trace": self.trace,
            "case": self.case,
            "configuration": self.configuration,
            "assertion_results": self.assertion_results,
            "evaluator_results": self.evaluator_results,
            "policies": self.policies,
            "repeats": self.repeats,
            "judge_deployment": self.judge_deployment,
            "metadata": self.metadata,
        }

    def validate_requirements(
        self, evaluator: str, requirements: tuple[InputRequirement, ...]
    ) -> None:
        available = self.available_inputs()
        missing = [
            requirement.name
            for requirement in requirements
            if requirement.required
            and (requirement.name not in available or available[requirement.name] is None)
        ]
        if missing:
            raise EvaluationError(
                "MISSING_INPUT",
                f"missing required evaluator input(s): {', '.join(missing)}",
                evaluator=evaluator,
                details={"missing": missing},
            )


@dataclass(frozen=True)
class EvaluatorResult:
    evaluator: str
    evaluator_version: str
    score: Score | None
    output: Any
    rationale: str = ""
    evidence_refs: tuple[str, ...] = ()
    configuration: Mapping[str, Any] = field(default_factory=dict)
    judge: Mapping[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    prompt_hash: str = ""
    latency_ms: float = 0.0
    token_usage: Mapping[str, int] = field(default_factory=dict)
    cost: Decimal | None = None
    error: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    result_id: str = ""

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("evaluation latency must be finite and non-negative")
        if self.error is not None and self.score is not None:
            raise ValueError("failed evaluator results cannot carry a score")
        if any(int(value) < 0 for value in self.token_usage.values()):
            raise ValueError("token usage cannot be negative")
        if self.cost is not None and Decimal(str(self.cost)) < 0:
            raise ValueError("evaluation cost cannot be negative")
        if not self.result_id:
            identity = {
                "evaluator": self.evaluator,
                "version": self.evaluator_version,
                "configuration": self.configuration,
                "judge": self.judge,
                "input_hash": self.input_hash,
                "prompt_hash": self.prompt_hash,
                "output": self.output,
                "error": self.error,
            }
            object.__setattr__(self, "result_id", f"eval-{content_digest(identity)[:24]}")

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "score": self.score.to_dict() if self.score else None,
            "output": self.output,
            "rationale": self.rationale,
            "evidence_refs": list(self.evidence_refs),
            "configuration": dict(self.configuration),
            "judge": dict(self.judge),
            "input_hash": self.input_hash,
            "prompt_hash": self.prompt_hash,
            "latency_ms": self.latency_ms,
            "token_usage": dict(self.token_usage),
            "cost": format(self.cost, "f") if self.cost is not None else None,
            "error": dict(self.error) if self.error else None,
            "metadata": dict(self.metadata),
        }

    def to_event(self):
        """Create the evaluator event used when attaching evidence to a trace span."""
        from blackbox.domain.trace import EvaluatorResultEvent
        return EvaluatorResultEvent(
            name=f"evaluator.{self.evaluator}",
            payload=self.to_dict(),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluatorResult":
        return cls(
            evaluator=str(value["evaluator"]),
            evaluator_version=str(value["evaluator_version"]),
            score=Score.from_dict(value["score"]) if value.get("score") else None,
            output=value.get("output"),
            rationale=str(value.get("rationale", "")),
            evidence_refs=tuple(str(v) for v in value.get("evidence_refs") or ()),
            configuration=dict(value.get("configuration") or {}),
            judge=dict(value.get("judge") or {}),
            input_hash=str(value.get("input_hash", "")),
            prompt_hash=str(value.get("prompt_hash", "")),
            latency_ms=float(value.get("latency_ms", 0.0)),
            token_usage={str(k): int(v) for k, v in (value.get("token_usage") or {}).items()},
            cost=Decimal(str(value["cost"])) if value.get("cost") is not None else None,
            error=dict(value["error"]) if value.get("error") else None,
            metadata=dict(value.get("metadata") or {}),
            result_id=str(value.get("result_id", "")),
        )


@runtime_checkable
class Evaluator(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    api_version: ClassVar[str]
    input_requirements: ClassVar[tuple[InputRequirement, ...]]

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult: ...

    async def evaluate_async(self, context: EvaluatorContext) -> EvaluatorResult: ...


class BaseEvaluator:
    """Convenience base with a safe async bridge for deterministic evaluators."""

    name: ClassVar[str] = "base"
    version: ClassVar[str] = "1.0.0"
    api_version: ClassVar[str] = "2.0"
    input_requirements: ClassVar[tuple[InputRequirement, ...]] = (
        InputRequirement("trace"),
        InputRequirement("case"),
    )

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        raise NotImplementedError

    async def evaluate_async(self, context: EvaluatorContext) -> EvaluatorResult:
        return await asyncio.to_thread(self.evaluate, context)

    def _result(
        self,
        context: EvaluatorContext,
        *,
        score: Score,
        output: Any,
        rationale: str = "",
        evidence_refs: tuple[str, ...] = (),
        configuration: Mapping[str, Any] | None = None,
        started_ns: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluatorResult:
        config = dict(configuration or context.configuration)
        input_value = {
            "trace": context.trace.to_dict() if hasattr(context.trace, "to_dict") else context.trace,
            "case": context.case.to_dict() if hasattr(context.case, "to_dict") else context.case,
        }
        return EvaluatorResult(
            self.name,
            self.version,
            score,
            output,
            rationale,
            evidence_refs,
            config,
            input_hash=content_digest(input_value),
            latency_ms=((time.perf_counter_ns() - started_ns) / 1_000_000)
            if started_ns is not None else 0.0,
            metadata=dict(metadata or {}),
        )


def failed_result(
    evaluator: Evaluator, context: EvaluatorContext, error: EvaluationError,
    *, latency_ms: float = 0.0,
) -> EvaluatorResult:
    input_value = {
        "trace": context.trace.to_dict() if hasattr(context.trace, "to_dict") else context.trace,
        "case": context.case.to_dict() if hasattr(context.case, "to_dict") else context.case,
    }
    return EvaluatorResult(
        evaluator.name,
        evaluator.version,
        None,
        None,
        configuration=dict(context.configuration),
        judge={"deployment": context.judge_deployment} if context.judge_deployment else {},
        input_hash=content_digest(input_value),
        latency_ms=latency_ms,
        error=error.to_dict(),
    )


def run_evaluator(
    evaluator: Evaluator, context: EvaluatorContext, *, capture_errors: bool = False
) -> EvaluatorResult:
    started = time.perf_counter_ns()
    try:
        context.validate_requirements(evaluator.name, evaluator.input_requirements)
        result = evaluator.evaluate(context)
        if inspect.isawaitable(result):
            raise EvaluationError(
                "ASYNC_RESULT_IN_SYNC_RUN",
                "sync evaluator returned an awaitable; use run_evaluator_async",
                evaluator=evaluator.name,
            )
        return result
    except EvaluationError as exc:
        if capture_errors:
            return failed_result(
                evaluator, context, exc,
                latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
            )
        raise


async def run_evaluator_async(
    evaluator: Evaluator, context: EvaluatorContext, *, capture_errors: bool = False
) -> EvaluatorResult:
    started = time.perf_counter_ns()
    try:
        context.validate_requirements(evaluator.name, evaluator.input_requirements)
        return await evaluator.evaluate_async(context)
    except EvaluationError as exc:
        if capture_errors:
            return failed_result(
                evaluator, context, exc,
                latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
            )
        raise
