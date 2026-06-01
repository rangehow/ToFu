---
name: mcp-bridge-arg-coercion
description: lib/mcp/client.py 在派发前按 JSON Schema 就地转换 LLM 参数类型（"1"→1, "true"→True, scalar→array, nullable, anyOf）
enabled: true
tags: [mcp, tool-args, coercion, schema]
created: 2026-05-08T01:12:26Z
updated: 2026-05-08T01:12:26Z
---

# MCP Bridge: Argument Type Coercion Against Tool Schema

## Where

`lib/mcp/client.py` — `_coerce_one(value, schema)` and
`_coerce_args_to_schema(args, schema)`. Invoked inside
`MCPBridge.call_tool()` RIGHT BEFORE `session.call_tool(...)` is dispatched.

## Why this exists

Weaker / sloppier LLMs routinely emit tool args with the wrong JSON
type — most often `{"expected_step_version": "1"}` instead of integer,
or `{"confirm": "true"}` instead of boolean. The MCP server's
jsonschema validator then rejects the call with
`'1' is not of type 'integer'`, the LLM sees the error and retries —
sometimes with the same bug, wasting round trips.

The bridge sits between the LLM and the MCP server and has the tool's
full input schema (cached at discovery time in `_tool_index`). Perfect
place to coerce once, centrally, benefiting every MCP server.

## What it handles

| LLM emits | Schema says | Result |
|---|---|---|
| `"1"` | integer | `1` |
| `"-5"` | integer | `-5` |
| `"1.5"` | number | `1.5` |
| `"true"` / `"True"` / `"TRUE"` / `"1"` / `"yes"` / `"y"` | boolean | `True` |
| `"false"` / `"False"` / `"0"` / `"no"` / `"n"` | boolean | `False` |
| `"lisi"` | `array<string>` | `["lisi"]` (scalar wrapped) |
| `["1", "2"]` | `array<integer>` | `[1, 2]` |
| `"4"` | `["integer","null"]` | `4` (nullable resolved to first non-null) |
| anyOf / oneOf | n/a | tries each branch, returns first that coerces |
| object with `properties` | object | recurses per field |
| `"abc"` | integer | `"abc"` (no coercion — downstream validator will complain with a clear message) |

## Crucial design decisions

1. **Never coerce Python `bool` to `int`.** `isinstance(True, int) is True`
   and `int(True) == 1`, but LLM emitting `True` for an integer field is a
   semantic mistake, not a format one. The `integer` branch is gated by
   `isinstance(value, str)`, so bools pass through unchanged and the
   server's validator surfaces the real problem.

2. **Unparseable values pass through unchanged.** Do NOT substitute a
   default here — the bridge is infra, it doesn't know what's safe for the
   downstream tool. Let the MCP server's jsonschema give the precise error.

3. **Type `list` in schema.** JSON Schema allows
   `"type": ["integer","null"]`. We resolve to the first non-null entry
   and coerce against that.

4. **Defense-in-depth is still needed downstream.** The scheduler's
   `_coerce_int_arg` (see `timer-tool-args-str-coercion` memory) still
   provides a default when the field is absent / unparseable; the MCP
   bridge only fixes format mismatches for declared schema types.

## Gotcha: reload required

The bridge is a module-level singleton loaded at Flask startup. If you
change `_coerce_one` / `_coerce_args_to_schema`, **restart Flask** —
existing MCP clients call into an already-imported module. In-memory
patching doesn't help.

## Tests

```python
python -c "
from lib.mcp.client import _coerce_args_to_schema
s = {'type':'object','properties':{'x':{'type':'integer'}, 'ok':{'type':'boolean'}}}
assert _coerce_args_to_schema({'x':'1','ok':'True'}, s) == {'x':1,'ok':True}
"
```

No dedicated test file yet; add to `tests/test_mcp_client.py` when one
is created.

