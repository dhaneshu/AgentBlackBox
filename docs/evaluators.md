# Evaluator platform

Evaluator plugins implement `blackbox.evaluators.Evaluator`: they declare
`name`, `version`, `api_version`, and `input_requirements`, then provide both
`evaluate(context)` and `evaluate_async(context)`. Results use a bounded
`Score` and an auditable `EvaluatorResult`. A failed evaluator must raise
`EvaluationError` or return an error result without a score.

```python
from blackbox.evaluators import EvaluatorContext, VerdictEvaluator

result = VerdictEvaluator().evaluate(EvaluatorContext(trace, case))
```

Deterministic evaluators are the default. Model judgment is explicitly opt-in:

```python
from blackbox.evaluators import AzureOpenAIJudge

judge = AzureOpenAIJudge(rubric="groundedness", deployment="judge-deployment")
result = judge.evaluate(EvaluatorContext(trace, case))
```

The deployment is resolved in precedence order from the evaluator, evaluator
context, and `OPENAI_DEPLOYMENT`. The resolved value is used unchanged in both
the request and persisted judge metadata for synchronous and asynchronous
evaluation.

Use Microsoft Entra authentication unless key authentication is explicitly
required. Judge inputs are untrusted data, structured output is mandatory, and
only declared HTTP/transient transport failures are retried. Cache model
results only with `EvaluationCacheKey.create`; changing the trace, case,
evaluator version, configuration, deployment, rubric/prompt, or other judge
metadata produces a different key.

Each case result has a canonical deterministic `evaluation_result_id` derived
from both `run_id` and `case_id`. Human reviews and adjudications reference this
ID; an adjudication cannot combine reviews of different evaluation results.

Policy compliance scores the currently configured versioned policy plugins.
Violations already attached to an imported trace remain available separately as
`historical_violations` and never suppress or replace current plugin results.

`EvaluatorEnsemble` provides `all`, `any`, `weighted_mean`, and `majority`.
Required outputs must exist and score ranges must match. `HumanReview` and
`Adjudication` produce `human_review` trace events. Build a
`CalibrationReport` from adjudicated `CalibrationExample` records.

Policies implement `PolicyPlugin` and declare a version, configuration JSON
Schema, severity, remediation, applicability predicate, and deterministic
evaluation. Existing `blackbox.policy.check` and `apply` APIs remain stable.
