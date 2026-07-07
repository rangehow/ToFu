---
name: force-refresh-streaming-stuck-waiting-bug
description: Streaming bubble stuck/reverting to "等待中…"(Waiting) mid-gen. STRUCTURAL fix (CAUSE 0c): _twFlush + cross-tab flush route through shared _streamFrameArg which applies buf.content||ckpt.content fallback — don't rely on connectToTask caller-side buf seeds. Plus reconnect seed / deferred-render / init-race causes.
enabled: true
tags: [javascript, streaming, race-condition, force-refresh, bug-fix, SSE, activeConvId, twUpdate, twFlush, streamFrameArg, IDB-cache, checkpoint, init-sequence]
created: 2026-03-31T17:37:36Z
updated: 2026-07-03T17:15:00Z
---

# Streaming Bubble Stuck / Reverting to "等待中…" (Waiting…)

The `updateStreamingUI` `wait` branch (`streaming_ui.js`) fires ONLY when
`!msg.content && !msg.thinking`. So ANY render that paints the bubble with an
empty `content` while the task is actually live shows "等待中…". Several distinct
causes below.

## CAUSE 0 — reconnect deferred re-render wipes checkpoint content (2026-07-03, conv mr4cgdakrl6s5i)
Symptoms: (1) fast conv-switch briefly shows real English, (2) force-refresh
shows Chinese translation but NO English, (3) "等待中…" while generating.

Three related defects on the reconnect path (all in `sse_pipeline.js` +
`stream_lifecycle.js`):

1. `connectToTask` seeded `buf.toolRounds` after `twStart` but NOT
   `buf.content`/`buf.thinking`. `twStart` mints an EMPTY buffer → stays empty
   until the first NEW SSE delta, though the checkpoint English is already on
   `assistantMsg.content`.
   **Fix (fresh-connection branch, `if(!activeStreams.has(convId))`):** after
   `twStart(convId)`: `if(assistantMsg.content) buf.content = assistantMsg.content;`
   `if(assistantMsg.thinking) buf.thinking = assistantMsg.thinking;`

1b. That seed only runs on the FRESH branch. A RE-ENTRY that finds an existing
   stream (overlapping reconnect race; a caller re-invoking connectToTask on a
   live conv) SKIPS twStart+seed. `_twFlush` (`health_stream_timer.js`) reads
   `buf.content` RAW with NO message fallback → an empty buffer there paints
   "等待中…" over checkpointed content. This safety was implicit + undocumented.
   **Fix (runs on EVERY entry, right after `const stream = activeStreams.get(convId)`):**
   STRICTLY-ADDITIVE seed — `if(!_reentryBuf.content && assistantMsg.content) _reentryBuf.content = …`
   (same for thinking/toolRounds). Guarded on empty so it NEVER clobbers deltas
   the live `_trySSE` closure is accumulating (that buffer is authoritative once
   it has data). Chose additive-seed over a msg-fallback in `_twFlush` because
   `_twFlush` is the live-streaming single-source-of-truth render — a msg
   fallback there could resurrect stale/wrong-message content.

2. `showStreamingUIForConv`'s 300ms deferred re-render read `dBuf.content` RAW —
   no `|| lastMsg.content` fallback like the INITIAL render one block above.
   **Fix:** deferred arg uses `dBuf.content || _deferLastMsg.content || ''`
   (+ thinking + `getToolRoundsFromMsg(_deferLastMsg)`).

Symptom #2 (Chinese but no English) = auto-translate watchdog/push fills the
SEPARATE `translatePreview` zone with Chinese while the `content` zone stays
blank because the buffer is empty.

Test: `tests/test_frontend_stream_deferred_no_wipe.py` (jsdom + 3 source guards:
fresh-seed, re-entry additive seed, deferred fallback; +2 runtime checks for the
re-entry fills-empty / never-clobbers-live semantics; byte-revert NCs bite).
**Rule: a deferred/second render reading a stream buffer MUST use
`buf.field || msg.field`; any reconnect creating OR re-entering a stream MUST
additively seed an empty buffer from the persisted message (never clobber live
deltas), not just toolRounds.**

## CAUSE 0c — `_twFlush` itself read `buf.content` RAW (STRUCTURAL fix, supersedes the 0/0b band-aids) (2026-07-03)
The 0/0b seeds papered over the real defect: `_twFlush` (`health_stream_timer.js`),
the HOTTEST per-frame render path, built its `updateStreamingUI` arg from raw
`buf.content`/`thinking`/`toolRounds` with NO message fallback — unlike EVERY
other render site. So a field-only `twUpdate` (e.g. an HG-translation flag flip
via `_autoTranslateHumanGuidance`), a re-entry that skipped the seed, or the
cross-tab visibility flush (`cross_tab_sync.js`, also raw) fed it an empty
buffer → "等待中…" over checkpointed content. 5 independent audits + this skill's
own 0b note ("_twFlush reads buf.content RAW") pointed here.
**Fix (correct-by-construction):** new shared `_streamFrameArg(convId)` builds
the payload applying `buf.content || ckpt.content` (thinking likewise;
tool-rounds via `getToolRoundsFromMsg(ckpt)` guarded on `buf.toolRounds.length`),
where `ckpt` = the trailing `assistant`/`_isEndpointReview` message; returns
`null` when no buffer. BOTH `_twFlush` and the cross-tab flush route through it.
**The 0b fear ("a msg-fallback in `_twFlush` could resurrect stale content") was
UNFOUNDED and is hereby retracted:** `retry_reset`/`delta_reset` (sse_pipeline.js)
clear the MESSAGE and buffer TOGETHER, and turn rotations push a FRESH empty
assistant — so the checkpoint is empty exactly when it must be. The 0/0b
`connectToTask` seeds stay as harmless defense-in-depth.
Test: `tests/test_frontend_twflush_msg_fallback.py` (jsdom drives REAL `_twFlush`
+ `updateStreamingUI`; double-neuter: raw-buffer revert → `twflush_no_wait…`
FAILS while `empty_stream_still_waits` stays green). GOTCHA: a synchronous rAF
stub + `_twFlush`'s `<33ms` rate-cap reschedule = infinite recursion — stub rAF
as a non-invoking no-op and call `_twFlush()` directly with an ever-advancing
`performance.now()`.
**Rule (upgraded): when N render sites must honour "buffer-or-checkpoint," put
the fallback in ONE shared payload-builder the render path calls — don't rely on
every caller pre-seeding the buffer. Caller-side seeds are a smell that the
render path reads raw state.**

## CAUSE 1 — `twUpdate` rAF guard drops SSE events during init
During page init after force refresh, `activeConvId` is null. If the SSE `state`
event's `twUpdate(convId)` rAF checks `activeConvId === cid` → null !== convId →
update silently dropped. **Fix:** also render when `activeConvId` is null but
`streaming-body` DOM exists:
`if (activeConvId === cid || (!activeConvId && document.getElementById('streaming-body')))`

## CAUSE 2 — Phase 2 skips checkpoint merge for active tasks (loadConversationMessages)
For convs with `activeTaskId` + IDB cache hit, Phase 2 skipped server data; the
cache may be stale (from before the task started). **Fix:** MERGE_ACTIVE_TASK
branch merges server checkpoint content/thinking INTO the existing assistant
message (never replace `conv.messages` — that orphans connectToTask's ref) AND
seeds `buf.content` when empty (`if(!buf.content) …`) — one of the seed paths
CAUSE 0-1b now no longer relies on solely.

## CAUSE 3 — Empty array truthy in buf fallback
`buf?.toolRounds || getToolRoundsFromMsg(lastMsg)` — `[]` is truthy, so an empty
buf array blocks the fallback. **Fix:** `(buf?.toolRounds?.length ? buf.toolRounds : null) || getToolRoundsFromMsg(lastMsg)`.

## CAUSE 4 — SSE connection latency
300ms deferred `updateStreamingUI` in `showStreamingUIForConv` catches SSE data
arriving during the connection setup window. (Same deferred render CAUSE 0 fixed
— it must use the msg fallback.)

## Init Sequence (reference)
newChat()→activeConvId=null → initActiveTasks()→loadConversationsFromServer +
loadConversationMessages → connectToTask (fire-and-forget; twStart, seeds buf) →
SSE yields → loadConversation → showStreamingUIForConv → SSE deltas → twUpdate.

