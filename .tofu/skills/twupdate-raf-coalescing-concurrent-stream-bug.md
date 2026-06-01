---
name: twUpdate-rAF-coalescing-concurrent-stream-bug
description: Bug fix: twUpdate rAF coalescing silently drops active conversation renders when background conv's event overwrites _twPendingConvId — fix by always preferring activeConvId as render target when it has a streamBuf
enabled: true
tags: [javascript, streaming, concurrent-streams, rAF, rendering, bug-fix, twUpdate, coalescing]
created: 2026-04-01T06:58:21Z
updated: 2026-04-01T06:58:21Z
---

# twUpdate rAF Coalescing — Concurrent Stream Rendering Bug

## Bug Pattern

`twUpdate(convId)` uses `requestAnimationFrame` coalescing with a single global `_twPendingConvId`. When multiple conversations stream concurrently:

1. Active conv A calls `twUpdate("A")` → `_twPendingConvId = "A"`, schedules rAF
2. Background conv B calls `twUpdate("B")` → `_twPendingConvId = "B"` (overwrite), rAF already scheduled
3. rAF fires: `cid = "B"`, `activeConvId = "A"` → cross-talk guard skips rendering → **active conv UI freezes**

Data continues accumulating in `streamBufs` and `assistantMsg`, but DOM never updates.

## Symptom

- Backend has generated response, frontend shows "Waiting..." or stale content
- Switching conversations and back "fixes" it (triggers `showStreamingUIForConv()` which reads buffers directly)
- Only happens with concurrent streams (multiple active tasks)

## Fix

In rAF callback, prefer `activeConvId` as the render target when it has a stream buffer:

```js
const renderCid = (activeConvId && streamBufs.has(activeConvId)) ? activeConvId : cid;
```

This ensures every rAF frame renders what the user is looking at, regardless of which background stream triggered the frame.

## File
`static/js/core.js` — `twUpdate()` function

