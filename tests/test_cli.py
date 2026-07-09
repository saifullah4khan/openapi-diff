import json

from openapi_diff.cli import main

OLD = """
openapi: 3.0.3
info: {title: Widgets, version: 1.0.0}
paths:
  /widgets:
    get:
      responses:
        "200": {description: ok}
    delete:
      responses:
        "204": {description: gone}
"""

NEW = """
openapi: 3.0.3
info: {title: Widgets, version: 2.0.0}
paths:
  /widgets:
    get:
      parameters:
        - {name: tenant, in: query, required: true, schema: {type: string}}
      responses:
        "200": {description: ok}
"""


def write_pair(tmp_path):
    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(OLD)
    new.write_text(NEW)
    return str(old), str(new)


def test_cli_exits_1_on_breaking_changes(tmp_path, capsys):
    old, new = write_pair(tmp_path)
    assert main([old, new]) == 1
    out = capsys.readouterr().out
    assert "BREAKING" in out
    assert "operation-removed" in out


def test_cli_exits_0_when_failures_are_disabled(tmp_path):
    old, new = write_pair(tmp_path)
    assert main([old, new, "--fail-on", "none"]) == 0


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    old, new = write_pair(tmp_path)
    main([old, new, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["breaking"] >= 2
    assert {"severity", "code", "location", "message"} <= set(payload["changes"][0])


def test_cli_exit_2_when_a_file_is_missing(tmp_path, capsys):
    old, _ = write_pair(tmp_path)
    assert main([old, str(tmp_path / "nope.yaml")]) == 2
    assert "openapi-diff:" in capsys.readouterr().err
