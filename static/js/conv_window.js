/* ═══════════════════════════════════════════════════════════════════
   conv_window.js — client half of windowed conversation reads

   Backend (routes/conversations.py get_conv, gated on TOFU_MESSAGES_ROWS_READD
   + a `window` query param) can serve only the TAIL N messages of a long
   conversation from the normalized conversation_messages row store, so
   first-open cost is O(window) not O(history). The response then carries a
   pagination envelope:

       { windowed:true, totalCount, firstLoadedSeq, lastLoadedSeq, hasMore,
         messages:[tail N] }

   This module owns the client side of that contract WITHOUT touching the
   render core:
     • windowParam()        — the ?window=N to request (0/absent = full load).
     • recordWindowState()  — stamp pagination state from a windowed response
                              onto the conv (no-op for a legacy full response).
     • loadEarlier()        — fetch the previous window (before_seq cursor) and
                              PREPEND it to conv.messages, preserving the scroll
                              anchor; re-renders the open conv.
     • wireScrollUpLoader() — attach a scroll-to-top sentinel handler.

   HARD INVARIANT (scale-out rollout): when the backend does NOT return
   windowed:true (flag off, or no window param sent), EVERYTHING here is inert —
   conv.messages is the full array exactly as today, no pagination state is set,
   and the scroll handler short-circuits. Single-box default is byte-identical.
   ═══════════════════════════════════════════════════════════════════ */

/* Requested tail size. 0 disables the window (full load) — the default until a
 *   deployment opts in via window.TOFU_CONV_WINDOW (set from a served config).
 *   Kept conservative; the backend also has its own TOFU_CONV_WINDOW ceiling. */
function _convWindowSize() {
  const n = (typeof window !== 'undefined' && window.TOFU_CONV_WINDOW) || 0;
  const v = parseInt(n, 10);
  return Number.isFinite(v) && v > 0 ? v : 0;
}

/* The ?window= value to attach to a first-open GET, or '' to request the full
 *   array (legacy behavior). Exposed so loadConversationMessages can build its
 *   query without knowing the windowing policy. */
function convWindowParam() {
  const n = _convWindowSize();
  return n > 0 ? String(n) : '';
}

/* Stamp pagination state from a GET response onto the conv. Returns true iff
 *   the response was windowed (so the caller knows earlier messages exist and
 *   must NOT treat conv.messages as the complete history — e.g. skip a
 *   "trim to server length" step). A legacy full response → false, no-op. */
function recordWindowState(conv, data) {
  if (!conv || !data || data.windowed !== true) {
    if (conv) conv._windowed = false;
    return false;
  }
  conv._windowed = true;
  conv._totalCount = data.totalCount;
  conv._firstLoadedSeq = data.firstLoadedSeq;
  conv._lastLoadedSeq = data.lastLoadedSeq;
  conv._hasMoreEarlier = !!data.hasMore;
  return true;
}

/* Whether there are older messages above the loaded window. */
function convHasMoreEarlier(conv) {
  return !!(conv && conv._windowed && conv._hasMoreEarlier);
}

/* Fetch the previous window and PREPEND it. Preserves the scroll anchor so the
 *   viewport does not jump when older content is inserted above. Idempotent /
 *   re-entrant-guarded via conv._loadingEarlier. Returns count prepended. */
async function loadEarlierMessages(convId) {
  const conv = (typeof conversations !== 'undefined')
    ? conversations.find((c) => c.id === convId) : null;
  if (!conv || !convHasMoreEarlier(conv) || conv._loadingEarlier) return 0;
  if (typeof conv._firstLoadedSeq !== 'number') return 0;
  conv._loadingEarlier = true;

  const container = document.getElementById('chatInner');
  const prevHeight = container ? container.scrollHeight : 0;
  const prevTop = container ? container.scrollTop : 0;

  try {
    const n = _convWindowSize() || 60;
    const data = await Api.conversations.get(convId, {
      query: { window: String(n), before_seq: String(conv._firstLoadedSeq) },
    });
    if (!data || !Array.isArray(data.messages) || data.messages.length === 0) {
      conv._hasMoreEarlier = false;
      return 0;
    }
    const earlier = data.messages;
    conv.messages = earlier.concat(conv.messages || []);
    // advance the cursor + hasMore from this page's envelope
    if (typeof data.firstLoadedSeq === 'number') conv._firstLoadedSeq = data.firstLoadedSeq;
    conv._hasMoreEarlier = !!data.hasMore;

    if (typeof activeConvId !== 'undefined' && activeConvId === convId
        && typeof renderChat === 'function') {
      renderChat(conv, false);
      // re-pin the scroll anchor: keep the previously-top message in place by
      // restoring scrollTop + (newHeight - oldHeight).
      if (container) {
        const newHeight = container.scrollHeight;
        container.scrollTop = prevTop + (newHeight - prevHeight);
      }
    }
    return earlier.length;
  } catch (e) {
    console.warn('[conv-window] loadEarlier failed for %s: %s', convId.slice(0, 8), e && e.message);
    return 0;
  } finally {
    conv._loadingEarlier = false;
  }
}

/* Attach a scroll-to-top loader on the chat container. When the user scrolls
 *   near the top AND the active conv has earlier messages, fetch+prepend them.
 *   Idempotent; safe to call on every conv open. No-op if not windowed. */
let _scrollUpWired = false;
function wireConvWindowScrollLoader() {
  if (_scrollUpWired) return;
  const container = document.getElementById('chatInner');
  if (!container) return;
  _scrollUpWired = true;
  const THRESHOLD = 120;  // px from the top that triggers a prefetch
  container.addEventListener('scroll', () => {
    if (container.scrollTop > THRESHOLD) return;
    const cid = (typeof activeConvId !== 'undefined') ? activeConvId : null;
    if (!cid) return;
    const conv = (typeof conversations !== 'undefined')
      ? conversations.find((c) => c.id === cid) : null;
    if (convHasMoreEarlier(conv) && !conv._loadingEarlier) {
      loadEarlierMessages(cid);
    }
  }, { passive: true });
}

if (typeof window !== 'undefined') {
  window.convWindowParam = convWindowParam;
  window.recordWindowState = recordWindowState;
  window.convHasMoreEarlier = convHasMoreEarlier;
  window.loadEarlierMessages = loadEarlierMessages;
  window.wireConvWindowScrollLoader = wireConvWindowScrollLoader;
}
