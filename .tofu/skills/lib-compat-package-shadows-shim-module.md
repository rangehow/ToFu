---
name: lib-compat-package-shadows-shim-module
description: lib/compat is now a package; cross-platform shim lives in lib/compat/_platform.py and is re-exported
enabled: true
tags: [architecture, imports, compat, gotcha]
created: 2026-05-26T05:17:42Z
updated: 2026-05-26T05:17:43Z
---

# `lib.compat` — package shadows the old cross-platform shim

## Layout
- `lib/compat/` is a **package** containing API-compatibility adapters
  (`openai.py`, `anthropic.py`, …) consumed by `routes/compat_*.py`.
- `lib/compat/_platform.py` holds the original cross-platform OS shim
  (`safe_signal`, `IS_LINUX`, `IS_WINDOWS`, `IS_MACOS`, `HAS_PROCFS`,
  `get_shell_args`, `get_username`, `get_temp_dir`, `is_process_alive`,
  `is_process_named`, `set_pipe_nonblocking`, `safe_select_pipes`,
  `is_network_mount`, `safe_shlex_split`). This file used to be
  `lib/compat.py` at the top level.
- `lib/compat/__init__.py` re-exports every shim symbol via
  `from lib.compat._platform import …` so all historical
  `from lib.compat import safe_signal` call sites (server.py, project_mod,
  scheduler, database, fetch, desktop_agent, tests) keep working.

## Why this matters
Python resolves `lib/compat/` (package) before `lib/compat.py` (module).
If someone re-creates a top-level `lib/compat.py` it will be silently
ignored. If someone adds a NEW shim function, put it in
`lib/compat/_platform.py` AND add it to the re-export list (and `__all__`)
in `lib/compat/__init__.py` — otherwise it won't be importable as
`from lib.compat import <name>`.

## Don't do
- Don't put route/Blueprint imports inside `lib/compat/__init__.py` —
  the package docstring forbids it (avoids circular imports).
- Don't reach into `lib.compat._platform` directly from other modules;
  always import from `lib.compat`.

