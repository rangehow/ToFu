---
name: tool-input-repair-module
description: Centralized tool-arg repair (lib/tool_input_repair.py): 6 value-repair patterns + param-KEY alias layer + structural transforms (MultiEdit→apply_diffs, AskUserQuestion→ask_human), schema-driven, audit telemetry
enabled: true
tags: [tool-dispatch, open-models, validation, repair-patterns]
created: 2026-05-12T05:31:43Z
updated: 2026-06-30T06:09:06Z
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
Secondary harnesses (paper report/Q&A) reach the SAME logic via
`parse_and_repair_tool_args(name, raw)` — so a fix here lands everywhere.

## Three repair layers (DON'T CONFUSE THEM)

0. **Structural transform** (added 2026-06-30) — `_STRUCTURAL_TRANSFORMS` +
   `_apply_structural_transform`: RIGHT (name-resolved) tool, but the model
   emitted another harness's whole-payload STRUCTURE, not just renamed keys.
   A flat key-rename can't express it. Two registered:
   `MultiEdit→apply_diffs` (push top-level `file_path` DOWN into each edit +
   rename per-edit `old_string/new_string→search/replace`) and
   `AskUserQuestion→ask_human` (lift `questions[0]` to top level, options→choice).
   Runs FIRST in `validate_then_repair`, BEFORE the key-alias + type passes
   (which mop up residual mismatch in the reshaped payload). Shape-GUARDED:
   fires only when args match the FOREIGN shape AND not the canonical one
   (native `apply_diffs`/`ask_human` calls are untouched). Logs
   `(tool_name, 'structural_transform')`. The matching tool-NAME aliases
   (`multiedit→apply_diffs`, `askuserquestion/ask_user→ask_human`,
   `webfetch→fetch_url`) live in `_TOOL_NAME_ALIASES`. NOTE our human tool is
   `ask_human`, NOT `ask_user`.
1. **Tool-NAME alias** — `resolve_tool_name` + `_TOOL_NAME_ALIASES`:
   wrong tool NAME from another harness (`read_file→read_files`,
   `edit→apply_diff`, `bash→run_command`). Lives in `tool_dispatch.parse_tool_calls`.
   Emits `tool_name_aliased` audit (attempted/resolved/kind/model) so the
   optimizer can quantify which cross-harness names each model emits.
2. **Parameter-KEY alias** (added 2026-06-30) — `_apply_param_aliases` +
   `_PARAM_ALIASES`: RIGHT tool, wrong-harness ARG KEYS. The canonical bug:
   `apply_diff` called with Claude *Edit* keys `{file_path, old_string,
   new_string}` instead of `{path, search, replace}` → schema walk drops the
   unknown keys → `path=''` → executor returns the empty-after-colon
   `File not found: ` (no path). Runs INSIDE `validate_then_repair`, BEFORE
   the type-walk (so a renamed key is also type-checked, e.g.
   `read_files paths→reads` then `bare_string_to_array`).
   Guards: rename `alias→canonical` only when canonical is a declared
   property, canonical ABSENT from args, alias NOT itself a declared property.
   Emits `param_alias` log entry; UI label in `_REPAIR_PATTERN_LABELS`.
   Tables for apply_diff(s)/write_file/insert_content(s)/read_files/list_dir/
   grep_search/find_files/run_command/fetch_url.
   **Tell-tale**: an empty `File not found: ` (or any tool error naming no
   path) = an arg key that didn't map to the schema.

## Six value-repair patterns (ORDERING IS LOAD-BEARING)

Inside `_repair_one_value`, applied per top-level key after an
`actual == expected` early-return (valid inputs are never touched):

0. `leaked_tool_call_syntax` — value is a string containing leaked
   Anthropic native text tool-call markup (`<parameter name="path">VALUE`,
   `</parameter>`, `<invoke ...>`, `<function_calls>`, incl. `antml:`
   variants). Strip via `_LEAKED_TOOL_XML_RE`, recover VALUE, then RECURSE.
1. `null_omission` — `{"k": null}` for non-required key → delete
2. `stringified_json` — `'["a"]'`/`'{"x":1}'` → `json.loads`
3. `stringified_primitive` — `"42"`/`"true"` → int/bool/float
4. `bare_string_to_array` — `"foo"` where array expected → `["foo"]`
5. `empty_placeholder_unwrap` — `{"a":"x"}` where array expected → `["x"]`

**Why pattern 2 must run before pattern 4**: otherwise `'["a","b"]'`
becomes `['["a","b"]']` — recoverable input becomes double-wrapped garbage.

## Schema source

Built once at import time by walking `lib.tools` (every dict with
`type=='function'` plus every list of such dicts). New tools added at
runtime won't be seen until restart. Tools without an indexed schema pass
through unchanged.

## Telemetry

Every repair emits `audit_log('tool_input_repaired', tool=..., model=...,
path=..., pattern=...)`. `grep tool_input_repaired logs/audit.log`.

## Critical invariants

- **Valid inputs are never touched.**
- **Only top-level properties** (nested-object repair out of scope).
- **Required keys keep their nulls** (pattern 1 skips required).

## Tests

`tests/test_tool_input_repair.py` — 40 tests (6 param-alias + 5 structural/
Claude-Code-alias added 2026-06-30), no pytest dependency (manual runner).
Run: `python3 tests/test_tool_input_repair.py`. The conda env's `pytest` is
BROKEN (vendored `_pytest` → `TypeError: required field "lineno"`).
Negative control for the structural layer: replace the
`_apply_structural_transform` call in `validate_then_repair` with
`(repaired, False)` → exactly the 3 reshape-dependent tests fail (the
`native_*_not_reshaped` no-op tests stay green); restore byte-identical.

## Empty-`model` audit fix (2026-06-30)

`task['model']` was only set at task FINALIZATION (orchestrator.py ~546), so
per-round telemetry during tool dispatch (`tool_hallucinated`,
`tool_name_aliased`) logged `model=""`. Fixed: set `task['model'] = model`
right after the LLM call in the round loop (orchestrator.py ~1581), as soon
as the model is known. Tell-tale of regression: `model:""` in audit events
that fire mid-task.
