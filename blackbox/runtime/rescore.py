"""Rescore immutable stored traces without invoking an agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackbox.domain.policy import PolicySet, apply
from blackbox.domain.result import RunScore, score_run, write_score
from blackbox.domain.suite import EvaluationCase
from blackbox.domain.trace import Trace, content_digest, load_traces
from blackbox.replay import Run


@dataclass(frozen=True)
class RescoreResult:
    score: RunScore
    source_digest: str
    policy_configuration: dict[str, Any]
    output_path: Path | None = None


def rescore_traces(
    traces: list[Trace] | str | Path,
    cases: list[EvaluationCase],
    *,
    policies: PolicySet | None = None,
    evaluators=None,
    run_id: str | None = None,
    output_directory: str | Path | None = None,
) -> RescoreResult:
    """Evaluate copies of historical evidence; no adapter is accepted or called."""
    originals = load_traces(traces) if isinstance(traces, (str, Path)) else list(traces)
    source_digest = content_digest([trace.to_dict() for trace in originals])
    selected_policies = policies or PolicySet()
    rescored: list[Trace] = []
    for original in originals:
        clone = Trace.from_dict(original.to_dict())
        clone.violations = []
        clone.steps = [
            step for step in clone.steps
            if not (step.kind == "policy" and step.name == "policy.check")
        ]
        apply(clone, selected_policies)
        rescored.append(clone)
    identifier = run_id or (originals[0].run_id if originals else "rescore")
    score = score_run(
        Run(identifier, originals[0].agent if originals else "", rescored),
        cases,
        evaluators=evaluators,
        policies=selected_policies,
    )
    path = write_score(score, output_directory) if output_directory else None
    return RescoreResult(
        score, source_digest, selected_policies.to_dict(), path
    )
