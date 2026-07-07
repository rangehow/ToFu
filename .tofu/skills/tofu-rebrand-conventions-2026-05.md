---
name: tofu-rebrand-conventions-2026-05
description: After the chatui→tofu rebrand: env vars are TOFU_*; lib/env_compat.getenv_compat() reads new with CHATUI_* legacy fallback. Memory + file-history dirs later moved to .tofu/ (2026-06); PG database name still 'chatui'
enabled: true
tags: [convention, rebrand, env-vars, compat]
created: 2026-05-09T06:48:02Z
updated: 2026-05-09T06:48:02Z
---

# Tofu Rebrand — What's Renamed, What Isn't

The project was originally branded ChatUI; the canonical name is now Tofu.
Renamed thoroughly in 2026-05.

## Canonical pattern: `lib/env_compat.py`

```python
from lib.env_compat import getenv_compat
db_path = getenv_compat('TOFU_DB_PATH', 'CHATUI_DB_PATH', default='data/tofu.db')
```

`getenv_compat(*names, default)` reads env vars in order, prefers the
first (canonical TOFU_* name), warns ONCE per process when a legacy
CHATUI_* name resolves the value. Always pass canonical first.

There's also `promote_legacy_env()` which copies all CHATUI_* env vars
to matching TOFU_* if unset — useful before subprocess spawn so children
see the new names.

## What was renamed

| Category | Renamed to | Notes |
|---|---|---|
| Env vars `CHATUI_*` | `TOFU_*` | Legacy name still honored via `getenv_compat`. ~25 vars total. |
| Default SQLite filename `data/chatui.db` | `data/tofu.db` | Legacy filename auto-picked-up if present (lib/database/_core.py L52-63). |
| Coordination files in `data/pgdata/` | `.tofu_heartbeat`, `.tofu_pg_start.lock` | New code reads BOTH names; writes BOTH; flocks BOTH (so old + new peers serialize). See `lib/database/_bootstrap.py`. |
| sessionStorage keys `chatui_*` | `tofu_activeConvId`, `tofu_vlm_pending` | One-time migration in `static/js/core.js` + `static/js/upload.js` copies legacy → canonical on page load. |
| IndexedDB cache name `chatui_conv_cache` | `tofu_conv_cache` | Old DB deleted on load (`indexedDB.deleteDatabase`). One-time blip: server re-fetches conv on first reopen. |
| Brand strings "ChatUI" in HTML/JS UI | "Tofu" | bootstrap status page, browser extension popup/manifest, export-images footer. |
| `application_name='chatui'` (PG) | `'tofu'` | lib/database/_core.py L666. |
| `_chatui_locate.png` (desktop agent temp file) | `_tofu_locate.png` | |
| `chatui-config-{date}.json` (Settings export) | `tofu-config-{date}.json` | |
| `chatui-browser-extension.zip` (build artifact) | `tofu-browser-extension.zip` | Legacy filename still in export.py exclude list. |
| `data/chatui_pg_backup.sql` | `data/tofu_pg_backup.sql` | scripts/pg_ctl.sh. |
| Default export dirs `chatui-personal/-internal/-opensource` | `tofu-personal/-internal/-opensource` | |

## What was NOT renamed (intentional)

| What | Why |
|---|---|
| **PG database name `chatui`** | Renaming the live DB requires `pg_dump | pg_restore` — silent rename would lose all conv data. Default stays `chatui`; override via `TOFU_PG_DBNAME`. |
| **Historical files** | `paper/emnlp-demo/tofu.tex`, `benchmarks/results_*.json`, `docs/SECURITY_AUDIT_REPORT.md` etc. — historical record. |

> **UPDATE (2026-06):** the memory + file-history dirs were LATER moved off the
> `.chatui/` prefix onto the `.tofu*` artifact prefix (CLAUDE.md §3.6):
> project memories now live at `<project>/.tofu/skills/` and file-history at
> `<base>/.tofu/file-history/` (`lib/memory/storage.py`, `lib/file_history/store.py`).
> Global memories moved further, out of any project, to the server store
> `<data>/memories/global/`. So `.chatui/skills` / `.chatui/file-history` are NO
> LONGER the active locations — only the PG database name stayed `chatui`.

## When to add a new env var

DO use `getenv_compat`:
```python
from lib.env_compat import getenv_compat
val = getenv_compat('TOFU_NEW_KNOB', 'CHATUI_NEW_KNOB', default='1')
```

Or, if it's a brand-new var that has no legacy form, just `os.environ.get('TOFU_NEW_KNOB', '1')`.

