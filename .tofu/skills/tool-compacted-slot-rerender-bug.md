---
name: tool-compacted-slot-rerender-bug
description: Bug: ptool slot for in-flight bubble doesn't re-render on tool_compacted SSE because all surgical-update branches in _syncToolRoundsDOM bail on already-finished slots
enabled: true
tags: [frontend, ui.js, compaction, render-diff]
created: 2026-05-12T06:44:15Z
updated: 2026-05-12T06:44:15Z
---

# tool_compacted: slot needs explicit re-render branch in _syncToolRoundsDOM

## The bug (2026-05-12, after the cross-message fix)
After fixing `tool_compacted` SSE to walk all assistant messages and stamp the
matching round, the COMPACTED L1 pill **still didn't render** for rounds in the
**in-flight assistant bubble** during streaming.

### Root cause
`_syncToolRoundsDOM` (`static/js/ui.js`) does per-slot surgical updates with an
`else if` chain. Container-level `_roundsFingerprint` correctly invalidates when
`compactionLayer` changes (`r.compactionLayer ? r.compactionLayer.length : 0`),
so the function re-runs — but for a slot that already finished rendering
(tool_complete fired earlier and added the Preview button), every existing
branch is false:

- not `isActive` / `pending_approval`
- no `.ptool-active` / `.ptool-cmd-running` / `.ptool-pending` / `.code-exec-running`
- no `.ptool-cmd-stdin` / `.hg-card` / `.hg-submitted-line` / timer-watcher
- the `round.toolContent && !slot.querySelector('[data-tc-preview]')` branch
  bails because the preview button is already there

→ slot keeps stale ptool-line HTML, no COMPACTED pill, only switching convs
or reloading surfaces it.

For *older* (non-streaming) messages, the cross-message fix triggers
`renderChat(_conv, false)` → `_msgFingerprint` already counts compactedCount
so those re-render fine. The bug was **only** in the in-flight bubble.

## The fix
Add an explicit branch in the `_syncToolRoundsDOM` slot-update chain:
```js
} else if (round.compactionLayer && !slot.querySelector('.ptool-compaction-label')) {
  slot.innerHTML = _renderUnifiedToolLine(round, false);
}
```
Place it *after* the toolContent/preview-button branch so it doesn't double-fire.

## Pattern lesson
Whenever you add a new in-place mutation to a tool round (compactionLayer, new
status, new badge), check `_syncToolRoundsDOM`'s else-if chain — surgical-DOM
diffing only re-renders slots whose CURRENT DOM state matches one of the
"needs rebuild" predicates. Pure data-only changes that don't flip an existing
DOM marker class will silently no-op even if the container fingerprint
invalidates correctly. Either:
1. Add a predicate branch keyed off the absence of the new marker
   (e.g. `!slot.querySelector('.ptool-compaction-label')`), or
2. Add the data field to one of the existing class markers' rebuild predicates.

`branch.js` uses a full `renderToolRoundsHTML` rebuild via
`_updateBranchStreamingUI`, so it doesn't have this trap.

## Files
- `static/js/ui.js` — `_syncToolRoundsDOM` else-if chain (~line 4838-4855)
- `static/js/ui.js` — `_msgFingerprint` (compactedCount/compactedToSum)
- `static/js/ui.js` — `tool_compacted` SSE handler (~line 6565)

