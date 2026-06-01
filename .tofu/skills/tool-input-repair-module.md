---
name: tool-input-repair-module
description: Centralized tool-arg repair (lib/tool_input_repair.py): 5 patterns + load-bearing ordering, schema-driven, audit_log telemetry
enabled: true
tags: [tool-dispatch, open-models, validation, repair-patterns]
created: 2026-05-12T05:31:43Z
updated: 2026-05-12T05:31:43Z
---

# Tool-Input Repair Module

`lib/tool_input_repair.py` — centralized validate-then-repair for tool
arguments emitted by open models. Inspired by Awais's open-model-harness
write-up; folds in the stringified-int trap from
`llm-string-coercion-traps`.

## Wiring

Called once per parsed tool call in `lib/tasks_pkg/tool_dispatch.py`
inside `parse_tool_calls`, **right after** `_repair_json` succeeds and
**before** the `parsed_tcs.append`. Look for:
```
# ── Schema-driven shape repair (Awais open-model-harness patterns) ──
fn_args, _repair_log = validate_then_repair(fn_name, fn_args, model=...)
```

## Five repair patterns (ORDERING IS LOAD-BEARING)

1. `null_omission` — `{"k": null}` for non-required key → delete
2. `stringified_json` — `'["a"]'`/`'{"x":1}'` → `json.loads`
3. `stringified_primitive` — `"42"`/`"true"` → int/bool/float
4. `bare_string_to_array` — `"foo"` where array expected → `["foo"]`
5. `empty_placeholder_unwrap` — `{"a":"x"}` where array expected → `["x"]`

**Why pattern 2 must run before pattern 4**: otherwise
`'["a","b"]'` becomes `['["a","b"]']` — recoverable input becomes
double-wrapped garbage. Tests in `tests/test_tool_input_repair.py`
specifically guard this ordering.

## Schema source

Built once at import time by walking `lib.tools` (every dict with
`type=='function'` plus every list of such dicts). New tools added at
runtime won't be seen until restart — same model as
`PROJECT_TOOL_NAMES`. Tools without an indexed schema pass through
unchanged (no-op for unknown tools).

## Telemetry

Every repair emits `audit_log('tool_input_repaired', tool=..., model=...,
path=..., pattern=...)` to `logs/audit.log`. A spike for one
(model, tool) pair after a model swap is the regression signal. Use
`grep tool_input_repaired logs/audit.log` to inspect.

## Critical invariants

- **Valid inputs are never touched** — `_repair_one_value` returns
  `changed=False` when actual type already matches expected.
- **Only top-level properties** — nested-object repair is intentionally
  out of scope (would risk over-repair of legitimate `write_file` content).
- **Required keys keep their nulls** — pattern 1 only fires when
  `key not in required`; this leaves the broken arg visible so the
  model self-corrects next turn.

## Tests

`tests/test_tool_input_repair.py` — 12 tests, no pytest dependency
(uses a manual runner). Run: `python tests/test_tool_input_repair.py`.

