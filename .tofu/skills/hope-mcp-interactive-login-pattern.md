---
name: hope-mcp-login-mobile-push-approval
description: hope login is mobile-push approval (no stdin prompt); drive it from MCP directly
enabled: true
tags: [hope-mcp, mcp, auth, subprocess, stdin-safety, pattern, workdir, env-injection]
created: 2026-04-22T11:13:07Z
updated: 2026-05-01T05:57:08Z
---

# Pattern: wrap a CLI "login" that blocks on out-of-band approval

## Key fact 1 — `hope login <username>` is mobile-push, not stdin
`hope login <username>` (argv form) does **NOT** prompt on the terminal.
It sends a push notification to the user's mobile office app; the CLI
blocks until the user taps Approve / Deny (or the server-side approval
window times out), then exits — rc=0 on approve, non-zero + rejection
message on deny.

The argv form has no stdin interaction, so it CAN be driven directly
from an MCP stdio subprocess.

## Key fact 2 — OTHER hope subcommands DO prompt interactively on a creds-less machine
`hope ls` / `hope status` / etc. on a machine with NO cached credentials
(`~/.hope/` missing or empty) do **NOT** fail fast with a "please login"
error. Instead hope prints a banner and drops into an **interactive
username prompt on the terminal**:

```
HOPE登录认证(帮助文档:https://km.sankuai.com/page/69700154)
用户名(misid): ▊
```

Implications for MCP:
- `stdin=DEVNULL` is NOT enough — hope doesn't treat EOF as "give up".
- Our `check_login` probe (`hope ls --num=1 --json`) hangs until the
  30 s timeout trips → surfaces as misleading "cluster unreachable"
  error → user can't tell auth from network issue.
- **Fix**: `check_login` MUST short-circuit when creds-dir is missing
  OR empty, BEFORE spawning any hope subprocess.
- **Defense in depth**: also pass `start_new_session=True` to
  `asyncio.create_subprocess_exec` so any child that tries
  `open("/dev/tty")` gets ENXIO instead of blocking on user input.

## Key fact 3 — `/workdir` may exist but be a BROKEN mount (added 2026-05-01)
Read `hope/tools/settings.py` — its `HOME_DIR` resolution is:

1. `$HOPE_HOME_DIR` if set
2. `/workdir/<user>/` if `os.path.exists("/workdir")` — "k8s branch"
3. `/opt/meituan/` if `getpass.getuser() == "sankuai"`
4. `$HOME` otherwise

Branch 2 uses only `os.path.exists` as the probe — no listability check.
On some container images `/workdir` exists as a ghost mount:
- `ls /workdir` **segfaults** (SIGSEGV rc=139) or returns unrelated
  entries like `codelab_deploy`, `cloud-ide` (VSCode remote mounts).
- `/workdir/<user>/` **doesn't exist**.
- Writes into `/workdir/<user>/.hope/.token` silently succeed into tmpfs
  that disappears.

Symptom: `hope login` succeeds (rc=0, prints "登录成功") but the VERY NEXT
`hope ls` / `hope status` / etc. fails with "not logged in". hope-mcp
loops forever calling `hope login` → "approved" → retry → "not logged in".

**Fix in `hope-mcp/src/hope_mcp/hope_home.py`** (new module, 2026-05-01):
- `resolve_hope_home_dir()` replicates hope's decision tree but probes
  `/workdir` for actual usability: `os.listdir("/workdir")` success AND
  `<user>` is in the listing AND `os.listdir("/workdir/<user>")` succeeds.
- On branch-2 rejection + `/workdir` exists, sets `apply_env_override=True`.
- `build_hope_env_override()` returns `{"HOPE_HOME_DIR": $HOME}` in that
  case; `cli.py::_build_hope_env()` injects it into EVERY hope subprocess.
  hope's own settings.py then picks priority-1 `$HOPE_HOME_DIR` → both
  sides agree on `$HOME/.hope/.token`. **Hardcoded — no user config.**
- `login()` does post-exec **token-file verification**: if `approved=True`
  but the resolved hope-home is still empty, demote `approved→False`,
  set `token_verified=False`, surface a pointed hint. Avoids the silent
  false-positive that caused the infinite loop.
- Cache is process-global with `reset_cache()`; tests use an autouse
  fixture to clear it AND delete `HOPE_HOME_DIR` from env between tests.

## Correct design (`hope-mcp/src/hope_mcp/tools/login.py`)
Two tools:

1. **`hope_check_login`** — inspects the RESOLVED hope-home first (not
   always `~/.hope`) via `_peek_hope_home()` → if empty, returns
   `logged_in=false` with `probe.skipped=true`. Only when creds exist
   does it run `hope ls --num=1 --json` with a clamped 30 s timeout.
2. **`hope_login(username, timeout_sec=300, st=False)`** — execs
   `hope login <username>`, blocks up to 5 min (clamped `[30, 900]`).
   Returns `{ok, approved, denied, approval_timed_out, token_verified,
   hint, next_action, cmd, returncode, stdout, stderr, elapsed_sec}`.
   `token_verified=False` means hope exited rc=0 but no token landed in
   the expected dir — treat as failure.

## Safety details (non-obvious)
- `close_stdin=True` → `stdin=asyncio.subprocess.DEVNULL`. The MCP
  server's own stdin is the framed JSON-RPC channel; children must
  NEVER inherit it.
- `start_new_session=True` — detaches the child from our controlling
  TTY so interactive prompts to `/dev/tty` fail fast with ENXIO.
- Username regex: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` — reject `;`,
  `|`, `&`, backticks, `$()`, spaces, newlines, `--flags`, absolute
  paths. Belt-and-braces with `shlex.quote` even after the regex.
- Outcome classification: `approved` = rc 0 AND no deny pattern AND
  token_verified; `denied` = rejection regex (EN: denied/rejected/
  cancelled by user; 中文: 拒绝/取消/未通过); `approval_timed_out` =
  outer timeout OR "waiting for approval" / "审批超时" regex.
- Timeouts: 300 s default; hard max 900 s.

## Testing tips (hope-mcp/tests/conftest.py)
- `fake_hope` fixture populates tmp `~/.hope/` + unsets `HOPE_HOME_DIR`.
- Autouse fixture `_auto_reset_hope_home_cache` resets the
  `hope_mcp.hope_home._CACHE` before/after every test and re-scrubs
  `HOPE_HOME_DIR` from env (a dev shell may leak it in; otherwise
  priority-1 wins every test regardless of HOME monkeypatch).
- Tests that simulate a successful login must have the shim ALSO
  write a `.token` file under `$HOME/.hope/` — otherwise post-verify
  correctly demotes `approved→False` and the test fails.

## Workflow
```
check_login()
  → resolved hope-home empty → return logged_in=false (no subprocess)
(tell user: "approve the mobile push when it arrives")
login(username=...) → blocks ~5–30 s typically
  → post-verify: confirm .token landed where we expect
  → {approved: true, token_verified: true}
check_login() → hope ls --num=1 succeeds → logged_in=true
```

## Generalization
- CLI's argv-form login blocks on out-of-band signal (mobile push,
  OIDC browser flow) → CAN wrap in MCP; long timeout + `stdin=DEVNULL`
  + `start_new_session=True`.
- OTHER subcommands of the same CLI may prompt interactively when
  creds are missing → **never run them blind; check for a populated
  credential dir first**, or they will hang until timeout.
- If the CLI has an env-var override for credential location, ALWAYS
  inject it from the MCP wrapper rather than trusting the CLI's
  auto-detection — container mounts lie.
- After any "login successful" exit, **verify the token actually
  landed** where the next call will look. "rc=0" is not proof.
