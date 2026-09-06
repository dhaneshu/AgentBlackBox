"""The recorder an agent writes to as it runs.

An agent that is instrumented afterwards records what its author remembered to
record. An agent that is handed a recorder records what it actually did, in
order, because every stage has to go through the same object.

The API is deliberately small. If instrumenting an agent is laborious, nobody
instruments the agent.
"""

from __future__ import annotations

from typing import Any

from blackbox.trace import Citation, Step, Trace, Violation, now


class Recorder:
    """Accumulates a `Trace` while an agent runs."""

    def __init__(
        self,
        run_id: str,
        case_id: str,
        agent: str,
        question: str,
        config: dict[str, Any] | None = None,
    ):
        self.trace = Trace(
            run_id=run_id,
            case_id=case_id,
            agent=agent,
            question=question,
            config=dict(config or {}),
            started_at=now(),
        )
        self.step("receive", "question", question)

    # --- recording ---------------------------------------------------------

    def step(self, kind: str, name: str, summary: str = "", **detail: Any) -> Step:
        entry = Step(
            index=len(self.trace.steps),
            kind=kind,
            name=name,
            summary=summary,
            detail=detail,
        )
        self.trace.steps.append(entry)
        return entry

    def retrieved(self, query: str, hits: list) -> Step:
        return self.step(
            "retrieve",
            "corpus.search",
            f"{len(hits)} passage(s), best {hits[0].score:.2f}" if hits else "no hits",
            query=query,
            hits=[
                {"citation": hit.passage.citation, "score": round(hit.score, 4)}
                for hit in hits
            ],
        )

    def tool(self, name: str, arguments: dict[str, Any], result: str) -> Step:
        return self.step(
            "tool", name, str(result)[:160], arguments=arguments, result=str(result)
        )

    def cite(self, claim: str, passage, support: float, quote: str = "") -> Citation:
        citation = Citation(
            source=passage.source,
            lines=passage.lines,
            quote=quote or passage.text[:180],
            claim=claim,
            support=support,
        )
        self.trace.citations.append(citation)
        return citation

    def answered(self, answer: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.trace.answer = answer
        self.trace.refused = False
        self.trace.tokens_in += tokens_in
        self.trace.tokens_out += tokens_out
        self.step("generate", "compose", answer[:160])

    def refused(self, reason: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.trace.answer = ""
        self.trace.refused = True
        self.trace.refusal_reason = reason
        self.trace.tokens_in += tokens_in
        self.trace.tokens_out += tokens_out
        self.step("refuse", "decline", reason)

    def violated(self, policy: str, severity: str, detail: str) -> Violation:
        violation = Violation(
            policy=policy,
            severity=severity,
            detail=detail,
            step_index=len(self.trace.steps) - 1,
        )
        self.trace.violations.append(violation)
        return violation

    def finish(self) -> Trace:
        return self.trace
