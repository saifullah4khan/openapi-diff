"""Compare two OpenAPI 3.x specs and classify every difference.

The public surface is small on purpose: load_spec(), diff_specs(), and the
Change/Severity types. Everything else is an implementation detail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "Severity",
    "Change",
    "SpecError",
    "load_spec",
    "parse_spec",
    "diff_specs",
    "worst_severity",
    "exit_code",
]

# How deep we walk into nested object schemas. Recursive schemas (a Node whose
# child is a Node) are legal OpenAPI and would otherwise never terminate.
MAX_SCHEMA_DEPTH = 8

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# The media type whose schema we compare field by field.
JSON_MEDIA_TYPE = "application/json"


class SpecError(ValueError):
    """The document is not a spec we can compare (bad JSON/YAML, bad $ref)."""


class Severity(str, Enum):
    BREAKING = "breaking"
    WARNING = "warning"
    NON_BREAKING = "non-breaking"


_RANK = {Severity.NON_BREAKING: 0, Severity.WARNING: 1, Severity.BREAKING: 2}


@dataclass(frozen=True)
class Change:
    """One classified difference between the old spec and the new one."""

    severity: Severity
    code: str
    location: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "location": self.location,
            "message": self.message,
        }


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_spec(source: str | Path) -> dict[str, Any]:
    """Load a spec from a path. JSON is native; YAML needs the [yaml] extra."""
    text = Path(source).read_text(encoding="utf-8")
    return parse_spec(text, hint=str(source))


def parse_spec(text: str, hint: str = "<string>") -> dict[str, Any]:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"{hint}: invalid JSON: {exc}") from exc
    else:
        try:
            import yaml
        except ImportError as exc:
            raise SpecError(
                f"{hint} looks like YAML but PyYAML is not installed. "
                "Install it with: pip install 'openapi-diff[yaml]'"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"{hint}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise SpecError(f"{hint}: expected the spec to be a mapping at the top level")
    return data


# --------------------------------------------------------------------------
# $ref resolution and schema flattening
# --------------------------------------------------------------------------


def _resolve(node: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Follow local $refs until we reach a real node. Ref cycles yield {}."""
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SpecError(f"only local '#/...' $refs are supported, got: {ref!r}")
        if ref in seen:
            return {}
        seen.add(ref)
        cursor: Any = spec
        for raw in ref[2:].split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if not isinstance(cursor, dict) or token not in cursor:
                raise SpecError(f"$ref does not resolve: {ref}")
            cursor = cursor[token]
        node = cursor
    return node if isinstance(node, dict) else {}


def _merge_all_of(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge an allOf composition into a single schema."""
    if "allOf" not in schema:
        return schema
    merged: dict[str, Any] = {
        k: v for k, v in schema.items() if k not in ("allOf", "properties", "required")
    }
    properties: dict[str, Any] = {}
    required: list[str] = []

    def absorb(node: dict[str, Any]) -> None:
        properties.update(node.get("properties") or {})
        for name in node.get("required") or []:
            if name not in required:
                required.append(name)
        if "type" in node:
            merged.setdefault("type", node["type"])

    for part in schema["allOf"]:
        absorb(_merge_all_of(_resolve(part, spec), spec))
    # The schema's own properties are absorbed last so they win over the parts.
    absorb({k: v for k, v in schema.items() if k in ("properties", "required", "type")})

    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def _type_name(schema: dict[str, Any]) -> str:
    declared = schema.get("type")
    if isinstance(declared, str):
        fmt = schema.get("format")
        return f"{declared}({fmt})" if isinstance(fmt, str) else declared
    if "properties" in schema:
        return "object"
    return "any"


@dataclass(frozen=True)
class _Field:
    type: str
    required: bool


def flatten_schema(
    schema: Any,
    spec: dict[str, Any],
    prefix: str = "",
    depth: int = 0,
) -> dict[str, _Field]:
    """Flatten an object schema into {"a.b[].c": _Field(type, required)}."""
    resolved = _merge_all_of(_resolve(schema, spec), spec)
    out: dict[str, _Field] = {}
    if depth >= MAX_SCHEMA_DEPTH:
        return out

    properties = resolved.get("properties")
    if not isinstance(properties, dict):
        return out
    required = set(resolved.get("required") or [])

    for name, raw_sub in properties.items():
        sub = _merge_all_of(_resolve(raw_sub, spec), spec)
        path = f"{prefix}{name}"
        out[path] = _Field(type=_type_name(sub), required=name in required)
        if "properties" in sub:
            out.update(flatten_schema(sub, spec, f"{path}.", depth + 1))
        elif sub.get("type") == "array":
            items = _merge_all_of(_resolve(sub.get("items") or {}, spec), spec)
            if "properties" in items:
                out.update(flatten_schema(items, spec, f"{path}[].", depth + 1))
    return out


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def _operations(path_item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        method: body
        for method, body in path_item.items()
        if method in HTTP_METHODS and isinstance(body, dict)
    }


def _parameters(
    path_item: dict[str, Any], operation: dict[str, Any], spec: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Path-level parameters, overridden by operation-level ones (name + in)."""
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (path_item.get("parameters") or [], operation.get("parameters") or []):
        for raw in source:
            param = _resolve(raw, spec)
            name, location = param.get("name"), param.get("in")
            if isinstance(name, str) and isinstance(location, str):
                collected[(name, location)] = param
    return collected


def _json_schema(container: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the JSON schema out of a requestBody/response `content` map."""
    content = _resolve(container, spec).get("content")
    if not isinstance(content, dict) or not content:
        return None
    media = content.get(JSON_MEDIA_TYPE)
    if media is None:
        media = next(iter(content.values()))
    schema = _resolve(media, spec).get("schema")
    return schema if isinstance(schema, dict) else None


def diff_specs(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Classify every difference between two loaded specs.

    Changes are reported from the perspective of an existing client: anything
    that could make a request that used to work start failing, or make a
    response field the client relied on disappear, is BREAKING.
    """
    changes: list[Change] = []
    old_paths = old.get("paths") or {}
    new_paths = new.get("paths") or {}

    for path in sorted(set(old_paths) | set(new_paths)):
        if path not in new_paths:
            changes.append(
                Change(Severity.BREAKING, "path-removed", path, "Path was removed.")
            )
            continue
        if path not in old_paths:
            changes.append(
                Change(Severity.NON_BREAKING, "path-added", path, "New path added.")
            )
            continue

        old_item, new_item = old_paths[path], new_paths[path]
        old_ops, new_ops = _operations(old_item), _operations(new_item)

        for method in sorted(set(old_ops) | set(new_ops)):
            where = f"{method.upper()} {path}"
            if method not in new_ops:
                changes.append(
                    Change(
                        Severity.BREAKING,
                        "operation-removed",
                        where,
                        "Operation was removed.",
                    )
                )
                continue
            if method not in old_ops:
                changes.append(
                    Change(
                        Severity.NON_BREAKING,
                        "operation-added",
                        where,
                        "New operation added.",
                    )
                )
                continue

            changes.extend(
                _diff_operation(
                    where, old_item, old_ops[method], new_item, new_ops[method], old, new
                )
            )

    changes.sort(key=lambda c: (-_RANK[c.severity], c.location, c.code))
    return changes


def _diff_operation(
    where: str,
    old_item: dict[str, Any],
    old_op: dict[str, Any],
    new_item: dict[str, Any],
    new_op: dict[str, Any],
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
) -> list[Change]:
    changes: list[Change] = []

    old_id, new_id = old_op.get("operationId"), new_op.get("operationId")
    if old_id != new_id and old_id is not None:
        changes.append(
            Change(
                Severity.WARNING,
                "operation-id-changed",
                where,
                f"operationId changed from {old_id!r} to {new_id!r}; "
                "generated client method names will change.",
            )
        )

    changes.extend(
        _diff_parameters(
            where,
            _parameters(old_item, old_op, old_spec),
            _parameters(new_item, new_op, new_spec),
        )
    )
    changes.extend(_diff_request_body(where, old_op, new_op, old_spec, new_spec))
    changes.extend(_diff_responses(where, old_op, new_op, old_spec, new_spec))
    return changes


def _diff_parameters(
    where: str,
    old_params: dict[tuple[str, str], dict[str, Any]],
    new_params: dict[tuple[str, str], dict[str, Any]],
) -> list[Change]:
    changes: list[Change] = []
    for key in sorted(set(old_params) | set(new_params)):
        name, location = key
        label = f"{where} ({location} parameter {name!r})"

        if key not in new_params:
            changes.append(
                Change(
                    Severity.WARNING,
                    "request-parameter-removed",
                    label,
                    "Parameter removed. Clients still sending it now depend on how "
                    "the server treats unknown parameters.",
                )
            )
            continue

        if key not in old_params:
            if new_params[key].get("required"):
                changes.append(
                    Change(
                        Severity.BREAKING,
                        "request-parameter-added-required",
                        label,
                        "New required parameter. Every existing request omits it.",
                    )
                )
            else:
                changes.append(
                    Change(
                        Severity.NON_BREAKING,
                        "request-parameter-added-optional",
                        label,
                        "New optional parameter.",
                    )
                )
            continue

        old_param, new_param = old_params[key], new_params[key]
        if not old_param.get("required") and new_param.get("required"):
            changes.append(
                Change(
                    Severity.BREAKING,
                    "request-parameter-became-required",
                    label,
                    "Optional parameter is now required.",
                )
            )

        old_type = _type_name(old_param.get("schema") or {})
        new_type = _type_name(new_param.get("schema") or {})
        if old_type != new_type:
            changes.append(
                Change(
                    Severity.BREAKING,
                    "request-parameter-type-changed",
                    label,
                    f"Type changed from {old_type} to {new_type}.",
                )
            )
    return changes


def _diff_request_body(
    where: str,
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
) -> list[Change]:
    changes: list[Change] = []
    old_body = _resolve(old_op.get("requestBody") or {}, old_spec)
    new_body = _resolve(new_op.get("requestBody") or {}, new_spec)

    if not old_body.get("required") and new_body.get("required"):
        changes.append(
            Change(
                Severity.BREAKING,
                "request-body-became-required",
                where,
                "A request body is now required.",
            )
        )

    if not old_body or not new_body:
        return changes

    old_schema = _json_schema(old_body, old_spec)
    new_schema = _json_schema(new_body, new_spec)
    if old_schema is None or new_schema is None:
        return changes

    old_fields = flatten_schema(old_schema, old_spec)
    new_fields = flatten_schema(new_schema, new_spec)

    for name in sorted(set(old_fields) | set(new_fields)):
        label = f"{where} (request body {name})"

        if name not in new_fields:
            changes.append(
                Change(
                    Severity.WARNING,
                    "request-property-removed",
                    label,
                    "Property removed from the request body.",
                )
            )
            continue

        if name not in old_fields:
            if new_fields[name].required:
                changes.append(
                    Change(
                        Severity.BREAKING,
                        "request-property-added-required",
                        label,
                        "New required property. Every existing request omits it.",
                    )
                )
            else:
                changes.append(
                    Change(
                        Severity.NON_BREAKING,
                        "request-property-added-optional",
                        label,
                        "New optional property.",
                    )
                )
            continue

        old_field, new_field = old_fields[name], new_fields[name]
        if not old_field.required and new_field.required:
            changes.append(
                Change(
                    Severity.BREAKING,
                    "request-property-became-required",
                    label,
                    "Optional property is now required.",
                )
            )
        elif old_field.required and not new_field.required:
            changes.append(
                Change(
                    Severity.NON_BREAKING,
                    "request-property-became-optional",
                    label,
                    "Required property is now optional.",
                )
            )
        if old_field.type != new_field.type:
            changes.append(
                Change(
                    Severity.BREAKING,
                    "request-property-type-changed",
                    label,
                    f"Type changed from {old_field.type} to {new_field.type}.",
                )
            )
    return changes


def _diff_responses(
    where: str,
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
) -> list[Change]:
    changes: list[Change] = []
    old_responses = {str(k): v for k, v in (old_op.get("responses") or {}).items()}
    new_responses = {str(k): v for k, v in (new_op.get("responses") or {}).items()}

    for status in sorted(set(old_responses) | set(new_responses)):
        label = f"{where} ({status} response)"

        if status not in new_responses:
            changes.append(
                Change(
                    Severity.BREAKING,
                    "response-status-removed",
                    label,
                    "Status code no longer documented.",
                )
            )
            continue
        if status not in old_responses:
            changes.append(
                Change(
                    Severity.NON_BREAKING,
                    "response-status-added",
                    label,
                    "New status code documented.",
                )
            )
            continue

        old_schema = _json_schema(old_responses[status], old_spec)
        new_schema = _json_schema(new_responses[status], new_spec)
        if old_schema is None or new_schema is None:
            continue

        old_fields = flatten_schema(old_schema, old_spec)
        new_fields = flatten_schema(new_schema, new_spec)

        for name in sorted(set(old_fields) | set(new_fields)):
            field_label = f"{where} ({status} response body {name})"
            if name not in new_fields:
                changes.append(
                    Change(
                        Severity.BREAKING,
                        "response-property-removed",
                        field_label,
                        "Property removed. Clients reading it will break.",
                    )
                )
            elif name not in old_fields:
                changes.append(
                    Change(
                        Severity.NON_BREAKING,
                        "response-property-added",
                        field_label,
                        "New property in the response.",
                    )
                )
            elif old_fields[name].type != new_fields[name].type:
                changes.append(
                    Change(
                        Severity.BREAKING,
                        "response-property-type-changed",
                        field_label,
                        f"Type changed from {old_fields[name].type} "
                        f"to {new_fields[name].type}.",
                    )
                )
    return changes


# --------------------------------------------------------------------------
# reporting helpers
# --------------------------------------------------------------------------


def worst_severity(changes: list[Change]) -> Severity:
    if not changes:
        return Severity.NON_BREAKING
    return max((c.severity for c in changes), key=lambda s: _RANK[s])


def exit_code(changes: list[Change], fail_on: str) -> int:
    """0 = pass, 1 = at least one change at or above the fail_on threshold."""
    if fail_on == "none":
        return 0
    threshold = _RANK[Severity(fail_on)]
    return int(any(_RANK[c.severity] >= threshold for c in changes))
