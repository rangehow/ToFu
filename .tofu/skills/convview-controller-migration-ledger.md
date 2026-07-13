---
name: convview-controller-migration-ledger
description: ConvView (static/js/conv_view.js) unified chatInner controller: surface + migration ledger. NOW OWNS streaming-bubble insert via startStreaming (identity-keyed, _evictByMsgId) enforcing one _msgId=>one DOM node
enabled: true
tags: [frontend, chatInner, rendering, convention, ledger]
created: 2026-05-15T04:40:31Z
updated: 2026-07-09T07:26:02Z
---

# ConvView controller — surface and migration ledger

Step 3 of the unified chatInner rendering refactor (2026-05-15).
Companion to the `bilateral-msgid-and-data-msg-id` memory.

`static/js/conv_view.js` exposes `window.ConvView` — the only
intended mutator of `#chatInner`.

## Bundler position
`'conv_view.js'` sits **last** in `lib/js_bundler.py:_BUNDLE_FILES`
(after ui.js / streaming_render.js / sse_pipeline.js / stream_lifecycle.js),
so `window.ConvView` is safe to reference at runtime from any earlier file.
Depends on `renderMessage`, `_streamingBubbleHTML`, `_surgicalTruncateDOM`,
`_convRenderFingerprint`, `renderChat`, `_lastRenderedFingerprint`, `_ensureMsgId`.

## API surface
```
ConvView.upsertMessage(convId, msg, {idx?, append?}) → bool
ConvView.removeMessage(convId, msgOrIdOrIdx)         → bool
ConvView.removeAfter(convId, cutoffIdx)              → bool
ConvView.replaceAll(convId)                          → bool
ConvView.startStreaming(convId, {role,status,timeStr,msgId}) → bool   ← 2026-07-09
ConvView.finalizeStreaming(convId, msg, {removeIfTruncated?=true}) → bool
```
All methods no-op when `activeConvId !== convId`. Identity lookup inside
`_findMsgEl` prefers `[data-msg-id="..."]` (CSS.escape) → falls back to `msg-${idx}`.

## THE RENDER INVARIANT (2026-07-09): one `_msgId` ⇒ at most one DOM node
Settled bubbles are keyed `id="msg-${idx}"` (MUTABLE array position) + a stable
`data-msg-id`; the live turn is the `#streaming-msg` singleton. The
"one data entry, two identical bubbles" RENDER duplicate (debug panel shows ONE
`conv.messages` entry; self-heals on refresh) came from reconciling the streaming
insert by INDEX: `connectToTask` did `getElementById('msg-'+lastIdx).remove()`,
which MISSED the real static bubble when the tail index had DRIFTED (placeholder
push / splice / lazy-window offset) → a fresh `#streaming-msg` inserted alongside
the stranded static node, then `finalizeStreaming`'s bare `sm.outerHTML=...` left
BOTH → two nodes for one `_msgId`.
Fix (identity-keyed projection, in conv_view.js):
- `_evictByMsgId(inner, msgId, exceptEl)` — removes every `[data-msg-id]` node
  except the keeper. Enforcement primitive.
- `startStreaming` — the SINGLE seam for inserting `#streaming-msg`; evicts any
  node bound to this msgId + any leftover `#streaming-msg` BEFORE inserting.
- `finalizeStreaming` sweep — after the outerHTML swap, `_evictByMsgId(inner,
  msg._msgId, keep=getElementById('msg-'+idx))` removes any surviving twin.
- `sse_pipeline.js connectToTask` reconnect: index eviction DELETED, routed
  through `window.ConvView.startStreaming(...)` (raw insertAdjacentHTML kept only
  as `!ConvView` fallback).
- `showStreamingUIForConv` (stream_lifecycle.js) NOT rerouted: it builds one
  batch `inner.innerHTML=html` = faithful full projection, can't strand a twin.
Test: `tests/test_frontend_convview_identity_render_dedupe.py` (jsdom via
`tests/_jsdom.py`): drift a static `msg-3` for `_msgId=m1`, drive
startStreaming+finalize, assert 1 node; NEUTER `_evictByMsgId`→`return 0` proves
2 nodes return. Harness gotcha: `_findConv` reads the BARE `conversations`
global (globalThis under indirect-eval), NOT `window.conversations` — seed
`globalThis.conversations` or finalizeStreaming silently no-ops (conv-not-found).

## Migration ledger — MIGRATED
### Streaming finalize (5 sites): main.js startAssistantResponse error path;
finishStream main path; endpoint entering-critic / stale-planner / planner-done /
critic-msg finalize. All were inline `sm.outerHTML=renderMessage(...)`.
### Truncate (2): regenerateFromUser, saveEditAndResend (`if(!_surgicalTruncateDOM())renderChat()`).
### Streaming INSERT (2026-07-09): connectToTask reconnect → startStreaming. (DONE)

## Migration ledger — STILL BYPASSING ConvView
- Single-message `outerHTML=renderMessage(msg,idx)`: main.js:1480/1548/1632/1830/2188;
  ui.js saveEdit*/cancelEdit/PATCH-revert/translation-swap; project.js message swap.
- endpoint_planner_done dangling-ref recovery — intentionally inline (re-inserts
  into conv.messages; DOM+model intents shouldn't be conflated).
- OTHER streaming-bubble creators still raw `insertAdjacentHTML(_streamingBubbleHTML())`:
  main.js send-path, streaming_render.js `_beginVuStreaming` (autopilot VU),
  finishStream queued-dispatch placeholder, endpoint critic/worker bubbles. These
  are candidates to also route through `startStreaming` for full invariant coverage.
- `_surgicalTruncateDOM` internals still walk `[id^="msg-"]` by numeric index.

## Verification
`node --check static/js/conv_view.js`;
`python3 -c "from lib.js_bundler import build_bundle; print(build_bundle())"`.
2026-07-09 build → `bundle-d3c94560.js`; `startStreaming:function` (def) +
`startStreaming(convId` (call site) both survive esbuild. Restart + hard-refresh.
