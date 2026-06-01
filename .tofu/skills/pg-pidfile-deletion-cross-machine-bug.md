---
name: pg-pidfile-deletion-cross-machine-bug
description: Bug fix: multi-machine chatui sharing FUSE pgdata — Machine B's _ensure_pg_running deletes Machine A's postmaster.pid causing PG immediate shutdown; root causes: remote_host typo→NameError swallowed by except Exception, bare except Exception returns, None owner_host logic hole
enabled: true
tags: [python, postgresql, pidfile, cross-machine, fuse, bug-fix, critical, database]
created: 2026-03-27T05:41:03Z
updated: 2026-03-27T05:41:03Z
---

# PostgreSQL postmaster.pid Cross-Machine Deletion Bug

## Symptom
PG repeatedly crashes with `could not open file "postmaster.pid": No such file or directory → performing immediate shutdown because data directory lock file is invalid` — looks like DolphinFS/FUSE file loss but is actually code deleting it.

## Root Cause
When multiple machines share the same pgdata on FUSE storage, Machine B importing `lib/database.py` triggers `_ensure_pg_running()` which calls `_pg_already_running_on_another_machine()`. Three bugs caused it to return `(False, None)` even when Machine A's PG was running:

1. **`remote_host` typo** — variable was named `owner_host` but referenced as `remote_host` → `NameError` swallowed by `except Exception: return False, None`
2. **Bare `except Exception` swallowers** — two places catch all exceptions silently, returning `(False, None)` without logging
3. **`None` owner_host logic hole** — when `.pg_owner_host` is missing, `None not in (local_ip, 'localhost', '127.0.0.1')` is `True` → returns `(True, None)` → Step 3's `if is_remote and remote_host` fails (None is falsy) → falls through to Step 4 which deletes pidfile

## Fix: Hard Ownership Rule
Rewrote `_pg_already_running_on_another_machine()` with one simple rule:

> **If `.pg_owner_host` exists and points to a DIFFERENT machine → ALWAYS return `(True, owner_host)`, regardless of whether remote PG is reachable.**

Only the owning machine is allowed to consider its own pidfile stale. Every return path has explicit logging.

Step 4 in `_ensure_pg_running()` has a double-check safety net that also reads `.pg_owner_host` before any deletion.

## Key Lesson
- Never use bare `except Exception: return` without logging — it hides the real error
- Variable name typos in rarely-tested error paths are deadly
- When dealing with shared-storage locks, default to "do NOT delete" — false-negative (fail to start locally) is far safer than false-positive (kill remote PG)

