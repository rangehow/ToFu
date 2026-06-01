---
name: hope-mcp-stray-init-py-false-positive
description: hope-mcp _override_looks_bogus false-positive on stray 0-byte __init__.py in credential cache
enabled: true
tags: [hope-mcp, gotcha, credentials, heuristic]
created: 2026-05-01T10:56:41Z
updated: 2026-05-01T10:56:41Z
---

# hope-mcp: stray `.hope/__init__.py` false-positive bogus-detection

## Symptom
- User's shell `hope ls` works fine (uses `HOPE_HOME_DIR=/mnt/.../hope`).
- hope-mcp reports `login_required / token expired` forever.
- `hope_check_login` returns `logged_in=false` with a `-2 Token expired` probe
  from a DIFFERENT token file (`$HOME/.hope/.token`).

## Root cause
`hope-mcp/src/hope_mcp/hope_home.py::_override_looks_bogus` used to reject any
`HOPE_HOME_DIR` path whose `.hope/__init__.py` existed (meant to catch the
footgun of pointing the env var at the hope source checkout).

Real credential-cache directories sometimes contain a stray 0-byte
`__init__.py` left over from a past `touch` — alongside a real `.token`,
`local_hope.log`, `jars/`, `.stage/`. The old rule mis-classified these as
source trees and silently rerouted MCP to `$HOME/.hope/`, so MCP and the
shell ended up using two different token files.

## Fix (implemented)
Rewrote `_override_looks_bogus` with a two-step rule:
1. If the dir (or its `.hope/` subdir) contains hope CREDENTIAL markers
   (`.token`, `.accesstoken`, `local_hope.log`, `jars`, `template_jobinfo`,
   `.stage`, `.jobinfo`) → ACCEPT unconditionally. A source checkout never
   carries these.
2. Else, reject only on STRUCTURAL source markers (`wrapper.py`,
   `tools/helper.py`, `tools/login.py`, `tools/settings.py`,
   `api_template.py`, or a non-empty top-level `__init__.py`).
3. Else, ACCEPT (brand-new machines before first login are empty).

## Regression tests added (`tests/test_hope_home.py`)
- `test_cred_cache_with_stray_empty_init_py_is_accepted` — the exact scenario
  from prod: `.hope/.token` + `.hope/local_hope.log` + 0-byte `.hope/__init__.py`.
- `test_bogus_hope_home_dir_with_real_source_tree_is_still_rejected` — uses
  the REAL hope package shape (0-byte `__init__.py` + substantive
  `wrapper.py` + `tools/helper.py`) to confirm the original footgun is still
  caught.

## How to diagnose in future
```python
from hope_mcp.hope_home import describe_resolution, reset_cache
reset_cache()
print(describe_resolution())
```
If `reasons` contains "REJECTED" but the rejected path contains `.token` and
`local_hope.log`, it's this bug.

