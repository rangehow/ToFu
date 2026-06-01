---
name: chatui-postgresql-migration-bugs
description: PostgreSQL migration bugs in chatui: JSONB \\u0000 escape rejection, naive strip → orphaned backslash corruption, centralized json_dumps_pg() fix, null bytes/surrogates, reserved word 'count', boolean=integer
enabled: true
tags: [postgresql, sqlite, migration, bug-fix, null-bytes, jsonb, database]
created: 2026-03-25T13:19:16Z
updated: 2026-03-27T04:10:22Z
---

# ChatUI SQLite→PostgreSQL Migration Bugs

## Bug 1 (CRITICAL): JSONB `\u0000` Escape Rejection + Naive Strip Corruption

**Root cause**: PostgreSQL rejects `\u0000` in JSONB (even though RFC 8259 allows it). `json.dumps()` encodes Python null bytes (`\x00`) as `\u0000` in JSON output. If `strip_null_bytes_deep()` misses a null byte (race, late injection, etc.), the `\u0000` reaches PostgreSQL and fails.

**The trap**: Naively stripping `\u0000` from JSON text **corrupts** escaped-backslash sequences. Example:
- Source code text: `# null bytes (\x00 / \u0000) in JSONB`
- After `json.dumps()`: `\\u0000` (escaped backslash + literal text "u0000")
- Naive `.replace('\\u0000', '')` matches the INNER 6 chars → leaves `\)` 
- PostgreSQL error: `Escape sequence "\)" is invalid`

**Fix**: `json_dumps_pg()` in `lib/database.py` — a centralized serializer:
1. `strip_null_bytes_deep(obj)` — removes `\x00` from raw Python data
2. `json.dumps(...)` — produces JSON text  
3. `_strip_json_null_escapes(text)` — smart post-pass using placeholder technique

```python
def _strip_json_null_escapes(json_text):
    if '\\u0000' not in json_text:
        return json_text  # fast path
    PH = '\x01\x01'  # safe: json.dumps always escapes \x01 as \u0001
    tmp = json_text.replace('\\\\', PH)   # protect escaped-backslash pairs
    tmp = tmp.replace('\\u0000', '')       # strip only REAL null escapes
    return tmp.replace(PH, '\\\\')         # restore backslash pairs
```

**Usage**: Replace all `json.dumps(strip_null_bytes_deep(x), ensure_ascii=False)` with `json_dumps_pg(x)`.

**Call sites** (all converted): `manager.py` (×2), `endpoint.py`, `compaction.py`, `feishu/conversation.py`, `routes/common.py` (×2).

## Bug 2: Exception Handling Still Uses sqlite3
`routes/common.py` `_db_safe` decorator only catches `sqlite3.OperationalError`. Must also catch `psycopg2.OperationalError`.

## Bug 3: Null Bytes / Surrogates in TEXT params
`_sanitize_pg_param()` strips `\x00` bytes and lone surrogates from all string params. Wired into `PgCursor.execute()`.

## Bug 4: Reserved Word `count`
Must double-quote: `"count"`. `translate_sql()` handles this.

## Bug 5: boolean = integer
PostgreSQL BOOLEAN columns reject `enabled=1`. `translate_sql()` rewrites to `enabled=TRUE`.

## Bug 6: setval on TEXT id columns
Migration script's `setval(pg_get_serial_sequence(...))` fails on TEXT id columns. Check `data_type='integer'` first.

