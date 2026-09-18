"""Scoring a run, and comparing two runs.

Five verdicts, because "accuracy" hides which failure occurred:

* `correct`            -- answered, and the answer holds up
* `correctly_refused`  -- declined something the corpus genuinely lacks
* `hallucinated`       -- asserted something the evidence does not carry
* `over_refused`       -- declined something it could have answered
* `wrong`              -- answered, cited properly, and still got it wrong

`over_refused` is the counterweight that makes the rest honest. An agent can
drive hallucination to zero by refusing everything, and any evaluation without
this verdict will call that an improvement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from blackbox.domain.schema import (
    CURRENT_SCHEMA_VERSION,
    evaluation_result_identifier,
)
from blackbox.domain.suite import Case
from blackbox.domain.trace import Trace

if TYPE_CHECKING:
    from blackbox.assertions import AssertionResult
    from blackbox.evaluators import Evaluator, EvaluatorResult

VERDICTS: tuple[str, ...] = (
    "correct",
    "correctly_refused",
    "hallucinated",
    "over_refused",
    "wrong",
)
GOOD_VERDICTS: frozenset[str] = frozenset({"correct", "correctly_refused"})


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    verdict: str
    reason: str
    grounded: bool
    violations: int
    tokens: int
    evaluation_result_id: str = ""
    assertion_results: tuple[AssertionResult, ...] = ()
    evaluator_results: tuple[EvaluatorResult, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evaluation_result_id",
            evaluation_result_identifier(self.run_id, self.case_id),
        )

    @property
    def good(self) -> bool:
        return self.verdict in GOOD_VERDICTS

    def to_dict(self) -> dict:
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "evaluation_result_id": self.evaluation_result_id,
            "case_id": self.case_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "grounded": self.grounded,
            "violations": self.violations,
            "tokens": self.tokens,
            "assertion_results": [result.to_dict() for result in self.assertion_results],
            "evaluator_results": [result.to_dict() for result in self.evaluator_results],
        }


@dataclass
class RunScore:
    run_id: str = ""
    agent: str = ""
    results: list[CaseResult] = field(default_factory=list)
    determinism: float | None = None
    schema_version: str = CURRENT_SCHEMA_VERSION
    stability: dict | None = None

    def __post_init__(self) -> None:
        self.results = [
            replace(result, run_id=self.run_id)
            if result.run_id != self.run_id
            else result
            for result in self.results
        ]

    @property
    def total(self) -> int:
        return len(self.results)

    def count(self, verdict: str) -> int:
        return sum(1 for r in self.results if r.verdict == verdict)

    def rate(self, verdict: str) -> float:
        return self.count(verdict) / self.total if self.total else 0.0

    @property
    def accuracy(self) -> float:
        good = sum(1 for r in self.results if r.good)
        return good / self.total if self.total else 0.0

    @property
    def hallucination_rate(self) -> float:
        return self.rate("hallucinated")

    @property
    def over_refusal_rate(self) -> float:
        return self.rate("over_refused")

    @property
    def groundedness(self) -> float:
        return (
            sum(1 for r in self.results if r.grounded) / self.total
            if self.total
            else 0.0
        )

    @property
    def violation_count(self) -> int:
        return sum(r.violations for r in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.results)

    def to_dict(self) -> dict:
        result = {
            "schema_version": self.schema_version,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "agent": self.agent,
            "total": self.total,
            "accuracy": round(self.accuracy, 4),
            "verdicts": {v: self.count(v) for v in VERDICTS},
            "hallucination_rate": round(self.hallucination_rate, 4),
            "over_refusal_rate": round(self.over_refusal_rate, 4),
            "groundedness": round(self.groundedness, 4),
            "violations": self.violation_count,
            "tokens": self.total_tokens,
            "determinism": self.determinism,
            "results": [
                (
                    replace(result, run_id=self.run_id)
                    if result.run_id != self.run_id
                    else result
                ).to_dict()
                for result in self.results
            ],
        }
        if self.stability is not None:
            result["stability"] = self.stability
        return result


@dataclass(frozen=True)
class Change:
    case_id: str
    before: str
    after: str

    @property
    def kind(self) -> str:
        was_good = self.before in GOOD_VERDICTS
        now_good = self.after in GOOD_VERDICTS
        if was_good and not now_good:
            return "regressed"
        if now_good and not was_good:
            return "fixed"
        return "unchanged"


@dataclass
class Comparison:
    before: RunScore
    after: RunScore
    changes: list[Change] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]

    @property
    def fixed(self) -> list[Change]:
        return self.of_kind("fixed")

    @property
    def regressed(self) -> list[Change]:
        return self.of_kind("regressed")

    @property
    def net(self) -> int:
        return len(self.fixed) - len(self.regressed)

    def to_dict(self) -> dict:
        return {
            "before": {"run_id": self.before.run_id, "agent": self.before.agent,
                       "accuracy": round(self.before.accuracy, 4)},
            "after": {"run_id": self.after.run_id, "agent": self.after.agent,
                      "accuracy": round(self.after.accuracy, 4)},
            "fixed": [c.case_id for c in self.fixed],
            "regressed": [c.case_id for c in self.regressed],
            "net": self.net,
        }


def judge(
    trace: Trace,
    case: Case,
    *,
    evaluators: dict[str, "Evaluator"] | None = None,
    policies=None,
    repeats: tuple[Trace, ...] = (),
) -> CaseResult:
    """Decide what happened on one case."""
    from blackbox.evaluators import (
        BUILTIN_EVALUATORS,
        AzureOpenAIJudge,
        EvaluatorContext,
        make_builtin,
        run_evaluator,
    )
    from blackbox.evaluators.builtins import evaluate_assertions

    declared = list(getattr(case, "evaluators", ()) or ())
    default_names = (
        "verdict", "groundedness", "assertions", "task_completion",
        "tool_correctness", "trajectory", "latency", "tokens", "cost",
        "policy_compliance", "stability",
    )
    specification_by_name = {name: {"name": name} for name in default_names}
    for specification in declared:
        specification_by_name[str(specification.get("name", ""))] = dict(specification)
    specifications = list(specification_by_name.values())
    available = dict(evaluators or {})
    outputs: list[EvaluatorResult] = []
    output_by_name: dict[str, EvaluatorResult] = {}
    assertion_results = evaluate_assertions(EvaluatorContext(trace, case))
    for specification in specifications:
        name = str(specification.get("name", ""))
        if name in output_by_name:
            continue
        configuration = dict(specification.get("configuration") or {})
        evaluator = available.get(name)
        if evaluator is None:
            if name in BUILTIN_EVALUATORS:
                evaluator = make_builtin(name)
            elif name == "azure_openai_judge":
                evaluator = AzureOpenAIJudge(**configuration)
                configuration = {}
            else:
                from blackbox.evaluators import EvaluationError
                raise EvaluationError(
                    "UNKNOWN_EVALUATOR", f"case {case.id!r} declares unknown evaluator {name!r}",
                    evaluator=name,
                )
        context = EvaluatorContext(
            trace, case, configuration, assertion_results,
            output_by_name, policies, repeats,
            str(specification.get("judge_deployment", "")),
        )
        evaluated = run_evaluator(evaluator, context)
        outputs.append(evaluated)
        output_by_name[name] = evaluated
    verdict_output = output_by_name["verdict"].output
    verdict = str(verdict_output["verdict"])
    reason = str(verdict_output["reason"])
    failed = [result for result in assertion_results if not result.passed]
    if failed:
        detail = ", ".join(
            f"{result.assertion_type}:{result.failure_code}" for result in failed
        )
        reason = f"{reason} Assertion failures: {detail}."
        if verdict in GOOD_VERDICTS:
            verdict = "wrong"
    return CaseResult(
        case_id=case.id,
        verdict=verdict,
        reason=reason,
        grounded=trace.grounded,
        violations=len(trace.violations),
        tokens=trace.total_tokens,
        run_id=trace.run_id,
        assertion_results=tuple(assertion_results),
        evaluator_results=tuple(outputs),
    )


def _verdict(trace: Trace, case: Case) -> tuple[str, str]:
    from blackbox.evaluators.builtins import legacy_verdict
    return legacy_verdict(trace, case)


def score_run(
    run,
    cases: list[Case],
    *,
    evaluators: dict[str, "Evaluator"] | None = None,
    policies=None,
) -> RunScore:
    """Judge every trace in a run against its case."""
    by_id = {case.id: case for case in cases}
    traces = run.traces if hasattr(run, "traces") else run
    result = RunScore(
        run_id=getattr(run, "run_id", ""), agent=getattr(run, "agent", "")
    )
    for trace in traces:
        case = by_id.get(trace.case_id)
        if case is None:
            continue
        result.results.append(
            judge(trace, case, evaluators=evaluators, policies=policies)
        )
    return result


def compare(before: RunScore, after: RunScore) -> Comparison:
    """Case-by-case verdict changes between two scored runs."""
    a = {r.case_id: r.verdict for r in before.results}
    b = {r.case_id: r.verdict for r in after.results}
    changes = [
        Change(case_id, a[case_id], b[case_id])
        for case_id in sorted(set(a) & set(b))
    ]
    return Comparison(before=before, after=after, changes=changes)


def write_score(result: RunScore, directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{result.run_id or 'run'}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path
