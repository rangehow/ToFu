---
name: cleanup-internal-shims-2026-05
description: Internal-only cleanup pass — _KEEP_RECENT_PAIRS / _find_pair_boundary / _cleanup_old_tasks shims removed
enabled: true
tags: [cleanup, compaction, tasks, convention]
created: 2026-05-31T15:36:52Z
updated: 2026-05-31T15:36:52Z
---


# Internal-Only Shim Removal — 2026-05-31

Cleanup pass after the audit-logging effort. All deletions confirmed
internal-only (zero public-API or cross-host coordination implications).

## Removed

1. **`_KEEP_RECENT_PAIRS`** (constant, was 8) and **`_find_pair_boundary`**
   (legacy wrapper) in `lib/tasks_pkg/compaction/`. Superseded by
   `_find_turn_boundary` + `_PRESERVE_BUDGET_RATIO` + `_MAX_PRESERVE_TURNS`
   (turn-based preservation, 2026-04-26 redesign). Removed from:
   - `_constants.py` (the constant)
   - `_layer2.py` (the wrapper + the import)
   - `compaction/__init__.py` facade re-exports
   - `tests/test_compaction_invariants.py` (`_PUBLIC_CONSTANTS`,
     `_REQUIRED_FACADE_NAMES`)
   - `tests/test_compaction_improvements.py` — the
     `TestPairBoundaryBackwardCompat` class was replaced with a
     `TestTurnBoundaryRegression` class that exercises the same
     conv=modearkif6k9tr regression directly via `_find_turn_boundary`
     using `_MAX_PRESERVE_TURNS` instead of `_KEEP_RECENT_PAIRS`.
     The two `Fix #1 thread-safety` tests (`test_no_global_mutation`,
     `test_force_compact_accepts_keep_recent_pairs`) stay intact.

2. **`_cleanup_old_tasks` shim** in `routes/trading_simulator.py`.
   Replaced both call sites with the inline pattern:
   ```python
   _stale = _runtime.cleanup_stale()
   if _stale:
       logger.info('[SimRoute] Cleaned up %d expired tasks', _stale)
   ```
   `tests/test_trading_simulator_migration.py::test_cleanup_runs_without_error`
   updated to call `_runtime.cleanup_stale()` directly.

## Renames (clarity)

- `lib/project_mod/tools.py::_format_device_range`
  → `_format_cuda_device_range`. Doc references the un-prefixed
  `lib/log_clean.py::_format_device_range` so future readers see both.
- `lib/token_counter/anthropic_api.py`:
  `_resolve_url` → `_resolve_anthropic_count_url`,
  `_build_body` → `_build_anthropic_count_body`.
- `lib/token_counter/gemini_api.py`:
  `_resolve_url` → `_resolve_gemini_count_url`,
  `_build_body` → `_build_gemini_count_body`.
  Both modules are leaf-private (no external `from lib.token_counter.X import _resolve_url` callers).

## Docstrings added

Top-hit handlers that were undocumented:
`routes/chat.py::chat_active`, `chat_start`, `chat_abort`, `chat_poll`;
`routes/conversations.py::list_convs`, `save_conv`;
`routes/api_v1/optimizer.py::list_proposals`, `get_proposal`,
`approve_proposal`, `reject_proposal`, `revert_proposal`, `run_now`.

## NOT touched (deliberately)

- `routes/legacy_redirects.py` — per `legacy-api-308-shim-pattern`,
  removal needs traffic-monitoring evidence (no observed 404 traffic
  drop) before shim deletion is safe.
- `lib/tasks_pkg/orchestrator.py:597` and `manager.py:510` "shim"
  comments — they describe non-obvious WHY (Tier-3 redesign, hasattr
  removal) so readers don't reintroduce them. Per CLAUDE.md
  "Don't remove existing comments unless removing the code they describe."
- `capabilities_legacy`, `TUNNEL_TOKEN`, preset-name aliases, `proxy.no_proxy`,
  the 24 `CHATUI_*` env-var aliases, `.chatui_heartbeat` lock files —
  all gated on a deprecation window the user is lining up.

## Verification

- `pytest tests/test_compaction_invariants.py` — 69/69 pass
- `pytest tests/test_compaction_improvements.py` — 101/101 pass
- `python3 tests/test_trading_simulator_migration.py` — 12/12 pass
- `pytest tests/test_log_clean.py` — 25/25 pass (untouched module)
- `python3 debug/audit_logging.py` — Tier A=0, Tier B=0, Tier C=28 (legitimate)

