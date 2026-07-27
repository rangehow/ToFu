/* ═══════════════════════════════════════════════════════════════════
   core/conversations.js — extracted from core.js (split 2026-05-28)

   Conversation persistence: saveConversations (debounced), syncConversationToServer, loadConversationsFromServer, loadConversationMessages, forceRecoverFromServer, auditConversations, recoverAll.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════════
   Reducer helpers (convAutoTranslate / assistantTailIsPriorTurn /
   pollWriteWouldClobberSettledTail / convTitleById /
   convAutoTranslateEffective) extracted 2026-07-25 to
   core/conv_reducers.js (pt_3879f00e sub-part 2, slice 1). They
   load BEFORE this file via _BUNDLE_FILES so downstream reads of
   the bare names still resolve at runtime.
   ═══════════════════════════════════════════════════════════════════ */

function saveConversations(changedConvId) {
  const now = Date.now();
  if (changedConvId) {
    const c = conversations.find((x) => x.id === changedConvId);
    /* ── Don't bump updatedAt during periodic streaming saves ──
     * When multiple conversations stream simultaneously, each calls
     * saveConversations every ~3s.  Bumping updatedAt each time makes
     * them compete for the top sort position, causing the sidebar to
     * flicker as conversations constantly swap order.
     * Fix: only bump updatedAt when the conversation is NOT actively
     * streaming.  The timestamp is already set when the user sends a
     * message (before streaming starts) and again in finishStream()
     * (after activeStreams.delete, so the guard passes). */
    if (c && !activeStreams.has(changedConvId)) c.updatedAt = now;
  }
  /* ★ DB-first: in-memory array is truth for this tab, DB across tabs/sessions. */
  conversations.sort(_convSorter);
  _broadcastToTabs("conv_saved", { convId: changedConvId });

  /* ── Throttled sidebar refresh during streaming ──
   * During active streaming, saveConversations is called every ~3s but
   * renderConversationList was NEVER called — so the sidebar sort order
   * and streaming dot were stale until the stream finished or user clicked
   * another conversation.  We now refresh the sidebar on a 2s throttle
   * so users see the active conversation bubble to the top promptly. */
  if (changedConvId && activeStreams.size > 0) {
    const _now = Date.now();
    const _sc = /** @type {any} */ (saveConversations);
    if (!_sc._lastSidebarRefresh || _now - _sc._lastSidebarRefresh > 2000) {
      _sc._lastSidebarRefresh = _now;
      requestAnimationFrame(() => {
        if (typeof renderConversationList === 'function') renderConversationList();
      });
    }
  }
}


/* _hydrateImageBase64 extracted 2026-07-26 to core/conv_image_hydrate.js
   (pt_3879f00e sub-part 2 slice 4). Loaded via _BUNDLE_FILES BEFORE this
   file so the two remaining CALL sites (loadConversationMessages
   initial-hydration branch, and its post-refresh path) still resolve the
   bare name at runtime via bundle-level window scope. */


// ── Debounced sync: coalesces rapid settings toggles into one request ──
// finishStream() calls syncConversationToServer() directly (immediate).
// Settings/toggle changes call syncConversationToServerDebounced() which
// waits 1.5s for additional changes before firing.
const _syncDebounceTimers = new Map();  // convId → timeoutId
function syncConversationToServerDebounced(conv, delayMs = 1500) {
  const existing = _syncDebounceTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _syncDebounceTimers.set(conv.id, setTimeout(() => {
    _syncDebounceTimers.delete(conv.id);
    syncConversationToServer(conv);
  }, delayMs));
}

/* ═══════════════════════════════════════════════════════════════════
   Persist helpers (_stripUsageTransient / _trimMsgForPersist /
   _serverHasSegmentsLocalLacks / _serverHasTranslationLocalLacks /
   _isErrorOnlyAssistant / _rebaseUnackedTail) + _USAGE_TRANSIENT_KEYS
   constant extracted 2026-07-25 to core/conv_persist_helpers.js
   (pt_3879f00e sub-part 2, slice 3). Loads BEFORE this file via
   _BUNDLE_FILES so downstream bare-name reads inside
   syncConversationToServer + loadConversationMessages still resolve.
   ═══════════════════════════════════════════════════════════════════ */

async function syncConversationToServer(conv, { allowTruncate = false } = {}) {
  try {
    /* Guard: skip sync while actively streaming — the assistant message is
     * incomplete, and uploading it would overwrite the server-side accumulator
     * with a partial snapshot.  finishStream() will trigger sync after done. */
    if (activeStreams.has(conv.id)) {
      debugLog(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} is actively streaming`, 'info');
      return;
    }
    /* Guard: skip sync while POST /chat/send is in flight for this conv.
     * The backend's _chat_send is the sole owner of the first-turn persist
     * (it appends a fresh translated user_msg + writes the row).  Any sync
     * that lands during that window — most commonly the
     * "rescue local-only conv" PUT triggered by loadConversationsFromServer
     * (60s timer / visibilitychange / cross-tab broadcast) — would plant
     * the optimistic untranslated msg as row #0, and chat_send would then
     * append its own translated msg as row #1 → duplicate user message.
     * Cleared by sendMessage()'s finally block. */
    if (conv._sendInFlight) {
      console.info(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} has POST /chat/send in flight `+
        `(backend owns the first-turn persist; rescue PUT here would duplicate the user message)`);
      return;
    }
    /* Guard: never sync a conversation with zero messages to the server.
     * This prevents the race where _saveConvToolState fires before the user
     * message is pushed, overwriting the server with messages:[]. */
    if (!conv.messages || conv.messages.length === 0) {
      console.log(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} has 0 messages (nothing to sync)`);
      return;
    }
    /* Guard: never ACCIDENTALLY overwrite the server with fewer messages (data
     * loss prevention against the async stale-overwrite race — see the
     * `stale-async-sync-overwrite-msg-regression` skill).
     *
     * ★ FIX: honour `allowTruncate`. A caller that passes `allowTruncate:true`
     * has DELIBERATELY reduced conv.messages (a ghost/buried-ghost sweep, a
     * Case-D delete, or an edit/regen truncation) and MUST be allowed to
     * persist the shorter list — otherwise this guard fires FIRST (it ran
     * before consulting the flag) and the removal is swept from the DOM every
     * load but never persisted, so buried ghosts RESURRECT on every reload.
     * The stale-overwrite race path never sets allowTruncate, so the guard
     * still protects it. Mirrors the Layer-1 staleness check below and the
     * backend `allow_truncate` bypass (routes/conversations.py). */
    if (!allowTruncate && conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — local ${conv.messages.length} msgs < server ${conv._serverMsgCount} msgs. ` +
        `This guard prevents overwriting server data, but local changes (including streamed content) will NOT be persisted to server!`);
      /* This is a documented silent-data-loss branch (local streamed content is
       * dropped). Surface it to the server log so "my message vanished" reports
       * are diagnosable instead of invisible. */
      debugLog(`[syncToServer] stale-overwrite guard skipped persist conv=${conv.id.slice(0,8)} local=${conv.messages.length} server=${conv._serverMsgCount}`, 'warn');
      return;
    }
    /* ★ CROSS-TALK DETECTION: check for sudden message count jumps that indicate injection */
    if (conv._lastSyncMsgCount !== undefined && conv.messages.length > conv._lastSyncMsgCount + 3) {
      console.error(
        `[syncToServer] ⛔ MESSAGE COUNT JUMP: conv=${conv.id.slice(0,8)} jumped from ` +
        `${conv._lastSyncMsgCount} to ${conv.messages.length} msgs (+${conv.messages.length - conv._lastSyncMsgCount}) ` +
        `since last sync — possible cross-talk injection! ` +
        `activeConvId=${activeConvId?.slice(0,8)||'null'} ` +
        `activeStreams=[${[...activeStreams.keys()].map(k=>k.slice(0,8)).join(',')}]`
      );
      /* Log the extra messages for forensic analysis */
      for (let i = conv._lastSyncMsgCount; i < conv.messages.length; i++) {
        const m = conv.messages[i];
        console.error(
          `[syncToServer] ⛔ INJECTED MSG #${i}: role=${m.role} ` +
          `contentLen=${(m.content||'').length} taskId=${m._taskId?.slice(0,8)||'N/A'} ` +
          `model=${m.model||'N/A'} timestamp=${m.timestamp}`
        );
      }
    }
    conv._lastSyncMsgCount = conv.messages.length;
    if (conv.messages.length === 0 && conv._needsLoad) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — 0 local messages and _needsLoad=true (not yet loaded from server)`);
      return;
    }
    const lastMsg = conv.messages[conv.messages.length - 1];
    /* ★ CROSS-TALK DETECTION: check if any messages have foreign task IDs or
     *   unexpected model/content patterns that suggest they belong to another conv */
    const _convTaskId = conv.activeTaskId;
    let _foreignMsgCount = 0;
    for (const m of conv.messages) {
      if (m._taskId && _convTaskId && m._taskId !== _convTaskId && m.role === 'assistant') {
        _foreignMsgCount++;
      }
    }
    if (_foreignMsgCount > 0) {
      console.error(
        `[syncToServer] ⛔ CROSS-TALK DETECTED: conv=${conv.id.slice(0,8)} has ${_foreignMsgCount} ` +
        `assistant message(s) with foreign taskId (conv.activeTaskId=${_convTaskId?.slice(0,8)||'null'}). ` +
        `These messages may have been injected from another conversation's SSE stream!`
      );
    }
    console.info(`[syncToServer] conv=${conv.id.slice(0,8)} msgs=${conv.messages.length} lastRole=${lastMsg?.role} ` +
      `contentLen=${lastMsg?.content?.length||0} thinkingLen=${lastMsg?.thinking?.length||0} hasError=${!!lastMsg?.error} ` +
      `activeTaskId=${_convTaskId?.slice(0,8)||'null'} foreignMsgCount=${_foreignMsgCount}`);
    const lightMsgs = conv.messages.map((m) => {
      let r = m;
      if (m.images?.length > 0)
        r = {
          ...r,
          images: m.images.map((img) => {
            const o = { mediaType: img.mediaType, sizeKB: img.sizeKB };
            if (img.url) {
              // Persist the canonical '/api/images/<f>' url unchanged, but the
              // preview is a render src — prefix with apiUrl() so it resolves
              // through the reverse-proxy base path.
              o.url = img.url;
              o.preview = (img.url.charAt(0) === "/" && typeof apiUrl === "function")
                ? apiUrl(img.url) : img.url;
            } else {
              o.preview = (img.preview || "").slice(0, 200) + "...";
            }
            if (img.pdfPage) o.pdfPage = img.pdfPage;
            if (img.pdfTotal) o.pdfTotal = img.pdfTotal;
            if (img.pdfName) o.pdfName = img.pdfName;
            if (img.caption) o.caption = img.caption;
            return o;
          }),
        };
      if (m.pdfTexts?.length > 0)
        r = {
          ...r,
          pdfTexts: m.pdfTexts.map((p) => ({
            name: p.name,
            pages: p.pages,
            textLength: p.textLength,
            isScanned: p.isScanned,
            method: p.method,
            text: p.text || "",
          })),
        };
      /* ★ `_pendingSync` is a CLIENT-ONLY durability marker (set when a send
       *   failed on a poor network so a refresh keeps the message and a retry
       *   re-attempts the PUT). It must NEVER be persisted to the server —
       *   otherwise it echoes back on the next load and could wrongly trigger
       *   the KEEP_LOCAL reconcile. Clone-and-strip (don't mutate the live
       *   message — the local marker stays until this PUT actually succeeds). */
      if (r._pendingSync) { r = { ...r }; delete r._pendingSync; }
      /* ★ Drop transient bloat (usage._wire_fp diagnostics, done-round
       *   _partialOutput) so a client PUT never re-inflates the DB payload
       *   the server-side sanitizer just trimmed. See _trimMsgForPersist. */
      r = _trimMsgForPersist(r);
      return r;
    });
    const settings = {
      preset: conv.model || conv.preset,
      model: conv.model || conv.preset,
      thinkingDepth: conv.thinkingDepth || config.defaultThinkingDepth,
      defaultThinkingDepth: config.defaultThinkingDepth,
      searchMode: conv.searchMode,
      fetchEnabled: conv.fetchEnabled,
      codeExecEnabled: conv.codeExecEnabled,
      browserEnabled: conv.browserEnabled,
      desktopEnabled: conv.desktopEnabled || false,
      memoryEnabled: conv.memoryEnabled !== undefined ? conv.memoryEnabled : true,
      schedulerEnabled: conv.schedulerEnabled || false,
      swarmEnabled: conv.swarmEnabled || false,
      endpointEnabled: conv.endpointEnabled || false,
      autopilotEnabled: conv.autopilotEnabled || false,
      activeFlow: conv.activeFlow || '',
      imageGenEnabled: conv.imageGenEnabled || false,
      imageGenMode: conv.imageGenMode || false,
      imageGenModel: conv.imageGenModel || null,
      humanGuidanceEnabled: conv.humanGuidanceEnabled || false,
      projectPath: conv.projectPath,
      projectPaths: conv.projectPaths || [],
      readOnlyPaths: conv.readOnlyPaths || [],
      autoTranslate: conv.autoTranslate,
      pinned: conv.pinned || false,
      pinnedAt: conv.pinnedAt || 0,
      folderId: conv.folderId || null,
      /* ★ Persist activeTaskId so the server knows which task is associated
       *   with this conversation.  On page reload, initActiveTasks reads this
       *   to recover completed task results even when the SSE stream died. */
      activeTaskId: conv.activeTaskId || null,
      /* ★ Persist last message info so initActiveTasks can detect orphaned user
       *   messages even for _needsLoad shell convs (metadata-only, no messages loaded).
       *   Without this, Case E is skipped for shell convs → orphan stuck forever. */
      lastMsgRole: lastMsg?.role || null,
      lastMsgTimestamp: lastMsg?.timestamp || null,
      /* ★ Preserve the human-only autopilot run-record sidecar across the
       * full-conv PUT. Each record carries the concluded status/reason + the
       * optional close-out report. The PUT rebuilds the entire settings column
       * from this whitelist, so omitting this would clobber a backend-written
       * record on the next sync. The autopilot_run_concluded SSE event (and the
       * disarm response) populate conv.autopilotSummaries BEFORE this sync. */
      ...(conv.autopilotSummaries ? { autopilotSummaries: conv.autopilotSummaries } : {}),
    };
    /* ★ FIX: Pre-send staleness check — if conv.messages grew since lightMsgs
     * was captured (due to sendMessage/startAssistantResponse running while we
     * were computing lightMsgs), this PUT would overwrite newer data.
     * Cancel and let the fresher sync win. */
    if (!allowTruncate && conv.messages.length > lightMsgs.length) {
      console.warn(
        `[syncToServer] ⏭ CANCELLED stale sync for conv=${conv.id.slice(0,8)} — ` +
        `lightMsgs=${lightMsgs.length} but conv.messages=${conv.messages.length} (grew by ${conv.messages.length - lightMsgs.length} during async). ` +
        `A fresher sync should follow.`
      );
      return;
    }
    const resp = await Api.conversations.put(conv.id, {
      title: conv.title,
      messages: lightMsgs,
      createdAt: conv.createdAt,
      updatedAt: conv.updatedAt || Date.now(),
      settings,
      /* ★ CAS base: the server-issued rev this client last saw. The server
       *   accepts the write only if baseRev == its current rev; a stale base
       *   (a concurrent tab/device/server-write advanced rev) → 409
       *   blocked_rev_conflict, handled below by rebase+retry. Omitted when we
       *   have never learned a rev (undefined) → server fails open to the
       *   legacy count guards. */
      ...(conv._serverRev !== undefined && conv._serverRev !== null
          ? { baseRev: conv._serverRev } : {}),
      ...(allowTruncate ? { allowTruncate: true } : {}),
    });
    if (resp && resp.ok) {
      conv._serverMsgCount = lightMsgs.length;
      /* ★ Record this local write so the event-driven cross-device notify
       *   handler (_onConvNotifyPush) can recognise the backend task-save's own
       *   `conv_changed` frame as a SELF-ECHO. The backend writes+pushes the
       *   result independently of this PUT, so its frame's rev can outrun our
       *   `_serverRev` until this PUT's response lands — a bare rev-gate misses
       *   that race and would flash a stale cache repaint over the conv we're
       *   viewing. This timestamp is the fast-path guard for that window. */
      conv._localWriteAt = Date.now();
      /* ★ Adopt the server's post-write rev as our new baseRev so the NEXT PUT
       *   carries a fresh base — otherwise a client that syncs twice in a row
       *   would false-409 its own second write. */
      try {
        const okBody = await resp.clone().json().catch(() => null);
        if (okBody && typeof okBody.rev === 'number') conv._serverRev = okBody.rev;
      } catch (e) { console.debug(`[syncToServer] rev adopt skipped: ${e && e.message}`); }
      debugLog(`[syncToServer] ✅ Conv ${conv.id.slice(0,8)} synced ${lightMsgs.length} msgs to server (rev=${conv._serverRev})`, 'info');
      /* ★ Server confirmed the write — clear any durability markers set by a
       *   prior failed send (poor-network path). The message is now on the
       *   server, so the retry poller can stop and the reconcile no longer
       *   needs to protect a "pending" local tail. */
      _clearPendingSyncMarkers(conv);
      /* ★ Write-through: update IndexedDB cache with the synced state.
       *   This ensures the cache always reflects the latest server-confirmed data,
       *   so the next page load gets an instant cache hit with fresh content. */
      ConvCache.put(conv);
      return true;
    } else {
      const errBody = (resp ? await resp.json().catch(() => ({})) : {});
      const status = resp ? resp.status : 0;
      debugLog(`[syncToServer] ⚠️ Conv ${conv.id.slice(0,8)} sync rejected: ${status} ${errBody.error || ''}`, 'warn');
      /* ★ CAS rev conflict (409 blocked_rev_conflict): our baseRev was stale —
       *   a concurrent tab/device/server-write advanced the row's rev. Rebase
       *   instead of clobbering: GET the authoritative server row, APPEND our
       *   local-only tail (by _msgId, preserving ids), adopt the fresh rev as
       *   the new base, and re-PUT through THIS SAME path (not a parallel
       *   mechanism) so the pending-sync poller's boolean contract is honored.
       *   Guarded against infinite recursion by _revRebaseDepth. */
      if (status === 409 && errBody.error === 'blocked_rev_conflict') {
        const _localBefore = conv._serverRev;
        console.warn(`[syncToServer] 🔄 rev conflict conv=${conv.id.slice(0,8)} — `
          + `client baseRev=${_localBefore} server rev=${errBody.serverRev}; rebasing local tail + retry`);
        if ((conv._revRebaseDepth || 0) >= 3) {
          console.error(`[syncToServer] rev-rebase depth exceeded for conv=${conv.id.slice(0,8)} — leaving pending`);
          return false;
        }
        try {
          const freshData = await Api.conversations.get(conv.id);
          const serverMsgs = (freshData && freshData.messages) || [];
          /* Append-missing-tail: server base + our un-acked local messages. */
          const rebased = _rebaseUnackedTail(serverMsgs, conv.messages);
          conv.messages = rebased;
          conv.title = (freshData && freshData.title) || conv.title;
          conv._serverMsgCount = serverMsgs.length;
          /* Adopt the fresh rev so the retry PUT carries the correct base. */
          if (freshData && typeof freshData.rev === 'number') conv._serverRev = freshData.rev;
          else if (typeof errBody.serverRev === 'number') conv._serverRev = errBody.serverRev;
          if (activeConvId === conv.id) {
            window.ConvView.replaceAll(conv.id, { forceScroll: false });
            if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
          }
          conv._revRebaseDepth = (conv._revRebaseDepth || 0) + 1;
          try {
            const retryOk = await syncConversationToServer(conv, { allowTruncate });
            return retryOk;
          } finally {
            conv._revRebaseDepth = 0;
          }
        } catch (rebaseErr) {
          console.error(`[syncToServer] rev-rebase failed for conv=${conv.id.slice(0,8)}:`, rebaseErr.message);
          return false;
        }
      }
      /* ★ FIX: When server rejects with blocked_stale_checkpoint (409), the local
       *   data is stale (e.g. IDB cache from interrupted streaming).  Reload from
       *   server to get the correct completed data with finishReason/usage. */
      if (status === 409 && errBody.error === 'blocked_stale_checkpoint') {
        console.warn(`[syncToServer] 🔄 Stale checkpoint detected for conv=${conv.id.slice(0,8)} — reloading from server`);
        try {
          const freshData = await Api.conversations.get(conv.id);
          if (freshData) {
            const freshMsgs = freshData.messages || [];
            if (freshMsgs.length > 0) {
              conv.messages = freshMsgs;
              conv.title = freshData.title || conv.title;
              conv._serverMsgCount = freshMsgs.length;
              ConvCache.put(conv);
              if (activeConvId === conv.id) {
                window.ConvView.replaceAll(conv.id, { forceScroll: false });
                if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
              }
              console.info(`[syncToServer] ✅ Recovered ${freshMsgs.length} msgs from server for conv=${conv.id.slice(0,8)}`);
            }
          }
        } catch (recoverErr) {
          console.error(`[syncToServer] Recovery fetch failed:`, recoverErr.message);
        }
      }
      return false;
    }
  } catch (e) {
    debugLog(`[syncToServer] ❌ Sync failed for ${conv.id.slice(0,8)}: ${e.message}`, "warn");
    return false;
  }
  /* Any early-return guard above (streaming / in-flight / count-drop / stale)
   * did not perform a PUT — report "not synced" so a caller relying on the
   * boolean (the pending-sync retry poller) keeps the conv queued. */
  return false;
}

/* ═══════════════════════════════════════════════════════════════════
   Pending-sync retry cluster (markConvPendingSync /
   _clearPendingSyncMarkers / convHasPendingSync /
   _startPendingSyncPolling / _flushPendingSyncs) extracted 2026-07-25
   to core/pending_sync.js (pt_3879f00e sub-part 2, slice 2). It loads
   BEFORE this file via _BUNDLE_FILES so downstream reads of the bare
   names still resolve at runtime.
   ═══════════════════════════════════════════════════════════════════ */

/* _applySettingsToConv extracted to static/js/core/conv_apply_settings.js
 * (pt_3879f00e sub-part 2 slice 5). Loaded via _BUNDLE_FILES BEFORE both
 * conversations.js and cross_tab_sync.js so every call site — 8 here + 1
 * in cross_tab_sync's _handleConvNotifyPush — resolves the bare name at
 * runtime via the shared bundle scope. */
/**
 * Paint the sidebar from the IndexedDB cache BEFORE (or without) a server
 * round-trip, so first paint shows real conversations with zero network
 * dependency — critical on a flaky tunnel/mobile network.
 *
 * Honesty note: `ConvCache.put()` only stores conversations that have
 * messages, so this reflects conversations OPENED on this device, NOT the
 * full server list. It is a best-effort head-start; the server list (when it
 * arrives) is the source of truth and reconciles these shells in-place via
 * the id-keyed merge in loadConversationsFromServer(). A cached-only conv the
 * server does not confirm (e.g. deleted elsewhere) is pruned by that merge's
 * existing empty-local-only sweep. The boot path never put()s the meta-only
 * server shells, so the cache never learns about un-opened conversations —
 * acceptable by design.
 *
 * @returns {Promise<number>} count of shells added from cache
 */
async function hydrateSidebarFromCache() {
  try {
    if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) return 0;
    /* ★ Prefer the FULL-LIST sidebar mirror (putSidebarList) — it holds EVERY
     *   conversation the server last reported, so the sidebar paints complete
     *   on a cold boot instead of only the handful opened on this device.
     *   Fall back to getAllMeta (opened-conv metas) when the mirror is empty
     *   (first run before any full load, or a v2→v3 upgrade). Both shapes carry
     *   id/title/updatedAt/settings + a message-count key, which is all the
     *   shell builder below needs. The mirror additionally carries `rev`, which
     *   we adopt as the CAS base so the id-keyed merge treats the shell as
     *   known (skew-proof staleness) rather than re-pulling every body. */
    let metas = [];
    let fromFullList = false;
    if (ConvCache.getSidebarList) {
      metas = await ConvCache.getSidebarList();
      fromFullList = !!(metas && metas.length);
    }
    if (!fromFullList) {
      metas = await ConvCache.getAllMeta();
    }
    if (!metas || !metas.length) return 0;
    const known = new Set(conversations.map(c => c.id));
    let added = 0;
    let pendingShells = 0;
    for (const m of metas) {
      if (!m.id || known.has(m.id)) continue;
      const _mCount = _serverConvCount(m);
      const nc = {
        id: m.id,
        title: m.title || 'Untitled',
        messages: [],
        _serverMsgCount: _mCount,
        _needsLoad: _mCount > 0,
        _fromCache: true,
        createdAt: m.createdAt || m.updatedAt || m.cachedAt || Date.now(),
        updatedAt: m.updatedAt || m.cachedAt || Date.now(),
        activeTaskId: null,
      };
      /* Adopt the mirror's CAS base rev when present (full-list rows carry it;
       * opened-conv metas do not) so loadConversationsFromServer's id-keyed
       * merge trusts the monotonic rev signal for this shell instead of a
       * skew-prone wall-clock, and a first PUT sends a matching baseRev. */
      if (typeof m.rev === 'number') nc._serverRev = m.rev;
      _applySettingsToConv(nc, m.settings);
      /* ★ Poor-network durability: restore the conv-level pending-sync marker
       *   from the cached meta so the flush poller can SEE a stranded pending
       *   tail on a shell that hasn't loaded its messages yet. Without this the
       *   message a failed poor-network send rescued into IndexedDB is invisible
       *   to _flushPendingSyncs (which reads conv.messages) until the user
       *   happens to open that exact conversation — silent data loss, one layer
       *   up from the message-level marker. */
      if (m.settings && m.settings._pendingSyncAt) {
        nc._pendingSyncAt = m.settings._pendingSyncAt;
        pendingShells++;
      }
      conversations.push(nc);
      added++;
    }
    if (added) {
      conversations.sort(_convSorter);
      if (typeof renderConversationList === 'function') renderConversationList();
      console.log(`[hydrateSidebarFromCache] painted ${added} cached conversation(s) before server load`);
    }
    /* Kick the retry poller if any hydrated shell carries a stranded pending
     * tail — it will hydrate + re-sync them (see _flushPendingSyncs). */
    if (pendingShells > 0) {
      console.info(`[hydrateSidebarFromCache] ${pendingShells} shell(s) carry a pending-sync tail — starting flush poller`);
      _startPendingSyncPolling();
      _flushPendingSyncs('cache_hydrate');
    }
    return added;
  } catch (e) {
    debugLog(`hydrateSidebarFromCache failed: ${e.message}`, 'warn');
    return 0;
  }
}

/**
 * Extract a conversation's message count from a server list row, tolerating
 * every key variant the backend may emit. The sidebar ?meta=1 path
 * (lib/conversations/meta_cache.py) sends `messageCount`; the default list
 * shape (routes/conversations.py::_conv_row_to_meta_dict) and the IDB cache
 * send `msgCount` / `msg_count`. This count feeds the SHELL VISIBILITY gate
 * (_needsLoad / _serverMsgCount → renderConversationList's filter), so reading
 * only one key would silently drop a real conversation from the sidebar when a
 * differently-shaped payload served it. Returns 0 when none are present.
 */
function _serverConvCount(sc) {
  if (!sc) return 0;
  const v = sc.messageCount != null ? sc.messageCount
    : (sc.msgCount != null ? sc.msgCount : sc.msg_count);
  return v || 0;
}


/**
 * Incrementally merge server metadata rows (from a folder-scoped or paginated
 * list query) into the in-memory `conversations` array, keyed by id.
 *
 * DISCIPLINE (the "never overwrite" invariant): a row that is ALREADY in
 * memory — because it's in the top-N sidebar window, is streaming, or has its
 * messages loaded — must NOT have its heavy/live fields clobbered by a
 * metadata-only row. We only FILL fields the local copy is missing:
 *   • messages / _serverRev / activeTaskId / _needsLoad → left untouched.
 *   • title / updatedAt / folderId → refreshed (cheap metadata, safe).
 *
 * A row NOT yet in memory is added as a SHELL with the same visibility-gate
 * fields the boot loader builds (_serverMsgCount from the count, _needsLoad
 * when the count is >0), so it passes renderConversationList's
 * `messages.length>0 || _serverMsgCount>0 || _needsLoad` filter instead of
 * silently hiding despite having been resolved by the folder query.
 *
 * Returns the number of NEW shells added (0 = every row was already known).
 */
function mergeServerConvShells(serverConvs) {
  if (!Array.isArray(serverConvs) || serverConvs.length === 0) return 0;
  const localMap = new Map(conversations.map((c) => [c.id, c]));
  let added = 0;
  for (const sc of serverConvs) {
    if (!sc || !sc.id) continue;
    const local = localMap.get(sc.id);
    const _scCount = _serverConvCount(sc);
    if (!local) {
      const nc = {
        id: sc.id,
        title: sc.title,
        messages: [],
        _serverMsgCount: _scCount,
        _needsLoad: _scCount > 0,
        createdAt: sc.createdAt,
        updatedAt: sc.updatedAt || sc.createdAt,
        activeTaskId: null,
      };
      _applySettingsToConv(nc, sc.settings);   // adopts folderId / pinned / etc.
      conversations.push(nc);
      added++;
    } else {
      /* Existing conv — refresh ONLY cheap metadata; never touch live/heavy
       * fields (messages, _serverRev, activeTaskId, _needsLoad). */
      if (sc.title) local.title = sc.title;
      const sT = sc.updatedAt || sc.createdAt || 0;
      if (sT && sT >= (local.updatedAt || 0)) local.updatedAt = sT;
      /* Keep _serverMsgCount at least as large as the server row reports so the
       * visibility gate stays satisfied, but never shrink it (a lagging list
       * snapshot mustn't rewind a count a fresher GET advanced). */
      if (_scCount > (local._serverMsgCount || 0)) local._serverMsgCount = _scCount;
      /* Adopt folderId from settings if the local copy doesn't have one yet
       * (mirrors setActiveFolderId's need to see members in the folder view). */
      if (sc.settings && sc.settings.folderId !== undefined && !local.folderId) {
        local.folderId = sc.settings.folderId;
      }
    }
  }
  return added;
}

let _convMetaEtag = null;   // ETag for 304 Not Modified support
/* ★ Observable-outcome signal for the boot-reconnect trigger. Because
 *   loadConversationsFromServer SWALLOWS its errors (try/catch → debugLog,
 *   resolves normally), the caller cannot decide "did the server load actually
 *   succeed?" from a thrown exception. This flag is the truth: true only on a
 *   real 200-with-data merge OR a legitimate 304 (unchanged list); false on a
 *   throw (tunnel drop / Failed to fetch) or a non-OK response. A 200 with an
 *   empty list is ALSO success (the server was reached — it just has no convs).
 *   main.js gates _bootReconnectWithBackoff on !serverLoadOk(). */
let _lastServerLoadOk = false;
function serverLoadOk() { return _lastServerLoadOk; }

/* ★ Authoritative global conversation total reported by the server (via the
 *   ?meta=1 X-Total-Count header, computed at cache-rebuild — NOT per poll).
 *   The sidebar compares it against how many convs are actually in memory to
 *   decide whether to show the "N earlier not loaded" affordance (C4). null
 *   until the first meta load reports it. */
let _serverTotalCount = null;
function getServerTotalCount() { return _serverTotalCount; }
async function loadConversationsFromServer(prefetchId) {
  _lastServerLoadOk = false;   // pessimistic — flipped true only at a genuine-success exit
  try {
    /* ── Fast path: only metadata for sidebar (no messages) ── */
    /** @type {Record<string,string>} */
    const headers = {};
    /* When prefetching, skip ETag/304 — we need fresh data + the conv body */
    if (!prefetchId && _convMetaEtag) headers['If-None-Match'] = _convMetaEtag;
    /* ★ Bound the fetch. Through a flaky tunnel (Android WebView over a VS Code
     *   port-forward) a raw fetch can HANG forever — never resolve, never
     *   reject. Since the outer try/catch only catches THROWS, a hung fetch
     *   means this fn never settles, so the `.finally()` that releases the
     *   shared `_bootLoadInFlight` latch never runs → every future reconcile +
     *   boot-reconnect is permanently wedged (the "always must refresh" bug).
     *   A timeout turns the hang into an AbortError → caught below → resolves →
     *   latch released. Mirrors loadConversationMessages' bounded fetch. */
    const _META_FETCH_TIMEOUT_MS = 12000;
    const _mkTimeoutSignal = (typeof AbortSignal !== 'undefined' && AbortSignal.timeout)
      ? (ms) => AbortSignal.timeout(ms)
      : (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
    let resp;
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await Api.conversations.listMeta({
        prefetch: prefetchId || undefined,
        headers,
        signal: _mkTimeoutSignal(_META_FETCH_TIMEOUT_MS),
      });
      if (resp.status === 503) {
        const delay = (parseInt(resp.headers.get('Retry-After'), 10) || (_attempt + 1)) * 1000;
        debugLog(`[loadConvs] 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    if (resp.status === 304) { _lastServerLoadOk = true; return; }   // unchanged list — legitimate success
    if (!resp.ok) return;   // non-OK — leave _lastServerLoadOk=false (triggers reconnect)
    /* Capture the authoritative global total (C4). Header absent on 304 (we
     * already returned) and on error paths; a parse failure leaves the prior
     * value untouched. */
    try {
      const _tc = resp.headers && resp.headers.get && resp.headers.get('X-Total-Count');
      if (_tc != null && _tc !== '') {
        const _n = parseInt(_tc, 10);
        if (!Number.isNaN(_n)) _serverTotalCount = _n;
      }
    } catch (_e) {
      debugLog(`[conversations] X-Total-Count header read failed: ${_e && _e.message}`, 'warn');
    }
    let serverConvs, prefetchedConv = null;
    if (prefetchId) {
      /* Combo response: { conversations: [...], prefetched: {...} | null } */
      const combo = await resp.json();
      serverConvs = combo.conversations || [];
      prefetchedConv = combo.prefetched || null;
    } else {
      _convMetaEtag = resp.headers.get('ETag') || null;
      serverConvs = await resp.json();
    }
    console.log(`[loadConversationsFromServer] Got ${serverConvs.length} convs from server, local has ${conversations.length}`);
    _lastServerLoadOk = true;   // server reached + responded with a list (empty is still a valid answer)
    /* ★ Persist the FULL authoritative list into the lightweight sidebar
     *   mirror so the NEXT cold boot paints the entire sidebar before its
     *   server round-trip (the "打开即在" fix). Fire-and-forget — never blocks
     *   the merge/render, and a cache write failure is cosmetic. Guarded on a
     *   non-empty list so a transient empty response can't wipe the mirror. */
    if (serverConvs.length && typeof ConvCache !== 'undefined' && ConvCache.putSidebarList) {
      try {
        ConvCache.putSidebarList(serverConvs).catch(e =>
          debugLog(`putSidebarList failed: ${e && e.message}`, 'warn'));
      } catch (e) { debugLog(`putSidebarList threw: ${e && e.message}`, 'warn'); }
    }
    if (!serverConvs.length) return;
    const localMap = new Map(conversations.map((c) => [c.id, c]));
    let merged = false,
      acChanged = false;
    for (const sc of serverConvs) {
      const local = localMap.get(sc.id);
      if (!local) {
        /* New conversation from server — create shell with empty messages.
         * ★ Read the count from ANY of the server key variants. The sidebar
         *   ?meta=1 path (lib/conversations/meta_cache.py) emits `messageCount`,
         *   but the default list shape (_conv_row_to_meta_dict) and the IDB
         *   cache path emit `msgCount`/`msg_count`. A shell built from a payload
         *   that lacks the exact `messageCount` key would get
         *   _serverMsgCount=0 && _needsLoad=false and be DROPPED by the sidebar
         *   filter (renderConversationList) — present in memory but invisible
         *   until clicked. Coalescing all three keys makes the visibility gate
         *   robust to which list shape served the row. */
        const _scCount = _serverConvCount(sc);
        const nc = {
          id: sc.id,
          title: sc.title,
          messages: [],
          _serverMsgCount: _scCount,
          _needsLoad: _scCount > 0,
          createdAt: sc.createdAt,
          updatedAt: sc.updatedAt || sc.createdAt,
          activeTaskId: null,
        };
        _applySettingsToConv(nc, sc.settings);
        conversations.push(nc);
        merged = true;
      } else if (!activeStreams.has(sc.id) && (!local.activeTaskId || sc.id === activeConvId)) {
        /* Update metadata for existing conversations.
         * ★ Cross-device fix: the OPEN conversation is allowed through this
         *   branch even when it merely HOLDS an activeTaskId pin (no LIVE
         *   stream — the activeStreams guard still bars a streaming conv), so a
         *   read-only body refresh can fire below. loadConversationMessages'
         *   MERGE_ACTIVE_TASK branch merges server content in place for a
         *   pinned conv (keep-longer, append-when-not-streaming) — it never
         *   replaces conv.messages, so the connectToTask assistantMsg ref is
         *   never orphaned and the pin is preserved. */
        const sT = sc.updatedAt || sc.createdAt || 0,
          mT = local.updatedAt || local.createdAt || 0;
        const serverMsgCount = _serverConvCount(sc);
        /* ★ Monotonic `rev` is the AUTHORITATIVE staleness signal — the same
         *   server-issued rev the body loader (:1278) and the notify push gate
         *   on. Wall-clock `updatedAt` is a FALLBACK only: it is skew-prone
         *   across devices and can't distinguish "server genuinely advanced"
         *   from "the two clocks disagree". When rev is comparable on BOTH
         *   sides we trust it; a stale-but-later local clock can never fake a
         *   body refetch, and a device that just wrote (its own PUT advanced
         *   _serverRev) sees sc.rev <= _serverRev → no spurious re-pull. */
        const sR = (typeof sc.rev === 'number') ? sc.rev : null;
        const mR = (typeof local._serverRev === 'number') ? local._serverRev : null;
        const revComparable = (sR !== null && mR !== null);
        const revNewer = revComparable && sR > mR;
        if (serverMsgCount > local.messages.length || revNewer || sT > mT) {
          local.title = sc.title;
          local.updatedAt = sc.updatedAt || sc.createdAt;
          local._serverMsgCount = serverMsgCount;
          /* Advance the CAS base to the authoritative list rev when it moved
           *   forward, so a later PUT sends the matching baseRev and the notify
           *   rev-gate treats this state as known (no re-verify loop). Never
           *   move it BACKWARD (a lagging list snapshot mustn't rewind a base a
           *   fresher GET already advanced). */
          if (revNewer) local._serverRev = sR;
          if (serverMsgCount > local.messages.length) {
            local._needsLoad = true;
          } else if (sc.id === activeConvId) {
            /* ★ Cross-device fix (content-only append): the OPEN conversation's
             *   server row advanced WITHOUT a message-count change — another
             *   device EXTENDED the trailing turn rather than adding a new
             *   message. Decide on the MONOTONIC rev (authoritative, skew-proof)
             *   when comparable, falling back to wall-clock ONLY for a legacy
             *   rev=0 / pre-rev row. This stops a clock-skewed updatedAt from
             *   triggering a spurious body refetch (and the position shuffle it
             *   causes) when rev proves nothing changed. When stale, mark the
             *   open conv so the active-conv re-pull below fetches the fresh
             *   body — routed through its keep-longer / KEEP_LOCAL / count-drop
             *   guards (ADD/UPDATE only; a stale device can never truncate). */
            const _contentStale = revComparable ? revNewer : (sT > mT);
            if (_contentStale) {
              local._needsLoad = true;
              /* ★ EQUAL-COUNT content grow (push-dead poll path).
               *   The trailing assistant turn grew IN PLACE (same message
               *   count). Routing this through loadConversationMessages reaches
               *   its OVERWRITE branch, whose staleness test at equal count is
               *   `serverUpdatedAt > _cachedUpdatedAt` — a wall-clock compare
               *   against the CACHE timestamp with NO content-length keep-longer
               *   fallback. When the cache's updatedAt already equals the
               *   server's (or the two clocks are skewed) the grown content is
               *   DROPPED and the partial bubble stays stale for the whole
               *   session — the exact "fills only after several manual
               *   refreshes" bug when the WebSocket is dead. Flag it so the
               *   `merged` tail adopts via the SAME keep-longer, non-destructive
               *   `_verifyActiveConvFromServer` the notify path uses (no cache
               *   flash, no scroll reset), instead of the count-plus-clock
               *   OVERWRITE. A real count grow (branch above) is unaffected —
               *   it keeps using loadConversationMessages. */
              local._contentGrewNeedsVerify = true;
            }
          }
          /* Preserve local pinned/folder state — these are client-side
             preferences and must not be overwritten by a potentially
             stale server snapshot during periodic refresh.           */
          const keepPinned = local.pinned, keepPinnedAt = local.pinnedAt;
          const keepFolderId = local.folderId;
          _applySettingsToConv(local, sc.settings);
          local.pinned = keepPinned; local.pinnedAt = keepPinnedAt;
          /* ★ Only restore local folderId if it was set locally but server
           *   doesn't have it yet (PATCH in-flight race). If server has a
           *   folderId, trust the server (it's the source of truth). */
          if (keepFolderId && !local.folderId) local.folderId = keepFolderId;
          if (sc.id === activeConvId) acChanged = true;
          merged = true;
        }
      }
    }
    /* ★ Rescue local-only conversations that exist in memory (from this
     *   session) but haven't been synced to the server yet.  This handles
     *   the edge case where the user sent a message while the server was
     *   briefly unreachable.  Ghost convs (empty, not on server) are simply
     *   dropped — they have no data worth saving. */
    const serverIdSet = new Set(serverConvs.map(sc => sc.id));
    for (const lc of conversations) {
      if (!serverIdSet.has(lc.id) && !activeStreams.has(lc.id)) {
        /* ★ DELETION vs OFFLINE-RESCUE disambiguator (epic pt_2fd936cd15c34a7f).
         *   Absence from the server list has TWO causes, distinguished by the
         *   monotonic `_serverRev` signal:
         *   • A conv that EVER carried a `_serverRev` was once server-known →
         *     its absence now means it was DELETED on another device. Re-PUTing
         *     it (the old blanket rescue) RESURRECTS a deleted conversation.
         *     Drop it locally + prune the IDB paint-cache (mirrors the
         *     conv_deleted notify tombstone path _applyRemoteConvDeleted).
         *   • A conv that NEVER had a `_serverRev` is a genuine "sent while the
         *     server was briefly unreachable" local creation → rescue it. */
        const wasServerKnown = (typeof lc._serverRev === 'number');
        if (wasServerKnown) {
          if (lc.id === activeConvId) {
            /* Don't yank the conv out from under the user mid-view; leave it
             *   until they navigate away. It won't be re-PUT (rescue skipped),
             *   so it can't resurrect on the server. */
            continue;
          }
          console.warn(`[loadConversationsFromServer] ✗ Dropping conv ${lc.id.slice(0,8)} ` +
            `— was server-known (rev=${lc._serverRev}) but absent from server list → deleted elsewhere`);
          conversations = conversations.filter(c => c.id !== lc.id);
          try { ConvCache.remove(lc.id); } catch (e) { console.debug(`[loadConversationsFromServer] IDB prune skipped: ${e && e.message}`); }
          merged = true;
        } else if (lc.messages.length > 0) {
          console.warn(`[loadConversationsFromServer] ★ Rescuing local-only conv ${lc.id.slice(0,8)} ` +
            `(${lc.messages.length} msgs, never server-known) — syncing to server`);
          syncConversationToServer(lc);
        } else if (lc.id !== activeConvId) {
          /* Empty local-only conv — drop it silently (it was never meaningful) */
          conversations = conversations.filter(c => c.id !== lc.id);
          merged = true;
        }
      }
    }

    /* ── Apply prefetched conversation data (eliminates second round-trip) ── */
    if (prefetchedConv && prefetchedConv.id) {
      const pc = conversations.find(c => c.id === prefetchedConv.id);
      /* ★ FIX: Allow prefetch even when activeTaskId is set.  After a server
       *   crash, the backend's recover_stale_tasks_on_startup() clears
       *   activeTaskId and merges interrupted content into conversation
       *   messages.  So by the time this runs, the conv has clean data.
       *   Previously, `!pc.activeTaskId` blocked prefetch for the exact
       *   case where it was most needed (crash recovery of the active conv),
       *   forcing an extra round-trip through loadConversationMessages. */
      if (pc && pc._needsLoad && !activeStreams.has(pc.id)) {
        const serverMsgs = prefetchedConv.messages || [];
        pc.messages = serverMsgs;
        pc.title = prefetchedConv.title || pc.title;
        pc.updatedAt = prefetchedConv.updatedAt || prefetchedConv.updated_at || pc.updatedAt;
        const keepPinned = pc.pinned, keepPinnedAt = pc.pinnedAt;
        _applySettingsToConv(pc, prefetchedConv.settings);
        pc.pinned = keepPinned; pc.pinnedAt = keepPinnedAt;
        pc._needsLoad = false;
        pc._serverMsgCount = serverMsgs.length;
        /* ★ Adopt the server rev — the prefetched conv came straight from the
         *   server, so its rev is authoritative. Without this, pc._serverRev
         *   stays undefined and pc's first PUT sends no baseRev (fail-open, no
         *   CAS protection). */
        if (typeof prefetchedConv.rev === 'number') pc._serverRev = prefetchedConv.rev;
        console.log(`[loadConversationsFromServer] ⚡ Prefetched conv ${pc.id.slice(0,8)}: ${serverMsgs.length} msgs — no second fetch needed`);
        /* ★ Update IndexedDB cache with the prefetched data */
        ConvCache.put(pc);
        merged = true;
      }
    }

    console.log(`[loadConversationsFromServer] merged=${merged}, total conversations now: ${conversations.length}, ` +
      `visible: ${conversations.filter(c => c.messages.length > 0 || (c._serverMsgCount||0) > 0 || c._needsLoad).length}`);
    /* ★ pt_e1c4693341b24730 follow-up: a conv created on ANOTHER device is
     *   unknown here when its first notify frame lands, so the reducer PARKS
     *   that frame's authoritative busy state instead of discarding it. The
     *   list we just merged is exactly what makes those convs known — replay
     *   now, BEFORE the sort/render below, so the busy dot paints in the same
     *   frame the conv first appears rather than staying dark until the next
     *   notify frame or an F5. Deliberately NOT solved by teaching the list
     *   endpoint to return runningTaskIds: that would be a SECOND busy-state
     *   source and breaks hard constraint #3 (task registry is the only SSOT). */
    if (typeof replayPendingBusyState === 'function') {
      replayPendingBusyState(conversations);
    }
    if (merged) {
      conversations.sort(_convSorter);
      renderConversationList();
      /* If the active conversation needs a full reload, do it now */
      if (activeConvId) {
        const ac = getActiveConv();
        /* ★ Equal-count trailing-turn grow on a SETTLED open conv → adopt via
         *   the notify path's keep-longer, non-destructive verify (no cache
         *   flash / scroll reset, and — unlike loadConversationMessages'
         *   count-plus-clock OVERWRITE — it actually adopts an equal-count
         *   content grow). Only for a settled conv (no activeTaskId, not
         *   streaming); a live/pinned conv still owns its own lifecycle. Falls
         *   back to loadConversationMessages when the verify fn is unavailable
         *   or the conv isn't settled, so count-grew / first-load / crash paths
         *   are unchanged. */
        const _canVerify = ac && ac._contentGrewNeedsVerify && !ac.activeTaskId
          && !activeStreams.has(activeConvId)
          && typeof _verifyActiveConvFromServer === 'function';
        if (_canVerify) {
          ac._contentGrewNeedsVerify = false;
          ac._needsLoad = false;
          await _verifyActiveConvFromServer(activeConvId);
        } else if (ac && ac._needsLoad) {
          if (ac._contentGrewNeedsVerify) ac._contentGrewNeedsVerify = false;
          await loadConversationMessages(activeConvId);
        } else if (acChanged && ac && !ac.activeTaskId) {
          window.ConvView.replaceAll(ac.id, { forceScroll: false });
          if (typeof _restoreConvToolState === "function")
            _restoreConvToolState(ac);
        }
      }
    }
  } catch (e) {
    debugLog(`Server load: ${e.message}`, "warn");
  }
}

/**
 * Load full messages for a single conversation on demand.
 * Returns the conversation object or null.
 */
/**
 * Toggle the "verifying" visual state on the chat viewport. Used when a
 * known-stale IndexedDB copy is painted optimistically while a background
 * server GET is in flight to correct it: the provisional content is dimmed so
 * the transient pre-correction render reads as "being checked", not as truth.
 * Cleared (on=false) the moment Phase-2 reconciles or the fetch settles.
 * Pure DOM decoration — no effect on state / persistence.
 */
function _setCacheVerifying(convId, on) {
  if (convId !== activeConvId) return;
  const inner = (typeof document !== 'undefined')
    ? document.getElementById('chatInner') : null;
  if (!inner) return;
  if (on) inner.classList.add('chat-cache-verifying');
  else inner.classList.remove('chat-cache-verifying');
}

/* An OPEN conversation held in memory may still carry a client-minted,
 * never-reconciled trailing empty-assistant placeholder (an orphaned "ghost"
 * bubble left behind when a task's stream dropped before its first token).
 * The backend GET path reconciles these away (routes/conversations.py →
 * lib/conversations/reconcile.py), but the warm-open early-return below would
 * skip that fetch entirely, so the ghost renders forever until a hard refresh.
 *
 * This predicate ONLY decides whether to re-verify against the server — it
 * NEVER decides the ghost is dead or splices it. When it returns true we fall
 * through to Phase 2 and adopt whatever the server says exists (if the server
 * still lists the placeholder, e.g. a live task it owns, it stays). That keeps
 * the server authoritative and avoids the banned frontend-lifecycle-inference
 * pattern (the client truncating history on its own verdict). A genuinely-live
 * stream is excluded via activeStreams so a legitimate "Preparing…" pre-first-
 * token bubble is never disturbed. */
function _openConvMayHoldOrphanGhost(conv, convId) {
  if (!conv || activeStreams.has(convId)) return false;
  const msgs = conv.messages;
  if (!msgs || msgs.length === 0) return false;
  const tail = msgs[msgs.length - 1];
  return !!tail && tail.role === 'assistant'
    && !(tail.content && tail.content.length)
    && !(tail.thinking && tail.thinking.length)
    && !(tail.toolRounds && tail.toolRounds.length)
    && !tail.finishReason
    && !tail.error;
}

/* ★ Bounded self-heal for an ACTIVE conv whose Phase-2 verify never landed
 *   (timeout / 5xx / offline). Without it, a failed background sync left the
 *   optimistic cache paint posing as final truth — the user stared at a stale
 *   "ended normally" for minutes and only a lucky later push fixed it (the
 *   reported historical-conv sync bug). The retry rides the SAME
 *   non-destructive _verifyActiveConvFromServer the notify path uses
 *   (adopt-on-change only, no cache repaint, no scroll reset). Bounded: once
 *   the delays are exhausted we leave _needsLoad=true + the verifying dim,
 *   and the next manual open re-verifies. */
const _CONV_VERIFY_RETRY_DELAYS_DEFAULT = [4000, 12000];
const _convVerifyRetryTimers = {};

function _convVerifyRetryDelays() {
  /* Test seam: a harness may shorten the backoff via a window override. */
  const d = (typeof window !== 'undefined') ? window._CONV_VERIFY_RETRY_DELAYS : null;
  return (Array.isArray(d) && d.length) ? d : _CONV_VERIFY_RETRY_DELAYS_DEFAULT;
}

function _scheduleConvVerifyRetry(convId) {
  if (convId !== activeConvId) return;   /* only the OPEN conv self-heals in place */
  if (typeof _verifyActiveConvFromServer !== 'function') return;
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  const delays = _convVerifyRetryDelays();
  const attempt = conv._verifyRetryCount || 0;
  if (attempt >= delays.length) return;
  clearTimeout(_convVerifyRetryTimers[convId]);
  _convVerifyRetryTimers[convId] = setTimeout(() => {
    delete _convVerifyRetryTimers[convId];
    const c = conversations.find((x) => x.id === convId);
    if (!c || convId !== activeConvId) return;
    if (activeStreams.has(convId) || _editingMsgIdx !== null || c.activeTaskId) return;
    c._verifyRetryCount = attempt + 1;
    Promise.resolve(_verifyActiveConvFromServer(convId)).then((adopted) => {
      if (adopted !== null && adopted !== undefined) {
        /* Verify landed (with or without changes) — the paint is server-true. */
        c._verifyRetryCount = 0;
        delete c._cacheKnownStale;
        _setCacheVerifying(convId, false);
      } else {
        _scheduleConvVerifyRetry(convId);
      }
    }).catch(() => _scheduleConvVerifyRetry(convId));
  }, delays[attempt]);
}

async function loadConversationMessages(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return null;
  /* Skip if already loaded and not stale — UNLESS the open conv may still hold
   * an orphaned, never-reconciled ghost tail, in which case re-verify against
   * the authoritative server list (see _openConvMayHoldOrphanGhost). */
  if (!conv._needsLoad && conv.messages.length > 0
      && !_openConvMayHoldOrphanGhost(conv, convId)) {
    return conv;
  }

  /* ═══ Phase 1: Try IndexedDB cache for instant render ═══ */
  let cacheHit = false;
  try {
    const cached = await ConvCache.get(convId);
    if (cached && cached.messages && cached.messages.length > 0) {
      /* Serve from cache immediately — user sees content with zero network wait */
      /* ★ Drop any transient autopilot VU streaming placeholder (`_streamingVu`)
       *   a pre-fix cache may still hold. It is in-memory-only by backend design
       *   (persisted ONLY on autopilot_vu_done, which clears the flag first), so
       *   a cached one is a ghost: it renders as a frozen "Autopilot starting…"
       *   pulse and makes the vu_start SSE replay early-return instead of
       *   re-creating a live stream. Filtering it here lets the reconnect replay
       *   stand up a fresh live bubble (or vu_done supply the settled one). */
      conv.messages = cached.messages.filter(function (m) { return !(m && m._streamingVu); });
      conv.title = cached.title || conv.title;
      /* Apply cached settings (preserving local overrides like pinned) */
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      if (cached.settings) _applySettingsToConv(conv, cached.settings);
      conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(cached.messages.length, conv._serverMsgCount || 0);
      conv._cachedUpdatedAt = cached.updatedAt || 0;
      cacheHit = true;
      // ★ Re-attach compaction markers (lazy, fire-and-forget) so the
      //   inline chips reappear after page reload. The viewer module
      //   handles missing gracefully if not yet loaded.
      try {
        if (typeof attachCompactionMarkersToConversation === 'function') {
          attachCompactionMarkersToConversation(convId, conv.messages).then(() => {
            /* ★ SCROLL FIX: repaint IN PLACE (scroll-preserving) instead of
             *   renderChat(conv,false). This callback lands after its own
             *   network round-trip — on the initial conversation switch
             *   (_initialSwitchLoad set) the full-render path force-scrolls to
             *   the bottom, and with the artifact hydrate + Phase-2 variants
             *   each landing at a different time the reader is yanked several
             *   times. _bgRefreshChat surgically swaps only changed assistant
             *   bubbles and is a no-op when nothing was attached. */
            if (convId === activeConvId && typeof _bgRefreshChat === 'function') {
              _bgRefreshChat(conv);
            }
          }).catch(e => console.debug('[compaction] attach (cache) failed:', e));
        }
      } catch (e) { console.debug('[compaction] attach (cache) hook error:', e); }
      // ★ Hydrate renderable artifacts after cache load so chips reappear
      //   on page reload without waiting for the server fetch.
      try {
        if (typeof window.Artifacts !== 'undefined' && window.Artifacts.hydrateConversation) {
          window.Artifacts.hydrateConversation(conv).then(() => {
            /* ★ SCROLL FIX: scroll-preserving in-place repaint (see the
             *   compaction callback above) — never force-scroll on open. */
            if (convId === activeConvId && typeof _bgRefreshChat === 'function') {
              _bgRefreshChat(conv);
            }
          }).catch(e => console.debug('[artifacts] hydrate (cache) failed:', e));
        }
      } catch (e) { console.debug('[artifacts] hydrate (cache) hook error:', e); }
      console.info(`[loadConvMsgs] ⚡ CACHE HIT conv=${convId.slice(0,8)}: ${cached.messages.length} msgs (cachedAt=${new Date(cached.cachedAt).toISOString()})`);
      /* ★ Known-stale first-paint suppression (cosmetic wrong-content flash on
       *   a poor connection). The sidebar list carries each conv's server-issued
       *   `updatedAt` (loadConversationsFromServer). If it is strictly NEWER than
       *   the cached copy's updatedAt, we KNOW the cache is outdated before the
       *   Phase-2 GET returns. We still paint instantly (no blank wait — the
       *   cache purpose stands), but mark the provisional render "verifying" so
       *   the transient pre-correction content reads as being checked, not as
       *   truth; the marker is cleared the moment Phase-2 reconciles. Persisted
       *   corruption is already prevented by the rev CAS — this closes only the
       *   visual stale flash. */
      const _cacheKnownStale = (conv.updatedAt || 0) > (cached.updatedAt || 0);
      conv._cacheKnownStale = _cacheKnownStale;
      /* Hydrate images from cache (URLs only, base64 stripped) */
      _hydrateImageBase64(conv);
      /* Render immediately from cache */
      if (convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          window.ConvView.replaceAll(conv.id, { forceScroll: false });
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
          _setCacheVerifying(convId, _cacheKnownStale);
        }
      }
    }
  } catch (cacheErr) {
    console.warn(`[loadConvMsgs] Cache read failed for ${convId.slice(0,8)}: ${cacheErr.message}`);
  }

  /* ═══ Phase 2: Fetch from server (verify freshness or first load) ═══ */
  /* ★ Snapshot local state BEFORE the server fetch begins.  The user can
   *   call sendMessage() during the (potentially multi-second) fetch — that
   *   pushes an optimistic user message into conv.messages with a fresh
   *   timestamp.  If we don't snapshot, the post-fetch reconciliation can't
   *   distinguish "cache + new local push" from "stale cache".  See the
   *   user-msg-disappears-on-send race below. */
  const _preFetchMsgCount = conv.messages.length;
  const _preFetchNewest = conv.messages.length > 0
    ? conv.messages.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0)
    : 0;
  const _preFetchActiveTaskId = conv.activeTaskId || null;
  try {
    let resp;
    /* Timeout: if server is frozen (FUSE/dolphins), don't hang forever.
     * With cache hit: user already sees content, 10s is generous for background check.
     * Without cache hit: 10s before showing retry button is acceptable. */
    const _fetchTimeout = cacheHit ? 10000 : 15000;
    const _mkSignal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
      ? (ms) => AbortSignal.timeout(ms)
      : (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
    /* ★ Windowed first-open: request only the tail N messages so the response
     *   body is bounded (root-cause fix for slow first-open of long convs over
     *   the tunnel — a 6 MB blob was timing the fetch out). '' when windowing
     *   is disabled → legacy full-blob load. recordWindowState() below stamps
     *   the pagination state and scroll-up loads earlier pages on demand. */
    const _winParam = (typeof convWindowParam === 'function') ? convWindowParam() : '';
    const _fetchOpts = _winParam
      ? { query: { window: _winParam } }
      : {};
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await Api.conversations.getResponse(
        convId, Object.assign({ signal: _mkSignal(_fetchTimeout) }, _fetchOpts));
      if (!resp) { /* network/abort — retry */ continue; }
      if (resp.status === 503) {
        /* DB temporarily busy — wait and retry */
        const delay = (parseInt(resp.headers.get('Retry-After'), 10) || (_attempt + 1)) * 1000;
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    /* All retries exhausted with null resp (network/abort/timeout). Treat
     * as a transient fetch failure: keep whatever we already rendered from
     * cache and bail out — caller decides whether to show the retry UI. */
    if (!resp) {
      debugLog(`Load conv ${convId}: fetch failed after retries (network/timeout)`, 'warn');
      if (cacheHit) {
        /* ★ The cache paint was NEVER server-verified — don't let it pose as
         *   final truth. Restore _needsLoad (Phase-1 cleared it optimistically)
         *   so the NEXT open re-verifies instead of early-returning this
         *   unverified copy forever; keep the "verifying" dim when we hold
         *   server-issued evidence the cache is outdated (_cacheKnownStale);
         *   and schedule a bounded self-heal retry while the conv stays open. */
        conv._needsLoad = true;
        if (!conv._cacheKnownStale) _setCacheVerifying(convId, false);
        _scheduleConvVerifyRetry(convId);
      } else {
        /* No cache paint at all — nothing provisional to flag. */
        delete conv._cacheKnownStale;
        _setCacheVerifying(convId, false);
      }
      if (!cacheHit && convId === activeConvId) {
        const inner = document.getElementById('chatInner');
        if (inner && conv._needsLoad && conv.messages.length === 0) {
          inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.7"><div class="welcome-icon">${Icon('zap', 40)}</div><h2>Failed to load conversation</h2><p>Network timeout — server unreachable.</p><p style="margin-top:1em"><button onclick="loadConversation('${convId}')" class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer">${Icon('refresh', 13)} Retry</button></p></div>`;
        }
      }
      return conv;
    }
    if (!resp.ok) {
      /* ── 404 ghost: conversation exists in sidebar but not on server ──
       * This happens when a conv was created locally but never synced to the DB
       * (e.g. disk I/O error, race condition, or the conv was only ever empty).
       * Without cleanup, the conv stays in the sidebar forever with _needsLoad=true,
       * and every click triggers another 404 → permanent "redirect to New Chat" loop.
       * Fix: mark it as fully loaded (empty) so _purgeEmptyConvs can remove it. */
      if (resp.status === 404) {
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 404 NOT FOUND — conversation not on server`, 'warn');
        /* Clean up stale cache entry too */
        ConvCache.remove(convId);
        /* If this is the active conversation, show a user-friendly message */
        if (convId === activeConvId) {
          const inner = document.getElementById('chatInner');
          if (inner) {
            inner.innerHTML = `<div class="welcome" id="welcome"><div class="welcome-icon">${Icon('zap', 40)}</div><h2>Conversation Not Found</h2><p>This conversation (<code>${convId}</code>) was not saved to the server.<br>It may have been created during a server error or was never synced.</p><p style="margin-top:1em"><button onclick="deleteConversation('${convId}')" class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer">${Icon('trash', 13)} Remove from sidebar</button>&nbsp;&nbsp;<button onclick="newChat()" class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer">${Icon('rocket', 13)} New Chat</button></p></div>`;
          }
        }
        /* Remove the orphan from the in-memory array + sidebar */
        conversations = conversations.filter(c => c.id !== convId);
        if (typeof renderConversationList === 'function') renderConversationList();
      }
      /* ★ Same unverified-cache contract as the !resp branch above: the
       *   server refused / was unreachable, so the cache paint was never
       *   verified. (Skipped for the 404 ghost path — that conv is gone.) */
      if (cacheHit && resp.status !== 404) {
        conv._needsLoad = true;
        if (!conv._cacheKnownStale) _setCacheVerifying(convId, false);
        _scheduleConvVerifyRetry(convId);
      }
      /* If we had a cache hit, the user already sees content — just return */
      return conv;
    }
    const data = await resp.json();
    /* ★ Windowed-read pagination state (no-op for a legacy full response, i.e.
     *   data.windowed !== true). When windowed, this stamps conv._windowed +
     *   the seq cursors so scroll-up can fetch earlier messages; the caller
     *   must then NOT treat conv.messages as the complete history. */
    const _isWindowed = (typeof recordWindowState === 'function')
      ? recordWindowState(conv, data) : false;
    const serverMsgs = data.messages || [];
    const serverUpdatedAt = data.updatedAt || data.updated_at || 0;
    /* ★ Adopt the server-issued rev as this conv's CAS base. The GET reflects
     *   the authoritative row (post GET-path reconcile), so a subsequent PUT
     *   sends the matching baseRev and only 409s on a GENUINE concurrent write.
     *   IDB is demoted to a pure paint-cache: it never supplies rev, so a stale
     *   cached copy can never present a fresher-looking base than the server. */
    if (typeof data.rev === 'number') conv._serverRev = data.rev;

    /* ── Freshness check: is server data newer than what we rendered from cache? ── */
    const hasLocalData = conv.messages.length > 0;
    const isStreaming = activeStreams.has(convId);
    /* Use reduce instead of Math.max(...) to avoid stack overflow on huge conversations */
    const localNewest = hasLocalData ? conv.messages.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;
    const serverNewest = serverMsgs.length > 0 ? serverMsgs.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;

    /* ★ Detect local activity that happened DURING the server fetch
     *   (sendMessage pushed an optimistic user/assistant msg, or
     *   startAssistantResponse set activeTaskId).  This must NOT be wiped
     *   by the server response — the server's snapshot was taken before
     *   that local push and overwriting would erase the user's just-typed
     *   message from chatInner until the next refresh.
     *
     *   Conditions for "local has fresh activity that server doesn't know about yet":
     *     a) conv.messages grew during the fetch (optimistic user/assistant push), OR
     *     b) the newest local timestamp moved forward, OR
     *     c) activeTaskId was set during the fetch (POST returned with a task)
     */
    const _localGrewDuringFetch = conv.messages.length > _preFetchMsgCount;
    const _localTsMovedDuringFetch = localNewest > _preFetchNewest;
    const _activeTaskIdAppearedDuringFetch =
      !_preFetchActiveTaskId && !!conv.activeTaskId;
    const _hasFreshLocalActivity =
      _localGrewDuringFetch || _localTsMovedDuringFetch || _activeTaskIdAppearedDuringFetch;

    /* Treat local data as "unsynced" if either:
     *   (a) the user pushed something during this fetch (cache hit or not), or
     *   (b) the originally-loaded data wasn't from cache and is newer than server.
     *
     *   ★ FIX: Previously this was gated by `!cacheHit`, which silently
     *   suppressed (a) — when sendMessage pushed an optimistic user msg
     *   during a Phase 2 fetch right after a cache hit, the user msg's
     *   newer timestamp was ignored and the branch below overwrote it.
     *   Symptom: user sends a message, only the assistant streams in
     *   chatInner; the user's prompt only reappears after manual refresh
     *   when the persisted server data is reloaded. */
    /* ★ Durable pending-sync tail (poor-network send failure): the message was
     *   marked _pendingSync and persisted to IndexedDB because its send POST
     *   failed and the rescue PUT never landed. On this reload the server copy
     *   is SHORTER (it never got the message) — treat local as authoritative so
     *   the OVERWRITE branch can't wipe it, and let KEEP_LOCAL re-sync it. */
    const _localHasPendingSync = convHasPendingSync(conv);
    /* ★ KEEP_LOCAL fires ONLY on genuine un-acked local writes — either activity
     *   that happened DURING this fetch (_hasFreshLocalActivity) or a durable
     *   pending-sync tail (_localHasPendingSync). The old third disjunct
     *   `localNewest > serverNewest` was a pure WALL-CLOCK tiebreaker: a stale
     *   IDB/in-memory copy whose max message timestamp merely exceeded the
     *   server's would win KEEP_LOCAL and then get PUT back, re-clobbering fresh
     *   server truth (the wrong-data-on-reconnect root cause). It is REMOVED —
     *   freshness is now decided by the server-issued monotonic `rev` (adopted
     *   at :data.rev), never by comparing clocks that can skew or be inflated by
     *   an optimistic push. Genuine local work is still protected by the two
     *   real signals above; a merely newer-looking timestamp is not. */
    const localHasUnsynced =
      _hasFreshLocalActivity ||
      _localHasPendingSync;

    /* ── One-line reconciliation snapshot ──
     *  Logged BEFORE the branch dispatch so that postmortem of any
     *  message-disappearance bug has a complete picture: what the cache
     *  loaded, what the server returned, what local activity happened
     *  during the fetch, and which branch the code is about to take.
     *  Server-side `/api/client-error` mirrors warn/error to logs/app.log. */
    const _reconBranch =
      localHasUnsynced ? 'KEEP_LOCAL'
      : (conv.activeTaskId && hasLocalData) ? 'MERGE_ACTIVE_TASK'
      : (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId && !_hasFreshLocalActivity)) ? 'OVERWRITE'
      : 'NOOP';
    console.info(
      `[loadConvMsgs] 📊 Phase2 reconcile conv=${convId.slice(0,8)} ` +
      `cacheHit=${cacheHit} preLen=${_preFetchMsgCount} curLen=${conv.messages.length} ` +
      `serverLen=${serverMsgs.length} ` +
      `preNewest=${_preFetchNewest} curNewest=${localNewest} serverNewest=${serverNewest} ` +
      `preTaskId=${_preFetchActiveTaskId ? _preFetchActiveTaskId.slice(0,8) : 'null'} ` +
      `curTaskId=${conv.activeTaskId ? conv.activeTaskId.slice(0,8) : 'null'} ` +
      `streaming=${activeStreams.has(convId)} ` +
      `freshLocal=${_hasFreshLocalActivity}(grew=${_localGrewDuringFetch},ts=${_localTsMovedDuringFetch},task=${_activeTaskIdAppearedDuringFetch}) ` +
      `→ branch=${_reconBranch}`
    );

    /* ★ Always-on translation merge: regardless of which branch below
     *   handles the main message-list reconciliation, copy server-side
     *   translations (translatedContent + showingTranslation + translateDone
     *   + translateModel + originalContent) into matching local messages.
     *   This covers endpoint-mode convs where the backend committed
     *   translatedContent for planner / worker / critic turns but the IDB
     *   cache was populated before those commits ran.  Without this pass
     *   the user sees English until the cache expires, even though the
     *   server has the Chinese ready.  Matches by index + role identity +
     *   content equality to avoid resurrecting stale translations.
     */
    /* Delegates the per-message field work to the SHARED reducer
     * core/conv_reducers.js::_mergeTranslationFields — the same one the
     * event-driven notify path (_verifyActiveConvFromServer) uses. The field
     * list (deliverable translatedContent + display flags, per-round
     * segments[].translatedText, the _translatePartialByRound sidecar) and the
     * same-turn identity guard live there ONCE, so the on-open lane and the
     * no-refresh lane can never drift apart the way the terminal-metadata list
     * did before _mergeTerminalTurnFields. This wrapper keeps the array-level
     * alignment + the merged count for the log line below. */
    const _mergeServerTranslations = (sourceMsgs, destMsgs) => {
      if (!Array.isArray(sourceMsgs) || !Array.isArray(destMsgs)) return 0;
      const overlap = Math.min(sourceMsgs.length, destMsgs.length);
      let merged = 0;
      for (let i = 0; i < overlap; i++) {
        merged += _mergeTranslationFields(destMsgs[i], sourceMsgs[i]);
      }
      return merged;
    };

    if (localHasUnsynced) {
      console.warn(`[loadConvMsgs] ⚠️ KEPT local data for conv=${convId.slice(0,8)} — ` +
        `local has ${conv.messages.length} msgs (newest=${new Date(localNewest).toISOString()}) ` +
        `vs server ${serverMsgs.length} msgs (newest=${new Date(serverNewest).toISOString()}) ` +
        `freshLocalActivity=${_hasFreshLocalActivity} (grew=${_localGrewDuringFetch} ` +
        `tsMoved=${_localTsMovedDuringFetch} taskAppeared=${_activeTaskIdAppearedDuringFetch}). ` +
        `${_hasFreshLocalActivity ? 'Skipping resync — backend send/start in flight owns next write.' : 'Will re-sync to server.'}`);
      /* ★ Skip the resync when fresh local activity caused this branch:
       *   sendMessage()/startAssistantResponse() are mid-flight and the
       *   backend's /api/chat/send (or /api/chat/start) will persist the
       *   optimistic message itself.  A racing PUT here would re-upload
       *   the un-translated user msg and could overwrite the backend's
       *   freshly-committed translatedContent. */
      if (!_hasFreshLocalActivity) {
        /* A durable pending-sync tail (poor-network send failure carried across
         * a reload) re-syncs through the retry poller so a still-flaky network
         * keeps re-attempting until the PUT lands; otherwise a one-shot sync. */
        if (_localHasPendingSync) {
          _flushPendingSyncs('reload_keep_local');
          _startPendingSyncPolling();
        } else {
          syncConversationToServer(conv);
        }
      }
      // Even when keeping local, merge server translations in — they are
      // strictly additive and can't harm unsynced newer content.
      const _mergedHU = _mergeServerTranslations(serverMsgs, conv.messages);
      if (_mergedHU > 0) {
        console.info(`[loadConvMsgs] 🈯 Merged ${_mergedHU} server translation(s) ` +
          `into local-unsynced conv=${convId.slice(0,8)}`);
      }
      /* ★ FIX: settle load-state like every other branch (MERGE_ACTIVE_TASK /
       *   OVERWRITE both do this). Without it, a KEEP_LOCAL reconcile leaves
       *   _needsLoad truthy and _serverMsgCount stale, so a later refocus /
       *   cross-device poll re-enters Phase-2 and — once the fresh-activity
       *   window has closed — takes the OVERWRITE branch, blanking the
       *   just-sent local message; and the stale (higher) _serverMsgCount
       *   makes syncConversationToServer's count-drop guard silently drop a
       *   subsequent legitimate edit/truncate. Use the local length so a
       *   longer local tail (the unsynced msgs we just KEPT) is authoritative. */
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
    } else if (conv.activeTaskId && hasLocalData
               && !activeStreams.has(convId) && serverMsgs.length < conv.messages.length
               && _openConvMayHoldOrphanGhost(conv, convId)) {
      /* ★ Adopt a SHORTER authoritative list FIRST — before the checkpoint
       *   "upgrade" block below can mutate the ghost tail. The backend GET-path
       *   reconcile already SWEPT an orphaned trailing empty-assistant ghost
       *   (a stale activeTaskId is set but NO stream is live, and this branch
       *   was reached with non-fresh local). Without this early adopt, the
       *   upgrade block copies the server's PREVIOUS settled reply into the
       *   empty ghost, producing a duplicate bubble instead of removing it.
       *   Narrowly gated on the pre-mutation orphan-ghost verdict; live-stream
       *   and KEEP_LOCAL are unreachable here, so no connectToTask ref is
       *   orphaned. */
      conv.messages = serverMsgs;
      console.info(`[loadConvMsgs] 🧹 MERGE_ACTIVE_TASK adopted reconciled server list ` +
        `(${serverMsgs.length} msgs) for conv=${convId.slice(0,8)} — swept an orphaned ` +
        `empty-assistant ghost tail.`);
      try { ConvCache.put(conv); } catch (_e) {
        debugLog(`[conversations] ConvCache.put failed (MERGE_ACTIVE_TASK adopt) conv=${convId.slice(0,8)}: ${_e && _e.message}`, 'warn');
      }
      if (convId === activeConvId && typeof renderChat === 'function') {
        window.ConvView.replaceAll(conv.id, { forceScroll: false });
      }
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
    } else if (conv.activeTaskId && hasLocalData) {
      /* ★ FIX: Active task with stale local data (e.g. IDB cache from before
       *   task started).  We can't replace conv.messages (would orphan the
       *   assistantMsg ref held by connectToTask), but we CAN merge server
       *   checkpoint data into the existing assistant message so the UI shows
       *   accumulated content immediately — instead of "Waiting…" until SSE. */
      const lastLocal = conv.messages[conv.messages.length - 1];
      if (lastLocal && lastLocal.role === 'assistant' && serverMsgs.length > 0) {
        const lastServer = serverMsgs[serverMsgs.length - 1];
        if (lastServer && lastServer.role === 'assistant') {
          /* Only upgrade: server content longer than local → server had checkpoint */
          if ((lastServer.content || '').length > (lastLocal.content || '').length) {
            lastLocal.content = lastServer.content;
          }
          if ((lastServer.thinking || '').length > (lastLocal.thinking || '').length) {
            lastLocal.thinking = lastServer.thinking;
          }
          if (lastServer.toolRounds?.length && !lastLocal.toolRounds?.length) {
            lastLocal.toolRounds = lastServer.toolRounds;
          }
          /* §7: no stream buffer — showStreamingUIForConv reads the document. */
        }
      }
      /* ★ FIX: Merge translatedContent from server messages into local messages.
       *   When entering this branch (activeTaskId set), we keep local messages
       *   to avoid orphaning refs, but the IDB cache may be stale and missing
       *   translations that the server has (from _commit_translation_to_db).
       *   Without this merge, translations disappear when viewing a conv with
       *   a stale activeTaskId, then get unnecessarily regenerated.
       *   Uses the strict identity check from _mergeServerTranslations. */
      const _mergedAT = _mergeServerTranslations(serverMsgs, conv.messages);
      if (_mergedAT > 0) {
        console.info(`[loadConvMsgs] 🈯 Merged ${_mergedAT} server translation(s) ` +
          `into active-task conv=${convId.slice(0,8)}`);
      }
      /* Also merge non-translation fields the local copy may lack. The
       * TERMINAL turn-metadata field list lives in exactly ONE place —
       * core/conv_reducers.js::_mergeTerminalTurnFields (shared with
       * cross_tab_sync Case 2 + init_tasks Case B/F) so it can never
       * drift a fourth time. Semantics unchanged from the inline list
       * this replaces: fill-if-missing, apiRounds upgrade-if-longer —
       * the fields the finish bar / cost popover read (apiRounds,
       * _taskId, cost, provider_id, preset, thinkingDepth, modified*,
       * fallback*). A local message cached BEFORE the turn's terminal
       * sync lacks them, and this keep-local branch is the ONLY top-up a
       * continuously-busy conv ever gets (the OVERWRITE branch never
       * fires while a task stays active). The surgical repaint trigger
       * for these late arrivals is the apiRounds/_taskId/usage fold in
       * _msgFingerprint (chat_render.js). */
      const _mergeLen = Math.min(conv.messages.length, serverMsgs.length);
      for (let _mi = 0; _mi < _mergeLen; _mi++) {
        _mergeTerminalTurnFields(conv.messages[_mi], serverMsgs[_mi]);
      }
      /* ★ FIX (autopilot VU + post-finish messages invisible after reload):
       *   When the IDB cache predates a finished autopilot follow-up, conv
       *   has fewer messages than the server but conv.activeTaskId is still
       *   set from server settings.  This branch previously merged only
       *   metadata at overlapping indices and never appended the trailing
       *   server messages — so the synthetic VU user message + the
       *   autopilot follow-up's assistant reply were silently dropped.
       *   Append them now, but ONLY when no stream is actually live for
       *   this conv (otherwise an orphaned assistantMsg ref held by
       *   connectToTask could be invalidated mid-stream). */
      if (!activeStreams.has(convId) && serverMsgs.length > conv.messages.length) {
        const _appendStart = conv.messages.length;
        const _appended = serverMsgs.slice(_appendStart);
        conv.messages.push(..._appended);
        console.info(`[loadConvMsgs] 📥 MERGE_ACTIVE_TASK appended ${_appended.length} ` +
          `trailing server msg(s) (idx ${_appendStart}..${serverMsgs.length - 1}) into ` +
          `conv=${convId.slice(0,8)} — cache predated post-finish writes ` +
          `(autopilot VU / queue dispatch / late persist).`);
        try { ConvCache.put(conv); } catch (_e) {
          debugLog(`[conversations] ConvCache.put failed (MERGE_ACTIVE_TASK append) conv=${convId.slice(0,8)}: ${_e && _e.message}`, 'warn');
        }
        if (convId === activeConvId && typeof renderChat === 'function') {
          window.ConvView.replaceAll(conv.id, { forceScroll: false });
        }
      }
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
    } else if (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId && !_hasFreshLocalActivity)) {
      /* Apply server data when:
       *  - No local data (first load, no cache)
       *  - Not actively streaming AND no active task starting AND
       *    no fresh local activity occurred during the fetch
       * ★ FIX: Previously, `cacheHit` bypassed the activeTaskId guard, causing
       *   Phase 2 server response to overwrite conv.messages even when
       *   startAssistantResponse had just pushed an assistant message and was
       *   awaiting POST /api/chat/start. This race condition caused connectToTask
       *   to see a user message as the last message → bail out → no SSE stream
       *   → sidebar shows pulsing dot but no Agent icon in chat area.
       *   Now we ALWAYS re-check activeTaskId/activeStreams at overwrite time.
       * ★ FIX 2: Also gate on _hasFreshLocalActivity — sendMessage pushes the
       *   optimistic user msg BEFORE awaiting POST, so activeTaskId is still
       *   null at this branch evaluation.  Without this guard, the user's
       *   just-typed prompt is wiped from chatInner; only the assistant's
       *   streamed reply is visible until manual refresh restores the user
       *   msg from the persisted server copy. */
      const cacheIsStale = !cacheHit ||
        serverMsgs.length !== conv.messages.length ||
        serverUpdatedAt > (conv._cachedUpdatedAt || 0) ||
        /* ★ The GET-path segments rehydrate is display-only (no count/updatedAt
         *   change), so a segment-less cache would otherwise be judged FRESH and
         *   the server's rehydrated tool/thinking timeline discarded. Treat
         *   "server carries segments the local copy lacks" as stale so the
         *   backstop always surfaces on historical turns. */
        _serverHasSegmentsLocalLacks(serverMsgs, conv.messages) ||
        /* ★ Symmetric to the segments backstop: server-side auto-translate
         *   commits translatedContent / segments[].translatedText AFTER the
         *   turn settled, and a client PUT of a still-English copy can leave
         *   the cache's updatedAt >= server's, hiding the change from the
         *   timestamp disjunct. Treat "server has a translation the local copy
         *   lacks" as stale so the reopened conv adopts the Chinese instead of
         *   rendering stale English narration. (The cache-fresh else-branch
         *   ALSO merges translations in-place; this disjunct covers the case
         *   where a full server adopt is cleaner.) */
        _serverHasTranslationLocalLacks(serverMsgs, conv.messages);

      if (cacheIsStale) {
        /* ★ Diagnostic: if we're about to wipe a non-empty local that
         *   doesn't match the server, leave a warn breadcrumb (forwarded
         *   to logs/app.log via /api/client-error).  This is the place
         *   the chatInner-disappearance bug was born; if it ever recurs,
         *   the postmortem starts here. */
        if (hasLocalData && conv.messages.length > serverMsgs.length) {
          if (typeof debugLog === 'function') {
            debugLog(
              `[loadConvMsgs] ⚠️ OVERWRITE conv=${convId.slice(0,8)} ` +
              `wiping local len=${conv.messages.length} → server len=${serverMsgs.length} ` +
              `(cacheHit=${cacheHit} preLen=${_preFetchMsgCount} ` +
              `freshLocal=${_hasFreshLocalActivity} taskId=${conv.activeTaskId || 'null'} ` +
              `streaming=${activeStreams.has(convId)})`,
              'warn'
            );
          }
        }
        conv.messages = serverMsgs;
        conv.title = data.title || conv.title;
        conv.updatedAt = serverUpdatedAt || conv.updatedAt;
        const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
        _applySettingsToConv(conv, data.settings);
        conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;

        if (cacheHit) {
          console.info(`[loadConvMsgs] 🔄 Cache STALE for conv=${convId.slice(0,8)} — ` +
            `server has ${serverMsgs.length} msgs (updatedAt=${serverUpdatedAt}), re-rendering`);
        }
      } else {
        /* Cache-fresh path: message count+timestamp matched, so we are not
         * replacing conv.messages.  But server-side auto-translate may have
         * committed translatedContent into the DB AFTER the cache was
         * written (this is the common endpoint-mode case).  Merge those
         * translations in-place so the user sees Chinese immediately. */
        const _mergedFresh = _mergeServerTranslations(serverMsgs, conv.messages);
        if (_mergedFresh > 0) {
          console.info(`[loadConvMsgs] 🈯 Cache FRESH but merged ${_mergedFresh} ` +
            `server translation(s) — conv=${convId.slice(0,8)}`);
          // Trigger re-render so Chinese appears now rather than on next action
          if (convId === activeConvId) {
            const _active = conversations.find(c => c.id === convId);
            if (_active) window.ConvView.replaceAll(_active.id, { forceScroll: false });
          }
          ConvCache.put(conv);  // persist merged translations into cache
        } else {
          console.info(`[loadConvMsgs] ✅ Cache FRESH for conv=${convId.slice(0,8)} — no re-render needed`);
        }
      }

      conv._needsLoad = false;
      /* ★ Verify landed — cancel any pending self-heal retry for this conv. */
      conv._verifyRetryCount = 0;
      clearTimeout(_convVerifyRetryTimers[convId]);
      delete _convVerifyRetryTimers[convId];
      /* ★ Windowed-read truncation guard: when the server served only the tail
       *   window (N msgs) of a longer conversation, stamp the sync baseline
       *   from the AUTHORITATIVE full count (data.totalCount), NOT the window
       *   length — otherwise syncConversationToServer's count-drop guard reads
       *   `N < N` = false and a later PUT of the N-msg tail TRUNCATES the full
       *   server conversation (permanent head loss on every windowed open). */
      conv._serverMsgCount = _isWindowed && typeof data.totalCount === 'number'
        ? data.totalCount
        : Math.max(serverMsgs.length, conv.messages.length);

      /* ★ Update IndexedDB cache with authoritative server data */
      ConvCache.put(conv);

      /* ★ Re-attach compaction markers from transcript_archive — see
       *    static/js/compaction-viewer.js. Fire-and-forget; the viewer
       *    will trigger a re-render once markers are populated. */
      try {
        if (typeof attachCompactionMarkersToConversation === 'function') {
          attachCompactionMarkersToConversation(convId, conv.messages).then(() => {
            /* ★ SCROLL FIX: scroll-preserving in-place repaint — see the
             *   Phase-1 compaction callback for the rationale. */
            if (convId === activeConvId && typeof _bgRefreshChat === 'function') {
              _bgRefreshChat(conv);
            }
          }).catch(e => console.debug('[compaction] attach (server) failed:', e));
        }
      } catch (e) { console.debug('[compaction] attach (server) hook error:', e); }

      /* ★ Hydrate renderable artifacts (md/html/svg) — chips appear next
       *    to the file-changes bar and open the right-side panel.  See
       *    lib/artifacts/ + static/js/artifacts.js.  Fire-and-forget. */
      try {
        if (typeof window.Artifacts !== 'undefined' && window.Artifacts.hydrateConversation) {
          window.Artifacts.hydrateConversation(conv).then(() => {
            /* ★ SCROLL FIX: scroll-preserving in-place repaint — never
             *   force-scroll on open. */
            if (convId === activeConvId && typeof _bgRefreshChat === 'function') {
              _bgRefreshChat(conv);
            }
          }).catch(e => console.debug('[artifacts] hydrate (server) failed:', e));
        }
      } catch (e) { console.debug('[artifacts] hydrate (server) hook error:', e); }

      /* ★ Clear stale "server_offline" errors: if the last assistant message
       *   has finishReason='server_offline' but we just successfully loaded
       *   from the server (proving it's online), clear the misleading error text.
       *   The "Server Offline" finish badge remains as a historical marker. */
      {
        const _lastMsg = conv.messages[conv.messages.length - 1];
        if (_lastMsg && _lastMsg.role === 'assistant' &&
            _lastMsg.finishReason === 'server_offline' &&
            _lastMsg.error && errorEnvelopeKind(_lastMsg.error) === 'server_offline') {
          console.info(`[loadConvMsgs] Clearing stale server_offline error for conv=${convId.slice(0,8)}`);
          delete _lastMsg.error;
          // Re-save to IDB and server to persist the cleanup
          ConvCache.put(conv);
          syncConversationToServer(conv);
        }
      }

      debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: server=${serverMsgs.length} msgs, local=${conv.messages.length} msgs, _serverMsgCount=${conv._serverMsgCount}, cacheHit=${cacheHit}`, 'info');

      /* Hydrate image base64 from server URLs */
      _hydrateImageBase64(conv);

      /* Re-render if server data was newer (or first load with no cache) */
      if (cacheIsStale && convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          window.ConvView.replaceAll(conv.id, { forceScroll: false });
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
        }
      }
    }

    /* ★ Re-trigger HG translations for any awaiting_human rounds after load.
     *   On page refresh, translation state is lost (only in-memory). This
     *   re-fires translation for pending guidance cards so users see Chinese. */
    if (typeof _retriggerHgTranslations === 'function') {
      _retriggerHgTranslations(convId);
    }

    /* Clean up transient cache tracking field */
    delete conv._cachedUpdatedAt;
    /* ★ Phase-2 settled — the paint is now server-verified; clear the
     *   known-stale "verifying" dim (no-op when it was never set). */
    delete conv._cacheKnownStale;
    _setCacheVerifying(convId, false);
    /* ★ Unify the queued-message BAR with the transcript projection. Both the
     *   chat bubbles and the queue bar are projections of the SAME server
     *   state: dispatching a queued message MOVES it out of the message_queue
     *   table and INTO the conversation as a real user turn. The transcript is
     *   re-derived from the server right here — but several reconcile branches
     *   (notably the MERGE_ACTIVE_TASK "appended trailing server msg(s)" path)
     *   surface the dispatched bubble WITHOUT going through _checkForQueuedTask,
     *   the only other place that refreshes the mirror. That left the drained
     *   item lingering in the bar with a count that never dropped (the reported
     *   "bubble appears but the queue doesn't discharge"). Re-deriving the queue
     *   mirror from the same authority in lockstep closes the drift. Guarded so
     *   it only fires for the OPEN conversation and never recurses into a load. */
    if (convId === activeConvId && typeof _refreshServerQueue === 'function') {
      try { _refreshServerQueue(convId); }
      catch (e) { console.debug('[loadConvMsgs] queue mirror refresh failed:', e); }
    }
    return conv;
  } catch (e) {
    debugLog(`Load conv ${convId}: ${e.message}`, "warn");
    /* If we had a cache hit, the user already sees content — just log the fetch failure */
    if (cacheHit) {
      /* ★ Same unverified-cache contract as the !resp branch above: the cache
       *   paint was never server-verified — restore _needsLoad so the next
       *   open re-verifies, keep the dim when known-stale, self-heal retry. */
      conv._needsLoad = true;
      if (!conv._cacheKnownStale) _setCacheVerifying(convId, false);
      _scheduleConvVerifyRetry(convId);
      console.warn(`[loadConvMsgs] ⚠️ Server fetch failed for ${convId.slice(0,8)} but cache was served: ${e.message}`);
      return conv;
    }
    delete conv._cacheKnownStale;
    _setCacheVerifying(convId, false);
    /* Network errors with no cache: show a retry-friendly message if this is the active conv */
    if (convId === activeConvId) {
      const inner = document.getElementById('chatInner');
      if (inner && conv._needsLoad && conv.messages.length === 0) {
        inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.7"><div class="welcome-icon">⚡</div><h2>Failed to load conversation</h2><p>${e.message}</p><p style="margin-top:1em"><button onclick="loadConversation('${convId}')" class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer">🔄 Retry</button></p></div>`;
      }
    }
    return conv;
  }
}

/**
 * Force-recover a conversation from server, ignoring local state.
 * Use when local messages appear truncated or missing.
 * Can be called from browser console: forceRecoverFromServer(convId)
 */
async function forceRecoverFromServer(convId) {
  convId = convId || activeConvId;
  if (!convId) { debugLog('[recover] No conversation ID', 'error'); return null; }
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) { debugLog(`[recover] Conversation not found: ${convId}`, 'error'); return null; }
  try {
    const data = await Api.conversations.get(convId);
    if (!data) { debugLog('[recover] Server returned no data', 'error'); return null; }
    const serverMsgs = data.messages || [];
    const localMsgs = conv.messages || [];
    console.log(`[recover] Conv ${convId}: local has ${localMsgs.length} msgs, server has ${serverMsgs.length} msgs`);
    if (serverMsgs.length > localMsgs.length) {
      conv.messages = serverMsgs;
      conv.title = data.title || conv.title;
      conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
      conv._serverMsgCount = serverMsgs.length;
      /* ★ Adopt the server rev so a later PUT carries the correct baseRev
       *   (this is a server GET — its rev is authoritative). */
      if (typeof data.rev === 'number') conv._serverRev = data.rev;
      conv._needsLoad = false;
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      _applySettingsToConv(conv, data.settings);
      conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
      saveConversations(convId);
      if (convId === activeConvId) {
        window.ConvView.replaceAll(conv.id, { forceScroll: false });
        if (typeof _restoreConvToolState === 'function') _restoreConvToolState(conv);
      }
      console.log(`[recover] ✅ Restored ${serverMsgs.length} messages (was ${localMsgs.length})`);
      return conv;
    } else {
      console.log(`[recover] ℹ️ Server has same or fewer messages — no recovery needed`);
      return conv;
    }
  } catch (e) {
    debugLog(`[recover] Failed: ${e.message}`, 'error');
    return null;
  }
}

/**
 * Audit all conversations for data loss: compare local message count vs server.
 * Run from console: auditConversations()
 */
async function auditConversations() {
  console.log('[audit] Checking all conversations for data loss...');
  const issues = [];
  for (const conv of conversations) {
    try {
      const data = await Api.conversations.get(conv.id);
      if (!data) continue;
      const serverCount = (data.messages || []).length;
      const localCount = (conv.messages || []).length;
      if (serverCount > localCount) {
        issues.push({ id: conv.id, title: conv.title, localCount, serverCount, diff: serverCount - localCount });
        console.warn(`[audit] ⚠️ "${conv.title}" — local: ${localCount}, server: ${serverCount} (+${serverCount - localCount} recoverable)`);
      }
    } catch (e) { /* skip */ }
  }
  if (issues.length === 0) {
    console.log('[audit] ✅ No data loss detected — all conversations match server');
  } else {
    console.log(`[audit] Found ${issues.length} conversation(s) with recoverable data:`);
    console.table(issues);
    console.log('[audit] Run forceRecoverFromServer("conv_id") to recover, or recoverAll() to fix all');
  }
  return issues;
}

/**
 * Batch recover all conversations that have more messages on server than locally.
 * Run from console: recoverAll()
 */
async function recoverAll() {
  const issues = await auditConversations();
  if (issues.length === 0) return;
  let recovered = 0;
  for (const issue of issues) {
    const result = await forceRecoverFromServer(issue.id);
    if (result) recovered++;
    await new Promise(r => setTimeout(r, 200)); /* small delay to not hammer server */
  }
  console.log(`[recoverAll] ✅ Recovered ${recovered}/${issues.length} conversations`);
}

