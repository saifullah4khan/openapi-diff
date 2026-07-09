# openapi-diff

Tell me whether the spec change I am about to merge will break somebody's client.

## The problem

An OpenAPI spec is a contract, but git treats it as text. A pull request that adds one
`required: true` to a query parameter looks like a one-line diff and quietly breaks every
integration in production. Reviewers cannot reliably spot that by eye, especially once
schemas are split across `$ref`s and `allOf` compositions, so the breakage gets found by
a customer instead of by CI.

`openapi-diff` compares the old spec against the new one, classifies each difference as
breaking, warning, or non-breaking from the point of view of an existing client, and
exits non-zero when something breaking slips in.

## Quickstart

```bash
pip install "openapi-diff[yaml] @ git+https://github.com/saifullah4khan/openapi-diff"

openapi-diff examples/petstore-v1.yaml examples/petstore-v2.yaml
```

```
BREAKING (5)
  /pets/{petId}
    [path-removed] Path was removed.
  GET /pets (200 response body items[].id)
    [response-property-type-changed] Type changed from string to integer.
  GET /pets (200 response body items[].tag)
    [response-property-removed] Property removed. Clients reading it will break.
  GET /pets (query parameter 'tenant')
    [request-parameter-added-required] New required parameter. Every existing request omits it.
  POST /pets (request body species)
    [request-property-added-required] New required property. Every existing request omits it.

Summary: 5 breaking, 0 warning, 1 non-breaking.
```

Exit status is `1`, so this drops straight into CI:

```yaml
- run: openapi-diff origin-main-spec.yaml openapi.yaml
```

As a library:

```python
from openapi_diff import Severity, diff_specs, load_spec

changes = diff_specs(load_spec("old.yaml"), load_spec("new.yaml"))
breaking = [c for c in changes if c.severity is Severity.BREAKING]
```

## What counts as breaking

Severity is judged from the perspective of a client that was written against the old
spec and will not be redeployed.

| Change | Severity | Reasoning |
| --- | --- | --- |
| Path or operation removed | breaking | Existing calls 404 or 405. |
| New required parameter or body property | breaking | Every existing request omits it. |
| Optional parameter or property becomes required | breaking | Same reason. |
| Parameter or property type changed | breaking | Serialization on either side stops matching. |
| Response status code removed | breaking | Clients branch on status codes. |
| Response property removed | breaking | Clients read that field. |
| Parameter or request property removed | warning | Harmless if the server ignores unknown input, fatal if it rejects it. The spec does not say which, so a human decides. |
| `operationId` changed | warning | Generated client method names change even though the wire format does not. |
| New path, operation, optional parameter, or response property | non-breaking | Old clients neither send nor read it. |
| Required property becomes optional | non-breaking | The server is asking for less than before. |

## Design decisions

**Three severities, not two.** The tempting design is a boolean: breaking or not.
But removing a request field genuinely depends on server behavior that the spec does not
encode. Forcing it into "breaking" trains people to pass `--fail-on none`, and forcing it
into "safe" hides a real outage. It gets its own tier and CI ignores it by default.

**Types include their format.** `integer/int32` and `integer/int64` compare as different
types. This produces the occasional finding a purist would call cosmetic, and it also
catches the ID-overflow class of bug that costs an afternoon.

**Schemas are flattened, not compared as trees.** Each schema becomes a flat map of
`owner.email` and `tags[].label` to a type and a required flag. Diffing two flat maps is
trivially correct, and the dotted path tells the reader exactly which field moved, which
matters more than a structural diff nobody wants to read at review time.

**Recursion has a hard cap rather than cycle detection.** A schema whose child is itself
is legal, and a naive walk never returns. The walk stops at `MAX_SCHEMA_DEPTH = 8`,
which no real payload reaches. Ref cycles are additionally short-circuited during
resolution.

**External `$ref`s raise instead of resolving.** Fetching `other.yaml#/Thing` means
filesystem and network access, and a spec that silently skips the refs it cannot follow
will happily report "no breaking changes" about a document it never fully read. A loud
`SpecError` is the honest answer.

**Exit code 2 for tool failure.** A missing file or malformed YAML must not read as
"the API is fine". `0` means compatible, `1` means the API broke, `2` means the tool
never got to look.

**YAML is an optional dependency.** The core has no dependencies at all and reads JSON
specs out of the box. Installing `openapi-diff[yaml]` pulls in PyYAML for YAML input.

## Configuration

| Flag | Default | Effect |
| --- | --- | --- |
| `--fail-on {breaking,warning,none}` | `breaking` | Severity at or above which the process exits `1`. |
| `--format {text,json}` | `text` | `json` emits `{"changes": [...], "summary": {...}}` for scripting. |
| `--all` | off | Include non-breaking changes in the text report. |

Exit codes: `0` compatible, `1` a change met the `--fail-on` threshold, `2` the specs
could not be read.

## Tests

```bash
pip install -e ".[yaml,dev]"
pytest
```

Every classification rule has a focused test, plus coverage for `$ref` resolution,
`allOf` merging, recursive schemas, and the CLI exit codes. Nothing touches the network.

## License

MIT. Questions: saifullah4khan@gmail.com
