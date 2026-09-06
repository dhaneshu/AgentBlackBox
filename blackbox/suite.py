"""The evaluation suite: questions, and what a correct agent does with them.

A case says what *should* happen, not what the agent currently does. That
distinction is the whole reason a suite is worth writing: an expectation
recorded before the run is an evaluation, an expectation recorded after it is a
description.

Two expectations exist, and both matter:

* `answer` -- the knowledge base contains the answer, so refusing is a failure.
* `refuse` -- it does not, so answering is a fabrication.

Most agent evaluations only measure the first. Measuring both is what stops a
team "fixing" hallucination by making the agent refuse everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPECTATIONS: tuple[str, ...] = ("answer", "refuse")


@dataclass(frozen=True)
class Case:
    """One question and the behaviour a correct agent shows on it."""

    id: str
    question: str
    expect: str = "answer"
    must_contain: tuple[str, ...] = ()
    must_cite: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Case":
        return cls(
            id=str(data.get("id", "")).strip(),
            question=str(data.get("question", "")).strip(),
            expect=str(data.get("expect", "answer")).strip().lower(),
            must_contain=tuple(str(x) for x in data.get("must_contain") or ()),
            must_cite=tuple(str(x) for x in data.get("must_cite") or ()),
            forbid=tuple(str(x) for x in data.get("forbid") or ()),
            notes=str(data.get("notes", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "expect": self.expect,
            "must_contain": list(self.must_contain),
            "must_cite": list(self.must_cite),
            "forbid": list(self.forbid),
            "notes": self.notes,
        }


@dataclass
class SuiteReport:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_suite(path: str | Path) -> list[Case]:
    """Read a JSONL suite, ignoring blank lines and `#` comments."""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Suite not found: {target}")

    cases: list[Case] = []
    for line_no, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            cases.append(Case.from_dict(json.loads(stripped)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}:{line_no}: invalid JSON ({exc.msg})") from exc
    return cases


def validate_suite(cases: list[Case], corpus=None) -> SuiteReport:
    """Catch the mistakes that would quietly invalidate every later number."""
    report = SuiteReport()
    seen: set[str] = set()

    for case in cases:
        where = case.id or "<no id>"
        if not case.id:
            report.errors.append("a case has no id")
        elif case.id in seen:
            report.errors.append(f"{where}: duplicate id")
        seen.add(case.id)

        if not case.question:
            report.errors.append(f"{where}: empty question")

        if case.expect not in EXPECTATIONS:
            report.errors.append(
                f"{where}: expect {case.expect!r} is not one of {', '.join(EXPECTATIONS)}"
            )

        if case.expect == "refuse" and (case.must_contain or case.must_cite):
            report.errors.append(
                f"{where}: a case expecting a refusal cannot also require content or citations"
            )

        if case.expect == "answer" and not (case.must_contain or case.must_cite):
            report.errors.append(
                f"{where}: a case expecting an answer must assert something about it, "
                "otherwise any answer passes"
            )

        if corpus is not None:
            known = {p.source for p in corpus.passages}
            for wanted in case.must_cite:
                if not any(source.endswith(wanted) or wanted in source for source in known):
                    report.errors.append(
                        f"{where}: must_cite {wanted!r} is not in the corpus"
                    )

    return report


def summarise(cases: list[Case]) -> str:
    answerable = sum(1 for c in cases if c.expect == "answer")
    refusable = len(cases) - answerable
    return (
        f"{len(cases)} case(s): {answerable} answerable, "
        f"{refusable} that must be refused"
    )
