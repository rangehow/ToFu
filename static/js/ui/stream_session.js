/* stream_session.js — the live-stream session state (RENDER_CONTRACT Phase 3.5 §7).
 *
 * ONE per-convId runtime slice for an in-flight stream. It holds the state
 * that is REAL but must NEVER live in the message document (the SSOT) —
 * runtime facts about "what the model is doing right now", not turn content:
 *
 *   { phase: {phase, detail, detailKey, detailArgs, tools, toolContext, round} | null }
 *
 * WRITERS (the only allowed ones):
 *   - the SSE PHASE event handler (sse_pipeline.js) — live events AND
 *     warm-reconnect replayed events share that one dispatch path, so a
 *     warm reconnect re-seeds the session from the server event log
 *     (evidence: lib/chat_dispatch.py:636 replays task['events'][cursor:],
 *     which includes PHASE events — docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §7.4).
 *   - the poll fallback (sse_poll_fallback.js) — server truth for phase.
 *   - VU streaming deltas (streaming_render.js) — phase clear/set mirror.
 * READERS:
 *   - health_stream_timer._streamFrameArg (the updateStreamingUI frame) and
 *     _updateStreamTimerUI (the liveness banner) — the ONLY paint readers.
 *
 * Presence semantics: an entry EXISTS only while its stream is live —
 * clearStreamSession() is called by every stop/teardown path (twStop,
 * streaming-bubble removal). For "is a stream live on this conv" prefer
 * activeStreams.has(convId); the session answers "what is the stream DOING".
 *
 * This REPLACES streamBufs (deleted in the §7 retirement): content, thinking
 * and toolRounds now project straight from the message document; phase —
 * which has no document home — lives here.
 */
/* `var` (not const/let): the production bundle concatenates all modules into
 * one script scope, but the JSDOM test harness evaluates each file via a
 * SEPARATE indirect eval — only `var` + function declarations leak onto the
 * global object there. Same pattern the other shared registries rely on. */
var streamSessions = new Map();

/** Return the live session slice for convId, lazily creating a blank one.
 *  Blank = { phase: null } — a fresh cursorless reconnect shows the default
 *  waiting pulse until the next live PHASE event (accepted transient, see
 *  plan §7.4 verdict-C semantics). */
function getStreamSession(convId) {
  let s = streamSessions.get(convId);
  if (!s) {
    s = { phase: null };
    streamSessions.set(convId, s);
  }
  return s;
}

/** Write the phase for a LIVE stream only. Mirrors the old buffer's
 *  create-on-twStart lifecycle: a phase event arriving after twStop (or for
 *  a conv with no live stream) is dropped instead of resurrecting a session
 *  the paint readers would treat as "stream exists". */
function setStreamPhase(convId, phase) {
  if (!streamSessions.has(convId)
      && !(typeof activeStreams !== 'undefined' && activeStreams.has(convId))) {
    return;
  }
  getStreamSession(convId).phase = phase;
}

/** Drop the session slice (stream stop / teardown / bubble removal). */
function clearStreamSession(convId) {
  streamSessions.delete(convId);
}
