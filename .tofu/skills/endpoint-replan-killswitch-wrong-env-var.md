---
name: endpoint-replan-killswitch-wrong-env-var
description: Bug: Endpoint Critic replan kill-switch read CHATUI_ENDPOINT_REPLAN only — TOFU_ENDPOINT_REPLAN documented in CLAUDE.md was a silent no-op
enabled: true
tags: [endpoint, env-vars, bug, tofu-rebrand]
created: 2026-05-15T10:35:05Z
updated: 2026-05-15T10:35:05Z
---

# Endpoint replan kill-switch read the legacy env var only (fixed 2026-05-15)

## Symptom
Setting `TOFU_ENDPOINT_REPLAN=0` (the canonical name documented in
CLAUDE.md §9 and used by `debug/test_endpoint_verdict.py`) did NOT
disable the endpoint-mode Critic's `[VERDICT: CONTINUE_PLANNER]`
downgrade.  Operators following the docs to hot-rollback the replan
redesign saw no behaviour change.

## Root cause
Both kill-switch helpers read the legacy var directly:
- `lib/tasks_pkg/endpoint_review.py:_replan_enabled()` — actually used by `_parse_verdict`
- `lib/tasks_pkg/endpoint.py:_replan_enabled()` — orphan duplicate, unused

```python
return os.environ.get('CHATUI_ENDPOINT_REPLAN', '1').strip() != '0'
```

The reverse-direction promotion only goes CHATUI_*→TOFU_* in
`lib/env_compat.py:promote_legacy_env()`.  Setting the canonical
`TOFU_ENDPOINT_REPLAN` doesn't propagate back to `CHATUI_*`, so the
direct `os.environ.get('CHATUI_ENDPOINT_REPLAN', ...)` never saw it.

The unit test in `debug/test_endpoint_verdict.py` sets
`TOFU_ENDPOINT_REPLAN` so it would have failed (couldn't run here due
to missing flask in the test env).

## Fix
Use `getenv_compat('TOFU_ENDPOINT_REPLAN', 'CHATUI_ENDPOINT_REPLAN', default='1')`
in both `endpoint_review.py:_replan_enabled` and
`endpoint.py:_replan_enabled` so canonical TOFU_* takes precedence,
CHATUI_* still works for legacy deployments, and a one-time
deprecation warning is logged when the legacy name is used.

## Audit pattern for similar bugs
Look for any direct `os.environ.get('CHATUI_...')` outside `lib/env_compat.py`:
```bash
grep_search "os\.environ\.get\(['\"]CHATUI_" lib/
```
Each hit is a candidate — the canonical TOFU_* form set per CLAUDE.md
will silently no-op.  Remaining benign instances at time of fix:
- `lib/file_history/api.py:44,58` — uses inline `or` chain
  (`os.environ.get('TOFU_FILE_HISTORY') or os.environ.get('CHATUI_FILE_HISTORY')`)
  which works correctly.
- `lib/token_counter/hf_counter.py:58` — `CHATUI_TOKEN_COUNTER_HF_AUTOFETCH`
  has no canonical TOFU_* alias yet; minor, off-by-default feature gate.

