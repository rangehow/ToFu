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
 * with cross_tab_sync.js's _onConvNotifyPush semantics.
 *
 * pt_ab42421158214591: normalize BOTH sides to string. The server-side
 * AuthContext.user_id is a str (see lib/api_keys/_context.py); the
 * legacy write-path notify frames still pass DEFAULT_USER_ID=1 (int)
 * while snapshots from a real tenant now carry the str user_id.
 * String coercion makes ``1 == '1'`` behave as identity, so a client
 * whose _currentUserId is '1' still accepts frames carrying userId=1,
 * and vice versa — otherwise, when auth lands and _currentUserId
 * gets set, single-user tabs would silently reject their own
 * server's DEFAULT_USER_ID=1 frames. Empty string / null / undefined
 * on EITHER side means "unscoped" and everything passes (matching
 * the pre-auth default). */
function _frameIsOurs(userId) {
  const myRaw = (typeof window !== 'undefined' &&
                 typeof window._currentUserId !== 'undefined' &&
                 window._currentUserId !== null) ? window._currentUserId : null;
  if (myRaw === null) return true;
  if (userId === undefined || userId === null) return true;
  const my = String(myRaw);
  const theirs = String(userId);
  if (my === '' || theirs === '') return true;
  return theirs === my;
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

/* ═══════════════════════════════════════════════════════════════════════════
   P5 — sync-drift probe (owner constraint #4)

   The two SSOT halves above (busy Set + rev) converge ONLY if every notify
   frame lands. A dropped frame is invisible locally — the dot looks right
   but ``_serverRev`` never catches up. The probe closes that hole by
   REPORTING a compact digest every 60s; the server compares it against the
   registry + ``conversations.rev`` and WARN-logs any divergence.

   Digest per conv covers BOTH (constraint #4):
     taskIds — sorted array from ``_authoritativeActiveTaskIds``
     rev     — ``_serverRev`` (the last rev this tab converged to)
   A conv with neither authoritative marker contributes nothing.
   ══════════════════════════════════════════════════════════════════════════ */
function buildSyncDigest(conversations) {
  const out = [];
  try {
    if (!Array.isArray(conversations)) return out;
    for (const conv of conversations) {
      if (!conv || !conv.id) continue;
      const hasAuth = Array.isArray(conv._authoritativeActiveTaskIdsRev);
      const rev = (typeof conv._serverRev === 'number') ? conv._serverRev : null;
      if (!hasAuth && rev === null) continue;
      const set = conv._authoritativeActiveTaskIds;
      const taskIds = (set && set.size) ? Array.from(set).sort() : [];
      out.push({ convId: conv.id, taskIds: taskIds, rev: rev });
    }
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog(`[conv-state-reducer] buildSyncDigest failed: ${e && e.message}`, 'warn');
    }
  }
  return out;
}

/* POST the digest. Returns the parsed {ok, checked, divergences} or null.
 * A non-empty divergences list is logged client-side too so a developer
 * watching the browser console sees the same drift the server WARNs about. */
async function reportSyncDigest(conversations) {
  const digests = buildSyncDigest(conversations);
  if (!digests.length) return null;
  try {
    if (typeof Api === 'undefined' || !Api.conversations ||
        typeof Api.conversations.reportSyncDigest !== 'function') return null;
    const resp = await Api.conversations.reportSyncDigest(digests);
    const divs = resp && resp.divergences;
    if (Array.isArray(divs) && divs.length && typeof console !== 'undefined') {
      console.warn('[conv-state] sync drift: server reports %d divergence(s): %o',
                   divs.length, divs);
    }
    return resp;
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog(`[conv-state-reducer] digest report failed: ${e && e.message}`, 'warn');
    }
    return null;
  }
}

/* Start the 60s probe. Idempotent; resolves the conversations array lazily
 * each tick (a caller may pass a getter, or the ambient global is used). */
let _syncDriftProbeTimer = null;
function startSyncDriftProbe(conversationsRef, intervalMs) {
  if (_syncDriftProbeTimer || typeof setInterval !== 'function') return;
  const iv = (typeof intervalMs === 'number' && intervalMs > 0) ? intervalMs : 60000;
  _syncDriftProbeTimer = setInterval(() => {
    try {
      const convs = (typeof conversationsRef === 'function') ? conversationsRef()
        : (typeof conversations !== 'undefined' ? conversations : null);
      reportSyncDigest(convs);
    } catch (e) { /* probe must never throw into the timer queue */ }
  }, iv);
}

/* ── Publish under both bare + window scopes so the JSDOM harness's
 *   eval() and the browser's script tag both see them. */
if (typeof window !== 'undefined') {
  window.applyRunningTaskIdsFrame = applyRunningTaskIdsFrame;
  window.applyConvStateSnapshot = applyConvStateSnapshot;
  window.computeConvBusy = computeConvBusy;
  window.pickAuthoritativeTaskIdForReconnect = pickAuthoritativeTaskIdForReconnect;
  window.buildSyncDigest = buildSyncDigest;
  window.reportSyncDigest = reportSyncDigest;
  window.startSyncDriftProbe = startSyncDriftProbe;
  /* The REFERENCE multi-user gate. cross_tab_sync.js (_onConvNotifyPush,
   * _onFoldersChangedPush) and conv_sync_push.js (_onConvSyncPush) implement
   * the same normalization inline (they run before this file in bundle order
   * for two of the three, so they can't call it); exported so a test can
   * drive the canonical semantics directly and pin all four in agreement. */
  window._frameIsOurs = _frameIsOurs;
}
