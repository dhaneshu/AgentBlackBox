"""Repeated stability trials with exact/semantic agreement and confidence intervals."""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass

from blackbox.adapters import AgentAdapter
from blackbox.domain.suite import EvaluationCase

from .runner import AsyncRunner, RunnerConfig


@dataclass(frozen=True)
class AgreementMetric:
    value: float
    successes: int
    comparisons: int
    confidence_interval: tuple[float, float]


@dataclass(frozen=True)
class StabilityReport:
    trials: int
    seed: int | None
    exact: AgreementMetric
    semantic: AgreementMetric
    semantic_threshold: float
    trial_run_ids: tuple[str, ...]
    case_seeds: dict[str, int]
    case_outcomes: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict:
        return {
            "trials": self.trials,
            "seed": self.seed,
            "semantic_threshold": self.semantic_threshold,
            "exact": _metric_dict(self.exact),
            "semantic": _metric_dict(self.semantic),
            "trial_run_ids": list(self.trial_run_ids),
            "case_seeds": self.case_seeds,
            "case_outcomes": {
                case_id: list(outcomes)
                for case_id, outcomes in self.case_outcomes.items()
            },
        }


async def run_stability_trials(
    adapter_factory,
    cases: list[EvaluationCase],
    *,
    trials: int = 3,
    seed: int | None = 0,
    semantic_threshold: float = 0.8,
    runner_config: RunnerConfig | None = None,
) -> StabilityReport:
    if trials < 2:
        raise ValueError("stability trials must be at least 2")
    if not 0 <= semantic_threshold <= 1:
        raise ValueError("semantic_threshold must be between 0 and 1")
    runs = []
    case_seeds: dict[str, int] = {}
    for trial in range(trials):
        adapter: AgentAdapter = adapter_factory()
        base = runner_config or RunnerConfig()
        trial_seed = seed + trial if seed is not None else None
        config = RunnerConfig(
            base.concurrency, base.timeout_seconds, base.max_retries,
            base.retry_delay_seconds, base.payload_limit_bytes, trial_seed,
        )
        run = await AsyncRunner(adapter, config).run(
            cases, run_id=f"stability-{trial:03d}"
        )
        runs.append(run)
        if adapter.supports_seed and trial_seed is not None:
            for execution in run.cases:
                recorded_seed = execution.trace.config.get(
                    "agentblackbox.random_seed"
                )
                if isinstance(recorded_seed, int):
                    case_seeds[f"{trial}:{execution.case_id}"] = recorded_seed
    exact_successes = semantic_successes = comparisons = 0
    case_outcomes: dict[str, tuple[str, ...]] = {}
    for case in cases:
        outputs = [_stability_outcome(run, case.id) for run in runs]
        case_outcomes[case.id] = tuple(
            outcome[0] if not outcome[3]
            else f"{outcome[0]}:{outcome[3]}"
            for outcome in outputs
        )
        for left, right in itertools.combinations(outputs, 2):
            comparisons += 1
            exact_successes += left == right
            if left[0] != "completed" or right[0] != "completed":
                semantic_successes += left == right
            else:
                semantic_successes += (
                    left[2] == right[2]
                    and _semantic_similarity(left[1], right[1])
                    >= semantic_threshold
                )
    return StabilityReport(
        trials, seed,
        _metric(exact_successes, comparisons),
        _metric(semantic_successes, comparisons),
        semantic_threshold,
        tuple(run.run_id for run in runs),
        case_seeds,
        case_outcomes,
    )


def _stability_outcome(run, case_id: str) -> tuple[str, str, bool, str]:
    execution = next(
        (item for item in run.cases if item.case_id == case_id), None
    )
    if execution is None:
        return ("missing", "", False, "CASE_MISSING")
    if execution.status != "completed":
        code = str((execution.error or {}).get("code", ""))
        return (execution.status, "", False, code)
    return (
        "completed", execution.trace.answer, execution.trace.refused, ""
    )


def _semantic_similarity(left: str, right: str) -> float:
    a = set(re.findall(r"[a-z0-9]+", left.lower()))
    b = set(re.findall(r"[a-z0-9]+", right.lower()))
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if a | b else 1.0


def _metric(successes: int, comparisons: int) -> AgreementMetric:
    value = successes / comparisons if comparisons else 1.0
    return AgreementMetric(value, successes, comparisons, _wilson(successes, comparisons))


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return (1.0, 1.0)
    value = successes / total
    denominator = 1 + z * z / total
    centre = (value + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        value * (1 - value) / total + z * z / (4 * total * total)
    ) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _metric_dict(metric: AgreementMetric) -> dict:
    return {
        "value": metric.value,
        "successes": metric.successes,
        "comparisons": metric.comparisons,
        "confidence_interval": list(metric.confidence_interval),
    }
