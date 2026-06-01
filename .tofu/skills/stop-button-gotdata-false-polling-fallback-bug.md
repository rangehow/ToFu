---
name: stop-button-gotdata-false-polling-fallback-bug
description: Bug fix: stop button before SSE data silently falls through to polling instead of terminating the stream
enabled: true
tags: [javascript, bug-fix, stop-button, sse, abort, polling-fallback, gotData, streaming]
created: 2026-04-16T15:35:35Z
updated: 2026-04-16T15:35:35Z
---

# Stop Button + gotData=false → Silent Polling Fallback Bug

## Bug
When user clicks stop (pause) before SSE receives any data (`gotData=false`), `_trySSE`'s
AbortError handler couldn't distinguish user-initiated abort from SSE connection timeout.
Both cases had `gotData=false` and no special flag.

**Result:** `_trySSE` returned `false` → `connectToTask` fell through to `_pollFallback` →
the task continued running via polling → "Waiting…" appeared. Each stop click repeated
the cycle, creating multiple empty assistant messages.

## Root Cause
In `static/js/ui.js` `_trySSE()` catch block:
```javascript
if (!gotData) {
    stream.controller = new AbortController();
    return false;  // ← WRONG: treats user stop same as SSE timeout
}
```

## Fix
1. Stop button sets `s._userAbort = true` before `s.controller.abort()`
2. `_trySSE` checks: `if (!gotData && !stream._userAbort)` — only falls back to polling when NOT user-initiated
3. Pattern matches existing `_probeAbort` flag mechanism

## Files Changed
- `static/js/ui.js`: Priority 3 stop handler + `_trySSE` AbortError handler

## Related
- `_probeAbort`: timer probe uses same pattern (set flag → abort → check in handler)
- Branch streams don't have this issue (use `_finishBranchStream` directly)

