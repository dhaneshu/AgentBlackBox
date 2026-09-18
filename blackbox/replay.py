"""Run a suite against an agent, and re-run it against a changed one.

Replay is the reason the recorder exists. A trace on its own tells you what went
wrong once; replaying the same suite against a changed configuration tells you
whether the fix worked, and — the part teams skip — what the fix broke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from blackbox.agents import Agent, build
from blackbox.corpus import Corpus
from blackbox.domain.policy import PolicySet, apply
from blackbox.recorder import Recorder
from blackbox.domain.suite import Case
from blackbox.domain.trace import Step, Trace, save_traces
from blackbox.fixtures import FixtureManager, default_fixture_manager


@dataclass
class Run:
    """Every trace produced by one agent over one suite."""

    run_id: str
    agent: str
    traces: list[Trace] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    def by_case(self) -> dict[str, Trace]:
        return {trace.case_id: trace for trace in self.traces}

    def save(self, directory: str | Path) -> Path:
        target = Path(directory) / f"{self.run_id}.jsonl"
        return save_traces(self.traces, target)


def run_suite(
    agent: Agent | str,
    cases: list[Case],
    corpus: Corpus,
    policies: PolicySet | None = None,
    run_id: str | None = None,
    fixture_manager: FixtureManager | None = None,
    approve_destructive_fixtures: bool = False,
) -> Run:
    """Execute every case and return the recorded traces, policy-checked."""
    if isinstance(agent, str):
        agent = build(agent)
    policies = policies or PolicySet()
    run_id = run_id or new_run_id(agent.name)
    fixture_manager = fixture_manager or default_fixture_manager(
        filesystem_root=Path.cwd()
    )

    run = Run(run_id=run_id, agent=agent.name, config=agent.config())

    for case in cases:
        recorder = Recorder(
            run_id=run_id,
            case_id=case.id,
            agent=agent.name,
            question=case.question,
            config=agent.config(),
        )
        def execute_case() -> Trace:
            agent.answer(case.question, corpus, recorder)
            return apply(recorder.finish(), policies)

        trace, fixture_executions = fixture_manager.run_many(
            case.fixture_refs, execute_case,
            approve_destructive=approve_destructive_fixtures,
        )
        for execution in fixture_executions:
            trace.artifacts.extend((execution.before, execution.after))
            verification_passed = (
                execution.verification.get("passed") is True
                if isinstance(execution.verification, dict)
                else execution.verification is True
            )
            trace.steps.append(Step(
                len(trace.steps), "fixture", "fixture.verify",
                f"{execution.name}: "
                f"{'passed' if verification_passed else 'failed'}",
                execution.to_dict(),
            ))
        run.traces.append(trace)

    return run


def replay(
    recorded: Run | list[Trace],
    agent: Agent | str,
    cases: list[Case],
    corpus: Corpus,
    policies: PolicySet | None = None,
    fixture_manager: FixtureManager | None = None,
    approve_destructive_fixtures: bool = False,
) -> Run:
    """Re-run exactly the cases a previous run covered, against a new agent.

    Restricting to the recorded case set is what makes the comparison fair: a
    new run over a different set of questions is a different experiment, not a
    regression test.
    """
    traces = recorded.traces if isinstance(recorded, Run) else recorded
    covered = {trace.case_id for trace in traces}
    subset = [case for case in cases if case.id in covered]
    missing = covered - {case.id for case in subset}
    if missing:
        raise ValueError(
            "Recorded run covers cases missing from the suite: "
            + ", ".join(sorted(missing))
        )
    return run_suite(
        agent, subset, corpus, policies, fixture_manager=fixture_manager,
        approve_destructive_fixtures=approve_destructive_fixtures,
    )


def determinism_check(
    agent: Agent | str, cases: list[Case], corpus: Corpus,
    policies: PolicySet | None = None,
    fixture_manager: FixtureManager | None = None,
    approve_destructive_fixtures: bool = False,
    *,
    trials: int = 3,
    semantic_threshold: float = 0.8,
    random_seed: int | None = 0,
) -> float:
    """Compatibility scalar backed by configurable repeated stability trials."""
    return float(stability_check(
        agent, cases, corpus, policies,
        fixture_manager=fixture_manager,
        approve_destructive_fixtures=approve_destructive_fixtures,
        trials=trials,
        semantic_threshold=semantic_threshold,
        random_seed=random_seed,
    )["exact_agreement"])


def stability_check(
    agent: Agent | str,
    cases: list[Case],
    corpus: Corpus,
    policies: PolicySet | None = None,
    fixture_manager: FixtureManager | None = None,
    approve_destructive_fixtures: bool = False,
    *,
    trials: int = 3,
    semantic_threshold: float = 0.8,
    random_seed: int | None = 0,
) -> dict:
    """Run repeated legacy-agent trials and return auditable agreement metrics."""
    if trials < 2:
        raise ValueError("stability trials must be at least 2")
    if not 0 <= semantic_threshold <= 1:
        raise ValueError("semantic_threshold must be between 0 and 1")
    selected = build(agent) if isinstance(agent, str) else agent
    supports_seed = bool(getattr(selected, "supports_seed", False))
    runs = []
    seeds: list[int] = []
    for trial in range(trials):
        trial_seed = random_seed + trial if random_seed is not None else None
        if supports_seed and trial_seed is not None:
            setter = getattr(selected, "set_seed", None)
            if callable(setter):
                setter(trial_seed)
                seeds.append(trial_seed)
        runs.append(run_suite(
            selected, cases, corpus, policies, run_id=f"stability-{trial:03d}",
            fixture_manager=fixture_manager,
            approve_destructive_fixtures=approve_destructive_fixtures,
        ))
    exact = semantic = comparisons = 0
    for case in cases:
        values = [
            (run.by_case()[case.id].answer, run.by_case()[case.id].refused)
            for run in runs if case.id in run.by_case()
        ]
        for left, right in itertools.combinations(values, 2):
            comparisons += 1
            exact += left == right
            semantic += (
                left[1] == right[1]
                and _semantic_similarity(left[0], right[0]) >= semantic_threshold
            )
    exact_value = exact / comparisons if comparisons else 1.0
    semantic_value = semantic / comparisons if comparisons else 1.0
    return {
        "trials": trials,
        "comparisons": comparisons,
        "exact_agreement": exact_value,
        "exact_confidence_interval": _wilson(exact, comparisons),
        "semantic_agreement": semantic_value,
        "semantic_confidence_interval": _wilson(semantic, comparisons),
        "semantic_threshold": semantic_threshold,
        "random_seed": random_seed if supports_seed else None,
        "trial_seeds": seeds,
    }


def _semantic_similarity(left: str, right: str) -> float:
    a = set(re.findall(r"[a-z0-9]+", left.lower()))
    b = set(re.findall(r"[a-z0-9]+", right.lower()))
    return len(a & b) / len(a | b) if a | b else 1.0


def _wilson(successes: int, total: int) -> list[float]:
    if total == 0:
        return [1.0, 1.0]
    z = 1.959963984540054
    value = successes / total
    denominator = 1 + z * z / total
    centre = (value + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        value * (1 - value) / total + z * z / (4 * total * total)
    ) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def new_run_id(agent_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{agent_name}"
