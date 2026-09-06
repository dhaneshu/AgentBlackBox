"""Command line entry point.

    python -m blackbox demo                    # record, inspect, replay, compare
    python -m blackbox record --agent grounded-v1
    python -m blackbox inspect --run <id> --case Q-004
    python -m blackbox replay --run <id> --agent grounded-v2
    python -m blackbox compare --before <id> --after <id>
    python -m blackbox validate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from blackbox.agents import REGISTRY, build
from blackbox.corpus import Corpus
from blackbox.policy import PolicySet
from blackbox.replay import Run, determinism_check, replay, run_suite
from blackbox.report import render_comparison, render_run, render_trace
from blackbox.score import compare, score_run, write_score
from blackbox.suite import load_suite, summarise, validate_suite
from blackbox.trace import load_traces

DEFAULT_CORPUS = "samples/corpus"
DEFAULT_SUITE = "samples/suites/northwind.jsonl"
DEFAULT_OUT = "out"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackbox",
        description="Record, replay and measure enterprise agent runs.",
    )
    _common = dict(corpus=DEFAULT_CORPUS, suite=DEFAULT_SUITE, out=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="run a suite and record every trace")
    _add_common(rec, _common)
    rec.add_argument("--agent", default="grounded-v1", choices=sorted(REGISTRY))
    rec.add_argument("--evidence-floor", type=float, default=0.35)
    rec.add_argument("--token-budget", type=int, default=0)
    rec.add_argument("--quiet", action="store_true")

    ins = sub.add_parser("inspect", help="show the readout for one recorded case")
    _add_common(ins, _common)
    ins.add_argument("--run", required=True)
    ins.add_argument("--case", default="")
    ins.add_argument("--failures", action="store_true", help="show every failing case")
    ins.add_argument("--no-color", action="store_true")

    rep = sub.add_parser("replay", help="re-run a recorded suite against another agent")
    _add_common(rep, _common)
    rep.add_argument("--run", required=True)
    rep.add_argument("--agent", required=True, choices=sorted(REGISTRY))
    rep.add_argument("--evidence-floor", type=float, default=0.35)
    rep.add_argument("--token-budget", type=int, default=0)
    rep.add_argument("--no-color", action="store_true")

    cmp_ = sub.add_parser("compare", help="compare two recorded runs case by case")
    _add_common(cmp_, _common)
    cmp_.add_argument("--before", required=True)
    cmp_.add_argument("--after", required=True)
    cmp_.add_argument("--no-color", action="store_true")

    val = sub.add_parser("validate", help="check the suite before trusting a number")
    _add_common(val, _common)

    demo = sub.add_parser("demo", help="the whole story on the sample project")
    _add_common(demo, _common)
    demo.add_argument("--no-color", action="store_true")

    args = parser.parse_args(argv)
    return _DISPATCH[args.command](args)


def _add_common(parser: argparse.ArgumentParser, defaults: dict) -> None:
    parser.add_argument("--corpus", default=defaults["corpus"])
    parser.add_argument("--suite", default=defaults["suite"])
    parser.add_argument("--out", default=defaults["out"])


# --- commands --------------------------------------------------------------

def _load(args):
    corpus = Corpus.load(args.corpus)
    cases = load_suite(args.suite)
    return corpus, cases


def _policies(args) -> PolicySet:
    return PolicySet(
        evidence_floor=getattr(args, "evidence_floor", 0.35),
        token_budget=getattr(args, "token_budget", 0),
    )


def cmd_validate(args) -> int:
    corpus, cases = _load(args)
    report = validate_suite(cases, corpus)
    print(f"{args.suite}: {summarise(cases)}")
    print(f"{args.corpus}: {len(corpus)} passage(s)")
    for error in report.errors:
        print(f"ERROR  {error}")
    if not report.ok:
        print(f"\n{len(report.errors)} error(s). Fix these before trusting any number.")
        return 1
    print("\nSuite is valid.")
    return 0


def cmd_record(args) -> int:
    corpus, cases = _load(args)
    report = validate_suite(cases, corpus)
    if not report.ok:
        print("Suite has errors; recording would produce meaningless numbers.",
              file=sys.stderr)
        for error in report.errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1

    run = run_suite(args.agent, cases, corpus, _policies(args))
    traces_path = run.save(Path(args.out) / "runs")

    result = score_run(run, cases)
    result.determinism = determinism_check(args.agent, cases, corpus, _policies(args))
    score_path = write_score(result, Path(args.out) / "scores")

    if not args.quiet:
        print(render_run(result))
    print(f"\nRun {run.run_id}")
    print(f"  traces -> {traces_path}")
    print(f"  score  -> {score_path}")
    return 0


def cmd_inspect(args) -> int:
    corpus, cases = _load(args)
    traces = load_traces(_run_path(args.out, args.run))
    by_id = {case.id: case for case in cases}
    colour = False if args.no_color else None

    if args.case:
        selected = [t for t in traces if t.case_id == args.case]
        if not selected:
            print(f"No case {args.case!r} in run {args.run!r}", file=sys.stderr)
            return 1
    elif args.failures:
        result = score_run(Run(args.run, traces[0].agent if traces else "", traces), cases)
        failing = {r.case_id for r in result.results if not r.good}
        selected = [t for t in traces if t.case_id in failing]
        if not selected:
            print("No failing cases in this run.")
            return 0
    else:
        selected = traces

    for trace in selected:
        print(render_trace(trace, by_id.get(trace.case_id), colour=colour))
        print()
    return 0


def cmd_replay(args) -> int:
    corpus, cases = _load(args)
    recorded = load_traces(_run_path(args.out, args.run))
    before_run = Run(args.run, recorded[0].agent if recorded else "", recorded)

    after_run = replay(before_run, args.agent, cases, corpus, _policies(args))
    after_run.save(Path(args.out) / "runs")

    before = score_run(before_run, cases)
    after = score_run(after_run, cases)
    after.determinism = determinism_check(args.agent, cases, corpus, _policies(args))
    write_score(after, Path(args.out) / "scores")

    colour = False if args.no_color else None
    print(render_run(after, colour=colour))
    print()
    print(render_comparison(compare(before, after), colour=colour))
    print(f"\nReplay run {after_run.run_id}")
    return 0


def cmd_compare(args) -> int:
    corpus, cases = _load(args)
    before_traces = load_traces(_run_path(args.out, args.before))
    after_traces = load_traces(_run_path(args.out, args.after))

    before = score_run(Run(args.before, before_traces[0].agent, before_traces), cases)
    after = score_run(Run(args.after, after_traces[0].agent, after_traces), cases)

    colour = False if args.no_color else None
    print(render_comparison(compare(before, after), colour=colour))
    return 0


def cmd_demo(args) -> int:
    """Record a flawed agent, find the failure, replay the fix, report honestly."""
    colour = False if args.no_color else None
    corpus, cases = _load(args)
    policies = PolicySet()
    out = Path(args.out)

    _beat(1, "An agent is shipped, and a suite is recorded")
    v1 = run_suite("grounded-v1", cases, corpus, policies, run_id="demo-v1")
    v1.save(out / "runs")
    before = score_run(v1, cases)
    before.determinism = determinism_check("grounded-v1", cases, corpus, policies)
    write_score(before, out / "scores")
    print(render_run(before, colour=colour))

    _beat(2, "The black box readout for the worst failure")
    failing = [r for r in before.results if r.verdict == "hallucinated"]
    if failing:
        worst = failing[0]
        trace = next(t for t in v1.traces if t.case_id == worst.case_id)
        case = next(c for c in cases if c.id == worst.case_id)
        print(render_trace(trace, case, colour=colour))
        print(
            "\n  The step that broke it is visible: retrieval scored below the "
            "evidence\n  floor and the agent answered anyway, citing a passage "
            "that does not\n  contain the claim.\n"
        )

    _beat(3, "The fix is replayed over exactly the same cases")
    v2 = replay(v1, "grounded-v2", cases, corpus, policies)
    v2.run_id = "demo-v2"
    v2.save(out / "runs")
    after = score_run(v2, cases)
    after.run_id = "demo-v2"
    after.determinism = determinism_check("grounded-v2", cases, corpus, policies)
    write_score(after, out / "scores")
    print(render_run(after, colour=colour))

    _beat(4, "Did it help, and what did it break?")
    comparison = compare(before, after)
    print(render_comparison(comparison, colour=colour))

    _beat(5, "The regression, in full")
    if comparison.regressed:
        case_id = comparison.regressed[0].case_id
        trace = next(t for t in v2.traces if t.case_id == case_id)
        case = next(c for c in cases if c.id == case_id)
        print(render_trace(trace, case, colour=colour))
        print(
            "\n  The fix works, and it costs something. This case is answerable, "
            "the\n  agent now refuses it, and the number that reveals that is "
            "over-refusal.\n  Any evaluation without it would have called this a "
            "clean win.\n"
        )
    else:
        print("  No regression in this configuration.\n")
    return 0


def _beat(number: int, title: str) -> None:
    header = f"\n>> Beat {number}. {title}"
    print(f"\033[1m{header}\033[0m" if sys.stdout.isatty() else header)
    print("-" * 96)


def _run_path(out: str, run_id: str) -> Path:
    path = Path(run_id)
    if path.exists():
        return path
    return Path(out) / "runs" / f"{run_id}.jsonl"


_DISPATCH = {
    "record": cmd_record,
    "inspect": cmd_inspect,
    "replay": cmd_replay,
    "compare": cmd_compare,
    "validate": cmd_validate,
    "demo": cmd_demo,
}


if __name__ == "__main__":
    raise SystemExit(main())
