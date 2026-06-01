---
name: tool-root-pill-multi-root-flicker-fix
description: _resolve_tool_root_name: check max(global,conv) registry size, not just the picked one — fixes inconsistent rootname pill per round
enabled: true
tags: [frontend, multi-root, tool-display, bug]
created: 2026-05-12T06:26:55Z
updated: 2026-05-12T06:26:55Z
---

# Tool-root pill: inconsistent rendering across rounds

## Symptom
In a multi-root workspace conversation, the same tool path
(`static/js/ui.js`, `lib/css_bundler.py`, …) renders WITH a
`chatui:` pill on some rounds and WITHOUT on others — purely
based on which message the round lives in. Looks like a half-
broken UI.

## Root cause
`lib/tasks_pkg/tool_display.py:_resolve_tool_root_name` did:

```python
registry = conv_map if conv_map else _roots
if len(registry) <= 1:
    return ''
```

This picks ONE registry and short-circuits to "single-root mode"
when that ONE has ≤ 1 entries. But the conv-scoped map can lag
the global one — a conv that was started before extra roots got
registered carries a single-entry `_conv_roots[conv_id]` even
after the global `_roots` has 4. The resolver picked the conv
map, saw 1 entry, returned `''`, no pill.

Worse, the registry chosen flickered across rounds — sometimes
the conv map was populated with multiple entries (after some
specific code path repopulated it), sometimes it wasn't. Hence
the inconsistency in the saved DB.

## Fix
Compute multi-root status from BOTH registries:

```python
global_count = len(_roots)
conv_count = len(conv_map) if conv_map else 0
if max(global_count, conv_count) <= 1:
    return ''
# Prefer conv-scoped, fall back to global when conv is single-entry
registry = conv_map if (conv_map and conv_count > 1) else _roots
```

Now the pill appears whenever the user is in a multi-root world
in EITHER scope, and resolution falls back to the global registry
when the conv map hasn't caught up yet.

## What's NOT fixed
Historical rounds saved with `_toolRoot` missing stay missing —
the field is persisted to DB and won't backfill. Only new rounds
benefit from the fix.

## Frontend
`static/js/ui.js:_renderToolRootPill` checks
`projectState.extraRoots.length > 0`. That's the right guard for
frontend (mirrors the global check). Untouched by this fix.

## Audit
```sql
SELECT (m->>'role'), m->'toolRounds'
FROM (SELECT jsonb_array_elements(messages) m FROM conversations
      WHERE id = '<conv_id>') sub
WHERE m->>'role' = 'assistant';
```

Look for `_toolRoot` presence on consecutive rounds with similar
fs tool args. If it flickers within one conv, the bug is back.

