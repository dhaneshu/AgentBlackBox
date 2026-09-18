"""Deterministic evaluator plugins shipped with Agent Black Box."""

from __future__ import annotations

import time
import itertools
import math
import re
from decimal import Decimal
from typing import Any, Mapping

from blackbox.assertions import AssertionEngine, AssertionResult
from blackbox.domain.policy import PolicySet, evaluate_policy_plugins
from blackbox.domain.trace import content_digest
from blackbox.evaluators.base import (
    BaseEvaluator,
    EvaluationError,
    EvaluatorContext,
    EvaluatorResult,
    InputRequirement,
    Score,
)

GOOD_VERDICTS = frozenset({"correct", "correctly_refused"})


def legacy_verdict(trace: Any, case: Any) -> tuple[str, str]:
    """The original five-way verdict algorithm, preserved byte-for-byte in meaning."""
    if case.expect == "refuse":
        if trace.refused:
            return "correctly_refused", "Declined something the corpus does not contain."
        return (
            "hallucinated",
            "Answered a question the corpus cannot support: "
            f'"{trace.answer[:80]}"',
        )
    if trace.refused:
        return "over_refused", "Refused a question the corpus can answer."
    missing = [
        needle for needle in case.must_contain
        if needle.lower() not in trace.answer.lower()
    ]
    if missing:
        return "wrong", f"Answer is missing required content: {missing}"
    banned = [
        needle for needle in case.forbid
        if needle.lower() in trace.answer.lower()
    ]
    if banned:
        return "wrong", f"Answer contains forbidden content: {banned}"
    cited = [str(c.source) for c in trace.citations]
    for wanted in case.must_cite:
        if not any(wanted in source for source in cited):
            return "wrong", f"Expected a citation to {wanted}, got {cited or 'none'}"
    if not trace.grounded:
        unsupported = trace.unsupported_claims
        detail = f"{unsupported[0]}" if unsupported else "no citations"
        return (
            "hallucinated",
            f"Answer content is not carried by the passage it cites ({detail}).",
        )
    return "correct", "Answered, cited, and the citation holds it up."


class VerdictEvaluator(BaseEvaluator):
    name = "verdict"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        verdict, rationale = legacy_verdict(context.trace, context.case)
        return self._result(
            context,
            score=Score(float(verdict in GOOD_VERDICTS), threshold=1.0, label=verdict),
            output={"verdict": verdict, "reason": rationale},
            rationale=rationale,
            evidence_refs=(f"trace:{context.trace.trace_id}",),
            started_ns=started,
        )


class GroundednessEvaluator(BaseEvaluator):
    name = "groundedness"
    version = "1.0.0"
    input_requirements = (InputRequirement("trace"),)

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        grounded = bool(context.trace.grounded)
        unsupported = [str(value) for value in context.trace.unsupported_claims]
        rationale = (
            "The trace is grounded."
            if grounded else
            f"Unsupported citation(s): {', '.join(unsupported) or 'none supplied'}."
        )
        return self._result(
            context,
            score=Score(float(grounded), threshold=1.0, label="groundedness"),
            output={"grounded": grounded, "unsupported_citations": unsupported},
            rationale=rationale,
            evidence_refs=tuple(
                f"citation:{index}" for index, _ in enumerate(context.trace.citations)
            ) or (f"trace:{context.trace.trace_id}",),
            started_ns=started,
        )


def evaluate_assertions(context: EvaluatorContext) -> tuple[AssertionResult, ...]:
    if context.assertion_results:
        return tuple(context.assertion_results)
    results = list(AssertionEngine().evaluate(context.case.assertions, context.trace))
    for step in context.trace.steps:
        if step.kind != "fixture" or step.name != "fixture.verify":
            continue
        verification = step.detail.get("verification") or {}
        passed = (
            verification.get("passed") is True
            if isinstance(verification, dict) else verification is True
        )
        results.append(AssertionResult(
            "pass" if passed else "fail",
            "fixture_verification",
            verification.get("expected") if isinstance(verification, dict) else True,
            verification.get("actual") if isinstance(verification, dict) else verification,
            (
                f"artifact:{step.detail.get('before', {}).get('artifact_id', '')}",
                f"artifact:{step.detail.get('after', {}).get('artifact_id', '')}",
            ),
            "" if passed else "FIXTURE_VERIFICATION_FAILED",
            "" if passed else f"fixture {step.detail.get('name', '')!r} verification failed",
            step.detail.get("name", ""),
        ))
    return tuple(results)


class AssertionEvaluator(BaseEvaluator):
    name = "assertions"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        results = evaluate_assertions(context)
        passed = sum(result.passed for result in results)
        value = passed / len(results) if results else 1.0
        return self._result(
            context,
            score=Score(value, threshold=1.0, label="assertion_pass_rate"),
            output={"passed": passed, "total": len(results),
                    "results": [result.to_dict() for result in results]},
            rationale=f"{passed}/{len(results)} assertions passed.",
            evidence_refs=tuple(
                ref for result in results for ref in result.evidence_refs
            ),
            started_ns=started,
        )


class TaskCompletionEvaluator(AssertionEvaluator):
    name = "task_completion"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        result = super().evaluate(context)
        return EvaluatorResult(
            self.name, self.version, result.score,
            {"completed": result.score.passed if result.score else False,
             **dict(result.output)},
            result.rationale, result.evidence_refs, result.configuration,
            input_hash=result.input_hash, latency_ms=result.latency_ms,
        )


class ToolCorrectnessEvaluator(BaseEvaluator):
    name = "tool_correctness"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        relevant = tuple(
            result for result in evaluate_assertions(context)
            if result.assertion_type.startswith("tool_")
        )
        passed = sum(result.passed for result in relevant)
        value = passed / len(relevant) if relevant else 1.0
        return self._result(
            context,
            score=Score(value, threshold=1.0, label="tool_correctness"),
            output={"passed": passed, "total": len(relevant)},
            rationale=f"{passed}/{len(relevant)} tool assertions passed.",
            evidence_refs=tuple(ref for result in relevant for ref in result.evidence_refs),
            started_ns=started,
        )


class TrajectoryEvaluator(BaseEvaluator):
    name = "trajectory"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        expected = context.configuration.get("expected")
        if expected is None:
            expected = context.case.metadata.get("expected_span_sequence", ())
        expected = list(expected or ())
        actual = [span.name for span in context.trace.spans]
        cursor = iter(actual)
        matched = all(any(item == wanted for item in cursor) for wanted in expected)
        return self._result(
            context,
            score=Score(float(matched), threshold=1.0, label="trajectory"),
            output={"expected": expected, "actual": actual, "conforms": matched},
            rationale="Expected span sequence found." if matched else "Expected span sequence missing.",
            evidence_refs=tuple(f"span:{span.span_id}" for span in context.trace.spans),
            started_ns=started,
        )


def _trace_latency_ms(trace: Any) -> float:
    spans = list(trace.spans)
    starts = [span.start_ns for span in spans if span.start_ns > 0]
    ends = [span.end_ns for span in spans if span.end_ns >= span.start_ns > 0]
    if starts and ends:
        return (max(ends) - min(starts)) / 1_000_000
    return max((span.duration_ns for span in spans), default=0) / 1_000_000


class LatencyEvaluator(BaseEvaluator):
    name = "latency"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        actual = _trace_latency_ms(context.trace)
        maximum = float(context.configuration.get("maximum_ms", float("inf")))
        passed = actual <= maximum
        score = 1.0 if passed else max(0.0, maximum / actual) if maximum > 0 else 0.0
        return self._result(
            context,
            score=Score(score, threshold=1.0, label="latency"),
            output={"latency_ms": actual, "maximum_ms": maximum if maximum != float("inf") else None},
            rationale=f"Trace latency was {actual:.3f} ms.",
            evidence_refs=tuple(f"span:{span.span_id}" for span in context.trace.spans),
            started_ns=started,
        )


class TokenUsageEvaluator(BaseEvaluator):
    name = "tokens"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        actual = max(
            int(context.trace.total_tokens),
            int(getattr(context.trace.usage, "total_tokens", 0)),
        )
        maximum = context.configuration.get("maximum")
        passed = maximum is None or actual <= int(maximum)
        value = 1.0 if passed else max(0.0, int(maximum) / actual) if int(maximum) else 0.0
        return self._result(
            context, score=Score(value, threshold=1.0, label="tokens"),
            output={"tokens": actual, "maximum": maximum},
            rationale=f"Trace used {actual} tokens.",
            evidence_refs=(f"trace:{context.trace.trace_id}",), started_ns=started,
        )


class CostEvaluator(BaseEvaluator):
    name = "cost"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        cost = context.trace.cost
        unknown = not cost.provider
        maximum = context.configuration.get("maximum")
        if unknown and maximum is not None:
            raise EvaluationError(
                "UNKNOWN_COST", "cost threshold cannot be evaluated without pricing",
                evaluator=self.name,
            )
        if unknown:
            return EvaluatorResult(
                self.name,
                self.version,
                None,
                {
                    "state": "unknown",
                    "amount": None,
                    "currency": cost.currency,
                    "provider": "",
                    "maximum": None,
                },
                "Cost is unknown because no price provider was recorded.",
                (f"trace:{context.trace.trace_id}",),
                dict(context.configuration),
                input_hash=content_digest({
                    "trace": context.trace.to_dict(),
                    "case": context.case.to_dict(),
                }),
                latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
            )
        passed = maximum is None or cost.amount <= Decimal(str(maximum))
        return self._result(
            context, score=Score(float(passed), threshold=1.0, label="cost"),
            output={"state": "known",
                    "amount": format(cost.amount, "f"), "currency": cost.currency,
                    "provider": cost.provider, "maximum": maximum},
            rationale=f"Trace cost was {cost.amount} {cost.currency}.",
            evidence_refs=(f"trace:{context.trace.trace_id}",), started_ns=started,
        )


class PolicyComplianceEvaluator(BaseEvaluator):
    name = "policy_compliance"
    version = "1.0.0"

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        policies = context.policies or PolicySet()
        policy_results = evaluate_policy_plugins(context.trace, policies)
        current_violations = [
            violation
            for result in policy_results
            for violation in result.violations
        ]
        historical_violations = list(context.trace.violations)
        return self._result(
            context,
            score=Score(
                float(not current_violations),
                threshold=1.0,
                label="policy_compliance",
            ),
            output={
                "violations": [
                    violation.to_dict() for violation in current_violations
                ],
                "historical_violations": [
                    violation.to_dict() for violation in historical_violations
                ],
                "policy_results": [result.to_dict() for result in policy_results],
            },
            rationale=f"{len(current_violations)} current policy violation(s).",
            evidence_refs=tuple(
                f"violation:{violation.policy}" for violation in current_violations
            ),
            started_ns=started,
        )


class StabilityEvaluator(BaseEvaluator):
    name = "stability"
    version = "2.0.0"
    input_requirements = (
        InputRequirement("trace"), InputRequirement("case"),
        InputRequirement("repeats", required=False),
    )

    def evaluate(self, context: EvaluatorContext) -> EvaluatorResult:
        started = time.perf_counter_ns()
        traces = (context.trace, *context.repeats)
        signatures = [
            content_digest({
                "answer": trace.answer, "refused": trace.refused,
                "citations": [citation.to_dict() for citation in trace.citations],
                "violations": [violation.to_dict() for violation in trace.violations],
            })
            for trace in traces
        ]
        exact = semantic = comparisons = 0
        threshold = float(context.configuration.get("semantic_threshold", 0.8))
        if not 0 <= threshold <= 1:
            raise EvaluationError(
                "INVALID_CONFIGURATION",
                "semantic_threshold must be between 0 and 1",
                evaluator=self.name,
            )
        for left, right in itertools.combinations(traces, 2):
            comparisons += 1
            exact += (
                left.answer, left.refused, left.citations, left.violations
            ) == (
                right.answer, right.refused, right.citations, right.violations
            )
            semantic += (
                left.refused == right.refused
                and _semantic_similarity(left.answer, right.answer) >= threshold
            )
        exact_value = exact / comparisons if comparisons else 1.0
        semantic_value = semantic / comparisons if comparisons else 1.0
        return self._result(
            context, score=Score(exact_value, threshold=1.0, label="exact_agreement"),
            output={
                "trials": len(signatures),
                "comparisons": comparisons,
                "exact_agreement": exact_value,
                "exact_confidence_interval": _wilson(exact, comparisons),
                "semantic_agreement": semantic_value,
                "semantic_confidence_interval": _wilson(semantic, comparisons),
                "semantic_threshold": threshold,
                "seed": context.metadata.get(
                    "random_seed", context.configuration.get("random_seed")
                ),
                "digests": signatures,
            },
            rationale=f"{exact}/{comparisons} trial pairs were exactly identical.",
            evidence_refs=tuple(f"trace:{trace.trace_id}" for trace in traces),
            started_ns=started,
        )


def _semantic_similarity(left: str, right: str) -> float:
    a = set(re.findall(r"[a-z0-9]+", left.lower()))
    b = set(re.findall(r"[a-z0-9]+", right.lower()))
    return len(a & b) / len(a | b) if a | b else 1.0


def _wilson(successes: int, total: int) -> list[float]:
    if total == 0:
        return [1.0, 1.0]
    z = 1.959963984540054
    value = successes / total
    denominator = 1 + z * z / total
    centre = (value + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        value * (1 - value) / total + z * z / (4 * total * total)
    ) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


BUILTIN_EVALUATORS: Mapping[str, type[BaseEvaluator]] = {
    evaluator.name: evaluator
    for evaluator in (
        VerdictEvaluator, GroundednessEvaluator, AssertionEvaluator,
        TaskCompletionEvaluator, ToolCorrectnessEvaluator, TrajectoryEvaluator,
        LatencyEvaluator, TokenUsageEvaluator, CostEvaluator,
        PolicyComplianceEvaluator, StabilityEvaluator,
    )
}


def make_builtin(name: str) -> BaseEvaluator:
    try:
        return BUILTIN_EVALUATORS[name]()
    except KeyError as exc:
        raise EvaluationError(
            "UNKNOWN_EVALUATOR", f"unknown built-in evaluator {name!r}",
            evaluator=name,
        ) from exc
