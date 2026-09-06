# Tasks: Agent Black Box

**Branch**: `001-agent-black-box` | [spec.md](./spec.md) | [plan.md](./plan.md)

`[x]` = done and covered by a test. `[ ]` = needs a real agent, a person, or
Hack Week.

**MVP = Phase 2.** A team with readouts and nothing else can already diagnose
failures they currently argue about.

---

## Phase 1: Foundations — done

- [x] **T001** `textutil.py`: tokenisation with hyphen splitting and prefix
      matching, asymmetric coverage, support ratio, sentence splitting.
- [x] **T002** `trace.py`: `Trace`, `Step`, `Citation`, `Violation`, JSONL
      round-trip, and a configuration fingerprint stored with every run.
- [x] **T003** `corpus.py`: passage splitting that keeps the line range each
      passage came from, with headings attached for retrieval.
- [x] **T004** `recorder.py`: the small API an agent writes to. Deliberately
      small — if instrumenting an agent is laborious, nobody instruments it.
- [x] **T005** Sample corpus and an eight-case suite: six answerable, two that
      must be refused.

---

## Phase 2: Record and diagnose (P1) — MVP, done

- [x] **T006** `agents.py`: `grounded-v1`, a deterministic reproduction of the
      real failure mode — falling back on parametric memory when retrieval is
      weak, and citing the nearest passage as though it were the source.
- [x] **T007** `policy.py`: six policies checked over the completed trace,
      never inside the agent.
- [x] **T008** `report.render_trace`: the readout — steps in execution order,
      retrieval scores, the answer, per-claim support, violations.
- [x] **T009** Tests: each policy fires on the run it was written for and stays
      quiet otherwise; a refusal is never penalised for lacking evidence.

**Checkpoint**: a failing case can be diagnosed from the readout alone. Video
beats 1 and 2 exist.

---

## Phase 3: Replay and compare (P2) — done

- [x] **T010** `agents.GroundedV2`: the fix — refuse below an evidence floor,
      answer only in the corpus's own words.
- [x] **T011** `replay.replay`: re-runs exactly the cases a recording covered,
      and fails loudly if the suite no longer contains one of them.
- [x] **T012** `replay.determinism_check`: same suite twice, agreement reported.
- [x] **T013** `score.compare`: per-case classification into fixed, regressed
      and unchanged.
- [x] **T014** `report.render_comparison`: before and after, with regressions
      reported prominently rather than netted off.
- [x] **T015** A deliberate regression in the shipped sample (`Q-008`), so the
      harness demonstrates the capability that matters rather than only
      improvement.

**Checkpoint**: video beats 3 and 4 exist. Beat 4 is the one that wins.

---

## Phase 4: Measure the right things (P3) — done

- [x] **T016** Five verdicts, with `over_refused` as a first-class failure.
- [x] **T017** Suite validation that rejects an answerable case asserting
      nothing about the answer — otherwise any answer passes.
- [x] **T018** Hallucination rate and over-refusal rate reported separately on
      the dashboard and in the comparison.
- [x] **T019** Tests: verdict logic for all five outcomes, and metric
      arithmetic against a hand-worked example.

---

## Phase 5: Beyond the toy agents (P4) — done

- [x] **T020** `azure_agent.py`: a real Azure OpenAI agent in the same trace
      format, which verifies the model's answer against the passages it was
      given rather than trusting a stated citation.
- [x] **T021** `demo` subcommand running all five beats in one command.
- [x] **T022** `.gitignore` covering `raw/`, `private/` and generated output.

---

## Phase 6: A real agent under test — before and during Hack Week

- [ ] **T023** Pick one agent already in use in delivery. Ideally one that has
      produced a wrong answer somebody remembers.
- [ ] **T024** Point it at its own knowledge base and instrument it with a
      `Recorder`. Should be a day's work; if it is not, `recorder.py` is wrong
      and that is worth knowing.
- [ ] **T025** Write 15–25 cases from questions people actually ask it,
      including **at least five that it should refuse**.
      *Owner: whoever supports the agent. Critical path.*
- [ ] **T026** Run `validate` until clean, then record a baseline.
- [ ] **T027** Capture `SC-006`: how long it currently takes to diagnose a
      reported failure. Needs a conversation, not code — **do this first**.
- [ ] **T028** Take one real reported failure, diagnose it from the readout, and
      time it.
- [ ] **T029** Make the fix its owners would have made anyway. Replay. Report
      what it fixed **and what it regressed**.
- [ ] **T030** Capture `SC-007`: regressions caught that would otherwise have
      shipped. This is the number that justifies the tool.

---

## Phase 7: Submission

- [ ] **T031** Confirm no customer content appears in any trace that will be on
      screen. The PII policy helps; it is not a substitute for looking.
- [ ] **T032** Record the two-minute video: the wrong answer → the readout
      naming the failing step → the fix replayed → **the regression the
      comparison caught** → the honest net result.
      **Beat 4 is the one that wins. Do not cut it for time.**
- [ ] **T033** Paste final copy and real numbers into Innovation Studio.
- [ ] **T034** Confirm challenge tags.
- [ ] **T035** Offer the harness to one other hackathon team building an agent.
      A second team's numbers are worth more than a second feature.

---

## Calendar

| When | Phase |
|---|---|
| Now | Phases 1–5 complete |
| Before Hack Week (6–13 Sept) | T023–T025, and T027 first |
| 14–15 Sept | T026, T028 — baseline and a real diagnosis |
| 16–17 Sept | T029, T030, T032 — the fix, the regression, the video |
| 18 Sept | T035, buffer, submission |

The one item that cannot be compressed is **T025**: someone who supports a real
agent writing down what it should and should not answer. Start it first.
