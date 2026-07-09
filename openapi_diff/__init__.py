"""Detect breaking changes between two OpenAPI 3.x specs."""

from openapi_diff.core import (
    Change,
    Severity,
    SpecError,
    diff_specs,
    exit_code,
    load_spec,
    parse_spec,
    worst_severity,
)

__version__ = "0.1.0"

__all__ = [
    "Change",
    "Severity",
    "SpecError",
    "diff_specs",
    "exit_code",
    "load_spec",
    "parse_spec",
    "worst_severity",
    "__version__",
]
