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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blackbox.suite import Case
from blackbox.trace import Trace

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

    @property
    def good(self) -> bool:
        return self.verdict in GOOD_VERDICTS

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "grounded": self.grounded,
            "violations": self.violations,
            "tokens": self.tokens,
        }


@dataclass
class RunScore:
    run_id: str = ""
    agent: str = ""
    results: list[CaseResult] = field(default_factory=list)
    determinism: float | None = None

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
        return {
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
            "results": [r.to_dict() for r in self.results],
        }


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


def judge(trace: Trace, case: Case) -> CaseResult:
    """Decide what happened on one case."""
    verdict, reason = _verdict(trace, case)
    return CaseResult(
        case_id=case.id,
        verdict=verdict,
        reason=reason,
        grounded=trace.grounded,
        violations=len(trace.violations),
        tokens=trace.total_tokens,
    )


def _verdict(trace: Trace, case: Case) -> tuple[str, str]:
    if case.expect == "refuse":
        if trace.refused:
            return "correctly_refused", "Declined something the corpus does not contain."
        return (
            "hallucinated",
            "Answered a question the corpus cannot support: "
            f'"{trace.answer[:80]}"',
        )

    if trace.refused:
        return "over_refused", "Refused a question the corpus can answer."

    missing = [
        needle for needle in case.must_contain
        if needle.lower() not in trace.answer.lower()
    ]
    if missing:
        return "wrong", f"Answer is missing required content: {missing}"

    banned = [
        needle for needle in case.forbid
        if needle.lower() in trace.answer.lower()
    ]
    if banned:
        return "wrong", f"Answer contains forbidden content: {banned}"

    cited = [str(c.source) for c in trace.citations]
    for wanted in case.must_cite:
        if not any(wanted in source for source in cited):
            return "wrong", f"Expected a citation to {wanted}, got {cited or 'none'}"

    if not trace.grounded:
        unsupported = trace.unsupported_claims
        detail = f"{unsupported[0]}" if unsupported else "no citations"
        return (
            "hallucinated",
            f"Answer content is not carried by the passage it cites ({detail}).",
        )

    return "correct", "Answered, cited, and the citation holds it up."


def score_run(run, cases: list[Case]) -> RunScore:
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
        result.results.append(judge(trace, case))
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
