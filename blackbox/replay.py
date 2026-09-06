"""Run a suite against an agent, and re-run it against a changed one.

Replay is the reason the recorder exists. A trace on its own tells you what went
wrong once; replaying the same suite against a changed configuration tells you
whether the fix worked, and — the part teams skip — what the fix broke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blackbox.agents import Agent, build
from blackbox.corpus import Corpus
from blackbox.policy import PolicySet, apply
from blackbox.recorder import Recorder
from blackbox.suite import Case
from blackbox.trace import Trace, save_traces


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
) -> Run:
    """Execute every case and return the recorded traces, policy-checked."""
    if isinstance(agent, str):
        agent = build(agent)
    policies = policies or PolicySet()
    run_id = run_id or new_run_id(agent.name)

    run = Run(run_id=run_id, agent=agent.name, config=agent.config())

    for case in cases:
        recorder = Recorder(
            run_id=run_id,
            case_id=case.id,
            agent=agent.name,
            question=case.question,
            config=agent.config(),
        )
        agent.answer(case.question, corpus, recorder)
        run.traces.append(apply(recorder.finish(), policies))

    return run


def replay(
    recorded: Run | list[Trace],
    agent: Agent | str,
    cases: list[Case],
    corpus: Corpus,
    policies: PolicySet | None = None,
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
    return run_suite(agent, subset, corpus, policies)


def determinism_check(
    agent: Agent | str, cases: list[Case], corpus: Corpus,
    policies: PolicySet | None = None,
) -> float:
    """Run the same suite twice and report the share of identical answers."""
    first = run_suite(agent, cases, corpus, policies, run_id="determinism-a")
    second = run_suite(agent, cases, corpus, policies, run_id="determinism-b")

    a, b = first.by_case(), second.by_case()
    shared = set(a) & set(b)
    if not shared:
        return 1.0

    same = sum(
        1
        for case_id in shared
        if (a[case_id].answer, a[case_id].refused) == (b[case_id].answer, b[case_id].refused)
    )
    return same / len(shared)


def new_run_id(agent_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{agent_name}"
