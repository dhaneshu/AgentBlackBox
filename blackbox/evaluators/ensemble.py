"""Validated aggregation of evaluator outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from blackbox.evaluators.base import EvaluationError, EvaluatorResult, Score

Aggregation = Literal["all", "any", "weighted_mean", "majority"]


@dataclass(frozen=True)
class EvaluatorEnsemble:
    name: str
    members: tuple[str, ...]
    aggregation: Aggregation
    weights: Mapping[str, float] = field(default_factory=dict)
    threshold: float = 0.5
    require_all: bool = True

    def aggregate(self, results: Mapping[str, EvaluatorResult]) -> EvaluatorResult:
        missing = [name for name in self.members if name not in results]
        if missing and self.require_all:
            raise EvaluationError(
                "MISSING_ENSEMBLE_OUTPUT",
                f"ensemble {self.name!r} is missing: {', '.join(missing)}",
                evaluator=self.name,
                details={"missing": missing},
            )
        selected = [results[name] for name in self.members if name in results]
        failed = [result.evaluator for result in selected if not result.succeeded]
        if failed:
            raise EvaluationError(
                "FAILED_ENSEMBLE_MEMBER",
                f"ensemble members failed: {', '.join(failed)}",
                evaluator=self.name,
            )
        if not selected or any(result.score is None for result in selected):
            raise EvaluationError(
                "MISSING_SCORE", "ensemble requires a score from every selected member",
                evaluator=self.name,
            )
        scores = [result.score for result in selected if result.score is not None]
        ranges = {(score.minimum, score.maximum) for score in scores}
        if len(ranges) != 1:
            raise EvaluationError(
                "INCOMPATIBLE_SCORE_RANGES",
                f"ensemble score ranges differ: {sorted(ranges)}",
                evaluator=self.name,
            )
        minimum, maximum = next(iter(ranges))
        passed = [
            score.passed if score.passed is not None
            else score.normalized() >= self.threshold
            for score in scores
        ]
        if self.aggregation == "all":
            normalized = float(all(passed))
        elif self.aggregation == "any":
            normalized = float(any(passed))
        elif self.aggregation == "majority":
            normalized = float(sum(passed) > len(passed) / 2)
        elif self.aggregation == "weighted_mean":
            weights = [float(self.weights.get(result.evaluator, 1.0)) for result in selected]
            if any(weight < 0 for weight in weights) or sum(weights) <= 0:
                raise EvaluationError(
                    "INVALID_ENSEMBLE_WEIGHTS",
                    "ensemble weights must be non-negative with a positive sum",
                    evaluator=self.name,
                )
            normalized = sum(
                score.normalized() * weight for score, weight in zip(scores, weights)
            ) / sum(weights)
        else:
            raise EvaluationError(
                "UNKNOWN_AGGREGATION", f"unknown aggregation {self.aggregation!r}",
                evaluator=self.name,
            )
        value = minimum + normalized * (maximum - minimum)
        score = Score(
            value, minimum, maximum,
            minimum + self.threshold * (maximum - minimum),
            self.aggregation,
        )
        return EvaluatorResult(
            evaluator=self.name,
            evaluator_version="1.0.0",
            score=score,
            output={
                "aggregation": self.aggregation,
                "members": [result.result_id for result in selected],
                "passed": score.passed,
            },
            rationale=f"{self.aggregation} aggregated {len(selected)} evaluator result(s).",
            evidence_refs=tuple(f"evaluation:{result.result_id}" for result in selected),
            configuration={
                "members": list(self.members), "weights": dict(self.weights),
                "threshold": self.threshold, "require_all": self.require_all,
            },
            input_hash="",
        )
