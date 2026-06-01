---
name: hope-mcp-dx-confirm-pid-allowlist
description: hope dx_confirm login writes pid allow-list; MCP strips it so shared-FS token works from any tree
enabled: true
tags: [hope-mcp, gotcha, dx_confirm, credentials, process-tree]
created: 2026-05-01T11:19:54Z
updated: 2026-05-01T11:19:54Z
---

# Hope dx_confirm login records a pid allow-list — strip it after MCP login

## Symptom
- MCP side: `hope` calls work fine after a successful `hope_login`.
- Shell side: `hope ls` from the user's terminal drops into the misid
  prompt as if NOT logged in, even though `.token` has a fresh,
  unexpired token for that user.
- Both sides share the exact same `.token` file (via `HOPE_HOME_DIR`
  pointing at `/mnt/dolphinfs/.../hope/.hope`).

## Root cause (from reading hope source)
`hope/tools/login.py::update_token_file(..., check_pids=True)` is
called when `HOPE_LOGIN_MODE == 'dx_confirm'` (Meituan's mobile-push
approval flow — the default on most internal hosts). It writes:

```json
{"user": {"token": "...", "expire_time": "...", "pids": [os.getppid(), ...]}}
```

Then `hope/tools/helper.py::get_user()` walks `os.getppid()` UPWARD
with `psutil.Process(pid).ppid()` and treats the session valid only
if some ancestor appears in the `pids` list. Exact code:

```python
pids = token_infos.get(user, {}).get('pids', [])
if pids:
    if check_parent_pid_recursive(os.getppid(), pids):
        return user
    else:
        _ = token_infos.pop(user)       # drop from consideration
```

The MCP server's process tree and the user's shell's process tree
are disjoint — so whichever side did NOT do the most recent login
gets locked out. The pid gate is a defense against stolen `.token`
blobs, but it breaks totally on shared FUSE mounts.

## Fix (in hope-mcp)
`src/hope_mcp/tools/login.py::_strip_pids_from_token_file` — after a
successful `login()` call with `approved=True && token_verified=True`,
re-open `.token`, pop the `pids` key for the user, rewrite. This is
EXACTLY what hope does itself when `HOPE_LOGIN_MODE != 'dx_confirm'`
(the default branch in `update_token_file` does `pop('pids', '')`).
The token blob stays identical; only the allow-list goes away, and
`get_user`'s `if pids:` guard then allows any process tree.

Return value exposes `pids_stripped: bool` and `pids_strip_note: str`.

## Tests (`tests/test_login.py`)
- `test_login_strips_pids_allowlist_from_token_file` — shim writes
  `.token` with a pids allow-list; verify we drop it post-login.
- `test_login_pid_strip_is_safe_when_token_not_json` — if `.token`
  is not JSON (legacy shim wrote a string), strip helper no-ops
  gracefully and does NOT flip the approved verdict.

## Security note
We're trading a weak stolen-token defense for cross-process-tree
sharing. The token file lives inside `HOPE_HOME_DIR` anyway — anyone
who can read it already owns the session. Net-new attack surface: 0.

## Quick diagnosis
```python
import json, os, psutil
with open(os.path.expandvars('$HOPE_HOME_DIR/.hope/.token')) as f:
    d = json.load(f)
for u, info in d.items():
    print(u, 'pids=', info.get('pids'))

# Walk my ancestor-pid chain
p = os.getpid(); chain=[]
while p: chain.append(p); p = psutil.Process(p).ppid()
print('my chain:', chain)
```
If `pids` is set AND has no intersection with `chain`, this bug.

