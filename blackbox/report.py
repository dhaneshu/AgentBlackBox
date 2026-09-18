"""Rendering: the black box readout, the run dashboard, the A/B comparison.

The readout is the view that makes a failure arguable instead of anecdotal. It
shows what the agent retrieved, what it decided, what it said, and which
sentence was not carried by the passage it cited — in that order, because that
is the order in which the failure happened.
"""

from __future__ import annotations

import os
import sys

from blackbox.domain.result import Comparison, RunScore
from blackbox.domain.suite import Case
from blackbox.domain.trace import Trace

_WIDTH = 96

_COLOURS = {
    "critical": "\033[97;41m",
    "high": "\033[91m",
    "medium": "\033[93m",
    "low": "\033[90m",
    "good": "\033[92m",
    "bad": "\033[91m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

_VERDICT_STYLE = {
    "correct": "good",
    "correctly_refused": "good",
    "hallucinated": "critical",
    "over_refused": "medium",
    "wrong": "high",
}

_STEP_GLYPH = {
    "receive": "?",
    "retrieve": "~",
    "tool": ">",
    "generate": "=",
    "refuse": "x",
    "policy": "!",
}


def use_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Painter:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, style: str) -> str:
        if not self.enabled or style not in _COLOURS:
            return text
        return f"{_COLOURS[style]}{text}{_COLOURS['reset']}"


def render_trace(
    trace: Trace, case: Case | None = None, colour: bool | None = None
) -> str:
    """The readout for a single run: what happened, in order, and what broke."""
    paint = Painter(use_colour() if colour is None else colour)
    lines = [
        _rule(),
        paint(f"  BLACK BOX READOUT  -  {trace.case_id}  -  {trace.agent}", "bold"),
        _rule(),
        f"  question   {trace.question}",
    ]

    if case is not None:
        lines.append(f"  expected   {case.expect}")
    lines.append(f"  config     {trace.config_hash}  ({_config_summary(trace)})")
    lines.append("")

    lines.append("  Steps")
    for step in trace.steps:
        glyph = _STEP_GLYPH.get(step.kind, "-")
        lines.append(f"    {glyph} {step.index:>2}  {step.kind:<9} {step.name:<16} {step.summary}")
        if step.kind == "retrieve":
            for hit in (step.detail.get("hits") or [])[:3]:
                lines.append(
                    paint(f"           {hit['score']:.2f}  {hit['citation']}", "dim")
                )
    lines.append("")

    if trace.refused:
        lines.append("  Outcome    " + paint("REFUSED", "medium"))
        for chunk in _wrap(trace.refusal_reason, _WIDTH - 14):
            lines.append(f"             {chunk}")
    else:
        lines.append("  Outcome    ANSWERED")
        for chunk in _wrap(trace.answer, _WIDTH - 14):
            lines.append(f"             {chunk}")
    lines.append("")

    if trace.citations:
        lines.append("  Citations")
        for citation in trace.citations:
            mark = "ok " if citation.supports else "BAD"
            style = "good" if citation.supports else "critical"
            lines.append(
                "    " + paint(f"[{mark}]", style)
                + f" {citation}  support {citation.support:.0%}"
            )
            for chunk in _wrap(f'claim: "{citation.claim}"', _WIDTH - 12):
                lines.append(paint(f"          {chunk}", "dim"))
            if not citation.supports:
                for chunk in _wrap(f'passage: "{citation.quote}"', _WIDTH - 12):
                    lines.append(paint(f"          {chunk}", "dim"))
        lines.append("")

    if trace.violations:
        lines.append("  Policy violations")
        for violation in trace.violations:
            lines.append(
                "    " + paint(f" {violation.severity.upper():8} ", violation.severity)
                + f" {violation.policy}"
            )
            for chunk in _wrap(violation.detail, _WIDTH - 8):
                lines.append(f"        {chunk}")
        lines.append("")
    else:
        lines.append("  Policy violations: " + paint("none", "good"))
        lines.append("")

    lines.append(_rule())
    return "\n".join(lines)


def render_run(result: RunScore, colour: bool | None = None) -> str:
    """The dashboard for one run."""
    paint = Painter(use_colour() if colour is None else colour)
    lines = [
        _rule(),
        paint(f"  RUN  {result.agent}  ({result.run_id})", "bold"),
        _rule(),
        f"  {result.total} case(s)",
        "",
        _metric("accuracy", result.accuracy, paint, good_high=True),
        _metric("groundedness", result.groundedness, paint, good_high=True),
        _metric("hallucination", result.hallucination_rate, paint, good_high=False),
        _metric("over-refusal", result.over_refusal_rate, paint, good_high=False),
        "",
        "  Verdicts",
    ]

    for verdict in ("correct", "correctly_refused", "hallucinated", "over_refused", "wrong"):
        count = result.count(verdict)
        if not count:
            continue
        lines.append(
            "      " + paint(f"{verdict:<20}", _VERDICT_STYLE[verdict]) + f"{count}"
        )

    lines.append("")
    lines.append(f"  Policy violations: {result.violation_count}")
    lines.append(f"  Tokens:            {result.total_tokens}")
    if result.determinism is not None:
        style = "good" if result.determinism >= 0.999 else "medium"
        lines.append("  Determinism:       " + paint(f"{result.determinism:.0%}", style))

    failures = [r for r in result.results if not r.good]
    if failures:
        lines.append("")
        lines.append("  Failures")
        for item in failures:
            lines.append(
                "      " + paint(f"{item.case_id}  {item.verdict}", _VERDICT_STYLE[item.verdict])
            )
            for chunk in _wrap(item.reason, _WIDTH - 12):
                lines.append(paint(f"          {chunk}", "dim"))

    lines.append(_rule())
    return "\n".join(lines)


def render_comparison(comparison: Comparison, colour: bool | None = None) -> str:
    """Before and after, with what the change fixed and what it broke."""
    paint = Painter(use_colour() if colour is None else colour)
    before, after = comparison.before, comparison.after

    lines = [
        _rule(),
        paint(f"  COMPARISON  {before.agent}  ->  {after.agent}", "bold"),
        _rule(),
        f"      {'metric':<18}{'before':>10}{'after':>10}{'change':>12}",
        _delta_row("accuracy", before.accuracy, after.accuracy, paint, good_high=True),
        _delta_row("groundedness", before.groundedness, after.groundedness, paint, True),
        _delta_row("hallucination", before.hallucination_rate,
                   after.hallucination_rate, paint, False),
        _delta_row("over-refusal", before.over_refusal_rate,
                   after.over_refusal_rate, paint, False),
        "",
        f"      {'violations':<18}{before.violation_count:>10}{after.violation_count:>10}",
        f"      {'tokens':<18}{before.total_tokens:>10}{after.total_tokens:>10}",
        "",
    ]

    lines.append(
        "  " + paint(f"{len(comparison.fixed)} fixed", "good")
        + "   "
        + paint(f"{len(comparison.regressed)} regressed", "bad" if comparison.regressed else "dim")
        + f"   {len(comparison.of_kind('unchanged'))} unchanged"
        + f"   net {comparison.net:+d}"
    )
    lines.append("")

    for change in comparison.fixed:
        lines.append(
            "      " + paint("FIXED     ", "good")
            + f"{change.case_id}  {change.before} -> {change.after}"
        )
    for change in comparison.regressed:
        lines.append(
            "      " + paint("REGRESSED ", "bad")
            + f"{change.case_id}  {change.before} -> {change.after}"
        )

    if comparison.regressed:
        lines.append("")
        lines.append(
            paint(
                "  A regression is reported, not buried. Driving hallucination to zero\n"
                "  by refusing more is not an improvement, and this is the row that\n"
                "  shows the difference.",
                "dim",
            )
        )

    lines.append(_rule())
    return "\n".join(lines)


def _metric(label: str, value: float, paint: Painter, good_high: bool) -> str:
    ok = value >= 0.8 if good_high else value <= 0.05
    style = "good" if ok else ("medium" if good_high else "high")
    return f"      {label:<18}" + paint(f"{value:>6.0%}", style) + "  " + _bar(value)


def _delta_row(
    label: str, before: float, after: float, paint: Painter, good_high: bool
) -> str:
    delta = after - before
    if abs(delta) < 0.005:
        marker = "        ="
    else:
        improved = delta > 0 if good_high else delta < 0
        marker = paint(f"{delta:+9.0%}", "good" if improved else "bad")
    return f"      {label:<18}{before:>10.0%}{after:>10.0%}{marker:>12}"


def _bar(value: float, width: int = 24) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def _config_summary(trace: Trace) -> str:
    keys = ("evidence_floor", "answer_floor", "support_floor", "sentence_floor")
    parts = [f"{k}={trace.config[k]}" for k in keys if k in trace.config]
    return ", ".join(parts) or "default"


def _rule() -> str:
    return "=" * _WIDTH


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]
