# Innovation Studio — submission copy

Paste-ready text for the Hackathon 2026 project page. Product claims and
metrics below use verified project evidence. Confirm venue and challenge
selections against the submission form before publishing.

---

## Page 1 — Core project details

### Title

> `Agent Black Box`

### Tagline

> Replay every agent decision. Prove the fix. Catch what it broke.

### Executive Challenge

> `InSpireD (ISD): Hack for Evals - From AI Demos to Learning Systems`

### Topic Challenges

> `Hack for AI Engineering and Reliability`
> `Hack for Responsible AI`
> `Agentic Engineering`
> `Digital Employees: Building the Hybrid Human + Agent Workforce`

### Description

Paste the complete content from **Full description** below into the Description field. The field accepts up to 30,000 characters.

### Keywords

`agent evaluation` · `replay` · `regression testing` · `groundedness` ·
`over-refusal` · `tracing` · `Responsible AI` · `Microsoft Foundry`

---

## 1. Challenge tags

**Executive Challenge (exactly one, required):**

> `InSpireD (ISD): Hack for Evals - From AI Demos to Learning Systems`

**Topic Challenges (up to five):**

> `Hack for AI Engineering and Reliability`
> `Hack for Responsible AI`
> `Agentic Engineering`
> `Digital Employees: Building the Hybrid Human + Agent Workforce`

---

## Page 2 — Additional information

### Hacking On

Add these keywords where available:

1. `Reliability`
2. `Responsible AI`
3. `AI`
4. `Evaluation`
5. `Developer Tools`
6. `Efficiency`

If the form permits only one keyword, use `Reliability`.

### Problem or opportunity statement

> When an AI agent fails, teams patch prompts from screenshots. They cannot reproduce the cause, prove the fix, or detect new hallucinations and unnecessary refusals elsewhere.

Character count: **174/200**

### Writing Code

> `Yes`

### Who is this for?

> `Microsoft Employees`

### Venue

> `India - Bengaluru, Vigyan`

### Existing Microsoft product or service

> Microsoft Foundry and Azure OpenAI. Agent Black Box adds structured tracing, deterministic evaluation, replay, and regression analysis for AI applications.

Character count: **155/200**

### Briefly describe what you made and how you made it

> We built Agent Black Box, a local-first flight recorder and regression platform for AI agents. It captures conversations, retrieval, tool calls, handoffs, generated claims, citations, policies, latency, tokens, cost, and side effects as replayable evidence. Deterministic assertions and optional model judges evaluate that evidence independently of the agent.
>
> Teams run identical datasets before and after a prompt, model, retrieval, tool, or policy change. The comparison reports fixed, regressed, and unchanged cases across accuracy, groundedness, hallucination, over-refusal, policy violations, latency, and cost. It runs offline through the Python CLI and SDK, connects to HTTP agents, Microsoft Foundry and OpenTelemetry, and includes a FastAPI/PostgreSQL service for collaborative operation.

Character count: **798/1,000**

---

## 3. Project name

**`Agent Black Box`**

A flight recorder is understood instantly: it exists so that after something goes wrong, you can establish exactly what happened rather than argue about it. That is the product. Two words, no "AI", no "Copilot".

---

## 4. Tagline

> Replay every agent decision. Prove the fix. Catch what it broke.

---

## 5. Short description

> An agent tells a delivery lead the contractual penalty is 5% of monthly fees per week of delay. It is in no document the agent can see. Agent Black Box records what the agent retrieved, decided and cited, shows the step where it went wrong, replays the same cases against the fix, and reports whether it worked — including the answerable question the safer agent now refuses.

---

## 6. Full description

> **The problem.** Teams are shipping agents into delivery faster than they can evaluate them. When one produces a wrong answer, the process is: somebody screenshots it, somebody else edits the prompt, and everyone agrees it seems better now. The failure cannot be reproduced, the fix cannot be verified, and the cost of the fix is invisible.
>
> **From demos to a learning system.** Agent Black Box turns one-off AI demos into a continuous engineering loop: observe, diagnose, replay, compare, and learn.
>
> **What we built.** Agent Black Box records every run as a structured trace: conversations, retrieval results and scores, tool calls, agent handoffs, generated claims, citations, policies, latency, tokens, cost, and side effects. Versioned policies and evaluators run **over the evidence, independently of the agent**, so teams can apply consistent controls across local Python, HTTP, Microsoft Foundry, webhook, JSONL, and OpenTelemetry integrations.
>
> **Diagnosis, not anecdote.** The readout for a failing case names the causal step. On the shipped sample: retrieval scored 0.25 against an evidence floor of 0.35, the agent answered anyway, and cited a passage containing 0% of what it claimed. Two critical policy violations, both pointing at the same moment.
>
> **The number nobody reports.** Any agent can reach zero hallucinations by refusing everything, and most agent evaluations would score that as a triumph, because they only measure whether answers are correct. We score five verdicts: correct, correctly refused, hallucinated, **over-refused**, and wrong. Over-refusal is the counterweight that makes the other four honest.
>
> **The result, in full.** Replaying the fixed agent over exactly the same cases: accuracy 75% → 88%, groundedness 75% → 100%, hallucination 25% → 0%, policy violations 4 → 0, and tokens down from 188 to 144. **Two cases fixed, one regressed.** The regression is a question the agent used to answer correctly — the same fact as another case, phrased the way a person actually types it ("remind me of the go live date for prod"). Retrieval scored 0.60 against an evidence floor of 0.65, so the tightened agent refused it.
>
> **That regression is the submission.** We could have tuned it away and shown a clean sweep. We shipped it, because a harness whose own demo shows only improvement has not demonstrated the capability it exists for. The trade between hallucination and over-refusal is a decision for a human; the harness's job is to make sure the human is shown it, rather than reading "hallucination down 25%" and shipping.
>
> **Why it is different.** Deterministic evaluation is the default; model judges are optional and auditable. Raw evidence is preserved, so historical traces can be rescored without invoking the agent again. Assertions cover answers, citations, tools, trajectories, policies, latency, tokens, cost, artifacts, and side effects. Over-refusal prevents "refuse everything" from appearing safe.
>
> **It runs with your agent.** Applications can integrate through the Python recorder and SDK, secure HTTP adapters, Microsoft Foundry, webhooks, JSONL, or OpenTelemetry. Azure OpenAI execution has been validated against a real deployment, while the shipped benchmark remains deterministic and offline so release gates do not depend on model variance.
>
> **Working proof.** On the shipped eight-case benchmark, hallucination falls from 25% to 0%, groundedness rises from 75% to 100%, policy violations fall from 4 to 0, and tokens fall from 188 to 144. Agent Black Box also exposes the cost: one previously correct case becomes over-refused. Two cases fixed, one regressed. The platform is covered by 222 automated tests.
>
> **Impact.** Developers can reproduce failures before changing prompts or code. AI quality teams can compare versions using consistent evidence. Delivery leaders can see whether a safety fix introduced unnecessary refusals. Platform teams can apply shared evaluation, retention, and audit controls across different agent frameworks.
>
> **Why this matters beyond the hackathon.** Teams putting agents into delivery need a defensible answer to "how do you know your fix worked?" Agent Black Box makes that question measurable.

---

## 7. Technology tags

`Azure OpenAI` · `Microsoft Foundry` · `Python` · `FastAPI` · `PostgreSQL` · `OpenTelemetry`

---

## 8. Team recruitment blurb

> Looking for two or three people for Hack Week (14–18 Sept).
>
> We are building the flight recorder for enterprise agents: structured traces, policy checks that run outside the agent, replay against a changed configuration, and a comparison that reports what your fix broke as well as what it fixed.
>
> Most useful next: **someone who supports a real agent in delivery and can define what it should and should not answer**, a Python engineer, and a React or product-design contributor.
>
> Bring your own agent. A second team's numbers are worth more to us than a second feature.

---

## 9. Validated evidence

| Evidence | Verified result |
|---|---:|
| Deterministic benchmark | 8 cases |
| Accuracy | 75% → 88% |
| Groundedness | 75% → 100% |
| Hallucination | 25% → 0% |
| Over-refusal | 0% → 12% |
| Policy violations | 4 → 0 |
| Tokens | 188 → 144 |
| Case comparison | 2 fixed, 1 regressed, 5 unchanged |
| Automated tests | 222 passed |
| External execution | Azure OpenAI integration validated |

These benchmark metrics demonstrate platform behavior; they are not presented
as customer production outcomes.

---

## 10. Video guidance

Six sections, following the hackathon two-minute structure:

1. **Problem / hook.** The agent states a contractual penalty that exists nowhere. Confident, specific, fabricated.
2. **Solution.** Introduce Agent Black Box as the flight recorder and regression platform for AI agents.
3. **Live demo.** Run the real CLI, inspect retrieval and citation evidence, replay the same cases, and show the honest comparison.
4. **Innovation / AI.** Explain deterministic-first evaluation, optional model judges, preserved evidence, and over-refusal.
5. **Architecture.** Show local, CI, self-hosted, and Azure operating modes without implying that future roadmap surfaces are already deployed.
6. **Impact and closing.** Name the beneficiaries and end with the measurable value proposition.

The regression inside the live demo is the strongest proof. Do not cut it for
time. Showing it is deliberate: the platform measures the complete effect of a
change rather than presenting only favorable metrics.

---

## 11. Image and video generation pack

### Hero image prompt

> Create a premium 16:9 enterprise editorial illustration for "Agent Black Box." Show an AI agent run as a transparent flight-recorder trace: user question, retrieved evidence, tool calls, generated claim, citation check, policy verdict, and replay comparison. One branch improves while another reveals a regression, making the trade-off visible. Dark graphite and deep navy palette with electric cyan traces and restrained red/amber fault markers. Precise, technical, trustworthy, cinematic depth, generous negative space for a title, no aircraft, no robots, no logos, no unreadable text, no fake metrics, no watermark, 1920x1080.

### Thumbnail prompt

> Create a bold hackathon thumbnail for "Agent Black Box." Show one glowing agent trace entering a black-box recorder, then splitting into "fixed" and visibly regressed paths represented only by green and amber visual signals, not generated words. High contrast, technical but understandable, dark navy and cyan, one red fault marker, minimal layout, room for title added later, no logos, no watermark, 16:9.

### What must be real versus generated

- Real application output is mandatory for the fabricated answer, trace
  readout, evidence score, replay results, and regression. Capture it by
  executing the CLI; it may be placed inside a branded terminal frame, but the
  values and output must remain unmodified.
- Generated video may illustrate invisible agent execution only in the opening and transitions.
- Never generate a fake trace, policy violation, evaluation result, or product UI.
- Keep the regression in the final video; it is the strongest proof that the harness is honest.

### Two-minute production plan

| Time | Visual | Voiceover goal |
|---|---|---|
| 0:00-0:15 | Problem / hook: unsupported but confident answer | Establish the immediate, recognizable pain |
| 0:15-0:30 | Solution: product name and one-line value proposition | Introduce the flight recorder and regression loop |
| 0:30-0:43 | Live application: run `python -m blackbox demo --short --no-color` | Prove that the application works |
| 0:43-0:55 | Live demo problem: weak retrieval and unsupported citation | Point to the causal step |
| 0:55-1:06 | Live demo AI action: replay the fixed agent | Explain reproducibility |
| 1:06-1:15 | Live demo result: two fixed and one regressed | Reveal the over-refusal trade-off |
| 1:15-1:30 | Innovation / AI | Explain deterministic evidence plus optional judges |
| 1:30-1:45 | Architecture | Show only the key local, service, data, integration, and Azure components |
| 1:45-2:00 | Impact and closing | Name beneficiaries and end with the memorable product line |

### AI video prompts for B-roll

**Opening clip**

> Visualize an enterprise AI request moving through retrieval, tool use, composition, and citation as a luminous trace in a dark technical environment. The trace reaches a red unsupported-claim marker and freezes for diagnosis. Elegant, restrained, highly legible motion graphics, no readable text, no robots, no logos, no watermark, 16:9, 5 seconds.

**Replay transition**

> A recorded AI trace rewinds smoothly, duplicates into before-and-after lanes, and runs again through the same checkpoints. One failure turns green while a different checkpoint turns amber, clearly conveying regression detection. Premium technical motion design, dark navy and cyan, no generated labels, no logos, no watermark, 16:9.

**Closing clip**

> A transparent flight-recorder-style trace remains visible and auditable while multiple generic AI systems connect to the same recorder interface. Communicate agent-agnostic evaluation and trust without showing robots or branded products. Cinematic enterprise visualization, negative space for title, no text, logos, or watermark, 16:9.

### Recording and editing instructions

1. Preload the exact failing and replay cases so the demo never depends on live model variance.
2. Record at 1920x1080 and enlarge the trace step, thresholds, and comparison table.
3. Use a cursor highlight only when pointing to the causal step or regression.
4. Freeze the screen for two seconds on "0% claim support" and again on the regressed case.
5. Use natural neural narration with deliberate sentence pauses; do not rely
   on legacy desktop text-to-speech rate controls.
6. Use no more than three on-screen headline captions: "Reproduce," "Diagnose," and "Replay."
7. Export H.264 MP4 at 1080p and confirm the final runtime is no more than two minutes.
8. Test playback without audio and on a small screen; the wrong answer, root cause, and regression must still be obvious.

### Voiceover prompt

> Write a two-minute hackathon voiceover for Agent Black Box using the exact
> timed storyboard above. Start with the unsupported contractual-penalty
> answer. Explain that the recorder captures retrieval, tools, composition,
> citations, and policy verdicts. State the causal evidence precisely, then
> compare the same cases before and after the fix. Celebrate the hallucination
> reduction, but pause on the newly over-refused case and explain why exposing
> that regression proves the platform is honest. Close with the impact for
> developers, quality teams, and delivery leaders. Do not invent agent names,
> metrics, customers, integrations, or results. Use plain spoken English,
> natural sentence breaks, and 200-230 words.

---

## 12. Reminders

- **A two-minute video is mandatory** for eligibility. Upload the validated
  final cut before the project video deadline.
- Challenge selection locks at the project video deadline.
- Challenge winners present to their executive sponsor; the overall ISD winner presents to Edwina Fitzmaurice.
- Check that no customer content appears in any trace shown on screen. The PII policy helps; it is not a substitute for looking.
