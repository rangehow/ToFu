---
name: thinking-depth-conv-sync-bugs
description: Thinking depth bugs: _applyBackendCapabilities display clobber AND selectThinkingDepth missing _saveConvToolState race
enabled: true
tags: [javascript, ui, bug, thinking-depth, perf-cache]
created: 2026-04-08T03:19:02Z
updated: 2026-04-08T09:48:45Z
---

# Thinking Depth Sync Bugs

## Bug 1: Display Clobber by _applyBackendCapabilities
`_applyBackendCapabilities()` unconditionally set `depthBar.style.display = ''` which clobbers
`_applyModelUI`'s `display:flex`, reverting to CSS default `display:none`.
The perf cache in `_applyModelUI` prevents re-apply.

**Fix**: Only hide when `caps.thinkingDepth === false`. When switching back from external
backend, invalidate `_lastAppliedModelId = null`.

## Bug 2: selectThinkingDepth Missing _saveConvToolState (Fixed 2026-04-08)
`selectThinkingDepth()` updated `config.thinkingDepth` (global) but did NOT call
`_saveConvToolState()`, leaving `conv.thinkingDepth` stale.

**Race condition**: async `loadConversationsFromServer` (triggered by visibilitychange,
BroadcastChannel, etc.) calls `_restoreConvToolState(conv)` which resets
`config.thinkingDepth = conv.thinkingDepth` → clobbers the user's depth change back
to the stale value → backend receives wrong depth → no thinking generated.

**Fix**: Added `_saveConvToolState()` call in `selectThinkingDepth()` to keep
`conv.thinkingDepth` in sync with `config.thinkingDepth`, matching all other toggle functions.

## Key Insight
Any function that modifies `config.thinkingDepth` or `thinkingEnabled` MUST also
call `_saveConvToolState()` to persist to `conv.*`. Otherwise, async operations
like server sync can clobber the change back via `_restoreConvToolState(conv)`.

