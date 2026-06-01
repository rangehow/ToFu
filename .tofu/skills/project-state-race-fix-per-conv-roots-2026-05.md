---
name: project-state-race-fix-per-conv-roots-2026-05
description: FIXED: lib/project_mod global _roots/_state was shared across all server tasks — concurrent set_project() clobbered each other, causing 'Unknown workspace root' errors. Fix: per-conv root registry with LRU cap
enabled: true
tags: [bug-fix, concurrency, critical, project_mod, multi-root]
created: 2026-05-05T06:31:28Z
updated: 2026-05-05T06:31:28Z
---

# Project-State Race — Per-Conv Root Registry Fix (2026-05-05)

## The bug
`lib/project_mod/config.py` kept MODULE-LEVEL mutable state:
- `_state` — current project path dict
- `_roots` — dict of workspace roots `{name → state}`

Both shared across every concurrent task on the server.

`lib/project_mod/scanner.py::set_project()` does `_roots.clear()` when
the primary changes. Under concurrency (e.g. SWE-bench 9 parallel tasks
on 9 different workspaces), this wiped other tasks' registrations:

1. Task A calls `ensure_project_state(/ws/inst-A)` → `_state['path']=/ws/inst-A`,
   `_roots={'inst-A': state_A}`. Its system prompt is built; it includes
   "inst-A" as a known root name because basename matches primary.
2. Task B calls `ensure_project_state(/ws/inst-B)` 2s later →
   `_roots.clear()`, `_roots={'inst-B': state_B}`. Task A's root is GONE.
3. Task A's model issues a tool call with path `inst-A:django/models.py`
   (natural — it saw its own workspace basename in the system prompt).
4. `resolve_namespaced_path('inst-A:...')` → `_roots` only has `inst-B`
   → raises `ValueError: Unknown workspace root: inst-A`.
5. `_resolve_base` converts to model-visible error. Tool call refused.
6. Model retries with wrong path, or gives up → task fails.

## Observable impact
During the 2026-05-04 SWE-bench rerun: **76 "Unknown workspace root"
events, 42 distinct refused tool calls**. Top offenders:
- `django__django-13925__tofu-opus`: 33 refused (task eventually resolved)
- `django__django-12406__tofu-glm`:   26 refused (FAILED)
- `django__django-11790__tofu-glm`:   26 refused (FAILED)

Also clobbered **user's chatui session** ("chatui:lib/..." refused because
primary was a django workspace at that moment — cross-task leakage).

## Fix architecture

### Added to `lib/project_mod/config.py`:

1. **Per-conv root registry** — `_conv_roots: OrderedDict[conv_id, dict[name, state]]`.
   LRU-capped at `MAX_CONV_ROOTS=512`. Also `_conv_primary[conv_id] → abs_path`.

2. **`set_conv_roots(conv_id, primary, extras=[])`** — registers the
   conv's roots in its own scope without touching the global `_roots`.
   Idempotent; re-registration refreshes LRU recency.

3. **`clear_conv_state(conv_id)`** — drop a conv's entry (optional; LRU
   handles it automatically if caller forgets).

4. **`resolve_namespaced_path(rel_path, conv_id=None)`** — new optional
   `conv_id` param. Resolution order:
   - If `conv_id` has its own registry: STRICT isolation — only look
     there, do NOT fall through to the global `_roots` (otherwise a
     concurrent task's roots could leak and cause silent clobber of
     write tools).
   - If no conv registry: use global `_roots` (legacy / single-user UI).

### Added to `lib/project_mod/tools.py`:

5. **`_resolve_base(..., conv_id=None)`** — threads `conv_id` into
   `resolve_namespaced_path`. Also a **self-heal fallback**: if
   `conv_id`-scoped lookup fails AND the global is gone too, check
   whether `os.path.basename(base_path)` matches the requested root
   name — if so, resolve to `base_path`. Safe because name and path
   agree by construction; covers edge cases where conv state was evicted.

6. **`execute_tool` binds `_rb/_rb_safe` closures** capturing `conv_id`
   and forwards to `_resolve_base`. All 13+ inner call sites updated.

### Updated callers:

7. `lib/project_mod/scanner.py::ensure_project_state(path, extra_paths, conv_id=None)`
   — when `conv_id` given, call `set_conv_roots` up-front (before
   touching the global registry) so the conv's tool calls resolve
   correctly even if another task's `set_project` arrives first.

8. `lib/tasks_pkg/orchestrator.py::run_task` — pass `task['convId']` to
   `ensure_project_state`.

9. `lib/tasks_pkg/streaming_tool_executor.py::_execute_one` — pass
   `conv_id` to `execute_tool` so pre-execution hits the right registry.

## Tests
- `debug/test_project_state_race.py` — reproduces bug pre-fix, validates
  post-fix for: basename self-heal, per-conv isolation, 8-worker stress.
- End-to-end scenario using real SWE-bench workspace names: 30/30 tool
  calls resolved correctly (vs many refused pre-fix).

## Recovery for the 2026-05-04 run data
Any run whose sole failure was "Unknown workspace root" or that exceeded
turn budget because too many refused tool calls degraded the task,
should be re-run. The recovery script pattern is:

    rows = find details where raw_output contains "Unknown workspace root"
    rerun those specific (instance, tool) pairs with --resume

## Production-side watchpoint
Any NEW code that calls `resolve_namespaced_path()` directly MUST pass
`conv_id` when called in a task context. Passing `None` is safe but
falls back to the shared global `_roots` — that's the legacy path we're
trying to retire. Places currently still on the legacy path:
- `lib/feishu/pipeline.py::execute_tool(...)` — no conv_id. Acceptable
  for single-user Feishu bot (one convo at a time), but a future
  concurrency upgrade there would reintroduce the bug.

## Why not just remove the global `_roots.clear()` in set_project?
That would fix the clobber BUT accumulate stale roots from every
project ever used by the UI, eventually causing stale-path resolves
(a write in the UI would route to an old directory). The per-conv
approach preserves the UI semantics (one active project at a time)
while isolating tasks.

## Files changed
- `lib/project_mod/config.py` — per-conv registry + updated resolver
- `lib/project_mod/tools.py` — _resolve_base/_resolve_base_safe accept
  conv_id; execute_tool threads it through via closures
- `lib/project_mod/scanner.py` — ensure_project_state accepts conv_id
- `lib/project_mod/__init__.py` — export new APIs
- `lib/tasks_pkg/orchestrator.py` — pass conv_id
- `lib/tasks_pkg/streaming_tool_executor.py` — pass conv_id
- `debug/test_project_state_race.py` — test harness

