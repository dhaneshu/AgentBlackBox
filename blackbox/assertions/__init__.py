"""Deterministic, evidence-rich assertion evaluation."""

from blackbox.assertions.engine import (
    AssertionContext,
    AssertionEngine,
    AssertionResult,
    evaluate_assertion,
    evaluate_assertions,
)
from blackbox.assertions.jsonpath import JSONPathError, select
from blackbox.domain.suite import Assertion


class TextAssertion(Assertion):
    def __init__(self, expected, *, mode="exact", selector="", **options):
        kind = {"exact": "text_exact", "contains": "text_contains",
                "not_contains": "text_not_contains"}.get(mode)
        if not kind:
            raise ValueError(f"unknown text assertion mode {mode!r}")
        super().__init__(kind, expected, selector, options)


class RegexAssertion(Assertion):
    def __init__(self, expected, *, selector="", **options):
        super().__init__("regex", expected, selector, options)


class RefusalAssertion(Assertion):
    def __init__(self, expected=True):
        super().__init__("refusal", expected)


class JSONSchemaAssertion(Assertion):
    def __init__(self, expected, *, selector=""):
        super().__init__("json_schema", expected, selector)


class NumericToleranceAssertion(Assertion):
    def __init__(self, expected, *, selector="", tolerance=0, relative=0):
        super().__init__("numeric_tolerance", expected, selector,
                         {"tolerance": tolerance, "relative": relative})


__all__ = [
    "Assertion", "AssertionContext", "AssertionEngine", "AssertionResult",
    "JSONPathError", "JSONSchemaAssertion", "NumericToleranceAssertion",
    "RefusalAssertion", "RegexAssertion", "TextAssertion",
    "evaluate_assertion", "evaluate_assertions", "select",
]
