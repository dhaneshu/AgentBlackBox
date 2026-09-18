"""Evaluation result cache with component-complete, content-addressed keys."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any, Mapping

from blackbox.domain.trace import content_digest
from blackbox.evaluators.base import EvaluatorResult


@dataclass(frozen=True)
class EvaluationCacheKey:
    trace_digest: str
    case_digest: str
    evaluator: str
    evaluator_version: str
    configuration_digest: str
    judge_digest: str

    @classmethod
    def create(
        cls,
        *,
        trace: Any,
        case: Any,
        evaluator: str,
        evaluator_version: str,
        configuration: Mapping[str, Any] | None = None,
        judge: Mapping[str, Any] | None = None,
    ) -> "EvaluationCacheKey":
        trace_digest = getattr(trace, "digest", None) or content_digest(trace)
        case_value = case.to_dict() if hasattr(case, "to_dict") else case
        return cls(
            str(trace_digest),
            content_digest(case_value),
            evaluator,
            evaluator_version,
            content_digest(dict(configuration or {})),
            content_digest(dict(judge or {})),
        )

    @property
    def digest(self) -> str:
        return content_digest({
            "trace": self.trace_digest,
            "case": self.case_digest,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "configuration": self.configuration_digest,
            "judge": self.judge_digest,
        })


@dataclass
class EvaluationCache:
    """Small backend-neutral cache; model results are safe only under an exact key."""

    backend: MutableMapping[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, key: EvaluationCacheKey) -> EvaluatorResult | None:
        value = self.backend.get(key.digest)
        return EvaluatorResult.from_dict(value) if value is not None else None

    def put(self, key: EvaluationCacheKey, result: EvaluatorResult) -> None:
        self.backend[key.digest] = result.to_dict()

    def get_or_none(
        self, key: EvaluationCacheKey, *, expected_judge: Mapping[str, Any] | None = None
    ) -> EvaluatorResult | None:
        if expected_judge is not None and key.judge_digest != content_digest(
            dict(expected_judge)
        ):
            return None
        return self.get(key)
