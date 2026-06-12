---
name: apply-diff-read-before-edit-gate
description: Read-before-edit gate refuses apply_diff/insert_content for unread files; batch tools gated PER-PATH (skip unread edits, run rest); bypass TOFU_APPLY_DIFF_READ_GATE=0
enabled: true
tags: [tools, apply_diff, guard]
created: 2026-05-20T07:24:07Z
updated: 2026-06-03T11:29:55Z
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
`lib/tasks_pkg/handlers/_read_gate.py`:
- `check_read_before_edit(task, fn_name, fn_args, project_path)` returns an
  error string when ALL/any target file unread (used for single-edit
  `apply_diff`/`insert_content` → wholesale refusal).
- `partition_batch_edits(task, fn_name, fn_args, project_path)` →
  `(skip_indices, unread_raw_paths)` for batch tools (`apply_diffs`/
  `insert_contents`).
- `_format_refusal(fn_name, raw_paths)` builds the model-facing message.

Called from `lib/tasks_pkg/handlers/project.py::_handle_project_tool`
BEFORE `execute_tool`:
- Single-edit tools: full refusal (unchanged).
- Batch tools: PER-PATH gating. Unread-target edits dropped from
  `fn_args['edits']`; remaining edits run. If ALL edits target unread
  files → full refusal. On partial, a `_gate_skip_note` is PREPENDED to
  the returned tool_content (AFTER meta build so the "Applied N/M edits"
  header parses cleanly) and `meta['badge']='partial: read first'`.

## Recognised "fresh enough" sources (any one is sufficient)
1. A `read_files` round with `status=='done'` in `task['toolRounds']`
   (current turn). Sibling reads still `'searching'` do NOT count.
2. A `read_files` / `write_file` / `apply_diff` / `insert_content` paired
   with a non-error tool result in `task['messages']` (prior turns).
   NOTE: Layer-2 compaction (`_layer2.py::_format_messages_for_summary`)
   DROPS all role:tool and tool-call-only assistant messages, so after a
   compaction the historical read is gone from `task['messages']` (only
   the path survives in the "Recently Accessed Files" summary footer) →
   gate correctly refuses, model must re-read (content is gone too).
3. Target file does not exist on disk → gate skipped.

## Q3 frontend bug (fixed)
`lib/tools/meta.py::_build_apply_diff`/`_build_insert_content` built
per-edit `editSummaries` defaulting `status='ok'`. On a refusal/error
string (no `[N] OK/FAIL` lines, no "Applied X/Y" header) every child
rendered a green check. Fix: `default_status = 'ok' if m else 'fail'`
(m = the "Applied/Inserted X/Y" header regex match) AND any edit with no
corresponding `[i]` result line → 'fail'.

## Env override
`TOFU_APPLY_DIFF_READ_GATE=0` disables the gate. Default is on.

## Tests
`tests/test_read_before_edit_gate.py` — 20 cases. Includes
`partition_batch_edits` (skip-only-unread, all-read-empty, all-unread,
dedup paths, env disable, nonexistent-not-skipped) and meta builder
(refusal→all-fail, partial→parsed statuses) coverage.

## Path resolution
`_resolve_abs` canonicalises via `lib.project_mod.tools._resolve_base` +
`os.path.abspath`. Handles `rootname:rel` prefixes and absolute paths;
comparison on absolute paths. A path-form mismatch between the historical
read and the current edit (e.g. `chatui:lib/x.py` vs `lib/x.py`) can still
cause a false negative if `_resolve_base` resolves them differently.
