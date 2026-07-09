"""Command line entry point: `openapi-diff old.yaml new.yaml`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from openapi_diff.core import (
    Change,
    Severity,
    SpecError,
    diff_specs,
    exit_code,
    load_spec,
)

_HEADINGS = {
    Severity.BREAKING: "BREAKING",
    Severity.WARNING: "WARNING",
    Severity.NON_BREAKING: "NON-BREAKING",
}


def _render_text(changes: list[Change], show_all: bool) -> str:
    if not changes:
        return "No differences found."

    lines: list[str] = []
    for severity in (Severity.BREAKING, Severity.WARNING, Severity.NON_BREAKING):
        if severity is Severity.NON_BREAKING and not show_all:
            continue
        group = [c for c in changes if c.severity is severity]
        if not group:
            continue
        lines.append(f"{_HEADINGS[severity]} ({len(group)})")
        for change in group:
            lines.append(f"  {change.location}")
            lines.append(f"    [{change.code}] {change.message}")
        lines.append("")

    counts = {s: sum(1 for c in changes if c.severity is s) for s in Severity}
    lines.append(
        f"Summary: {counts[Severity.BREAKING]} breaking, "
        f"{counts[Severity.WARNING]} warning, "
        f"{counts[Severity.NON_BREAKING]} non-breaking."
    )
    return "\n".join(lines)


def _render_json(changes: list[Change]) -> str:
    payload = {
        "changes": [c.as_dict() for c in changes],
        "summary": {
            s.value: sum(1 for c in changes if c.severity is s) for s in Severity
        },
    }
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openapi-diff",
        description="Compare two OpenAPI 3.x specs and classify what changed.",
    )
    parser.add_argument("old", help="path to the previous spec (JSON or YAML)")
    parser.add_argument("new", help="path to the current spec (JSON or YAML)")
    parser.add_argument(
        "--fail-on",
        choices=["breaking", "warning", "none"],
        default="breaking",
        help="exit 1 when a change at or above this severity is found "
        "(default: breaking)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include non-breaking changes in text output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        old_spec = load_spec(args.old)
        new_spec = load_spec(args.new)
        changes = diff_specs(old_spec, new_spec)
    except (SpecError, OSError) as exc:
        # Exit 2 keeps "the tool broke" distinct from "the API broke".
        print(f"openapi-diff: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(_render_json(changes))
    else:
        print(_render_text(changes, show_all=args.all))

    return exit_code(changes, args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
