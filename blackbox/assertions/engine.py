"""Assertion engine over trace dictionaries and domain objects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from blackbox.assertions.jsonpath import JSONPathError, select
from blackbox.domain.suite import Assertion

PASS, FAIL, ERROR = "pass", "fail", "error"

_ALIASES = {
    "exact_text": "text_exact", "substring": "text_contains",
    "not_contains": "text_not_contains", "jsonschema": "json_schema",
    "numeric": "numeric_tolerance", "citations": "citation",
    "span_existence": "span_exists", "ordered_span_sequence": "span_sequence",
    "tool_arguments": "tool_args", "latency_ms": "latency",
    "token_count": "tokens", "cost_amount": "cost",
}


@dataclass(frozen=True)
class AssertionResult:
    status: str
    assertion_type: str
    expected: Any = None
    actual: Any = None
    evidence_refs: tuple[str, ...] = ()
    failure_code: str = ""
    message: str = ""
    assertion_id: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "assertion_type": self.assertion_type,
            "assertion_id": self.assertion_id, "expected": self.expected,
            "actual": self.actual, "evidence_refs": list(self.evidence_refs),
            "failure_code": self.failure_code, "message": self.message,
        }


@dataclass(frozen=True)
class AssertionContext:
    trace: Any
    fixture_verifications: Mapping[str, Any] = field(default_factory=dict)

    @property
    def document(self) -> dict[str, Any]:
        value = self.trace.to_dict() if hasattr(self.trace, "to_dict") else self.trace
        if not isinstance(value, Mapping):
            raise TypeError("assertion trace must be a mapping or expose to_dict()")
        result = dict(value)
        result["fixture_verifications"] = dict(self.fixture_verifications)
        return result


def _result(
    assertion: Assertion, passed: bool, actual: Any, refs: Iterable[str] = (),
    *, code: str = "VALUE_MISMATCH", message: str = "",
) -> AssertionResult:
    return AssertionResult(
        PASS if passed else FAIL, assertion.type, assertion.expected, actual,
        tuple(refs), "" if passed else code, message, assertion.assertion_id,
    )


def _spans(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [v for v in document.get("spans", []) if isinstance(v, Mapping)]


def _events(span: Mapping[str, Any], event_type: str) -> list[Mapping[str, Any]]:
    return [v for v in span.get("events", [])
            if isinstance(v, Mapping) and v.get("type") == event_type]


def _tool_records(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = []
    for span in _spans(document):
        if span.get("kind") != "tool":
            continue
        requests, responses = _events(span, "tool_request"), _events(span, "tool_response")
        request = requests[-1] if requests else {}
        response = responses[-1] if responses else {}
        records.append({
            "name": span.get("name", ""),
            "args": request.get("payload", {}).get(
                "arguments", request.get("payload", {}).get("args")
            ),
            "result": response.get("payload", {}).get(
                "result", response.get("payload", {}).get("output")
            ),
            "span_id": span.get("span_id", ""),
        })
    return records


def _default_actual(kind: str, document: Mapping[str, Any]) -> tuple[Any, tuple[str, ...]]:
    spans = _spans(document)
    if kind.startswith("text") or kind in {"regex", "json_schema", "numeric_tolerance"}:
        return document.get("answer", ""), (f"trace:{document.get('trace_id', '')}",)
    if kind == "refusal":
        return bool(document.get("refused", False)), (f"trace:{document.get('trace_id', '')}",)
    if kind == "citation":
        citations = document.get("citations", [])
        return citations, tuple(
            f"citation:{v.get('passage_id', index)}" if isinstance(v, Mapping)
            else f"citation:{index}" for index, v in enumerate(citations)
        )
    if kind in {"span_exists", "span_sequence"}:
        return [s.get("name", "") for s in spans], tuple(
            f"span:{s.get('span_id', '')}" for s in spans
        )
    if kind.startswith("tool_"):
        tools = _tool_records(document)
        return tools, tuple(f"span:{v['span_id']}" for v in tools)
    if kind == "latency":
        intervals = sorted(
            (int(s.get("start_ns", 0)), int(s.get("end_ns", 0)))
            for s in spans
            if int(s.get("start_ns", 0)) > 0
            and int(s.get("end_ns", 0)) >= int(s.get("start_ns", 0))
        )
        duration = 0
        if intervals:
            start, end = intervals[0]
            for next_start, next_end in intervals[1:]:
                if next_start <= end:
                    end = max(end, next_end)
                else:
                    duration += end - start
                    start, end = next_start, next_end
            duration += end - start
        else:
            # Duration-only legacy spans lack coordinates and cannot be unioned.
            duration = max((int(s.get("duration_ns", 0)) for s in spans), default=0)
        return duration / 1_000_000, tuple(f"span:{s.get('span_id', '')}" for s in spans)
    if kind == "tokens":
        usage = document.get("usage") or {}
        value = usage.get("total_tokens")
        if value is None:
            value = int(usage.get("input_tokens", document.get("tokens_in", 0))) + int(
                usage.get("output_tokens", document.get("tokens_out", 0))
            )
        return value, (f"trace:{document.get('trace_id', '')}",)
    if kind == "cost":
        return (document.get("cost") or {}).get("amount"), (
            f"trace:{document.get('trace_id', '')}",
        )
    if kind == "policy_violation":
        violations = document.get("violations", [])
        return violations, tuple(
            f"violation:{v.get('policy', index)}" if isinstance(v, Mapping)
            else f"violation:{index}" for index, v in enumerate(violations)
        )
    if kind == "artifact_digest":
        artifacts = document.get("artifacts", [])
        return artifacts, tuple(
            f"artifact:{v.get('artifact_id', index)}" if isinstance(v, Mapping)
            else f"artifact:{index}" for index, v in enumerate(artifacts)
        )
    return None, ()


def _comparison(assertion: Assertion, actual: Any, refs: tuple[str, ...]) -> AssertionResult:
    kind, expected, options = assertion.type, assertion.expected, assertion.options
    if kind in {"text", "text_exact"}:
        left, right = str(actual), str(expected)
        if options.get("case_sensitive", True) is False:
            left, right = left.casefold(), right.casefold()
        return _result(assertion, left == right, actual, refs, code="TEXT_NOT_EXACT")
    if kind in {"text_contains", "text_not_contains"}:
        left, right = str(actual), str(expected)
        if options.get("case_sensitive", False) is False:
            left, right = left.casefold(), right.casefold()
        contains = right in left
        passed = contains if kind == "text_contains" else not contains
        return _result(
            assertion, passed, actual, refs,
            code="TEXT_MISSING" if kind == "text_contains" else "FORBIDDEN_TEXT_FOUND",
        )
    if kind == "regex":
        try:
            flags = re.IGNORECASE if options.get("ignore_case") else 0
            matched = re.search(str(expected), str(actual), flags) is not None
        except re.error as exc:
            return AssertionResult(
                ERROR, kind, expected, actual, refs, "INVALID_REGEX", str(exc),
                assertion.assertion_id,
            )
        return _result(assertion, matched, actual, refs, code="REGEX_NO_MATCH")
    if kind == "refusal":
        return _result(assertion, bool(actual) is bool(expected), actual, refs,
                       code="REFUSAL_MISMATCH")
    if kind == "json_schema":
        try:
            value = json.loads(actual) if isinstance(actual, str) else actual
        except json.JSONDecodeError as exc:
            return AssertionResult(ERROR, kind, expected, actual, refs, "INVALID_JSON",
                                   str(exc), assertion.assertion_id)
        try:
            import jsonschema
        except ImportError:
            return AssertionResult(ERROR, kind, expected, value, refs,
                                   "JSON_SCHEMA_UNAVAILABLE",
                                   "install the 'sdk' extra for JSON Schema assertions",
                                   assertion.assertion_id)
        try:
            jsonschema.Draft202012Validator.check_schema(expected)
            jsonschema.validate(value, expected)
        except jsonschema.SchemaError as exc:
            return AssertionResult(ERROR, kind, expected, value, refs, "INVALID_JSON_SCHEMA",
                                   exc.message, assertion.assertion_id)
        except jsonschema.ValidationError as exc:
            return _result(assertion, False, value, refs, code="JSON_SCHEMA_MISMATCH",
                           message=exc.message)
        return _result(assertion, True, value, refs)
    if kind == "numeric_tolerance":
        target = expected.get("value") if isinstance(expected, Mapping) else expected
        absolute = options.get("absolute", options.get("tolerance", 0))
        relative = options.get("relative", 0)
        try:
            number, target_number = Decimal(str(actual)), Decimal(str(target))
            tolerance = max(Decimal(str(absolute)),
                            abs(target_number) * Decimal(str(relative)))
            passed = number.is_finite() and abs(number - target_number) <= tolerance
        except (InvalidOperation, TypeError):
            return AssertionResult(ERROR, kind, expected, actual, refs,
                                   "NON_NUMERIC_VALUE", "numeric value required",
                                   assertion.assertion_id)
        return _result(assertion, passed, actual, refs, code="NUMERIC_OUT_OF_TOLERANCE")
    if kind == "citation":
        sources = [
            str(v.get("source", v.get("uri", ""))) if isinstance(v, Mapping) else str(v)
            for v in actual
        ]
        return _result(assertion, any(str(expected) in v for v in sources), sources, refs,
                       code="CITATION_MISSING")
    if kind == "span_exists":
        wanted = expected if isinstance(expected, Mapping) else {"name": expected}
        spans = actual if actual and isinstance(actual[0], Mapping) else None
        if spans is None:
            passed = str(wanted.get("name", "")) in actual
        else:
            passed = any(all(span.get(k) == v for k, v in wanted.items()) for span in spans)
        return _result(assertion, passed, actual, refs, code="SPAN_NOT_FOUND")
    if kind == "span_sequence":
        wanted = list(expected or [])
        names = [v.get("name", "") if isinstance(v, Mapping) else str(v) for v in actual]
        iterator = iter(names)
        passed = all(any(item == wanted_name for item in iterator) for wanted_name in wanted)
        return _result(assertion, passed, names, refs, code="SPAN_SEQUENCE_MISMATCH")
    if kind == "tool_call_count":
        name = options.get("name")
        calls = [v for v in actual if not name or v["name"] == name]
        target = expected if not isinstance(expected, Mapping) else expected.get("count")
        return _result(assertion, len(calls) == int(target), len(calls), refs,
                       code="TOOL_CALL_COUNT_MISMATCH")
    if kind in {"tool_args", "tool_result"}:
        name = options.get("name")
        field_name = "args" if kind == "tool_args" else "result"
        values = [v[field_name] for v in actual if not name or v["name"] == name]
        passed = any(v == expected for v in values)
        return _result(assertion, passed, values, refs,
                       code="TOOL_ARGUMENTS_MISMATCH" if kind == "tool_args"
                       else "TOOL_RESULT_MISMATCH")
    if kind in {"latency", "tokens", "cost"}:
        try:
            value, target = Decimal(str(actual)), Decimal(str(expected))
            op = str(options.get("operator", "lte"))
            passed = {"lt": value < target, "lte": value <= target, "eq": value == target,
                      "gte": value >= target, "gt": value > target}.get(op)
            if passed is None:
                raise ValueError(f"unsupported comparison operator {op!r}")
        except (InvalidOperation, TypeError) as exc:
            return AssertionResult(ERROR, kind, expected, actual, refs, "NON_NUMERIC_VALUE",
                                   str(exc), assertion.assertion_id)
        except ValueError as exc:
            return AssertionResult(ERROR, kind, expected, actual, refs, "INVALID_OPERATOR",
                                   str(exc), assertion.assertion_id)
        return _result(assertion, passed, actual, refs, code=f"{kind.upper()}_LIMIT_EXCEEDED")
    if kind == "policy_violation":
        wanted = expected if isinstance(expected, Mapping) else {"policy": expected}
        matched = [v for v in actual if isinstance(v, Mapping)
                   and all(v.get(k) == value for k, value in wanted.items())]
        should_exist = bool(options.get("exists", True))
        return _result(assertion, bool(matched) == should_exist, matched, refs,
                       code="POLICY_VIOLATION_MISMATCH")
    if kind == "artifact_digest":
        name = options.get("name")
        matched = [v for v in actual if isinstance(v, Mapping)
                   and (not name or v.get("name") == name)]
        return _result(assertion, any(v.get("digest") == expected for v in matched),
                       matched, refs, code="ARTIFACT_DIGEST_MISMATCH")
    return AssertionResult(ERROR, kind, expected, actual, refs, "UNKNOWN_ASSERTION_TYPE",
                           f"unknown assertion type {kind!r}", assertion.assertion_id)


def evaluate_assertion(assertion: Assertion | Mapping[str, Any], context: AssertionContext | Any) -> AssertionResult:
    declaration = assertion if isinstance(assertion, Assertion) else Assertion.from_dict(assertion)
    if declaration.type in _ALIASES:
        declaration = Assertion(
            _ALIASES[declaration.type], declaration.expected, declaration.selector,
            declaration.options, declaration.assertion_id,
        )
    ctx = context if isinstance(context, AssertionContext) else AssertionContext(context)
    try:
        document = ctx.document
    except (TypeError, ValueError) as exc:
        return AssertionResult(ERROR, declaration.type, declaration.expected, None, (),
                               "INVALID_ASSERTION_CONTEXT", str(exc), declaration.assertion_id)
    if declaration.selector:
        try:
            values = select(document, declaration.selector)
        except JSONPathError as exc:
            return AssertionResult(ERROR, declaration.type, declaration.expected, None, (),
                                   "INVALID_JSONPATH", str(exc), declaration.assertion_id)
        if not values:
            return AssertionResult(FAIL, declaration.type, declaration.expected, None, (),
                                   "JSONPATH_NO_MATCH",
                                   f"selector {declaration.selector!r} matched no values",
                                   declaration.assertion_id)
        actual = values[0] if len(values) == 1 else values
        refs = (f"jsonpath:{declaration.selector}",)
    else:
        actual, refs = _default_actual(declaration.type, document)
        if declaration.type == "span_exists":
            actual = _spans(document)
    return _comparison(declaration, actual, refs)


def evaluate_assertions(
    assertions: Iterable[Assertion | Mapping[str, Any]], context: AssertionContext | Any,
) -> list[AssertionResult]:
    return [evaluate_assertion(assertion, context) for assertion in assertions]


class AssertionEngine:
    def evaluate(
        self, assertions: Iterable[Assertion | Mapping[str, Any]],
        trace: Any, *, fixture_verifications: Mapping[str, Any] | None = None,
    ) -> list[AssertionResult]:
        return evaluate_assertions(
            assertions, AssertionContext(trace, fixture_verifications or {})
        )
