/* ═══════════════════════════════════════════════════════════════════════════
   pt_conv_state_ssot P2 — server-authoritative conv busy state reducer
   ═══════════════════════════════════════════════════════════════════════════

   Owner-approved architecture (board pt_e1c4693341b24730, 2026-07-24).

   The disease this cures ── multiple concurrent sources of "is conv X
   busy" (activeStreams / conv.activeTaskId / settings.activeTaskId /
   /chat/active) drift under multi-device usage. Symptom: phone shows
   3 conversations generating, PC sidebar shows fewer + click-through
   renders a half-baked "finish" tag until F5.

   Design ── task registry on the server is the ONLY SSOT. Every
   authoritative mutation ships two new fields on the notify frame
   (P1 33d55537):

     runningTaskIds:    [str, ...]                — snapshot of the registry
     runningTaskIdsRev: [monotonic_ns, replica_id] — strict lex ordering

   And every fresh PushClient connection to notify:* gets a one-shot
   conv_state_snapshot frame (P1.5 3954bd52) covering EVERY running conv
   so the client boots with the current fleet-wide state, not "whatever
   settings.activeTaskId was persisted last".

   This module is the client-side reducer. Bifurcated fields (owner
   hard constraint #2):

     conv.activeTaskId               ← LOCAL OPTIMISTIC single value.
                                       Written by ~20 sender/regen/edit/
                                       continue/reconnect sites (unchanged).
                                       Reflects THIS tab's own send. Kept
                                       as-is for backward compat with the
                                       existing writer contract.
     conv._authoritativeActiveTaskIds
                                     ← SERVER-AUTHORITATIVE Set. Written
                                       ONLY by this reducer, from notify
                                       frames + connect snapshots.
     conv._authoritativeActiveTaskIdsRev
                                     ← [ns, replica_id] high-water mark.
                                       An incoming frame must exceed this
                                       (strict lex compare) to be applied
                                       — otherwise it is a stale reorder
                                       and dropped.

   ``convIsBusy`` reads the UNION: ``activeStreams.has(id) ||
   activeTaskId || _authoritativeActiveTaskIds size > 0``. The two
   fields NEVER merge into one — that would resurrect the "two
   truth sources fighting on the same key" mistake this whole epic is
   fixing (owner constraint #2, verbatim).

   Task registry is the ONLY physical SSOT (owner constraint #3): this
   reducer NEVER writes settings.activeTaskId, NEVER calls
   saveConversations. Only the sender/regen/edit sites that write
   ``conv.activeTaskId`` optimistically also persist settings — that
   contract is intact.

   ══════════════════════════════════════════════════════════════════════════ */

/* Compare two ``[ns, replica_id]`` tuples with strict-greater lex order.
 * A missing prior is treated as -infinity so the first frame ever always
 * applies. Non-array / malformed input is defensively treated as -infinity. */
function _revStrictlyGreater(next, prior) {
  if (!Array.isArray(next) || next.length !== 2) return false;
  if (!Array.isArray(prior) || prior.length !== 2) return true;
  if (next[0] !== prior[0]) return next[0] > prior[0];
  return String(next[1]) > String(prior[1]);
}

/* Multi-user gate: drop a frame whose ``userId`` is not ours. When our
 * identity is unset (single-user default) every frame is ours. Shared
 * with cross_tab_sync.js's _onConvNotifyPush semantics. */
function _frameIsOurs(userId) {
  const my = (typeof window !== 'undefined' &&
              typeof window._currentUserId !== 'undefined' &&
              window._currentUserId !== null) ? window._currentUserId : null;
  if (my === null) return true;
  if (userId === undefined) return true;
  return userId === my;
}

/* Consume a per-conv notify frame's server-authoritative half:
 *
 *   frame = { convId, runningTaskIds: [...], runningTaskIdsRev: [ns, rid],
 *             userId? }
 *
 * Best-effort: an unknown ``convId`` is a no-op (a genuinely new conv
 * lands via the list-refresh path). A malformed rev drops the frame.
 * NEVER writes settings; NEVER calls saveConversations.
 */
function applyRunningTaskIdsFrame(conversations, frame) {
  try {
    if (!frame || !Array.isArray(conversations)) return;
    if (!_frameIsOurs(frame.userId)) return;
    const convId = frame.convId;
    if (!convId) return;
    const conv = conversations.find((c) => c && c.id === convId);
    if (!conv) return;
    const nextRev = frame.runningTaskIdsRev;
    const priorRev = conv._authoritativeActiveTaskIdsRev;
    if (!_revStrictlyGreater(nextRev, priorRev)) return;
    const tids = Array.isArray(frame.runningTaskIds) ? frame.runningTaskIds : [];
    conv._authoritativeActiveTaskIds = new Set(tids);
    conv._authoritativeActiveTaskIdsRev = nextRev;
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog(`[conv-state-reducer] frame apply failed: ${e && e.message}`, 'warn');
    }
  }
}

/* Consume a connect-time full snapshot:
 *
 *   frame = { convs: { convId: {runningTaskIds, runningTaskIdsRev} }, userId? }
 *
 * Snapshot semantics: convs PRESENT in the frame are UPDATED (still
 * gated per-conv on rev); convs ABSENT are CLEARED. A server that no
 * longer has conv-X in its running set must extinguish the local dot
 * — that is the whole point of receiving a fresh full projection at
 * connect time. Best-effort per conv; a bad rev drops that conv's
 * update but does NOT abort the whole snapshot.
 */
function applyConvStateSnapshot(conversations, frame) {
  try {
    if (!frame || !Array.isArray(conversations)) return;
    if (!_frameIsOurs(frame.userId)) return;
    const convs = (frame && typeof frame.convs === 'object' && frame.convs) || {};
    for (const conv of conversations) {
      if (!conv || !conv.id) continue;
      const entry = convs[conv.id];
      if (entry) {
        applyRunningTaskIdsFrame(conversations, {
          convId: conv.id,
          runningTaskIds: entry.runningTaskIds,
          runningTaskIdsRev: entry.runningTaskIdsRev,
          userId: frame.userId,
        });
      } else if (conv._authoritativeActiveTaskIds &&
                 conv._authoritativeActiveTaskIds.size > 0) {
        /* CLEARED: not present in snapshot → server says NOT running.
         * The snapshot is more recent than any prior notify frame by
         * construction (it was built at connect time from the current
         * registry), so we don't rev-gate the clear — we advance the
         * rev to a fresh sentinel so no stale notify can un-clear it. */
        conv._authoritativeActiveTaskIds = new Set();
        /* Advance the rev to now-ish so a stale frame can't resurrect
         * the cleared state. Uses Date.now() * 1e6 as an ns proxy —
         * the server's monotonic_ns is process-relative, so on the
         * client we synthesize a rev whose ns dwarfs any prior server
         * one to guarantee monotonicity of the CLEAR event. */
        conv._authoritativeActiveTaskIdsRev = [Date.now() * 1e6, 'snapshot-clear'];
      }
    }
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog(`[conv-state-reducer] snapshot apply failed: ${e && e.message}`, 'warn');
    }
  }
}

/* Union predicate — the single source-of-truth for "is conv X busy":
 *   - Local stream in THIS tab → busy (unchanged from before).
 *   - Local optimistic activeTaskId → busy (unchanged, our own send).
 *   - Server-authoritative Set non-empty → busy (NEW: sibling device
 *     is generating, or this tab is receiving snapshot on reconnect).
 *   - Prefix scan for branch/compound task IDs → busy (unchanged from
 *     ui/conversation_list.js:convIsBusy).
 *
 * ``activeStreamsRef`` is injected so this is a pure function testable
 * without the ambient global. The shipped ui/conversation_list.js's
 * convIsBusy delegates here. */
function computeConvBusy(conv, activeStreamsRef) {
  if (!conv) return false;
  const streams = activeStreamsRef ||
                  (typeof activeStreams !== 'undefined' ? activeStreams : null);
  if (streams && streams.has && streams.has(conv.id)) return true;
  if (conv.activeTaskId) return true;
  if (conv._authoritativeActiveTaskIds &&
      conv._authoritativeActiveTaskIds.size > 0) return true;
  if (streams && streams.keys) {
    const prefix = conv.id + ':';
    for (const k of streams.keys()) {
      if (typeof k === 'string' && k.startsWith(prefix)) return true;
    }
  }
  return false;
}

/* Reconnect target picker — when the click-open reconnect path needs a
 * task id to attach to, prefer THIS tab's own optimistic ``activeTaskId``
 * (that is our own send, natural target). Fall back to any tid in the
 * authoritative Set — that covers the phone-vs-PC case: PC has never
 * been the origin of the task, so activeTaskId is null; the sidebar dot
 * lit because the authoritative Set has the phone-originated tid, and
 * clicking through to view lets THIS tab attach to the running SSE.
 * Return null when both are empty. */
function pickAuthoritativeTaskIdForReconnect(conv) {
  if (!conv) return null;
  if (conv.activeTaskId) return conv.activeTaskId;
  const set = conv._authoritativeActiveTaskIds;
  if (set && set.size > 0) {
    /* Deterministic pick: iteration order of a Set is insertion order,
     * which mirrors the server's registry-dict iteration when the
     * snapshot was built. Any tid works — reconnecting to one attaches
     * the SSE that carries all activity for the conv. */
    return set.values().next().value;
  }
  return null;
}

/* ── Publish under both bare + window scopes so the JSDOM harness's
 *   eval() and the browser's script tag both see them. */
if (typeof window !== 'undefined') {
  window.applyRunningTaskIdsFrame = applyRunningTaskIdsFrame;
  window.applyConvStateSnapshot = applyConvStateSnapshot;
  window.computeConvBusy = computeConvBusy;
  window.pickAuthoritativeTaskIdForReconnect = pickAuthoritativeTaskIdForReconnect;
}
