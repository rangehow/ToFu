---
name: apply-diff-read-before-edit-gate
description: Tool-side gate refuses apply_diff / insert_content for files not read earlier in the conversation; bypasses with TOFU_APPLY_DIFF_READ_GATE=0
enabled: true
tags: [tools, apply_diff, guard]
created: 2026-05-20T07:24:07Z
updated: 2026-05-20T07:24:07Z
---

# apply_diff / insert_content Read-Before-Edit Gate

## Why
The model's parallel tool-call habit produced this dominant failure: in
one assistant turn it would issue `apply_diff(file)` AND `read_files(file)`
together, the apply_diff built from guessed content, the read_files
intended to "verify" — but tool calls in one turn are independent, so the
read can't help and the patch fails. A soft prompt rule kept getting
ignored.

## What changed
`lib/tasks_pkg/handlers/_read_gate.py` — `check_read_before_edit(task,
fn_name, fn_args, project_path)` returns an error string when the target
file has not been read/written earlier in the conversation. Called from
`lib/tasks_pkg/handlers/project.py::_handle_project_tool` BEFORE
`execute_tool` for `apply_diff` and `insert_content`.

## Recognised "fresh enough" sources (any one is sufficient)
1. A `read_files` round with `status=='done'` in `task['toolRounds']`
   (current turn). Sibling reads still `'searching'` do NOT count —
   that's the original failure mode being prevented. Note that write
   tools dispatch BEFORE the parallel read pool runs (see
   `tool_dispatch.py` "Write-tool serial phase"), so a same-turn sibling
   read is always still `'searching'` when the gate fires.
2. A `read_files` / `write_file` / `apply_diff` / `insert_content` paired
   with a non-error tool result in `task['messages']` (prior turns).
3. Target file does not exist on disk → gate skipped (downstream will
   return the cleaner "File not found" error and the model can decide
   to write_file).

## Env override
`TOFU_APPLY_DIFF_READ_GATE=0` disables the gate. Default is on.

## Tool descriptions
`lib/tools/project.py` — both `apply_diff` and `insert_content`
descriptions explicitly tell the model the gate exists, including the
critical sentence "A sibling read_files issued in the SAME parallel
batch does NOT satisfy the gate."

## Tests
`tests/test_read_before_edit_gate.py` — 11 cases covering:
- block unread, allow after done read, block sibling-read-still-searching,
  allow after prior turn read, reject failed reads, skip nonexistent,
  batch-with-one-unread, write_file satisfies, env disable, non-gated
  tools pass through, insert_content also gated.

## Path resolution
The gate canonicalises every path via `lib.project_mod.tools._resolve_base`
+ `os.path.abspath`. This handles `rootname:rel` prefixes and absolute
paths; comparison is on absolute paths so the gate works in multi-root
workspaces.

