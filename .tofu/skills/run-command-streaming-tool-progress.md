---
name: run_command streaming output via tool_progress SSE
description: run_command/code_exec stream stdout/stderr live to the frontend via tool_progress SSE events; on_chunk callback + 200ms/4KB coalescing in handlers/code_exec.py; _partialOutput survives reconnect; final meta.output stays authoritative
enabled: true
tags: [run_command, code_exec, sse, streaming, tool_progress]
created: 2026-05-07T06:15:35Z
updated: 2026-05-07T06:15:35Z
---

# run_command streaming output via `tool_progress` SSE

## What

`run_command` (and `code_exec`) stream stdout/stderr to the frontend live,
so users see terminal output as it arrives instead of waiting for the
command to finish.

## Pipeline

1. **`lib/project_mod/tools.py`** — `tool_run_command` / `_run_command_simple` /
   `_run_command_interactive` / `execute_standalone_command` / `execute_tool`
   all accept an optional `on_chunk(stream, text)` callback.
   - `_run_command_simple` was rewritten from `proc.wait()+read()` to a
     non-blocking `safe_select_pipes` loop (mirrors `_run_command_interactive`).
   - Chunks are appended to `stdout_chunks`/`stderr_chunks` as bytes AND
     forwarded to `on_chunk('stdout'|'stderr', decoded_text)`.
   - Callback exceptions are swallowed via `_safe_on_chunk` — MUST NOT abort
     the subprocess.
   - After timeout/abort kills, `_drain_after_kill()` grabs whatever was
     already buffered in the pipe before SIGTERM, so the final `meta.output`
     stays consistent with what the frontend received live.

2. **`lib/tasks_pkg/handlers/code_exec.py`** — `_make_run_command_progress_cb(task, rn, round_entry, command)`
   builds a coalescing callback:
   - **Coalesce**: 200 ms OR 4 KB, whichever first (tunable consts
     `_COALESCE_MS`, `_COALESCE_BYTES`).
   - Merges consecutive same-stream chunks into one emission.
   - Mirrors emitted text onto `round_entry['_partialOutput']` (rides along
     in task snapshots → survives reconnect).
   - Emits `append_event(task, {type:'tool_progress', roundNum, stream,
     chunk, toolName})`.
   - Exposes `.flush()` attribute for the handler to drain the tail after
     the command returns.

3. **`lib/tasks_pkg/handlers/project.py`** — wraps `execute_tool` with
   `on_chunk` + `try/finally` flush, for the project-mode `run_command`
   path (the primary one LLMs hit).

4. **Frontend** (`static/js/ui.js`, `branch.js`, `paper-reader.js`) — new
   `tool_progress` case: appends `ev.chunk` to `round._partialOutput`.
   - `_renderUnifiedToolLine`'s `isSearching && run_command` branch renders
     the live partial output inside the running terminal block.
   - Live view cap: last 20 000 chars (DOM perf).
   - Post-render, scroll all `.ptool-cmd-output-live` to the bottom so the
     newest output is visible.
   - Once `tool_result` lands, status flips to `done`, the authoritative
     `meta.output` replaces the partial buffer, collapse toggle reappears.

5. **CSS** (`static/styles.css`) — `.ptool-cmd-output-live` class: always
   visible, `max-height: 220px`, `overflow-y: auto`.

## Key properties

- **Safe on error**: `on_chunk` exceptions never abort the subprocess.
- **Survives reconnect**: `_partialOutput` lives on `round_entry` in
  `task['toolRounds']`, which is checkpointed by `manager.checkpoint_task_partial`.
- **Final output is authoritative**: `tool_result` meta.output comes from
  the same `stdout_chunks` buffer the live view was reading from — no
  divergence.
- **SSE proxy padding**: `/api/chat/stream` already has the canonical 4KB
  padding (see sse-proxy-buffer-padding-required memory); tool_progress
  events do not need additional padding.

## Hyperparameters (§10.1 — approval required to change)

- `_COALESCE_MS = 200` — max wall-clock between SSE flushes
- `_COALESCE_BYTES = 4096` — force flush when buffer exceeds
- Frontend live cap: 20 000 chars
- Live box CSS max-height: 220 px

## Gotchas hit during implementation

- `finally` closed proc.stdout/stderr BEFORE the tail drain ran →
  moved drain INSIDE the try-block, right after `_kill_process_tree`.
- `_run_command_simple` was `text=True` (auto-decode); switched to
  `text=False` for non-blocking compatibility, manual
  `decode('utf-8', errors='replace')` on each chunk.
- Throttling via `threading.Timer` requires a deferred flush path —
  otherwise a small final chunk sits forever waiting for a follow-up.
