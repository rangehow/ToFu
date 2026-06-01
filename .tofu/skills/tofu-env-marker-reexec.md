---
name: tofu-env-marker-reexec
description: .tofu_env.json marker bridges install.sh → server.py/bootstrap.py: re-exec into the right conda env without conda init
enabled: true
tags: [install, conda, bootstrap, server]
created: 2026-05-06T03:36:27Z
updated: 2026-05-06T03:36:27Z
---

# Tofu Env Marker (.tofu_env.json) — re-exec pattern

## Problem
Users run `python server.py` from random shells where the Tofu conda env isn't
activated (fresh terminal, IDE play button, system /usr/bin/python first on
PATH). Running `conda init` to fix this would mutate ~/.bashrc — DANGEROUS in
shared codelab containers (see codelab-bashrc-danger memory).

## Solution
`install.sh` writes `<INSTALL_DIR>/.tofu_env.json` (gitignored). The guard at
the top of `server.py` AND `bootstrap.py` reads it and, if needed, re-execs
into the env's python via `os.execv`. Loop guard: `_TOFU_ENV_REEXEC=1`
(mirrors existing `_CHATUI_VIA_BOOTSTRAP=1`).

## Marker schema
```json
{
  "schema": 1,
  "created_at": <unix>,
  "conda_base":   "/abs/path/tofu-miniforge3",
  "env_name":     "tofu",
  "env_prefix":   "/abs/path/tofu-miniforge3/envs/tofu",
  "python":       "/abs/path/tofu-miniforge3/envs/tofu/bin/python",
  "owned_by_tofu_install": true,
  "note": "..."
}
```

## Re-exec sets BEFORE execv
- `LD_LIBRARY_PATH` ← prepend `<env_prefix>/lib` (libpq, libxml2, Chromium libs)
- `PATH` ← prepend `<env_prefix>/bin` (so subprocesses find pg_ctl, playwright)
- `CONDA_PREFIX`, `CONDA_DEFAULT_ENV` set so subprocess detection works
- `_TOFU_ENV_REEXEC=1` to prevent infinite loop

## Conda discovery in install.sh
- `MIN_CONDA_MAJOR=24` — version gate. Existing user conda accepted only if
  `major >= 24`; otherwise install sibling Miniforge.
- Sibling install path: `<parent of INSTALL_DIR>/tofu-miniforge3` (NOT $HOME —
  users on shared FS often lack $HOME write perms).
- `CONDA_OWNED_BY_US=1` flag gates whether we run `conda update` / write
  `.condarc`. We never touch a pre-existing user conda.
- NEVER call `conda init` — would mutate ~/.bashrc.

## Files involved
- `install.sh` — Step 1 (locate/install) + Step 2 (update only if owned) +
  Step 4.5 (write marker after env activate)
- `server.py` — `_tofu_maybe_reexec_into_env()` BEFORE bootstrap excepthook
- `bootstrap.py` — same guard, then handles missing-deps repair
- `.gitignore` — `.tofu_env.json` excluded
- `export.py` — `.tofu_env.json` in `ALWAYS_EXCLUDE_FILES`

## Bootstrap UI provider templates
`/bootstrap/provider-templates` endpoint serves `_BUILTIN_PROVIDER_TEMPLATES`
merged with `static/provider_templates/*.json` (so `meituan.json` works
automatically). Frontend renders provider cards + model dropdown — same
shape as Settings UI's `_PROVIDER_TEMPLATES` but smaller curated builtin
list (~12 entries).

