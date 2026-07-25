/* ═══════════════════════════════════════════════════════════════════
   core/conv_reducers.js — pure conversation reducers.

   Extracted from core/conversations.js (pt_3879f00e sub-part 2, slice 1):
   the five stateless helpers historically at the TOP of that file are
   now their own leaf module. Zero runtime state, zero side effects at
   load time — every read of an external symbol (conversations,
   autoTranslate, t) is done at CALL time, so this file can safely
   load BEFORE the heavier conversations.js module.

   Symbols:
     convAutoTranslate(conv)            — SEND-time (frozen) auto-translate flag
     convAutoTranslateEffective(conv)   — LIVE (retro-open) auto-translate flag
     assistantTailIsPriorTurn(msg, tid) — SSE-connect prior-turn detector
     pollWriteWouldClobberSettledTail(msg, taskId, data)
                                        — poll-fold monotonic-writer guard
     convTitleById(cid)                 — id → human title lookup
     _mergeTerminalTurnFields(lm, sm)   — terminal turn-metadata field list
                                        — single source of truth (apiRounds
                                          upgrade-if-longer, rest fill-if-missing)

   This file is concatenated into the core bundle BEFORE
   core/conversations.js (guarded by _BUNDLE_FILES ordering +
   tests/test_frontend_conv_reducers_extracted.py).
   ═══════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════════
   Canonical auto-translate decision (frontend single source of truth).

   The per-conversation `autoTranslate` flag historically read with mixed
   `!== undefined ? : true/false` fallbacks across ~8 trigger sites, which —
   together with the backend's own divergent defaults — made auto-translate
   fire unpredictably. This helper expresses the ONE default (OPT-IN / OFF,
   matching the backend `lib.conv_config.resolve_auto_translate` and the
   `AUTO_TRANSLATE_DEFAULT = False` constant) so every frontend trigger path
   agrees. Pass the conversation object; an explicit per-conv value always
   wins, otherwise the global toolbar flag, otherwise OFF.
   ═══════════════════════════════════════════════════════════════════ */
function convAutoTranslate(conv) {
  if (conv && conv.autoTranslate !== undefined) return !!conv.autoTranslate;
  if (typeof autoTranslate !== 'undefined' && autoTranslate !== undefined) return !!autoTranslate;
  return false;
}
if (typeof window !== 'undefined') window.convAutoTranslate = convAutoTranslate;

/* ═══════════════════════════════════════════════════════════════════
   Canonical "does this assistant tail belong to a PRIOR, already-settled
   turn?" reducer (frontend single source of truth).

   When a task's SSE stream connects (live connect in ui/sse_pipeline.js) or
   is reconnected on startup (main/main_init_tasks.js Case A), the tail
   assistant message might be the PREVIOUS, completed turn rather than an
   empty placeholder for the incoming turn. Streaming into it replays the old
   turn's content into the new bubble ("上一轮对话又重新流式吐出"). The fix is
   to push a fresh placeholder — but ONLY when the tail is genuinely a prior
   turn.

   This is NOT lifecycle INFERENCE (the retired ghost-classifier anti-pattern):
   the decision reads only BACKEND-ISSUED FACTS already stamped on the message —
   `_taskId` (the task→msg bind set from the SSE `state` event / poll payload;
   the backend even keys segment recovery on it in reconcile.py) and
   `finishReason` (from the done/poll payload). It is a pure equality/presence
   reducer over those facts, which is exactly what the front/back-contract
   invariant PRESCRIBES for placement decisions ("use a server-assigned stable
   id, never transient client state").

   Historically the identical predicate was copy-pasted at the two connect
   sites and could drift (one comment aspired to a `_doneAt` check the code
   never had). Centralising it here removes that drift. Endpoint-mode gating is
   deliberately LEFT at each call site — the two contexts guard it differently
   (outer `_epIteration` scan vs inline `_isEndpointReview` flags) and both are
   correct for their path.

   @param {object} msg - the candidate tail message (may be undefined).
   @param {string} activeTaskId - the task now connecting/reconnecting.
   @returns {boolean} true iff `msg` is an assistant tail owned by a different
     task, or already carries a terminal `finishReason` — i.e. a prior turn a
     fresh placeholder must be pushed ahead of.
   ═══════════════════════════════════════════════════════════════════ */
function assistantTailIsPriorTurn(msg, activeTaskId) {
  if (!msg || msg.role !== 'assistant') return false;
  const _staleTaskId = !!(msg._taskId && msg._taskId !== activeTaskId);
  const _isCompletedTurn = !!msg.finishReason;
  return _staleTaskId || _isCompletedTurn;
}
if (typeof window !== 'undefined') window.assistantTailIsPriorTurn = assistantTailIsPriorTurn;

/* ═══════════════════════════════════════════════════════════════════
   pollWriteWouldClobberSettledTail(msg, polledTaskId, data) — P1b flicker
   guard. Single source of truth for "may this poll / Case-B recovery snapshot
   OVERWRITE the trailing assistant message's content?".

   The flicker (conv mrnee15nzqnoej): a turn interrupted by a server crash has
   NO persisted terminal metadata, so several recovery writers — the SSE
   cold-replay `state`, the `_pollFallback` loop, and startup Case-B — each
   recompute the tail content from a DIFFERENT fold source (event-log fold vs
   the 5 s checkpoint) and repaint. When the two folds are similar length,
   neither wins decisively and the bubble visibly swaps back and forth.

   The fix is a monotonic, single-writer-wins rule for a SETTLED tail:
   once the tail carries a terminal `finishReason` (the turn is settled —
   interrupted / stop / …), a later poll snapshot may only be adopted when it
   STRICTLY GROWS the content; an equal-or-shorter variant (the competing fold)
   is rejected so it can never swap the displayed text. A snapshot from a
   DIFFERENT task than the one that owns the tail is likewise rejected — a
   stale/superseded task must not rewrite a settled turn.

   Returns true when the write must be SUPPRESSED (would clobber). A live,
   not-yet-settled tail (no finishReason) is never suppressed — normal
   streaming/growth flows through untouched.

   @param {object} msg - trailing assistant message (may be undefined).
   @param {string} polledTaskId - the task id this poll snapshot is FOR.
   @param {object} data - the poll/recovery payload ({content, status, ...}).
   @returns {boolean} true iff adopting `data.content` would clobber a settled tail.
   ═══════════════════════════════════════════════════════════════════ */
function pollWriteWouldClobberSettledTail(msg, polledTaskId, data) {
  if (!msg || msg.role !== 'assistant') return false;
  const settled = !!msg.finishReason;
  if (!settled) return false;                 // live turn — allow normal writes
  // A snapshot for a different task than the settled tail owns must not rewrite it.
  if (msg._taskId && polledTaskId && msg._taskId !== polledTaskId) return true;
  const newContent = (data && typeof data.content === 'string') ? data.content : null;
  if (newContent == null) return false;       // no content in payload — nothing to clobber
  const oldLen = (msg.content || '').length;
  // Adopt ONLY a strict growth; equal/shorter competing fold is suppressed so
  // the displayed content cannot oscillate between two variants.
  return newContent.length <= oldLen;
}
if (typeof window !== 'undefined') window.pollWriteWouldClobberSettledTail = pollWriteWouldClobberSettledTail;

/* ═══════════════════════════════════════════════════════════════════
   convTitleById(cid) — resolve a conversation id to its human-readable TITLE.

   Peer/operator surfaces (the queued-message bar, the project_message /
   project_intervene delivery card) carry a bare conversation id — often the
   TRUNCATED 8-char display form (`mradmzmd`) — where a user expects a title.
   A raw id is meaningless to a human, so this is the single frontend seam that
   maps an id → title. It matches on the full id first, then by unique prefix
   (so an 8-char id still resolves against the loaded `conversations` list),
   and falls back to a localized "Untitled chat" — NEVER a bare id.

   Returns '' when `cid` is empty. Best-effort: reads the in-memory
   `conversations` list only (no fetch), so it degrades to the fallback label
   when the conversation isn't loaded rather than blocking on the network.
   ═══════════════════════════════════════════════════════════════════ */
function convTitleById(cid) {
  const _t = (typeof t === 'function') ? t : (k => k);
  if (!cid) return '';
  try {
    if (typeof conversations !== 'undefined' && Array.isArray(conversations)) {
      let hit = conversations.find(c => c && c.id === cid);
      if (!hit) {
        // Prefix match (the id may be the 8-char display form). Accept only an
        // unambiguous single match — never guess between two conversations.
        const pre = conversations.filter(c => c && c.id && c.id.indexOf(cid) === 0);
        if (pre.length === 1) hit = pre[0];
      }
      if (hit && hit.title && String(hit.title).trim()) return String(hit.title).trim();
    }
  } catch (e) { if (typeof console !== 'undefined') console.debug('[convTitleById] lookup failed', e); }
  return _t('toast.untitledConv');
}
if (typeof window !== 'undefined') window.convTitleById = convTitleById;

/* ═══════════════════════════════════════════════════════════════════
   convAutoTranslateEffective(conv) — resolver for the ON-OPEN / ON-ACTIVATE
   retro-translate decision (a FINISHED message that still has no translation).

   The per-conversation `autoTranslate` is FROZEN at send-time so a mid-task
   toggle can't change an in-flight run (the cross-talk fix — see
   .tofu/skills/finishstream-global-autotranslate-bug.md). That freeze is right
   for the live send/regenerate path, but it must NOT permanently veto the
   user's CURRENT intent for an already-generated message: a conversation
   frozen OFF could otherwise never be auto-translated even after the global
   toggle is turned ON, leaving an old reply demanding a manual click forever
   (the reported bug). So for THIS decision the LIVE global toggle wins when
   it's ON; otherwise fall back to the frozen per-conv value (an explicit
   per-conv ON is still honored). The live send/regenerate/in-flight paths keep
   using `convAutoTranslate` (the frozen value) unchanged.
   ═══════════════════════════════════════════════════════════════════ */
function convAutoTranslateEffective(conv) {
  if (typeof autoTranslate !== 'undefined' && autoTranslate) return true;
  return convAutoTranslate(conv);
}
if (typeof window !== 'undefined') window.convAutoTranslateEffective = convAutoTranslateEffective;

/* ═══════════════════════════════════════════════════════════════════
   _mergeTerminalTurnFields(localMsg, serverMsg) — THE single source of
   truth for the terminal turn-metadata field list.

   The bug class this kills (conv mrzutwddkeuw0n, 2026-07-25): every
   keep-local merge site used to hand-enumerate the fields it copied from
   the server's settled message, and every hand-written list predated the
   terminal cost-accounting fields. A locally-cached mid-stream message
   then half-upgraded on a merge — usage showed, cost lazily computed —
   while the finish bar's per-round table (needs apiRounds), the popover
   Task ID row (needs _taskId) and the key-tail route tag (needs
   apiRounds[-1].usage._dispatch) NEVER rendered. Hand-copying the list a
   third/fourth time is exactly how it kept drifting; it lives HERE once.

   Semantics (identical at every call site):
     • apiRounds — upgrade-if-longer: adopt the server's list ONLY when
       the local copy is missing or SHORTER (a mid-stream local may hold
       a partial round list; never downgrade it).
     • everything else — fill-if-missing: adopt only fields the local
       message lacks (never clobber a value already present).

   Call-site notes:
     • core/conversations.js (loadConversationMessages MERGE_ACTIVE_TASK)
       routes its whole per-index merge loop through this.
     • core/cross_tab_sync.js (_verifyActiveConvFromServer Case 2) calls
       it OUTSIDE the content-growth gate — a settled turn's fields must
       land even when nothing grew — and keeps its deliberate server-wins
       lines for content/thinking/toolRounds inside the gate.
     • main/main_init_tasks.js (initActiveTasks Case B / Case F) calls it
       AFTER its deliberate server-wins lines (the poll payload is
       terminal-authoritative), so those no-op here and only the
       previously-missing fields land. Case B adapts the poll payload's
       task id onto the source's `_taskId` first (the wire carries it as
       `taskId`/`id`, not `_taskId`).

   Pure reducer over two message dicts; reads nothing else, so it is
   load-order-safe for all consumers (every call is at runtime, never at
   module-load time). Returns the number of fields filled/upgraded so
   callers can gate their changed/repaint flag.
   ═══════════════════════════════════════════════════════════════════ */
function _mergeTerminalTurnFields(lm, sm) {
  if (!lm || !sm || typeof lm !== 'object' || typeof sm !== 'object') return 0;
  let n = 0;
  if (Array.isArray(sm.apiRounds)
      && (!Array.isArray(lm.apiRounds) || sm.apiRounds.length > lm.apiRounds.length)) {
    lm.apiRounds = sm.apiRounds;
    n++;
  }
  const _TERMINAL_FILL = (
    'finishReason usage model _taskId cost provider_id preset thinkingDepth ' +
    'modifiedFiles modifiedFileList fallbackModel fallbackFrom fallbackReason fallbackKind'
  ).split(' ');
  for (const f of _TERMINAL_FILL) {
    if (sm[f] != null && lm[f] == null) { lm[f] = sm[f]; n++; }
  }
  return n;
}
if (typeof window !== 'undefined') window._mergeTerminalTurnFields = _mergeTerminalTurnFields;
