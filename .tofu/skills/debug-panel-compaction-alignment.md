---
name: debug-panel-compaction-alignment
description: Debug panel must patch _debugCache on tool_compacted SSE; tool_compacted now carries compactedContent
enabled: true
tags: [debug-panel, compaction, sse, alignment]
created: 2026-05-13T04:59:48Z
updated: 2026-05-13T04:59:48Z
---

# Debug panel ↔ compaction alignment (2026-05-13)

## Problem
The debug panel renders the api-form `messages` snapshot the model just
saw. Its source of truth is the `messages_snapshot` SSE emitted twice
per round (pre-LLM in `lib/tasks_pkg/orchestrator.py:1122`, post-tool in
`lib/tasks_pkg/tool_dispatch.py:1093`).

`tool_compacted` SSE flips the COMPACTED chip on a tool round, but only
the chip — the panel's cached snapshot still held the pre-compaction
~100 KB content until the NEXT `messages_snapshot`, which never arrives
if the task ends/pauses. Reload was even worse: the rebuild via
`/api/conversations/<id>/debug-messages` could read pre-CAS-write DB
state and show original content with a compacted chip.

## Fix
1. **Server**: `tool_compacted` SSE now carries `compactedContent`
   (the placeholder string).
   - `lib/tasks_pkg/compaction.py:_stamp_l1` — L1 path
   - `lib/tasks_pkg/tool_dispatch.py` — L0 aggregate-budget path

2. **Frontend (`static/js/ui.js` `tool_compacted` handler)**: after
   stamping toolRounds + triggering chip re-render, also patch
   `_debugCache[convId].messages` by toolCallId — set
   `m.content = ev.compactedContent` and stash
   `_compactionLayer / _compactedFromChars / _compactedToChars / _toolTokens`.
   Then call `showMessagesInDebug(..., true, ...)` to incrementally
   re-render. `_msgFingerprint` now includes the compaction marker so
   the row is detected as changed.

3. **Display improvements (`static/js/core.js`)**:
   - `_debugMsgChars` / `_debugMsgTokens` / `_debugCompactionInfo` /
     `_fmtKB` — pure helpers shared by full + incremental paths.
   - Compaction info also recovered by content-shape sniff
     (matches `^\[<tool> result compacted — was N chars`) so messages
     loaded from `/debug-messages` (no `_compactionLayer` field) still
     get the badge.
   - Header summary now shows `(N) · 🔧tools · 🗜compacted/totalTool · ~Ttok`.
   - Per-message: orange 🗜 badge with `from→to` chars + `border-left`
     accent; per-message summary includes `~tok` estimate.
   - CSS: `.debug-compact-badge`, `.debug-msg-block.debug-msg-compacted`
     in `static/styles.css` (light + tofu themes covered).

## Lesson
SSE events that mutate **persistent client state** (tool round chips,
debug snapshots, conversation messages) need to carry **all** the data
needed to keep that state coherent — never assume a follow-up event
will fill in the gap. The follow-up may not come.

## Files
- `lib/tasks_pkg/compaction.py` (search for `compactedContent`)
- `lib/tasks_pkg/tool_dispatch.py` (search for `compactedContent`)
- `static/js/ui.js` (`tool_compacted` SSE handler ~line 6575)
- `static/js/core.js` (`showMessagesInDebug` + new `_debug*` helpers)
- `static/styles.css` (`.debug-compact-badge`)

Restart server to rebuild the JS bundle (no hot reload).

