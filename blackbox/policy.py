"""Compatibility exports for domain policy types and checks."""

import warnings

warnings.warn(
    "blackbox.policy is a compatibility module; import policy contracts from "
    "blackbox.domain.policy",
    DeprecationWarning,
    stacklevel=2,
)

from blackbox.domain.policy import (
    POLICIES,
    POLICY_PLUGINS,
    BuiltinPolicy,
    PolicyPlugin,
    PolicyResult,
    PolicySet,
    apply,
    check,
    evaluate_policy_plugins,
)

__all__ = [
    "POLICIES", "POLICY_PLUGINS", "BuiltinPolicy", "PolicyPlugin",
    "PolicyResult", "PolicySet", "apply", "check", "evaluate_policy_plugins",
]
