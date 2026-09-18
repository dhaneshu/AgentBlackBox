"""Compatibility exports for result scoring and comparison."""

import warnings

warnings.warn(
    "blackbox.score is a compatibility module; import result contracts from "
    "blackbox.domain.result",
    DeprecationWarning,
    stacklevel=2,
)

from blackbox.domain.result import (
    GOOD_VERDICTS,
    VERDICTS,
    CaseResult,
    Change,
    Comparison,
    RunScore,
    compare,
    judge,
    score_run,
    write_score,
)

__all__ = [
    "GOOD_VERDICTS",
    "VERDICTS",
    "CaseResult",
    "Change",
    "Comparison",
    "RunScore",
    "compare",
    "judge",
    "score_run",
    "write_score",
]
