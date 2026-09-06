"""Policies checked against a completed run.

Policies run *after* the agent, over the recorded trace, and never inside it.
That separation matters: an agent cannot be trusted to mark its own homework,
and a policy layer that lives inside the agent gets bypassed by the next
refactor. Checking the trace means the check applies to any agent that produces
a trace, including one written by a team that has never heard of this project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from blackbox.trace import Trace, Violation

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d ()-]{9,}\d")

#: One line per policy, so the catalogue is legible without reading the code.
POLICIES: dict[str, str] = {
    "UNCITED_ANSWER": "The agent answered without citing anything",
    "UNSUPPORTED_CITATION": "A cited passage does not contain the claim it is attached to",
    "ANSWERED_WITHOUT_EVIDENCE": "The agent answered although retrieval found nothing usable",
    "TOOL_NOT_ALLOWED": "A tool outside the allowlist was called",
    "PII_IN_OUTPUT": "The answer contains an email address or phone number",
    "TOKEN_BUDGET_EXCEEDED": "The run cost more tokens than the budget allows",
}


@dataclass(frozen=True)
class PolicySet:
    """The rules a run is held to. Recorded with the run so it can be compared."""

    evidence_floor: float = 0.35
    token_budget: int = 0            # 0 disables the check
    allowed_tools: tuple[str, ...] = ("corpus.search",)
    require_citations: bool = True
    forbid_pii: bool = True

    def to_dict(self) -> dict:
        return {
            "evidence_floor": self.evidence_floor,
            "token_budget": self.token_budget,
            "allowed_tools": list(self.allowed_tools),
            "require_citations": self.require_citations,
            "forbid_pii": self.forbid_pii,
        }


def check(trace: Trace, policies: PolicySet) -> list[Violation]:
    """Every policy this run broke, worst first."""
    found: list[Violation] = []
    for rule in _RULES:
        found.extend(rule(trace, policies))

    order = ("critical", "high", "medium", "low")
    found.sort(key=lambda v: order.index(v.severity))
    return found


def apply(trace: Trace, policies: PolicySet) -> Trace:
    """Attach violations to the trace and record that the check ran."""
    trace.violations = check(trace, policies)
    trace.config.setdefault("policies", policies.to_dict())
    trace.steps.append(
        _policy_step(len(trace.steps), len(trace.violations))
    )
    return trace


# --- individual policies ---------------------------------------------------

def _uncited_answer(trace: Trace, policies: PolicySet) -> list[Violation]:
    if trace.refused or not policies.require_citations:
        return []
    if trace.citations:
        return []
    return [
        Violation(
            "UNCITED_ANSWER",
            "critical",
            "The agent produced an answer with no citation at all.",
        )
    ]


def _unsupported_citation(trace: Trace, _policies: PolicySet) -> list[Violation]:
    if trace.refused:
        return []
    return [
        Violation(
            "UNSUPPORTED_CITATION",
            "critical",
            f'"{citation.claim[:90]}" cites {citation} but only '
            f"{citation.support:.0%} of it appears there.",
        )
        for citation in trace.unsupported_claims
    ]


def _answered_without_evidence(trace: Trace, policies: PolicySet) -> list[Violation]:
    if trace.refused:
        return []
    best = _best_retrieval_score(trace)
    if best >= policies.evidence_floor:
        return []
    return [
        Violation(
            "ANSWERED_WITHOUT_EVIDENCE",
            "high",
            f"Best retrieval score was {best:.2f}, below the evidence floor of "
            f"{policies.evidence_floor:.2f}, and the agent answered anyway.",
        )
    ]


def _tool_not_allowed(trace: Trace, policies: PolicySet) -> list[Violation]:
    return [
        Violation(
            "TOOL_NOT_ALLOWED",
            "critical",
            f"Called {step.name!r}, which is not in the allowlist "
            f"{list(policies.allowed_tools)}.",
            step.index,
        )
        for step in trace.steps
        if step.kind == "tool" and step.name not in policies.allowed_tools
    ]


def _pii_in_output(trace: Trace, policies: PolicySet) -> list[Violation]:
    if not policies.forbid_pii or not trace.answer:
        return []
    leaks = _EMAIL_RE.findall(trace.answer) + _PHONE_RE.findall(trace.answer)
    if not leaks:
        return []
    return [
        Violation(
            "PII_IN_OUTPUT",
            "high",
            f"Answer contains {len(leaks)} identifier(s), e.g. {leaks[0]!r}.",
        )
    ]


def _token_budget(trace: Trace, policies: PolicySet) -> list[Violation]:
    if not policies.token_budget or trace.total_tokens <= policies.token_budget:
        return []
    return [
        Violation(
            "TOKEN_BUDGET_EXCEEDED",
            "medium",
            f"Run used {trace.total_tokens} tokens against a budget of "
            f"{policies.token_budget}.",
        )
    ]


_RULES: tuple[Callable[[Trace, PolicySet], list[Violation]], ...] = (
    _uncited_answer,
    _unsupported_citation,
    _answered_without_evidence,
    _tool_not_allowed,
    _pii_in_output,
    _token_budget,
)


def _best_retrieval_score(trace: Trace) -> float:
    best = 0.0
    for step in trace.steps:
        if step.kind != "retrieve":
            continue
        for hit in step.detail.get("hits") or []:
            best = max(best, float(hit.get("score", 0.0)))
    return best


def _policy_step(index: int, violation_count: int):
    from blackbox.trace import Step

    return Step(
        index=index,
        kind="policy",
        name="policy.check",
        summary=f"{violation_count} violation(s)",
    )
