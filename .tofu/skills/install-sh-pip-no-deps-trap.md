---
name: install-sh-pip-no-deps-trap
description: install.sh installs trafilatura/courlan/dateparser via pip --no-deps; transitive pure-Python runtime deps (babel, tld, pytz, regex, tzlocal) MUST be listed explicitly in PIP_ONLY_PKGS or server boot crashes
enabled: true
tags: [install, bug-pattern, dependencies]
created: 2026-05-10T06:25:15Z
updated: 2026-05-10T06:25:15Z
---

# install.sh `pip --no-deps` trap

## Why the flag exists
`install.sh` installs pure-Python web-fetch deps with `pip install --no-deps`
to prevent pip from downgrading conda-forge's `lxml>=6` (which would in turn
force `libxml2<2.14` → `icu<76` → blocks PG 18). Comment at install.sh:570.

## The trap
`--no-deps` means **every transitive runtime dep must be listed explicitly**
in `PIP_ONLY_PKGS`. Each upstream dependency bump silently breaks server
boot until the list is updated.

## Known transitive deps (as of 2026-05)

| Package | Required by | Symptom if missing |
|---|---|---|
| `babel` | `courlan>=1.3` | `from babel import Locale, UnknownLocaleError` at `courlan/filters.py:13` → server import crashes |
| `tld` | `courlan` | `ModuleNotFoundError: tld` |
| `pytz` | `dateparser` | (often pulled in transitively, but not guaranteed) |
| `regex` | `dateparser` | `ModuleNotFoundError: regex` |
| `tzlocal` | `dateparser` | `ModuleNotFoundError: tzlocal` (note: tzlocal doesn't expose `__version__`) |

## Defense-in-depth layers added in install.sh
1. Explicit list of all 5 transitive deps in `PIP_ONLY_PKGS` (so the
   first install lays them down).
2. Final import probe (`info "Verifying lxml + trafilatura ..."`)
   includes all 5 too.
3. **Self-heal**: if the import probe fails, re-run pip WITH
   dependency resolution but with a `--constraint` file pinning
   `lxml>=6` so the resolver can't downgrade conda's lxml.
4. Only after BOTH attempts fail does the script `fail` (was a
   `warn` before — that's why the babel crash slipped through to
   `server.py` boot in May 2026).

## When upgrading trafilatura/courlan/htmldate
1. Read upstream `pyproject.toml` for the new dep list.
2. Add any new pure-Python runtime dep to both `PIP_ONLY_PKGS`
   and the import-probe line in install.sh.
3. Do NOT remove `--no-deps` — the lxml<6 / icu / PG 18 chain still
   matters.

