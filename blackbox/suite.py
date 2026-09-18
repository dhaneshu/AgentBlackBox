"""Compatibility exports for the schema-v2 suite API."""

import warnings

warnings.warn(
    "blackbox.suite is a compatibility module; import suite contracts from "
    "blackbox.domain.suite",
    DeprecationWarning,
    stacklevel=2,
)

from blackbox.domain.suite import (
    Assertion,
    Dataset,
    EXPECTATIONS,
    Case,
    EvaluationCase,
    FixtureRef,
    SamplingConfig,
    SuiteReport,
    load_dataset,
    load_suite,
    summarise,
    validate_suite,
    validate_case_schema,
)

__all__ = [
    "EXPECTATIONS",
    "Assertion",
    "Case",
    "Dataset",
    "EvaluationCase",
    "FixtureRef",
    "SamplingConfig",
    "SuiteReport",
    "load_dataset",
    "load_suite",
    "summarise",
    "validate_suite",
    "validate_case_schema",
]
