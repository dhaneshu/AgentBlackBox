# Innovation Studio — submission copy

Paste-ready text for the Hackathon 2026 project page. Placeholders in `[ ]` are
real-agent numbers to fill before the video deadline. Everything else is final
and asserted by the test suite.

---

## 1. Challenge tags

**Executive Challenge (exactly one, required):**

> `InSpireD (ISD): Hack for Evals`

**Topic Challenges (up to five):**

> `Hack for AI Engineering and Reliability`
> `Hack for Responsible AI`
> `Agentic Engineering`
> `Hack for Continuous Agent Improvement`
> `Digital Employees: Building the Hybrid Human + Agent Workforce`

`Hack for Continuous Agent Improvement` ("building agents that get better as you
use them") is the closest fit after Evals, because a replayable regression suite
is the mechanism by which an agent actually gets better rather than differently
wrong.

---

## 2. Project name

**`Agent Black Box`**

A flight recorder is understood instantly: it exists so that after something
goes wrong, you can establish exactly what happened rather than argue about it.
That is the product. Two words, no "AI", no "Copilot".

---

## 3. Tagline

> Every agent decision recorded, replayable and measured — including what your
> fix broke.

---

## 4. Short description

> An agent tells a delivery lead the contractual penalty is 5% of monthly fees
> per week of delay. It is in no document the agent can see. Agent Black Box
> records what the agent retrieved, decided and cited, shows the step where it
> went wrong, replays the same cases against your fix, and reports whether the
> fix worked — including the question it now refuses that it used to answer.

---

## 5. Full description

> **The problem.** Teams are shipping agents into delivery faster than they can
> evaluate them. When one produces a wrong answer, the process is: somebody
> screenshots it, somebody else edits the prompt, and everyone agrees it seems
> better now. The failure cannot be reproduced, the fix cannot be verified, and
> the cost of the fix is invisible.
>
> **What we built.** Agent Black Box records every run as a structured trace:
> the question, what retrieval returned and with what scores, which tools were
> called, what was composed, and — the part that matters — how much of each
> claim the passage it cited actually supports. Six policies then run **over the
> trace, after the agent**, never inside it, so they apply to any agent that
> produces a trace, including one written by a team that has never heard of us.
>
> **Diagnosis, not anecdote.** The readout for a failing case names the causal
> step. On the shipped sample: retrieval scored 0.25 against an evidence floor
> of 0.35, the agent answered anyway, and cited a passage containing 0% of what
> it claimed. Two critical policy violations, both pointing at the same moment.
>
> **The number nobody reports.** Any agent can reach zero hallucinations by
> refusing everything, and most agent evaluations would score that as a triumph,
> because they only measure whether answers are correct. We score five verdicts:
> correct, correctly refused, hallucinated, **over-refused**, and wrong.
> Over-refusal is the counterweight that makes the other four honest.
>
> **The result, in full.** Replaying the fixed agent over exactly the same
> cases: accuracy 75% → 88%, groundedness 75% → 100%, hallucination 25% → 0%,
> policy violations 4 → 0, and tokens down from 188 to 144. **Two cases fixed,
> one regressed.** The regression is a question the agent used to answer
> correctly — the same fact as another case, phrased the way a person actually
> types it ("remind me of the go live date for prod"). Retrieval scored 0.60
> against an evidence floor of 0.65, so the tightened agent refused it.
>
> **That regression is the submission.** We could have tuned it away and shown a
> clean sweep. We shipped it, because a harness whose own demo shows only
> improvement has not demonstrated the capability it exists for. The trade
> between hallucination and over-refusal is a decision for a human; the harness's
> job is to make sure the human is shown it, rather than reading "hallucination
> down 25%" and shipping.
>
> **It runs on your agent.** Any agent that accepts a recorder gets the whole
> harness — policies, verdicts, replay, comparison — unchanged. An Azure OpenAI
> implementation is included, and it does the thing models make necessary: it
> verifies the model's answer against the passages it was given, because a
> stated citation is not a checked one.
>
> **Real agent, real numbers.** Applied to [AGENT], a suite of [N] cases
> including [R] that it should refuse. Time to diagnose a reported failure went
> from **[B] to [A]**. [G] regressions were caught that would otherwise have
> shipped.
>
> **Why this matters beyond the hackathon.** Every ISD team is putting agents
> into delivery, and almost none can answer "how do you know your fix worked?"
> This is the layer that makes that question answerable, and it is agent-agnostic
> by construction.

---

## 6. Technology tags

`Azure OpenAI` · `Microsoft Foundry` · `Microsoft 365 Copilot` ·
`Azure DevOps` · `Python`

---

## 7. Team recruitment blurb

> Looking for two or three people for Hack Week (14–18 Sept).
>
> We are building the flight recorder for enterprise agents: structured traces,
> policy checks that run outside the agent, replay against a changed
> configuration, and a comparison that reports what your fix broke as well as
> what it fixed.
>
> Most useful right now: **someone who supports a real agent in delivery and can
> write down what it should and should not answer** (no coding — critical path),
> a Python engineer, and someone comfortable cutting a tight two-minute video.
>
> Bring your own agent. A second team's numbers are worth more to us than a
> second feature.

---

## 8. Numbers to fill before the video deadline

| Placeholder | Where it comes from | Owner |
|---|---|---|
| `[AGENT]` | the real agent chosen in T023 | Team |
| `[N]`, `[R]` | `python -m blackbox validate` summary line | Engineer |
| `[B]` → `[A]` diagnosis time | 2–3 past failures, before; the readout, after | Agent owner |
| `[G]` regressions caught | comparison output on the real fix | Engineer |

Capture the "before" half of `[B]` this week — it needs a conversation, not code.

---

## 9. Video guidance

Five beats, roughly twenty-five seconds each:

1. **The wrong answer.** The agent states a contractual penalty that exists
   nowhere. Confident, specific, fabricated.
2. **The readout.** Retrieval scored 0.25 against a floor of 0.35; it answered
   anyway; the cited passage contains 0% of the claim. *Spoken line: "we can
   point at the step."*
3. **The fix, replayed.** Same cases, changed agent. Hallucination 25% → 0%.
4. **The regression.** One case it used to answer, it now refuses — named,
   shown, and counted. **No other team will show their own fix breaking
   something as a designed feature.**
5. **The honest net.** Two fixed, one regressed, net +1, and a human decides
   whether to take that trade.

Beat 4 is the one that wins. Do not cut it for time.

---

## 10. Reminders

- **A two-minute video is mandatory** for eligibility. Record by 17 Sept.
- Challenge selection locks at the project video deadline.
- Challenge winners present to their executive sponsor; the overall ISD winner
  presents to Edwina Fitzmaurice.
- Check that no customer content appears in any trace shown on screen. The PII
  policy helps; it is not a substitute for looking.
