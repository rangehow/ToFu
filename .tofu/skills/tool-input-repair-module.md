---
name: tool-input-repair-module
description: Centralized tool-arg repair (lib/tool_input_repair.py): 6 patterns incl. leaked_tool_call_syntax, load-bearing ordering, schema-driven, audit telemetry
enabled: true
tags: [tool-dispatch, open-models, validation, repair-patterns]
created: 2026-05-12T05:31:43Z
updated: 2026-06-01T06:05:23Z
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

## Six repair patterns (ORDERING IS LOAD-BEARING)

Inside `_repair_one_value`, applied per top-level key after an
`actual == expected` early-return (valid inputs are never touched):

0. `leaked_tool_call_syntax` — value is a string containing leaked
   Anthropic native text tool-call markup (`<parameter name="path">VALUE`,
   `</parameter>`, `<invoke ...>`, `<function_calls>`, incl. `antml:`
   variants). Strip the markup via `_LEAKED_TOOL_XML_RE`, recover VALUE,
   then RECURSE on the cleaned value so an array slot still gets wrapped.
   Labelled `leaked_tool_call_syntax` regardless of the inner pattern.
1. `null_omission` — `{"k": null}` for non-required key → delete
2. `stringified_json` — `'["a"]'`/`'{"x":1}'` → `json.loads`
3. `stringified_primitive` — `"42"`/`"true"` → int/bool/float
4. `bare_string_to_array` — `"foo"` where array expected → `["foo"]`
5. `empty_placeholder_unwrap` — `{"a":"x"}` where array expected → `["x"]`

**Why pattern 2 must run before pattern 4**: otherwise
`'["a","b"]'` becomes `['["a","b"]']` — recoverable input becomes
double-wrapped garbage. Tests in `tests/test_tool_input_repair.py`
specifically guard this ordering.

**Why pattern 0 is safe**: it only fires on the shape-mismatch path
(after `actual == expected` returned), and only when
`_LEAKED_TOOL_XML_RE` actually matches — so a well-formed `write_file`
`content` string (type already matches `string`) is never stripped, and
a plain `a<b.py` path (no markup) falls through to bare_string_to_array.

## Real bug this fixed (conv mpus9bcfbrkbvq, 2026-06-01)

Claude **Opus 4.8** leaked `{"reads": "\n<parameter name=\"path\">CLAUDE.md"}`
into all 8 `read_files` calls in one conversation → every one returned
`File not found: <parameter name="path">CLAUDE.md`. Sibling tools
(list_dir/grep_search/find_files/run_command) all succeeded because
their args were flat scalars. This is a MODEL glitch specific to the
nested-array schema of `read_files`; the harness now self-heals it.
DB verified via `tofu` PG db on port 15439 (NOT `chatui` — renamed).

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

`tests/test_tool_input_repair.py` — 15 tests, no pytest dependency
(uses a manual runner). Run: `python tests/test_tool_input_repair.py`.
Includes 3 leaked-markup regression tests using conv mpus9bcfbrkbvq shapes.
