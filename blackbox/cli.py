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
import asyncio
import json
import sys
import warnings
from pathlib import Path

from blackbox.agents import REGISTRY, build
from blackbox.corpus import Corpus
from blackbox.domain.policy import PolicySet
from blackbox.replay import Run, determinism_check, replay, run_suite, stability_check
from blackbox.report import render_comparison, render_run, render_trace
from blackbox.domain.result import compare, score_run, write_score
from blackbox.domain.suite import load_suite, summarise, validate_suite
from blackbox.domain.trace import load_traces

DEFAULT_CORPUS = "samples/corpus"
DEFAULT_SUITE = "samples/suites/northwind.jsonl"
DEFAULT_OUT = "out"


def main(argv: list[str] | None = None) -> int:
    warnings.warn(
        "The Phase 1 CLI is a compatibility interface; its commands remain "
        "supported while versioned SDK interfaces are introduced",
        DeprecationWarning,
        stacklevel=2,
    )
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
    rec.add_argument(
        "--approve-destructive-fixtures", action="store_true",
        help="explicitly approve suite fixtures declared destructive",
    )

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
    rep.add_argument("--approve-destructive-fixtures", action="store_true")

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
    demo.add_argument(
        "--short",
        action="store_true",
        help="show the compact hackathon storyline",
    )
    demo.add_argument("--approve-destructive-fixtures", action="store_true")

    async_run = sub.add_parser(
        "run-async", help="run a local agent with bounded async concurrency"
    )
    _add_common(async_run, _common)
    async_run.add_argument("--agent", default="grounded-v1", choices=sorted(REGISTRY))
    async_run.add_argument("--concurrency", type=int, default=4)
    async_run.add_argument("--timeout", type=float, default=30.0)
    async_run.add_argument("--retries", type=int, default=2)
    async_run.add_argument("--seed", type=int)

    imp = sub.add_parser(
        "import-traces", help="import Foundry, OTLP, or mapped JSONL traces"
    )
    imp.add_argument("--format", choices=("jsonl", "foundry", "otlp"), required=True)
    imp.add_argument("--input", required=True)
    imp.add_argument("--output", required=True)
    imp.add_argument("--mapping", help="JSON file mapping destination fields to source paths")
    imp.add_argument("--max-bytes", type=int, default=4_194_304)

    rescore = sub.add_parser(
        "rescore", help="rescore stored traces without calling an agent"
    )
    _add_common(rescore, _common)
    rescore.add_argument("--run", required=True)
    rescore.add_argument("--output-run-id", default="")

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

    run = run_suite(
        args.agent, cases, corpus, _policies(args),
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    traces_path = run.save(Path(args.out) / "runs")

    result = score_run(run, cases)
    result.stability = stability_check(
        args.agent, cases, corpus, _policies(args),
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    result.determinism = result.stability["exact_agreement"]
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

    after_run = replay(
        before_run, args.agent, cases, corpus, _policies(args),
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    after_run.save(Path(args.out) / "runs")

    before = score_run(before_run, cases)
    after = score_run(after_run, cases)
    after.stability = stability_check(
        args.agent, cases, corpus, _policies(args),
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    after.determinism = after.stability["exact_agreement"]
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
    if args.short:
        return _cmd_demo_short(args)

    colour = False if args.no_color else None
    corpus, cases = _load(args)
    policies = PolicySet()
    out = Path(args.out)

    _beat(1, "An agent is shipped, and a suite is recorded")
    v1 = run_suite(
        "grounded-v1", cases, corpus, policies, run_id="demo-v1",
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    v1.save(out / "runs")
    before = score_run(v1, cases)
    before.stability = stability_check(
        "grounded-v1", cases, corpus, policies,
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    before.determinism = before.stability["exact_agreement"]
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
    v2 = replay(
        v1, "grounded-v2", cases, corpus, policies,
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    v2.run_id = "demo-v2"
    v2.save(out / "runs")
    after = score_run(v2, cases)
    after.run_id = "demo-v2"
    after.stability = stability_check(
        "grounded-v2", cases, corpus, policies,
        approve_destructive_fixtures=args.approve_destructive_fixtures,
    )
    after.determinism = after.stability["exact_agreement"]
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


def _cmd_demo_short(args) -> int:
    """Run the same comparison with a concise, presentation-friendly readout."""
    corpus, cases = _load(args)
    policies = PolicySet()
    out = Path(args.out)
    fixture_approval = args.approve_destructive_fixtures

    v1 = run_suite(
        "grounded-v1",
        cases,
        corpus,
        policies,
        run_id="demo-v1",
        approve_destructive_fixtures=fixture_approval,
    )
    v1.save(out / "runs")
    before = score_run(v1, cases)
    write_score(before, out / "scores")

    v2 = replay(
        v1,
        "grounded-v2",
        cases,
        corpus,
        policies,
        approve_destructive_fixtures=fixture_approval,
    )
    v2.run_id = "demo-v2"
    v2.save(out / "runs")
    after = score_run(v2, cases)
    after.run_id = "demo-v2"
    write_score(after, out / "scores")
    comparison = compare(before, after)

    hallucination = next(
        result for result in before.results if result.verdict == "hallucinated"
    )
    regression = comparison.regressed[0] if comparison.regressed else None
    percent = lambda value: f"{value:.0%}"

    print("\nAGENT BLACK BOX  |  COMPACT DEMO")
    print("=" * 72)
    print(
        f"1  RECORD   {before.total} cases  |  accuracy {percent(before.accuracy)}  |  "
        f"hallucination {percent(before.hallucination_rate)}"
    )
    print(
        f"2  TRACE    {hallucination.case_id} hallucinated  |  unsupported answer and citation"
    )
    print(
        f"3  REPLAY   hallucination {percent(before.hallucination_rate)} -> "
        f"{percent(after.hallucination_rate)}  |  groundedness "
        f"{percent(before.groundedness)} -> {percent(after.groundedness)}"
    )
    print(
        f"4  COMPARE  {len(comparison.fixed)} fixed  |  "
        f"{len(comparison.regressed)} regressed  |  "
        f"{len(comparison.changes) - len(comparison.fixed) - len(comparison.regressed)} unchanged"
    )
    if regression:
        print(
            f"5  HONEST   {regression.case_id} {regression.before} -> {regression.after}  |  "
            f"over-refusal {percent(before.over_refusal_rate)} -> "
            f"{percent(after.over_refusal_rate)}"
        )
    print("-" * 72)
    print("Every agent decision: captured, explainable, replayable, measurable.\n")
    return 0


def cmd_run_async(args) -> int:
    from blackbox.adapters import InProcessAgentAdapter
    from blackbox.runtime import AsyncRunner, RunnerConfig

    corpus, cases = _load(args)
    report = validate_suite(cases, corpus)
    if not report.ok:
        for error in report.errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1
    adapter = InProcessAgentAdapter(args.agent, corpus, PolicySet())
    runner = AsyncRunner(adapter, RunnerConfig(
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
        max_retries=args.retries,
        random_seed=args.seed,
    ))
    run = asyncio.run(runner.run(cases))
    path = run.save(Path(args.out) / "runs")
    score = score_run(run, cases)
    score_path = write_score(score, Path(args.out) / "scores")
    print(f"Run {run.run_id} ({run.status})")
    print(f"  traces -> {path}")
    print(f"  score  -> {score_path}")
    return 0 if run.status == "completed" else 2


def cmd_import_traces(args) -> int:
    from blackbox.adapters import (
        DeclarativeMapping,
        FoundryTraceImporter,
        JSONLTraceImporter,
    )
    from blackbox.domain.trace import save_traces
    from blackbox.otel import OTLPTraceImporter

    source, target = Path(args.input), Path(args.output)
    mapping = {}
    if args.mapping:
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise ValueError("mapping file must contain a JSON object")
    if args.format == "jsonl":
        traces = JSONLTraceImporter(
            DeclarativeMapping(mapping), payload_limit_bytes=args.max_bytes
        ).import_file(source)
    elif args.format == "foundry":
        document = json.loads(source.read_text(encoding="utf-8"))
        values = document if isinstance(document, list) else [document]
        traces = FoundryTraceImporter(
            payload_limit_bytes=args.max_bytes
        ).import_many(values)
    else:
        traces = OTLPTraceImporter(
            payload_limit_bytes=args.max_bytes
        ).import_http(source.read_bytes())
    save_traces(traces, target)
    print(f"Imported {len(traces)} trace(s) -> {target}")
    return 0


def cmd_rescore(args) -> int:
    from blackbox.runtime import rescore_traces

    _corpus, cases = _load(args)
    result = rescore_traces(
        _run_path(args.out, args.run),
        cases,
        run_id=args.output_run_id or f"{args.run}-rescore",
        output_directory=Path(args.out) / "scores",
    )
    print(f"Rescored {result.score.total} trace(s) without invoking an agent")
    print(f"  source digest -> {result.source_digest}")
    print(f"  score         -> {result.output_path}")
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
    "run-async": cmd_run_async,
    "import-traces": cmd_import_traces,
    "rescore": cmd_rescore,
}


if __name__ == "__main__":
    raise SystemExit(main())
