# Implementation Plan: Agent Black Box

**Branch**: `001-agent-black-box` | **Date**: 2026-09-06 | [spec.md](./spec.md)

## Summary

A recorder, a trace format, a policy layer, a replay engine and a scorer.
Everything is files. The whole pipeline runs offline, which is what makes it
testable — and a harness that cannot be tested has no business measuring
anything else.

## Technical Context

| | |
|---|---|
| Language | Python 3.13 |
| Dependencies | `pytest` for tests. `openai` only for the Azure agent. |
| Storage | JSONL traces, JSON scores. No database. |
| Testing | `pytest`; the sample comparison is the system-level test |
| Target | CLI on Windows and Linux |

## Key Design Decisions

**Policies run over the trace, after the agent.** An agent cannot be trusted to
mark its own homework, and a check that lives inside the agent gets bypassed by
the next refactor. Checking the trace means the check applies to any agent that
produces a trace, including one written by a team that has never heard of this
project.

**Five verdicts, not two.** `over_refused` exists because an agent can reach
zero hallucinations by refusing everything, and an evaluation without that
verdict will score that as a triumph. It is the single most important design
decision here.

**Groundedness is lexical.** A model-based groundedness checker needs its own
evaluation, and this project is the thing you use when you are evaluating
something else. Word overlap is crude and inspectable; a human can always
overrule the verdict, which they cannot meaningfully do with an embedding
distance.

**A stated citation is not a checked one.** The Azure agent asks the model to
answer from passages, then independently verifies the answer against those
passages. Models cite confidently and incorrectly.

**Replay is restricted to the recorded case set.** A new run over a different
set of questions is a different experiment, not a regression test. Replay fails
loudly if the suite no longer covers a recorded case.

**Configuration is fingerprinted and stored with the run.** A comparison between
two runs is meaningless if you cannot say what differed between them.

**The demo agents are deterministic simulations.** A harness must be
byte-identical between runs; with a sampling model you could never tell whether
a changed number came from your fix or from the sampler. The simulated
"parametric memory" lives in one small labelled dictionary so nobody mistakes it
for retrieved fact.

**The shipped comparison contains a regression on purpose.** A harness whose own
demo shows only improvement has not demonstrated the capability that matters.

## Data Flow

```
samples/corpus/*.md ──> Corpus (passages with line ranges)
                              │
samples/suites/*.jsonl ──> Cases (expected behaviour, written before any run)
                              │
                              v
        Agent + Recorder ──> Trace ──> policy.apply ──> traces/<run>.jsonl
                                                              │
                                              ┌───────────────┴────────────┐
                                              v                            v
                                        score_run                       replay
                                              │                            │
                                        scores/<run>.json            new Trace set
                                              │                            │
                                              └──────> compare <───────────┘
                                                          │
                                                fixed / regressed / unchanged
```

## Module Map

| Module | Responsibility |
|---|---|
| `blackbox/textutil.py` | Tokenisation, coverage, support ratio, sentence splitting |
| `blackbox/trace.py` | `Trace`, `Step`, `Citation`, `Violation`, JSONL I/O, config hash |
| `blackbox/corpus.py` | Passage splitting with line ranges, lexical retrieval |
| `blackbox/recorder.py` | The small API an agent writes to as it runs |
| `blackbox/policy.py` | Six policies, checked over a completed trace |
| `blackbox/suite.py` | Cases and the validator that protects them |
| `blackbox/agents.py` | `grounded-v1` (flawed) and `grounded-v2` (fixed) |
| `blackbox/azure_agent.py` | A real Azure OpenAI agent in the same trace format |
| `blackbox/replay.py` | Run a suite, replay a recording, check determinism |
| `blackbox/score.py` | Five verdicts, run metrics, fixed/regressed comparison |
| `blackbox/report.py` | Readout, run dashboard, comparison |
| `blackbox/cli.py` | `record`, `inspect`, `replay`, `compare`, `validate`, `demo` |

## The shipped result

| | grounded-v1 | grounded-v2 |
|---|---|---|
| Accuracy | 75% | 88% |
| Groundedness | 75% | 100% |
| Hallucination | 25% | 0% |
| Over-refusal | 0% | 12% |
| Policy violations | 4 | 0 |
| Tokens | 188 | 144 |

Two cases fixed (`Q-004`, `Q-005`), one regressed (`Q-008`), net +1. The
regression is a question the agent used to answer correctly — the same fact as
another case, phrased the way a person actually types it. Retrieval scored 0.60
against an evidence floor of 0.65.

## Risks

| Risk | Mitigation |
|---|---|
| Lexical groundedness is crude | Stated plainly; the support figure is shown, not just a verdict, so a human can overrule it. |
| Demo agents are not real models | Azure agent provided in the same trace format; the limitation is documented rather than glossed. |
| A team games the harness by loosening cases | Suite validation rejects an answerable case that asserts nothing about the answer. |
| Over-refusal is ignored anyway | It is a first-class metric, on the dashboard and in the comparison, and the shipped demo regresses on it. |
| Traces contain customer data | `raw/` and `private/` are gitignored; the PII policy flags identifiers in answers. |

## Verification

`python -m pytest tests/ -q` — 90 tests, no network.

`tests/test_pipeline.py` asserts the exact figures in this plan and the README,
including that the comparison reports a regression. Change a threshold and the
build tells you the story changed.
