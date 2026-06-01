---
name: hope-mcp-auto-login-one-field
description: HOPE_USERNAME is the one required MCP setting; preflight auto-runs hope login on session expiry
enabled: true
tags: [hope-mcp, mcp, auth, ux]
created: 2026-04-23T04:41:22Z
updated: 2026-04-23T04:41:22Z
---

# Auto-login: one setting (HOPE_USERNAME), login becomes invisible

## UX contract
The MCP catalog entry for Hope exposes exactly ONE required field:
`HOPE_USERNAME`. Everything else (HOPE_BIN, HOPE_MCP_TIMEOUT,
HOPE_MCP_MAX_PARALLEL, HOPE_MCP_DRY_RUN_DEFAULT) is optional with
sensible defaults, hidden behind a 高级设置 toggle in the install
modal.

## Behavior
When any non-login tool (list_jobs, stop_job, get_status, …) triggers
the preflight gate and finds no valid session:

1. If `HOPE_USERNAME` is set in `CONFIG.username`, preflight itself
   calls `login(username=CONFIG.username)` ONE TIME.
2. On approval → prime cache, let the original tool call proceed
   normally (no short-circuit).
3. On denied / timeout / bad user → return a tool result with
   `auto_login_attempted: true`, `auto_login_result: {...}`,
   `error_hint: "..."`. NEVER loops.
4. If `HOPE_USERNAME` is unset → fall back to the classic
   "short-circuit with login_required=true and ask the LLM to call
   hope_login" flow.

## Code
- `hope-mcp/src/hope_mcp/config.py`: `CONFIG.username` reads `HOPE_USERNAME`.
- `hope-mcp/src/hope_mcp/tools/preflight.py::ensure_logged_in`:
  new "auto-login branch" between probe-failed and short-circuit.
- `chatui/lib/mcp/registry.py`: Hope entry's env_specs now lead with
  `{key: HOPE_USERNAME, required: True}`.
- `chatui/static/js/settings.js::_mcpOpenInstallModal`: renders
  required specs top-level, optional specs under a `<details>` toggle.

## Frontend detail
No new SSE event needed — the tool result JSON already flows to the
tool-panel UI. When `ok=false && auto_login_attempted=true`, the
panel shows the full JSON including the `hint` so users see exactly
why (denied / approval timed out / wrong username).

## Tests
`hope-mcp/tests/test_auto_login.py` — 5 tests:
1. happy path: list_jobs with no session + HOPE_USERNAME → shim log
   shows `ls-probe → login alice → ls-real`
2. cache-fresh after auto-login: 3 consecutive list_jobs → exactly 1
   auto-login total
3. denied: tool returns `auto_login_attempted=true, denied=true`;
   exactly 1 login attempt (no retry loop)
4. no HOPE_USERNAME: falls back to classic manual flow
5. already logged in: no auto-login attempt

## Test isolation gotcha
`jobs.py` has `from .preflight import ensure_logged_in, mark_logged_out`
at module load, so reload order is critical:
`config → cli → preflight → login → jobs`. Otherwise jobs ends up
bound to the OLD preflight module whose `_state` dict doesn't track
the new one.

