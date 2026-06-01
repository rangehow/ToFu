---
name: unpin-falsy-guard-restore-bug
description: Bug fix: unpin reverts because `if (keepPinned)` guard skips restore when pinned=false — all 5 code paths now fixed
enabled: true
tags: [javascript, bug, truthiness, pinned]
created: 2026-04-02T23:32:29Z
updated: 2026-04-04T04:16:34Z
---

# Unpin reverting bug — falsy guard on boolean restore

## Bug
When saving/restoring local pinned state around `_applySettingsToConv()`, the restore was
guarded by `if (keepPinned)`. When user unpins (`pinned = false`), `keepPinned` is falsy →
restore skipped → stale server `pinned: true` persists → conversation re-pins.

## Affected code paths (all in core.js)
There are **5 places** that do the save/apply/restore pattern for pinned state:

1. `loadConversationsFromServer()` — main merge loop (~line 1399)
2. `loadConversationsFromServer()` — prefetched conv path (~line 1435)
3. `loadConvMsgs()` — IndexedDB cache hit path (~line 1488)
4. `loadConvMsgs()` — stale cache server refresh path (~line 1651)
5. `loadConvMsgs()` — direct fetch (no cache) path (~line 1736)

ALL five must use unconditional restore. The bundle file must be updated in sync.

## Fix
```js
// WRONG: if (keepPinned) { x.pinned = keepPinned; x.pinnedAt = keepPinnedAt; }
// RIGHT: x.pinned = keepPinned; x.pinnedAt = keepPinnedAt;
```

## Status
Fixed 2026-04-04: all 5 paths now unconditionally restore. Previously only path #1 was fixed.

## Pattern
When preserving a boolean value across a function that might overwrite it,
always restore unconditionally — never guard with `if (value)` when the
value can legitimately be `false`.
