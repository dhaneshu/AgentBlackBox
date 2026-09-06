# Feature Specification: Agent Black Box

**Branch**: `001-agent-black-box`
**Created**: 2026-09-06
**Status**: Implemented (sample agents); pending a real agent under test

**Input**: Make an agent's failures reproducible, diagnosable and measurable, so
that a fix can be shown to be a fix.

## Problem

Teams are shipping agents into delivery faster than they can evaluate them. When
one produces a wrong answer, the current process is: someone screenshots it,
someone else edits the prompt, and everybody agrees it seems better now.

Three things are missing, and all three are the same missing thing — a record:

1. **The failure cannot be reproduced.** Nobody kept what the agent retrieved,
   what it decided, or what configuration it was running.
2. **The fix cannot be verified.** "Seems better" is not a measurement, and the
   original failing case is rarely re-run deliberately.
3. **The cost of the fix is invisible.** A prompt tightened to stop one
   hallucination will refuse questions it used to answer, and nothing surfaces
   that trade.

The third is the dangerous one. An agent can reach zero hallucinations by
refusing everything, and any evaluation that measures only answer correctness
will score that as an improvement.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Engineer diagnoses a specific failure (P1)

An engineer has a case where the agent answered wrongly. They open the recorded
trace and see, in order: the question, what retrieval returned and with what
scores, which tools were called, what was composed, and which sentence was not
carried by the passage it cited.

**Why this priority**: Without this, everything downstream is guesswork. It is
also independently useful: a team with readouts and nothing else can already
diagnose failures they currently argue about.

**Independent Test**: Run a knowingly flawed agent, open the readout for a
failing case, and confirm the step that caused the failure is identifiable
without reading the agent's source.

**Acceptance Scenarios**:

1. **Given** a completed run, **When** a trace is opened, **Then** every step
   appears in execution order with its inputs and outputs.
2. **Given** an answer, **When** its citations are shown, **Then** each states
   how much of the claim the cited passage actually supports.
3. **Given** a citation, **When** it is followed, **Then** the line range
   resolves to real content in the corpus.
4. **Given** a run, **When** it is inspected, **Then** the configuration that
   produced it is recorded with it.
5. **Given** an agent that answered with no retrieval at all, **When** policies
   run, **Then** a critical violation is recorded.

---

### User Story 2 - Engineer proves a fix works (P2)

The engineer changes the agent, replays exactly the cases the previous run
covered, and receives a case-by-case comparison: what improved, what stayed the
same, and what regressed.

**Why this priority**: This is the difference between an eval harness and a log
viewer. It depends on P1 producing traces.

**Independent Test**: Replay a recorded run against a changed agent and confirm
the comparison names each changed case and classifies it correctly.

**Acceptance Scenarios**:

1. **Given** a recorded run and a changed agent, **When** replay runs, **Then**
   only the cases the recording covered are re-run.
2. **Given** a suite missing a recorded case, **When** replay is attempted,
   **Then** it fails rather than silently comparing different experiments.
3. **Given** two scored runs, **When** compared, **Then** each case is
   classified as fixed, regressed or unchanged.
4. **Given** a regression, **When** the comparison is rendered, **Then** it is
   reported prominently rather than netted off against the fixes.
5. **Given** the same agent run twice, **When** compared, **Then** determinism
   is reported.

---

### User Story 3 - A team measures over-refusal, not just hallucination (P3)

The suite declares, for each case, whether a correct agent answers or refuses.
Refusing an answerable question is scored as a failure in its own right.

**Why this priority**: It is what stops the obvious cheat, and it costs almost
nothing once cases carry an expectation.

**Acceptance Scenarios**:

1. **Given** a case expecting an answer, **When** the agent refuses, **Then**
   the verdict is `over_refused`, not a pass.
2. **Given** a case expecting a refusal, **When** the agent answers, **Then**
   the verdict is `hallucinated`.
3. **Given** a suite where an answerable case asserts nothing about the answer,
   **When** validated, **Then** it is rejected, because any answer would pass.
4. **Given** a run, **When** metrics are reported, **Then** hallucination rate
   and over-refusal rate are both shown.

---

### User Story 4 - Policies apply to an agent nobody here wrote (P4)

A team instruments their own agent by handing it a recorder. Every policy,
verdict, replay and comparison then applies unchanged.

**Acceptance Scenarios**:

1. **Given** any agent producing a trace, **When** policies run, **Then**
   violations are recorded without the agent participating in the check.
2. **Given** an agent calling a tool outside the allowlist, **When** policies
   run, **Then** a critical violation is recorded.
3. **Given** an answer containing an email address or phone number, **When**
   policies run, **Then** a violation is recorded.

### Edge Cases

- Retrieval returns nothing and the agent answers anyway — no citation exists,
  and that must be caught rather than passing silently.
- A model states a citation that does not support its answer — a stated citation
  is not a checked one.
- A case appears in a recorded run but not in the current suite — replay must
  refuse rather than compare different experiments.
- An answerable case that asserts nothing — must be rejected at validation.
- A refusal must never be penalised for lacking evidence; it asserted nothing.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST record every stage of a run in execution order.
- **FR-002**: System MUST record the configuration that produced a run, with a
  stable fingerprint.
- **FR-003**: System MUST attach a source and line range to every citation.
- **FR-004**: System MUST compute and record how much of a claim its cited
  passage supports.
- **FR-005**: System MUST evaluate policies over the trace, after the agent, not
  inside it.
- **FR-006**: System MUST flag an answer produced with no citations.
- **FR-007**: System MUST flag a citation that does not carry its claim.
- **FR-008**: System MUST flag answering below a configured evidence floor.
- **FR-009**: System MUST flag tool calls outside an allowlist.
- **FR-010**: System MUST flag personal identifiers in an answer.
- **FR-011**: System MUST replay only the cases a recorded run covered, and fail
  if the suite no longer contains them.
- **FR-012**: System MUST classify each case as correct, correctly refused,
  hallucinated, over-refused or wrong.
- **FR-013**: System MUST report hallucination rate and over-refusal rate
  separately.
- **FR-014**: System MUST classify each changed case as fixed, regressed or
  unchanged, and report regressions prominently.
- **FR-015**: System MUST measure run-to-run determinism.
- **FR-016**: System MUST validate a suite and refuse to score against an
  invalid one.
- **FR-017**: System MUST run end to end with no network access.

### Success Criteria

- **SC-001**: A failing case's causal step is identifiable from the readout
  alone, without reading agent source.
- **SC-002**: 100% determinism for the deterministic agents.
- **SC-003**: Every citation in every shipped run resolves to real lines.
- **SC-004**: Each hallucination in the sample is caught by a critical policy.
- **SC-005**: The shipped comparison reports at least one regression, so the
  harness is shown detecting the failure mode it exists for.
- **SC-006**: Time to diagnose a real agent failure, before and after —
  *to be captured*.
- **SC-007**: Regressions caught before release on a real agent that would
  otherwise have shipped — *to be captured*.

## Out of Scope

- Serving or hosting agents. This observes and measures; it does not run
  production traffic.
- Automatic prompt repair. The harness reports; a human decides what to change.
- Semantic or model-based groundedness scoring. A checker that depends on a
  model needs its own evaluation, which defeats the purpose.
- A web dashboard. Terminal output and JSON are enough, and JSON is what a
  pipeline consumes.
