/* ═══════════════════════════════════════════════════════════════════
   streaming render — extracted from ui.js (split 2026-05-28)

   Streaming-message rendering: autopilot VU events, surgical DOM updates, lazy-load older messages.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Locate an existing VU streaming message by its stable `_msgId`.
 * Used by `_handleAutopilotVuEvent` to route streaming updates from the
 * parent task's SSE pipe into the right user-message object on the
 * conversation.
 */
function _findVuMsgById(conv, vuMsgId) {
  if (!conv || !Array.isArray(conv.messages) || !vuMsgId) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._msgId === vuMsgId && m._isVirtualUser) {
      return { msg: m, idx: i };
    }
  }
  return null;
}

/**
 * Remove a still-streaming VU placeholder bubble LOCALLY (client-side).
 *
 * WHY this exists separate from the `autopilot_vu_cancel` handler: when the
 * user clicks Stop WHILE the VU is streaming, the SSE reader is torn down by
 * `controller.abort()` BEFORE the backend's `autopilot_vu_cancel` frame can be
 * read — so the splice in `_handleAutopilotVuEvent` never runs and a dangling
 * `_streamingVu:true` bubble is left rendering the frozen "Autopilot…" pulse
 * forever (only a page reload cleared it, via the idb-cache `_streamingVu`
 * filter). The Stop handler + `finishStream`'s autopilot branch call THIS to do
 * the removal locally instead of waiting for an event that can't arrive.
 *
 * Mirrors the `autopilot_vu_cancel` teardown (splice + `#streaming-msg`
 * removal + `twStop` + `buildTurnNav` + persist), and is a no-op unless the
 * conversation's TAIL message is a streaming VU bubble (so it can never remove
 * a settled VU turn or a real message).
 *
 * ★ BATON PRESERVED: this removes only the message object. `conv._apPendingBaton`
 * (the authoritative conv-level autopilot follow-up baton) is a conv FIELD, not
 * a message — the splice cannot touch it, and this function deliberately never
 * clears it. So `_findAutopilotPendingCarrier` still resolves any pending
 * follow-up after the ghost bubble is gone (the
 * test_frontend_autopilot_baton_survives_splice.py contract).
 *
 * @returns {boolean} true if a streaming VU bubble was removed.
 */
function _removeStreamingVuBubbleIfTail(conv, convId) {
  if (!conv || !Array.isArray(conv.messages) || !conv.messages.length) return false;
  const last = conv.messages[conv.messages.length - 1];
  if (!last || !last._isVirtualUser || !last._streamingVu) return false;
  conv.messages.pop();
  console.info(
    `[Autopilot VU] ✂ local splice on stop — removed streaming placeholder ` +
    `vuMsgId=${(last._msgId || '').slice(0,12)} for conv=${(convId || conv.id || '').slice(0,8)}`
  );
  if (activeConvId === convId) {
    const sm = document.getElementById("streaming-msg");
    if (sm) { try { sm.remove(); } catch (e) { /* already detached */ } }
    if (typeof twStop === "function") { try { twStop(convId); } catch (e) { /* idempotent */ } }
    if (typeof buildTurnNav === "function") buildTurnNav(conv);
  }
  if (typeof saveConversations === "function") saveConversations(convId);
  try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
  catch (e) { /* non-fatal */ }
  return true;
}
if (typeof window !== 'undefined') window._removeStreamingVuBubbleIfTail = _removeStreamingVuBubbleIfTail;

/**
 * Surgically re-render a single message by index.  Used by the
 * autopilot VU streaming pipeline so each delta / tool_start /
 * tool_result update repaints just that bubble — far cheaper than a
 * full `renderChat()` and preserves the parent task's
 * `#streaming-msg` element + scroll position.
 */
function _surgicalRerenderMsg(convId, idx) {
  if (activeConvId !== convId) return;
  const conv = conversations.find(c => c.id === convId);
  if (!conv || !conv.messages || idx < 0 || idx >= conv.messages.length) return;
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  const el = document.getElementById("msg-" + idx);
  const html = renderMessage(conv.messages[idx], idx);
  if (el) {
    el.outerHTML = html;
  } else {
    /* Insert AFTER the parent's #streaming-msg bubble so the natural
     * turn order is preserved: parent_user → parent_assistant
     * (#streaming-msg, still streaming) → VU_user (new) → next
     * assistant placeholder.  When there's no live streaming-msg
     * (e.g. SSE cold replay), append at the end of the chat. */
    const streaming = document.getElementById("streaming-msg");
    if (streaming && streaming.parentNode === inner) {
      streaming.insertAdjacentHTML("afterend", html);
    } else {
      inner.insertAdjacentHTML("beforeend", html);
    }
  }
  if (typeof isNearBottom === "function" && isNearBottom(80)) scrollToBottom();
}

/**
 * Push the empty VU message into conv.messages and stand up the SHARED
 * streaming substrate for it — the SAME `#streaming-msg` + live-session
 * (streamSessions) + elapsed-timer machinery the worker turn uses (mirrors
 * the worker→critic handoff in dispatchSSEEvent).  This is what makes the
 * autopilot reply
 * render *identically to the agent*: incremental markdown, thinking block,
 * tool rounds, and the live elapsed-time bar.
 *
 * The parent worker turn's `#streaming-msg` is finalized to a static bubble
 * first (its finish bar is refreshed later when the parent `done` event
 * lands — see the done handler's autopilot branch in sse_pipeline.js).
 *
 * @param {Object} [parentMessage] — the SETTLED parent worker assistant dict
 *   (== the parent `done` event's `committedMessage`), delivered early on
 *   `autopilot_vu_start`. When present, projected onto the parent assistant
 *   BEFORE it is finalized so its finish bar (model / usage / cost /
 *   finishReason) is complete at handoff — not incomplete until the parent
 *   `done` event fires tens of seconds later (that event is withheld until the
 *   whole VU stream completes). Absent on backend skip paths → the parent keeps
 *   its transient buffer, and the later `done` re-render still fills the bar.
 * @returns {{msg:Object, idx:number}} the VU message entry.
 */
function _beginVuStreaming(convId, conv, vuMsgId, parentMessage) {
  const vuMsg = {
    role: "user",
    content: "",
    thinking: "",
    _msgId: vuMsgId,
    _isVirtualUser: true,
    _streamingVu: true,
    timestamp: Date.now(),
    toolRounds: [],
  };

  if (activeConvId === convId) {
    /* Finalize the parent worker's streaming bubble so the VU can own the
     * single `#streaming-body` element.  Walk back to the nearest non-VU
     * assistant (the worker turn that just stopped). */
    const sm = document.getElementById("streaming-msg");
    if (sm) {
      let parentAssistant = null;
      for (let i = conv.messages.length - 1; i >= 0; i--) {
        const m = conv.messages[i];
        if (m && m.role === "assistant" && !m._isVirtualUser) { parentAssistant = m; break; }
      }
      if (parentAssistant) {
        /* ★ Project the parent worker's SETTLED finish metadata NOW (delivered
         * on vu_start as `parentMessage`) so its finish bar renders COMPLETE at
         * handoff — model + tokens + cost + finishReason ✓. Without this the
         * early finalize below stamps a bar carrying ONLY the model tag (the
         * reported "incomplete finish bar") for the whole VU turn, because the
         * usage/cost/finishReason-bearing parent `done` event is withheld until
         * the VU stream ends. This mirrors the `done` handler's committedMessage
         * projection — same authoritative dict, delivered early. VERBATIM
         * projection: copy the settled fields onto the existing object (not
         * replace it) so frontend-local fields (_translate*, _msgId, branches)
         * survive. */
        if (parentMessage && typeof parentMessage === 'object') {
          const _pm = parentMessage;
          if (_pm.content != null) parentAssistant.content = _pm.content;
          if (_pm.thinking != null) parentAssistant.thinking = _pm.thinking;
          if (Array.isArray(_pm.toolRounds)) parentAssistant.toolRounds = _pm.toolRounds;
          if (Array.isArray(_pm.segments)) parentAssistant.segments = _pm.segments;
          for (const _k of ['finishReason', 'usage', 'preset', 'toolSummary',
                            'model', 'provider_id', 'apiRounds', 'modifiedFiles',
                            'modifiedFileList', 'cost', '_taskId',
                            'fallbackModel', 'fallbackFrom', 'fallbackReason',
                            'fallbackKind', 'error', 'thinkingDepth', '_gitSha']) {
            if (_pm[_k] != null) parentAssistant[_k] = _pm[_k];
          }
        }
        /* Mark the parent so the worker turn's `done` handler still re-renders
         * its finish bar. `done` remains AUTHORITATIVE (it ships the same
         * committedMessage verbatim); this early projection just prevents the
         * incomplete-bar window on the skip-free path, and is a harmless no-op
         * repaint on `done`. When vu_start carried NO parentMessage (backend
         * skip path), the `done` re-render is the sole fill — preserved. */
        parentAssistant._vuTookOverBubble = true;
        window.ConvView.finalizeStreaming(convId, parentAssistant);
      } else {
        try { sm.remove(); } catch (e) { /* already detached */ }
      }
    }
  }

  conv.messages.push(vuMsg);
  const idx = conv.messages.length - 1;

  /* Fresh streaming buffer + elapsed timer for the VU turn. */
  if (typeof twStart === "function") twStart(convId);

  if (activeConvId === convId) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      inner.insertAdjacentHTML(
        "beforeend",
        _streamingBubbleHTML("autopilot", null, null, vuMsgId)
      );
      if (typeof buildTurnNav === "function") buildTurnNav(conv);
      if (typeof scrollToBottom === "function"
          && (typeof isNearBottom !== "function" || isNearBottom(80))) {
        scrollToBottom();
      }
    }
  }
  return { msg: vuMsg, idx };
}

/**
 * Push the accumulated VU state to the shared streaming UI (worker
 * substrate).  Projects the message document exactly like the worker
 * delta path, so the VU bubble renders with identical layout.
 */
function _flushVuStreaming(convId) {
  if (activeConvId !== convId) return;
  if (typeof twUpdate === "function") { twUpdate(convId); return; }
}

/**
 * Auto-translate a finalized VU message to the UI language (Chinese) so the
 * user reads the simulated-user reply in their language even though the VU
 * composed it in the assistant's language.  Mirrors the assistant turn's
 * auto-translate path; gated on the per-conv autoTranslate setting and the
 * shared `_isAlreadyChinese` skip (so a Chinese VU reply isn't re-translated).
 * Fire-and-forget — failure leaves the original VU text shown.
 */
function _maybeAutoTranslateVu(convId, conv, entry) {
  try {
    if (!conv || !entry || !entry.msg) return;
    const msg = entry.msg;
    if (!msg.content || msg.translatedContent || msg._translateDone) return;
    if (!convAutoTranslate(conv)) return;
    if (typeof _startAutoTranslateForMsg !== 'function') return;
    /* idx is the message's current position; _findVuMsgById gives a stable
     * lookup but the pipeline needs the array index for surgical re-render. */
    const idx = conv.messages.indexOf(msg);
    if (idx < 0) return;
    msg._translateDone = false;  // show the "translating…" indicator
    _startAutoTranslateForMsg(conv, convId, idx, msg);
  } catch (e) {
    console.warn('[Autopilot VU] auto-translate trigger failed:', e && e.message);
  }
}

/**
 * Handle the four autopilot_vu_* SSE event types.  See `_processSSELine`
 * for the contract.  The VU streams through the SAME substrate as the
 * worker (`#streaming-msg` + live session + `twUpdate`), so its reply is
 * presented exactly like an agent turn (incremental markdown, thinking,
 * tool rounds, elapsed-time bar) — just in the user lane with the
 * Autopilot avatar/label.
 */
function _handleAutopilotVuEvent(convId, ev) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.debug(`[Autopilot VU] conv=${convId.slice(0,8)} not found — dropping ${ev.type}`);
    return;
  }
  const vuMsgId = ev.vuMsgId;
  if (!vuMsgId) {
    console.warn(`[Autopilot VU] missing vuMsgId on ${ev.type}`, ev);
    return;
  }

  if (ev.type === "autopilot_vu_start") {
    /* Stand up the VU streaming bubble the moment autopilot kicks in, so
     * the user sees an "Autopilot · composing…" bubble in the USER lane
     * that then streams the reply live — identical to a worker turn.
     * In-memory ONLY: nothing is persisted until autopilot_vu_done.  If
     * the bubble already exists (reconnect / duplicate start) reuse it. */
    if (_findVuMsgById(conv, vuMsgId)) return;
    const entry = _beginVuStreaming(convId, conv, vuMsgId, ev.parentMessage);
    console.info(
      `[Autopilot VU] ▶ began VU streaming bubble vuMsgId=${vuMsgId.slice(0,12)} ` +
      `at idx=${entry.idx} for conv=${convId.slice(0,8)}`
    );
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    return;
  }

  if (ev.type === "autopilot_vu_cancel") {
    /* The VU bailed out (TASK_DONE / aborted / queued real-user msg).
     * Remove the placeholder and tear down the streaming substrate. */
    const entry = _findVuMsgById(conv, vuMsgId);
    if (!entry) return;
    conv.messages.splice(entry.idx, 1);
    console.info(
      `[Autopilot VU] ⛔ cancel — removed placeholder vuMsgId=${vuMsgId.slice(0,12)} ` +
      `for conv=${convId.slice(0,8)}`
    );
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      if (sm) { try { sm.remove(); } catch (e) { /* detached */ } }
      if (typeof twStop === "function") twStop(convId);
      if (typeof buildTurnNav === "function") buildTurnNav(conv);
    }
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    return;
  }

  if (ev.type === "autopilot_vu_done") {
    /* Replace the placeholder's fields with the authoritative final copy
     * from the backend (content + toolRounds), clear the streaming
     * decoration, and finalize the bubble (convert `#streaming-msg` →
     * static) through the same path the worker uses. */
    let entry = _findVuMsgById(conv, vuMsgId);
    if (!entry) {
      /* Shouldn't happen — the start event runs first.  Log + push as
       * a fresh tail message so the user still sees the VU reply. */
      console.warn(
        `[Autopilot VU] ✓ done but no placeholder for vuMsgId=${vuMsgId.slice(0,12)} — ` +
        `appending fresh for conv=${convId.slice(0,8)}`
      );
      const fresh = Object.assign({}, ev.vuMessage || {}, {
        _msgId: vuMsgId, _isVirtualUser: true,
      });
      delete fresh._streamingVu;
      conv.messages.push(fresh);
      entry = { msg: fresh, idx: conv.messages.length - 1 };
    } else {
      /* SETTLED state = pure projection of the ONE backend-authoritative
       * record (ev.vuMessage — the same dict _append_vu_message_to_conv
       * wrote to the DB). Rendered VERBATIM: no `|| buf.content` fallback,
       * because a local-buffer fallback makes the frontend a SECOND source
       * of truth and a stuck bubble un-diagnosable from one place. An empty
       * `content` here is a legitimate "keep going" VU reply (the backend
       * bails to VU_CANCEL, not DONE, when the VU produced nothing) — render
       * it as-is; if it's ever wrongly empty that's a backend bug to fix at
       * the source. See .tofu/skills/separation-of-concerns-directive.md. */
      const finalMsg = ev.vuMessage || {};
      entry.msg.content = finalMsg.content || "";
      entry.msg.toolRounds = Array.isArray(finalMsg.toolRounds) ? finalMsg.toolRounds : [];
      /* ★ Segments (epic pt_cb8f98b0cb9b47fb): project the backend's typed
       * timeline VERBATIM so the settled VU bubble renders the IDENTICAL agent
       * inline per-tool timeline (widened gate in chat_render.js). Without this
       * the live stream shows the timeline but the settle repaint drops it →
       * the grouped-panel snap-back. Same verbatim contract as content /
       * toolRounds above: NO local-buffer fallback. Absent (legacy backend /
       * assembly failure) → grouped render, graceful. */
      entry.msg.segments = Array.isArray(finalMsg.segments) ? finalMsg.segments : [];
      delete entry.msg._streamingVu;
      console.info(
        `[Autopilot VU] ✓ done — finalized vuMsgId=${vuMsgId.slice(0,12)} ` +
        `(${(entry.msg.content||'').length} chars, ${(entry.msg.toolRounds||[]).length} rounds) ` +
        `for conv=${convId.slice(0,8)}`
      );
    }
    if (activeConvId === convId) {
      /* Convert the live `#streaming-msg` into the settled static bubble,
       * then tear down the buffer + elapsed timer.  finalizeStreaming now
       * accepts _isVirtualUser (see conv_view.js). */
      window.ConvView.finalizeStreaming(convId, entry.msg);
      if (typeof twStop === "function") twStop(convId);
    }
    if (typeof saveConversations === "function") saveConversations(convId);
    try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
    catch (e) { /* non-fatal */ }
    /* ★ Display-language parity: the VU composes in the assistant's language,
     * but the user should SEE it in their UI language.  Reuse the same
     * assistant→Chinese auto-translate pipeline as agent turns when
     * autoTranslate is on (the _isAlreadyChinese guard skips a no-op when the
     * VU already replied in Chinese).  Fire-and-forget; renders bilingually
     * via the VU branch in chat_render.js. */
    _maybeAutoTranslateVu(convId, conv, entry);
    return;
  }

  /* autopilot_vu_event — route the inner event into the VU bubble's shared
   * stream buffer.  The bubble is normally created EAGERLY by
   * autopilot_vu_start, but we keep a LAZY-CREATION fallback here for
   * resilience: if a content-bearing event arrives before / without a
   * start event (dropped frame, cold replay), stand up the bubble now. */
  const inner = ev.inner || {};
  const itype = inner.type || "";

  let entry = _findVuMsgById(conv, vuMsgId);
  if (!entry) {
    /* Lazy-create the bubble when a content-bearing frame arrives without a
     * preceding autopilot_vu_start (reconnect / late-connect / dropped start).
     * `phase` counts as content-bearing: a rate-limited first-token stall
     * emits phase-only frames (waiting_model / retrying/限流中) for tens of
     * seconds BEFORE any delta — if a replay cursor lands inside that window
     * and we dropped them, the bubble would never materialize and the user
     * would stare at a dead warm-up state during precisely the window where
     * the phase chip is the ONLY liveness signal. The `phase` branch below
     * sets buf.phase + flushes once `entry` exists. Non-rendered interactive
     * types (stdin_request / write_approval_request / human_guidance_*) still
     * do NOT create the bubble — the VU IS the user, it hosts no widgets. */
    const _isContentBearing =
      (itype === "tool_start") ||
      (itype === "phase") ||
      (itype === "delta" && (inner.content || inner.thinking));
    if (!_isContentBearing) {
      return; // silently skip — nothing to show yet
    }
    entry = _beginVuStreaming(convId, conv, vuMsgId);
    console.info(
      `[Autopilot VU] ▶ lazy-began VU streaming bubble vuMsgId=${vuMsgId.slice(0,12)} ` +
      `at idx=${entry.idx} on inner=${itype} for conv=${convId.slice(0,8)}`
    );
  }
  const vuMsg = entry.msg;
  if (!Array.isArray(vuMsg.toolRounds)) vuMsg.toolRounds = [];

  if (itype === "delta") {
    if (inner.content) vuMsg.content = (vuMsg.content || "") + inner.content;
    if (inner.thinking) vuMsg.thinking = (vuMsg.thinking || "") + inner.thinking;
    /* §7: content/thinking live on the document (vuMsg); only phase needs
     * the session slice — mirror the worker's phase handling: content delta
     * clears the phase; thinking-only delta shows the reasoning indicator. */
    if (typeof setStreamPhase === 'function') {
      if (inner.content) setStreamPhase(convId, null);
      else if (inner.thinking) setStreamPhase(convId, { phase: "thinking_active" });
    }
  } else if (itype === "tool_start") {
    vuMsg.toolRounds.push({
      roundNum: inner.roundNum,
      query: inner.query,
      results: null,
      status: "searching",
      toolName: inner.toolName || null,
      toolCallId: inner.toolCallId || null,
      toolArgs: inner.toolArgs || null,
      llmRound: inner.llmRound != null ? inner.llmRound : null,
      _swarm: false,
    });
    if (typeof setStreamPhase === 'function') {
      setStreamPhase(convId, { phase: "tool_exec", detail: inner.query || inner.toolName || "" });
    }
  } else if (itype === "tool_result") {
    const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
    if (r) {
      r.results = inner.results;
      r.status = "done";
      if (inner.searchDiag) r.searchDiag = inner.searchDiag;
      if (inner.engineBreakdown) r.engineBreakdown = inner.engineBreakdown;
    }
  } else if (itype === "tool_progress") {
    const r = vuMsg.toolRounds.find(rr => rr.roundNum === inner.roundNum);
    if (r) {
      if (typeof r._partialOutput !== "string") r._partialOutput = "";
      r._partialOutput += (inner.chunk || "");
    }
  } else if (itype === "tool_complete") {
    const r = vuMsg.toolRounds.find(rr =>
      rr.roundNum === inner.roundNum && rr.toolCallId === inner.toolCallId);
    if (r) {
      r.toolContent = inner.toolContent || null;
      if (inner.toolTokens != null) r.toolTokens = inner.toolTokens;
      if (inner.compactionLayer) {
        r.compactionLayer = inner.compactionLayer;
        r.compactedFromChars = inner.compactedFromChars;
        r.compactedToChars = inner.compactedToChars;
      }
    }
  } else if (itype === "tool_compacted") {
    const r = vuMsg.toolRounds.find(rr => rr.toolCallId === inner.toolCallId);
    if (r) {
      r.compactionLayer = inner.compactionLayer || r.compactionLayer || "L1";
      if (inner.compactedFromChars != null) r.compactedFromChars = inner.compactedFromChars;
      if (inner.compactedToChars != null) r.compactedToChars = inner.compactedToChars;
      if (inner.toolTokens != null) r.toolTokens = inner.toolTokens;
    }
  } else if (itype === "phase") {
    /* Drive the same phase indicator the worker uses (tool_exec /
     * llm_thinking / retrying / working show in the elapsed bar). */
    if (typeof setStreamPhase === 'function') {
      setStreamPhase(convId, {
        phase: inner.phase,
        detail: inner.detail || "",
        tools: inner.tools || [],
        toolContext: inner.toolContext || "",
        round: inner.round || 0,
      });
    }
  } else {
    /* stdin_request / write_approval_request / human_guidance_request etc.
     * — the VU bubble doesn't host interactive widgets (the VU IS the
     * user), so log and ignore. */
    console.debug(
      `[Autopilot VU] inner type=${itype} not surfaced in VU bubble for ` +
      `conv=${convId.slice(0,8)} (vuMsgId=${vuMsgId.slice(0,12)})`
    );
    return;
  }

  /* Render through the shared streaming substrate — identical to the
   * worker delta path. */
  _flushVuStreaming(convId);
}

/**
 * Apply a BACKEND-AUTHORITATIVE autopilot run-concluded record onto a conv.
 *
 * ONE record per run (`{runId, status:'concluded', reason:'task_done'|
 * 'stopped', content?, translatedContent?, ts, _summaryId}`) carries BOTH the
 * terminal fold-fact AND the optional close-out report (a manual stop has no
 * `content`). It is human-only: stored backend-side under
 * `settings.autopilotSummaries[runId]`, mirrored here onto
 * `conv.autopilotSummaries[runId]`, and NEVER entered into `conv.messages`
 * (transcript) nor the LLM context. Idempotent + monotonic: a re-delivery
 * (reconnect / cold-replay / settings round-trip) overwrites the same runId
 * entry, but a bare `stopped` record NEVER clobbers an existing `task_done`
 * with a report (the user's manual stop is a lower-priority truth).
 *
 * The record is projected into the UI (fold gate) via the `conv.autopilotSummaries`
 * getter in `chat_render.js`.  The `_summaryId` is a stable ID for the
 * backend's summary message; the frontend's `_summaryMsg` is a transient
 * pointer to the bubble that rendered that summary (so the fold gate can
 * scroll to it).  This function does NOT create that bubble — the summary
 * bubble is created by `_maybeRenderAutopilotSummary` (called from
 * `renderChat`).
 */
function _applyAutopilotRunConcluded(conv, ev) {
  if (!conv || !ev.runId) return;
  if (typeof ev.status !== 'string' || ev.status !== 'concluded') return;
  if (typeof conv.autopilotSummaries !== 'object') conv.autopilotSummaries = {};
  const existing = conv.autopilotSummaries[ev.runId];
  /* ★ MONOTONIC: a manual stop (`reason:'stopped'`) is a LOWER-PRIORITY truth
   * than a clean `task_done` with a report — the user can stop a run that
   * would have succeeded, but that's not a reason to discard a report that
   * already arrived.  So a `stopped` record NEVER overwrites a `task_done`
   * record (the report stays).  The opposite direction (`task_done` overwrites
   * `stopped`) IS allowed — a late-arriving report (delayed by a slow summary
   * generation) should replace the manual-stop placeholder. */
  if (existing && existing.reason === 'task_done' && ev.reason === 'stopped') {
    console.debug(
      `[Autopilot] run=${ev.runId.slice(0,8)} manual stop ignored — ` +
      `already have a task_done report (conv=${(conv.id||'').slice(0,8)})`
    );
    return;
  }
  /* ★ Idempotent: a re-delivery (reconnect / cold replay) may have the exact
   * same fields; still update the timestamp so the UI knows it's fresh. */
  conv.autopilotSummaries[ev.runId] = {
    runId: ev.runId,
    status: ev.status,
    reason: ev.reason || 'unknown',
    content: ev.content,
    translatedContent: ev.translatedContent,
    ts: ev.ts || Date.now(),
    _summaryId: ev._summaryId,
    _summaryMsg: existing ? existing._summaryMsg : undefined,
  };
  console.info(
    `[Autopilot] run=${ev.runId.slice(0,8)} concluded (${ev.reason}) ` +
    `— stored summary for conv=${(conv.id||'').slice(0,8)}`
  );
}

/**
 * Apply a disarmed response (a user message that was intercepted by the
 * autopilot disarm system and replaced with a canned "I'll handle that"
 * reply).  The backend sends this as a separate SSE event so the frontend
 * can show the ORIGINAL user message (as a ghost) and the canned reply
 * (as the real bubble) side-by-side, exactly like the disarm UI in the
 * web app.
 *
 * This is a one-off: the event contains the original user message (the
 * one that triggered the disarm) and the canned reply (the one that
 * should be shown).  We push both into conv.messages, but mark the
 * original as `_disarmed: true` so the renderer can style it as a ghost.
 * The canned reply is a normal user message (role='user') with
 * `_isVirtualUser: true` and `_disarmReply: true`.
 *
 * The event also carries the `runId` of the autopilot run that will
 * handle the disarmed request, so the fold gate can link to it.
 */
function _applyDisarmResponse(conv, ev) {
  if (!conv || !Array.isArray(conv.messages)) return;
  if (!ev.original || !ev.reply) return;
  /* Push the original user message (ghost) */
  const original = Object.assign({}, ev.original, { _disarmed: true });
  conv.messages.push(original);
  /* Push the canned reply (real bubble) */
  const reply = Object.assign({}, ev.reply, {
    role: 'user',
    _isVirtualUser: true,
    _disarmReply: true,
    _disarmRunId: ev.runId,
  });
  conv.messages.push(reply);
  console.info(
    `[Autopilot] disarmed user message — added ghost + canned reply ` +
    `(run=${(ev.runId||'').slice(0,8)}, conv=${(conv.id||'').slice(0,8)})`
  );
}

/**
 * Handle the `autopilot_run_concluded` SSE event — the single
 * BACKEND-AUTHORITATIVE "this autopilot run is over" fact.  See
 * `_applyAutopilotRunConcluded` for the contract.
 *
 * Tolerates legacy field shapes (the backend may deliver the summary
 * as `ev.summary` or `ev.summaryMessage` for a while).  The record is
 * human-only (never enters conv.messages).
 */
function _handleAutopilotRunConcluded(convId, ev) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.debug(`[Autopilot] conv=${convId.slice(0,8)} not found — dropping run_concluded`);
    return;
  }
  /* ★ Legacy field tolerance (the backend may send `summary` or
   * `summaryMessage` for a while).  The authoritative shape is the
   * typed `ev` fields (runId, status, reason, content, ts, _summaryId). */
  if (ev.summary && typeof ev.summary === 'object') {
    ev = Object.assign({}, ev.summary, { runId: ev.runId || ev.summary.runId });
  } else if (ev.summaryMessage && typeof ev.summaryMessage === 'object') {
    ev = Object.assign({}, ev.summaryMessage, { runId: ev.runId || ev.summaryMessage.runId });
  }
  _applyAutopilotRunConcluded(conv, ev);
  /* ★ If this run is the one currently streaming (the VU bubble is up),
   * the run-concluded event means the VU stream is DONE — but the
   * authoritative `autopilot_vu_done` event is still pending (the
   * backend withholds it until the VU stream completes).  We must NOT
   * finalize the VU bubble here — the `vu_done` event will do that.
   * However, we CAN update the fold gate to reflect the concluded
   * status (the run is over, even though the VU reply is still
   * streaming).  The fold gate reads `conv.autopilotSummaries[runId]`
   * directly, so the update is immediate. */
  try { if (typeof ConvCache !== "undefined") ConvCache.put(conv); }
  catch (e) { /* non-fatal */ }
}

/**
 * Build the HTML for a streaming bubble (worker / planner / critic /
 * autopilot VU).  The bubble is a `<div class="message" id="streaming-msg">`
 * with the appropriate avatar, label, and status zone.
 *
 * @param {string} role — 'worker', 'planner', 'critic', 'autopilot'
 * @param {string|null} status — the short status label (e.g. 'Preparing…',
 *   'Autopilot 启动中…', '限流中').  If null, uses the default for the role.
 * @param {string|null} detail — optional longer detail text for the status zone
 *   (e.g. rate-limit detail).  If null, the status zone is omitted.
 * @param {string} [msgId] — optional `data-msg-id` attribute for surgical
 *   re-render targeting (used by autopilot VU).
 * @returns {string} HTML string (safeHtml tagged template).
 */
function _streamingBubbleHTML(role, status, detail, msgId) {
  const _id = msgId ? ` data-msg-id="${escapeHtml(msgId)}"` : '';
  let avatar = '', label = '', defaultStatus = '';
  switch (role) {
    case 'worker':
      avatar = Icon('worker');
      label = t('stream.role.worker');
      defaultStatus = t('stream.phase.preparing');
      break;
    case 'planner':
      avatar = Icon('planner');
      label = t('stream.role.planner');
      defaultStatus = t('stream.phase.preparing');
      break;
    case 'critic':
      avatar = Icon('critic');
      label = t('stream.role.critic');
      defaultStatus = t('stream.phase.preparing');
      break;
    case 'autopilot':
      avatar = Icon('autopilot');
      label = t('autopilot.label');
      defaultStatus = t('autopilot.warming');
      break;
    default:
      avatar = Icon('worker');
      label = t('stream.role.worker');
      defaultStatus = t('stream.phase.preparing');
  }
  const _status = status || defaultStatus;
  const _detail = detail || '';
  /* The status zone is a `<div data-zone="status">` inside the body.
   * When `_detail` is empty, the zone is omitted entirely (the bubble
   * shows only the avatar + label).  When present, the zone renders
   * the status text + optional detail. */
  const statusZone = _detail
    ? `<div data-zone="status" class="stream-status">
        <span class="stream-status-text">${escapeHtml(_status)}</span>
        <span class="stream-status-detail">${escapeHtml(_detail)}</span>
       </div>`
    : (_status !== defaultStatus
      ? `<div data-zone="status" class="stream-status">
          <span class="stream-status-text">${escapeHtml(_status)}</span>
         </div>`
      : '');
  /* The content zone is empty initially — filled by `updateStreamingUI`
   * as content deltas arrive.  The thinking zone is also empty (shown
   * only when `thinking` is non-empty). */
  return safeHtml`
    <div class="message streaming-message" id="streaming-msg"${_id}>
      <div class="message-avatar">${raw(avatar)}</div>
      <div class="message-body" id="streaming-body">
        <div class="message-head">
          <span class="message-role">${escapeHtml(label)}</span>
          <span class="message-time">${raw(formatClockTime())}</span>
        </div>
        ${raw(statusZone)}
        <div data-zone="content" class="stream-content"></div>
        <div data-zone="thinking" class="stream-thinking" style="display:none"></div>
        <div data-zone="tool" class="stream-tool"></div>
        <div data-zone="fc" class="stream-fc"></div>
        <div data-zone="swarmInbox" class="stream-swarm-inbox"></div>
      </div>
    </div>`;
}

/**
 * Determine the streaming role for a conversation based on its current
 * active task type.  Used by `showStreamingUIForConv` to pick the right
 * avatar/label.
 *
 * @param {string} convId
 * @returns {'worker'|'planner'|'critic'|'autopilot'}
 */
function _streamingBubbleRole(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return 'worker';
  /* The autopilot VU bubble is a special case: it's a USER message
   * (role='user') but streams through the same substrate.  The
   * `_isVirtualUser` flag is set by `_beginVuStreaming`. */
  const last = conv.messages[conv.messages.length - 1];
  if (last && last._isVirtualUser && last._streamingVu) return 'autopilot';
  /* Otherwise, infer from the task type (if known) or default to worker. */
  const taskType = conv._activeTaskType;
  if (taskType === 'planner') return 'planner';
  if (taskType === 'critic') return 'critic';
  return 'worker';
}

/**
 * Surgically truncate the DOM to match a truncated conversation.
 *
 * When the backend truncates a conversation (e.g. on a retry_reset),
 * the frontend's DOM still contains the old messages.  This function
 * walks the DOM and removes any message elements whose index is beyond
 * the new length.  It also removes the streaming-msg if the truncated
 * tail included it.
 *
 * Used by `_handleSseEvent` for `retry_reset` and `delta_reset` events.
 */
function _surgicalTruncateDOM(convId, newLength) {
  if (activeConvId !== convId) return;
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  /* Remove message elements with index >= newLength */
  for (let i = newLength; ; i++) {
    const el = document.getElementById("msg-" + i);
    if (!el) break;
    el.remove();
  }
  /* If the streaming-msg is after the new tail, remove it too */
  const sm = document.getElementById("streaming-msg");
  if (sm && !document.getElementById("msg-" + (newLength - 1))) {
    sm.remove();
  }
  /* Reset lazy‑render window so the next render starts from the new tail */
  if (typeof _lazyRenderedFrom !== 'undefined') {
    _lazyRenderedFrom = Math.max(0, newLength - _MAX_RENDER_WINDOW);
    _lazyRenderedTo = newLength;
  }
}

/**
 * Hard‑cancel an active stream (SSE reader) for a conversation.
 * Used by the Stop button and the `finishStream` autopilot branch.
 *
 * This is a last‑resort teardown: aborts the SSE controller, calls
 * `twStop`, and removes the streaming‑msg DOM element.  It does NOT
 * touch `conv.messages` — that's left to the `autopilot_vu_cancel`
 * handler or the local‑splice function.
 */
function _hardCancelActiveStream(convId) {
  const stream = activeStreams.get(convId);
  if (stream && stream.controller) {
    try { stream.controller.abort(); } catch (e) { /* already detached */ }
  }
  if (typeof twStop === "function") twStop(convId);
  const sm = document.getElementById("streaming-msg");
  if (sm) { try { sm.remove(); } catch (e) { /* detached */ } }
  console.info(`[Streaming] hard‑canceled stream for conv=${convId.slice(0,8)}`);
}

/* ───────────────────────────────────────────────────────────────────
   Lazy‑render window (virtualized chat history)
   ─────────────────────────────────────────────────────────────────── */

const _MAX_RENDER_WINDOW = 100;          // max messages to keep in DOM
let _lazyObserver = null;                // IntersectionObserver for upward load
let _lazyConvId = null;                  // which conversation is lazily rendered
let _lazyRenderedFrom = 0;               // first rendered index (inclusive)
let _lazyRenderedTo = 0;                 // last rendered index (exclusive)
let _loadingOlder = false;               // guard against concurrent upward loads
let _loadingNewer = false;               // guard against concurrent downward loads
let _lazyBottomObserver = null;          // IntersectionObserver for downward load

/**
 * Initialize lazy rendering for a conversation.  Called by `renderChat`
 * when a conversation is first displayed.
 *
 * Renders the tail `_MAX_RENDER_WINDOW` messages (or fewer if the
 * conversation is shorter), and sets up observers to load older/newer
 * messages as the user scrolls.
 */
function _INITIAL_RENDER(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  _lazyConvId = convId;
  const total = conv.messages.length;
  const start = Math.max(0, total - _MAX_RENDER_WINDOW);
  _lazyRenderedFrom = start;
  _lazyRenderedTo = total;
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  let html = '';
  for (let i = start; i < total; i++) html += renderMessage(conv.messages[i], i);
  inner.innerHTML = html;
  /* Set up the upward sentinel (load older messages) if there are any
   * messages before the rendered window. */
  if (start > 0) {
    const s = _ensureTopSentinel(inner, start);
    if (s) {
      _ensureObserver();
      _lazyObserver.observe(s);
    }
  }
  /* Set up the downward sentinel (load newer messages) if there are any
   * messages after the rendered window (should be zero on initial render,
   * but we keep the symmetry). */
  const hiddenBelow = Math.max(0, total - _lazyRenderedTo);
  const s2 = _ensureBottomSentinel(inner, hiddenBelow);
  if (s2) {
    _ensureBottomObserver();
    _lazyBottomObserver.observe(s2);
  }
}

function _ensureObserver() {
  if (_lazyObserver) return;
  _lazyObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        _lazyObserver.unobserve(e.target);
        _loadOlderMessages();
      });
    },
    {
      root: document.getElementById("chatContainer"),
      rootMargin: "600px 0px 0px 0px",
    },
  );
}

function _ensureTopSentinel(inner, hiddenAbove) {
  let sentinel = document.getElementById("_lazyLoadSentinelTop");
  if (!sentinel) {
    sentinel = document.createElement("div");
    sentinel.id = "_lazyLoadSentinelTop";
    sentinel.style.height = "1px";
    sentinel.style.visibility = "hidden";
    inner.prepend(sentinel);
  }
  sentinel.textContent = hiddenAbove > 0 ? `↑ ${hiddenAbove} older messages` : '';
  return sentinel;
}

function _loadOlderMessages() {
  if (_loadingOlder) return;
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  const total = conv.messages.length;
  const BATCH = 20;
  if (!Number.isFinite(_lazyRenderedFrom) || _lazyRenderedFrom <= 0) return;
  const inner = document.getElementById("chatInner");
  const sentinel = document.getElementById("_lazyLoadSentinelTop");
  if (!inner || !sentinel) return;
  const container = document.getElementById("chatContainer");
  _loadingOlder = true;

  const endIdx = _lazyRenderedFrom;
  const startIdx = Math.max(0, endIdx - BATCH);
  let html = "";
  for (let i = startIdx; i < endIdx; i++) html += renderMessage(conv.messages[i], i);

  const prevScrollTop = container.scrollTop;
  const prevScrollHeight = container.scrollHeight;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
  sentinel.after(frag);  // insert the older bubbles BELOW the top sentinel
  _lazyRenderedFrom = startIdx;

  /* Compensate the viewport for the added height ABOVE the current scroll
   * position, so the user stays anchored to the same content. */
  const newScrollHeight = container.scrollHeight;
  container.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight);

  /* Evict from the BOTTOM to keep the window bounded. */
  const beforeH = container.scrollHeight;
  const removedBottom = _evictBelowWindow(inner, container);
  void beforeH;
  if (removedBottom > 0) {
    /* Removing content BELOW the fold does not affect the viewport,
     * so no scroll compensation needed. */
  }

  /* Refresh / remove the top sentinel for the remaining hidden head. */
  const hiddenAbove = _lazyRenderedFrom;
  const s = _ensureTopSentinel(inner, hiddenAbove);
  if (s) { _ensureObserver(); _lazyObserver.observe(s); }
  else { _destroyObserver(); }
  _loadingOlder = false;
}

function _evictBelowWindow(inner, container) {
  const viewportBottom = container.scrollTop + container.clientHeight;
  let removed = 0;
  while (_lazyRenderedTo - _lazyRenderedFrom > _MAX_RENDER_WINDOW && _lazyRenderedTo > _lazyRenderedFrom + 1) {
    const idx = _lazyRenderedTo - 1;
    const el = document.getElementById("msg-" + idx);
    if (el) el.remove();
    _lazyRenderedTo = idx;
  }
  const hiddenBelow = Math.max(0, total - _lazyRenderedTo);
  const s = _ensureBottomSentinel(inner, hiddenBelow);
  if (s) {
    _ensureBottomObserver();
    _lazyBottomObserver.observe(s);
  } else {
    _destroyBottomObserver();
  }
  return removed;
}

function _ensureBottomSentinel(inner, hiddenBelow) {
  let sentinel = document.getElementById("_lazyLoadSentinelBottom");
  if (!sentinel) {
    sentinel = document.createElement("div");
    sentinel.id = "_lazyLoadSentinelBottom";
    sentinel.style.height = "1px";
    sentinel.style.visibility = "hidden";
    inner.append(sentinel);
  }
  sentinel.textContent = hiddenBelow > 0 ? `↓ ${hiddenBelow} newer messages` : '';
  return sentinel;
}

function _destroyObserver() {
  if (_lazyObserver) {
    _lazyObserver.disconnect();
    _lazyObserver = null;
  }
}

function _destroyBottomObserver() {
  if (_lazyBottomObserver) {
    _lazyBottomObserver.disconnect();
    _lazyBottomObserver = null;
  }
}

function _evictAboveWindow(inner, container) {
  const viewportTop = container.scrollTop;
  let removed = 0;
  while (_lazyRenderedTo - _lazyRenderedFrom > _MAX_RENDER_WINDOW && _lazyRenderedFrom < _lazyRenderedTo - 1) {
    const idx = _lazyRenderedFrom;
    const el = document.getElementById("msg-" + idx);
    if (el && el.offsetTop + el.offsetHeight < viewportTop - 1000) {
      el.remove();
      _lazyRenderedFrom = idx + 1;
      removed += el.offsetHeight;
    } else {
      break;
    }
  }
  return removed;
}

function _evictBelowWindow(inner, container) {
  const viewportBottom = container.scrollTop + container.clientHeight;
  let removed = 0;
  while (_lazyRenderedTo - _lazyRenderedFrom > _MAX_RENDER_WINDOW && _lazyRenderedTo > _lazyRenderedFrom + 1) {
    const idx = _lazyRenderedTo - 1;
    const el = document.getElementById("msg-" + idx);
    if (el && el.offsetTop > viewportBottom + 1000) {
      el.remove();
      _lazyRenderedTo = idx;
      removed += el.offsetHeight;
    } else {
      break;
    }
  }
  return removed;
}

function _ensureBottomObserver() {
  if (_lazyBottomObserver) return;
  _lazyBottomObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        _lazyBottomObserver.unobserve(e.target);
        _loadNewerMessages();
      });
    },
    {
      root: document.getElementById("chatContainer"),
      rootMargin: "0px 0px 600px 0px",
    },
  );
}

/* Symmetric downward loader: when the reader scrolls back down to the bottom
 * sentinel, re-render the next BATCH of tail messages that were evicted, then
 * evict from the HEAD to keep the window bounded. Mirror of
 * `_loadOlderMessages`; preserves the scroll anchor via the same
 * `prevScrollTop + (newHeight - prevHeight)` head-compensation used on upward
 * load. */
function _loadNewerMessages() {
  if (_loadingNewer) return;
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  if (activeStreams.has(conv.id)) return;  // stream owns the tail — don't fight it
  const total = conv.messages.length;
  const BATCH = 20;
  if (!Number.isFinite(_lazyRenderedTo) || _lazyRenderedTo >= total) return;
  const inner = document.getElementById("chatInner");
  const sentinel = document.getElementById("_lazyLoadSentinelBottom");
  if (!inner || !sentinel) return;
  const container = document.getElementById("chatContainer");
  _loadingNewer = true;

  const startIdx = _lazyRenderedTo;
  const endIdx = Math.min(total, startIdx + BATCH);
  let html = "";
  for (let i = startIdx; i < endIdx; i++) html += renderMessage(conv.messages[i], i);

  const prevScrollTop = container.scrollTop;
  const prevScrollHeight = container.scrollHeight;

  const wrapper = document.createElement("div");
  wrapper.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
  sentinel.before(frag);  // insert the newer bubbles ABOVE the bottom sentinel
  _lazyRenderedTo = endIdx;

  /* Growing content BELOW the fold does not move what the reader currently
   * sees, so the head stays put — no scroll compensation needed for the append
   * itself. (prevScrollTop/prevScrollHeight are captured only for symmetry with
   * the head-eviction compensation below.) */
  void prevScrollTop; void prevScrollHeight;

  /* Now evict from the HEAD to keep the window bounded. The reader is anchored
   * near the bottom, so compensate the viewport for removed head height. */
  const beforeH = container.scrollHeight;
  const removedTop = _evictAboveWindow(inner, container);
  void beforeH;
  if (removedTop > 0) container.scrollTop = Math.max(0, container.scrollTop - removedTop);

  /* Refresh / remove the bottom sentinel for the remaining hidden tail. */
  const hiddenBelow = Math.max(0, total - _lazyRenderedTo);
  const s = _ensureBottomSentinel(inner, hiddenBelow);
  if (s) { _ensureBottomObserver(); _lazyBottomObserver.observe(s); }
  else { _destroyBottomObserver(); }
  _loadingNewer = false;
}

/**
 * Reliably scroll a container to the very bottom.
 * Uses double-rAF to wait for layout, then a fallback timer
 * to handle async content (images, KaTeX, code highlights).
 */
function _forceScrollToBottom(container, forceActualHeights) {
  if (!container) container = document.getElementById("chatContainer");
  if (!container) return;
  const inner = document.getElementById("chatInner");
  // Override CSS scroll-behavior:smooth so programmatic scrolls are instant.
  container.style.scrollBehavior = 'auto';

  if (forceActualHeights && inner) {
    // Disable content-visibility:auto so the browser computes REAL heights
    // synchronously instead of using the 120px estimate.  This makes
    // scrollHeight accurate on the very first read — no flash.
    inner.classList.add('cv-off');
    // Force sync reflow so heights are computed NOW.
    void container.scrollHeight;
  }

  container.scrollTop = container.scrollHeight;

  // Safety net for async content (images, KaTeX, code highlights).
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  });
  setTimeout(() => {
    container.scrollTop = container.scrollHeight;
    container.style.scrollBehavior = '';
    if (forceActualHeights && inner) {
      // Re-enable content-visibility:auto only AFTER scroll has fully settled.
      // Keeping cv-off across the rAF + 150ms passes means every tool-round slot
      // ([data-prn]) stays rendered during the switch instead of flashing empty
      // (collapsing to its 32px contain-intrinsic-size placeholder) as the browser
      // re-evaluates off-screen slots against a still-moving scroll position.
      // The browser cached real heights (via "auto" contain-intrinsic-size) while
      // cv-off was on, so scrollHeight stays correct when auto-skip resumes here.
      inner.classList.remove('cv-off');
    }
  }, 150);
}