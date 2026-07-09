"""Every rule the differ knows about gets one focused test."""

from __future__ import annotations

import copy
import json

import pytest

from openapi_diff import Severity, SpecError, diff_specs, exit_code, parse_spec
from openapi_diff.core import MAX_SCHEMA_DEPTH, flatten_schema

BASE = {
    "openapi": "3.0.3",
    "info": {"title": "Widgets", "version": "1.0.0"},
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}}
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Widget"}
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "createWidget",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "color": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}},
            },
        }
    },
    "components": {
        "schemas": {
            "Widget": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}, "size": {"type": "integer"}},
            }
        }
    },
}


def spec():
    return copy.deepcopy(BASE)


def codes(changes):
    return {c.code for c in changes}


def find(changes, code):
    matches = [c for c in changes if c.code == code]
    assert matches, f"expected a {code} change, got {sorted(codes(changes))}"
    return matches[0]


def test_identical_specs_produce_no_changes():
    assert diff_specs(spec(), spec()) == []


def test_removed_path_is_breaking():
    new = spec()
    del new["paths"]["/widgets"]
    change = find(diff_specs(spec(), new), "path-removed")
    assert change.severity is Severity.BREAKING


def test_added_path_is_non_breaking():
    new = spec()
    new["paths"]["/gadgets"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    change = find(diff_specs(spec(), new), "path-added")
    assert change.severity is Severity.NON_BREAKING


def test_removed_operation_is_breaking():
    new = spec()
    del new["paths"]["/widgets"]["post"]
    assert find(diff_specs(spec(), new), "operation-removed").severity is Severity.BREAKING


def test_changed_operation_id_is_a_warning():
    new = spec()
    new["paths"]["/widgets"]["get"]["operationId"] = "getWidgets"
    assert find(diff_specs(spec(), new), "operation-id-changed").severity is Severity.WARNING


def test_new_required_parameter_is_breaking():
    new = spec()
    new["paths"]["/widgets"]["get"]["parameters"].append(
        {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    change = find(diff_specs(spec(), new), "request-parameter-added-required")
    assert change.severity is Severity.BREAKING
    assert "tenant" in change.location


def test_new_optional_parameter_is_non_breaking():
    new = spec()
    new["paths"]["/widgets"]["get"]["parameters"].append(
        {"name": "cursor", "in": "query", "schema": {"type": "string"}}
    )
    assert (
        find(diff_specs(spec(), new), "request-parameter-added-optional").severity
        is Severity.NON_BREAKING
    )


def test_parameter_becoming_required_is_breaking():
    new = spec()
    new["paths"]["/widgets"]["get"]["parameters"][0]["required"] = True
    assert (
        find(diff_specs(spec(), new), "request-parameter-became-required").severity
        is Severity.BREAKING
    )


def test_parameter_type_change_is_breaking():
    new = spec()
    new["paths"]["/widgets"]["get"]["parameters"][0]["schema"] = {"type": "string"}
    change = find(diff_specs(spec(), new), "request-parameter-type-changed")
    assert "integer to string" in change.message


def test_path_level_parameters_are_inherited_by_operations():
    old = spec()
    old["paths"]["/widgets"]["parameters"] = [
        {"name": "trace", "in": "header", "schema": {"type": "string"}}
    ]
    new = spec()  # no inherited parameter at all
    change = find(diff_specs(old, new), "request-parameter-removed")
    assert "trace" in change.location
    assert change.severity is Severity.WARNING


def test_new_required_request_property_is_breaking():
    new = spec()
    body = new["paths"]["/widgets"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    body["properties"]["sku"] = {"type": "string"}
    body["required"].append("sku")
    assert (
        find(diff_specs(spec(), new), "request-property-added-required").severity
        is Severity.BREAKING
    )


def test_relaxing_a_required_request_property_is_non_breaking():
    new = spec()
    new["paths"]["/widgets"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["required"] = []
    assert (
        find(diff_specs(spec(), new), "request-property-became-optional").severity
        is Severity.NON_BREAKING
    )


def test_request_body_becoming_required_is_breaking():
    old = spec()
    old["paths"]["/widgets"]["post"]["requestBody"]["required"] = False
    assert (
        find(diff_specs(old, spec()), "request-body-became-required").severity
        is Severity.BREAKING
    )


def test_removed_response_property_is_breaking_via_ref():
    new = spec()
    del new["components"]["schemas"]["Widget"]["properties"]["size"]
    change = find(diff_specs(spec(), new), "response-property-removed")
    assert change.severity is Severity.BREAKING
    assert "response body size" in change.location


def test_added_response_property_is_non_breaking():
    new = spec()
    new["components"]["schemas"]["Widget"]["properties"]["weight"] = {"type": "number"}
    assert (
        find(diff_specs(spec(), new), "response-property-added").severity
        is Severity.NON_BREAKING
    )


def test_response_property_type_change_is_breaking():
    new = spec()
    new["components"]["schemas"]["Widget"]["properties"]["id"] = {"type": "integer"}
    change = find(diff_specs(spec(), new), "response-property-type-changed")
    assert "string to integer" in change.message


def test_removed_response_status_is_breaking():
    new = spec()
    del new["paths"]["/widgets"]["get"]["responses"]["200"]
    new["paths"]["/widgets"]["get"]["responses"]["204"] = {"description": "empty"}
    found = codes(diff_specs(spec(), new))
    assert "response-status-removed" in found
    assert "response-status-added" in found


def test_format_is_part_of_the_type_so_int32_to_int64_is_flagged():
    old = spec()
    old["components"]["schemas"]["Widget"]["properties"]["size"] = {
        "type": "integer",
        "format": "int32",
    }
    new = spec()
    new["components"]["schemas"]["Widget"]["properties"]["size"] = {
        "type": "integer",
        "format": "int64",
    }
    change = find(diff_specs(old, new), "response-property-type-changed")
    assert "integer(int32) to integer(int64)" in change.message


def test_all_of_is_merged_before_comparison():
    doc = {
        "components": {
            "schemas": {
                "Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
            }
        }
    }
    schema = {
        "allOf": [{"$ref": "#/components/schemas/Base"}],
        "properties": {"name": {"type": "string"}},
    }
    fields = flatten_schema(schema, doc)
    assert fields["id"].required is True
    assert fields["name"].required is False


def test_nested_objects_and_arrays_get_dotted_paths():
    doc = {}
    schema = {
        "type": "object",
        "properties": {
            "owner": {"type": "object", "properties": {"email": {"type": "string"}}},
            "tags": {
                "type": "array",
                "items": {"type": "object", "properties": {"label": {"type": "string"}}},
            },
        },
    }
    assert set(flatten_schema(schema, doc)) == {
        "owner",
        "owner.email",
        "tags",
        "tags[].label",
    }


def test_recursive_schema_terminates_at_the_depth_cap():
    doc = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "child": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        }
    }
    fields = flatten_schema({"$ref": "#/components/schemas/Node"}, doc)
    assert "child.child.value" in fields
    assert len(fields) <= 2 * MAX_SCHEMA_DEPTH


def test_unresolvable_ref_raises_spec_error():
    doc = {"components": {"schemas": {}}}
    with pytest.raises(SpecError, match="does not resolve"):
        flatten_schema({"$ref": "#/components/schemas/Missing"}, doc)


def test_external_ref_is_rejected_rather_than_silently_ignored():
    with pytest.raises(SpecError, match="only local"):
        flatten_schema({"$ref": "other.yaml#/Thing"}, {})


def test_changes_are_sorted_breaking_first():
    new = spec()
    new["paths"]["/gadgets"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    del new["paths"]["/widgets"]["post"]
    changes = diff_specs(spec(), new)
    assert changes[0].severity is Severity.BREAKING
    assert changes[-1].severity is Severity.NON_BREAKING


def test_exit_code_respects_the_threshold():
    new = spec()
    new["paths"]["/widgets"]["get"]["operationId"] = "renamed"
    changes = diff_specs(spec(), new)  # one warning, nothing breaking
    assert exit_code(changes, "breaking") == 0
    assert exit_code(changes, "warning") == 1
    assert exit_code(changes, "none") == 0


def test_parse_spec_reads_json_without_pyyaml():
    doc = parse_spec(json.dumps(BASE))
    assert doc["info"]["title"] == "Widgets"


def test_parse_spec_rejects_a_non_mapping_document():
    with pytest.raises(SpecError, match="mapping"):
        parse_spec("[1, 2, 3]")
