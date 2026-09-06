"""The recording format.

A trace is what the flight recorder keeps: everything the agent saw, retrieved,
called, checked and concluded, in order, with enough detail that the run can be
re-created and argued about later.

Two properties matter more than completeness:

* **Every claim in the answer is tied to the passage it came from.** Without
  that, "the agent hallucinated" is an opinion.
* **The configuration is recorded with the run.** A comparison between two runs
  is meaningless if you cannot say what differed between them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: Ordered stages of a run. Anything an agent does lands in one of these.
STEP_KINDS: tuple[str, ...] = (
    "receive",    # the question arrived
    "retrieve",   # the knowledge base was searched
    "tool",       # an external tool was called
    "generate",   # an answer was composed
    "refuse",     # the agent declined to answer
    "policy",     # a policy check ran
)


@dataclass(frozen=True)
class Citation:
    """A passage an answer sentence leaned on, and whether it actually holds it up."""

    source: str
    lines: tuple[int, int]
    quote: str = ""
    claim: str = ""
    support: float = 0.0

    @property
    def supports(self) -> bool:
        return self.support >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lines": list(self.lines),
            "quote": self.quote,
            "claim": self.claim,
            "support": round(self.support, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Citation":
        lines = data.get("lines") or [0, 0]
        return cls(
            source=str(data.get("source", "")),
            lines=(int(lines[0]), int(lines[-1])),
            quote=str(data.get("quote", "")),
            claim=str(data.get("claim", "")),
            support=float(data.get("support", 0.0)),
        )

    def __str__(self) -> str:
        lo, hi = self.lines
        return f"{self.source}:{lo}" if lo == hi else f"{self.source}:{lo}-{hi}"


@dataclass(frozen=True)
class Violation:
    """A policy the run broke."""

    policy: str
    severity: str          # critical | high | medium | low
    detail: str
    step_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Violation":
        return cls(
            policy=str(data.get("policy", "")),
            severity=str(data.get("severity", "low")),
            detail=str(data.get("detail", "")),
            step_index=int(data.get("step_index", -1)),
        )


@dataclass(frozen=True)
class Step:
    """One thing the agent did."""

    index: int
    kind: str
    name: str
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "summary": self.summary,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        return cls(
            index=int(data.get("index", 0)),
            kind=str(data.get("kind", "")),
            name=str(data.get("name", "")),
            summary=str(data.get("summary", "")),
            detail=dict(data.get("detail") or {}),
        )


@dataclass
class Trace:
    """One agent run, start to finish."""

    run_id: str
    case_id: str
    agent: str
    question: str
    answer: str = ""
    refused: bool = False
    refusal_reason: str = ""
    steps: list[Step] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: str = ""

    @property
    def config_hash(self) -> str:
        """Stable fingerprint of the configuration this run used."""
        payload = json.dumps(self.config, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    @property
    def grounded(self) -> bool:
        """True when the agent either refused, or supported everything it said."""
        if self.refused:
            return True
        return bool(self.citations) and all(c.supports for c in self.citations)

    @property
    def unsupported_claims(self) -> list[Citation]:
        return [c for c in self.citations if not c.supports]

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def worst_violation(self) -> str:
        order = ("low", "medium", "high", "critical")
        if not self.violations:
            return ""
        return max(self.violations, key=lambda v: order.index(v.severity)).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "agent": self.agent,
            "question": self.question,
            "answer": self.answer,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "steps": [s.to_dict() for s in self.steps],
            "citations": [c.to_dict() for c in self.citations],
            "violations": [v.to_dict() for v in self.violations],
            "config": self.config,
            "config_hash": self.config_hash,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trace":
        return cls(
            run_id=str(data.get("run_id", "")),
            case_id=str(data.get("case_id", "")),
            agent=str(data.get("agent", "")),
            question=str(data.get("question", "")),
            answer=str(data.get("answer", "")),
            refused=bool(data.get("refused", False)),
            refusal_reason=str(data.get("refusal_reason", "")),
            steps=[Step.from_dict(s) for s in data.get("steps") or []],
            citations=[Citation.from_dict(c) for c in data.get("citations") or []],
            violations=[Violation.from_dict(v) for v in data.get("violations") or []],
            config=dict(data.get("config") or {}),
            tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
            started_at=str(data.get("started_at", "")),
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_traces(traces: Iterable[Trace], path: str | Path) -> Path:
    """Write traces as JSONL, one run per line."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
    return target


def load_traces(path: str | Path) -> list[Trace]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Trace file not found: {target}")
    traces: list[Trace] = []
    for line_no, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            traces.append(Trace.from_dict(json.loads(stripped)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}:{line_no}: invalid JSON ({exc.msg})") from exc
    return traces
