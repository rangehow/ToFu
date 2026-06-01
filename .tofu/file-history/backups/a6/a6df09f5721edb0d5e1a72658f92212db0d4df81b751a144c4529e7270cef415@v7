/* ═══════════════════════════════════════════
   translation.js — Async Translation Tasks
   ═══════════════════════════════════════════ */

// ═══════════════════════════════════════════════════════════
//  ★ Unified translation pipeline — see _runTranslationPipeline
// ═══════════════════════════════════════════════════════════
//
// Three call sites (manual click / auto-translate / resume-retry) all
// funnel through _runTranslationPipeline.  The shared rules are:
//   • Server persistence: _patchMessageOnServer (targeted PATCH only)
//   • Re-render:          surgical outerHTML of #msg-N
//   • Poll cadence:       2s × 5, then 4s, capped at ~150s
//   • Cache field:        writes translatedContent only; reads also
//                         _translatedCache for backward compat
//   • mode='auto'         retries on terminal failure with a fresh task
//   • mode='manual'       surfaces the error and waits for next click

const _TRANSLATE_POLL_MAX_ATTEMPTS = 40;   // ~150s total budget
const _TRANSLATE_POLL_FAST_DELAY  = 2000;  // first 5 polls
const _TRANSLATE_POLL_SLOW_DELAY  = 4000;  // remaining polls
const _TRANSLATE_STALE_FRAC       = 0.15;  // <15% → stale partial translation

/**
 * Surgical single-message re-render with scroll preservation.
 * Replaces the duplicated outerHTML+scrollTop pattern that lived in
 * three different places across translation flows.
 */
function _renderMsgInPlace(convId, idx, msg) {
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return;
  if (typeof renderMessage !== 'function') return;
  const el = document.getElementById(`msg-${idx}`);
  if (!el) return;
  const ct = document.getElementById('chatContainer');
  const sv = ct ? ct.scrollTop : -1;
  el.outerHTML = renderMessage(msg, idx);
  if (sv >= 0 && ct) ct.scrollTop = sv;
}

/**
 * Stale-partial detector: a translation produced from mid-stream
 * partial content tends to be < 15% of the (now-final) source length.
 */
function _isStalePartialTranslation(msg) {
  return !!(msg && msg.translatedContent && msg.content && msg.content.length > 500 &&
            msg.translatedContent.length < msg.content.length * _TRANSLATE_STALE_FRAC);
}

function _resetTranslationState(msg) {
  delete msg.translatedContent;
  delete msg._translatedCache;
  delete msg._translateDone;
  delete msg._translateTaskId;
  delete msg._translateError;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
}

/**
 * Apply a 'done' translation result to the message and persist it.
 * Handles both fields: 'translatedContent' (assistant bilingual)
 * and 'content' (user-edit translation).
 */
function _applyTranslationDone(convId, idx, msg, result, field) {
  if (field === 'translatedContent') {
    msg.translatedContent = result.translated;
    msg._translatedCache = result.translated;  // backward-compat readers
    msg._showingTranslation = true;
  } else if (field === 'content') {
    if (!msg.originalContent) msg.originalContent = msg.content;
    msg.content = result.translated;
  }
  if (result.model) msg._translateModel = result.model;
  msg._translateDone = true;
  delete msg._translateTaskId;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
  delete msg._translateError;

  if (typeof saveConversations === 'function') saveConversations(convId);

  // Targeted PATCH — never full-conv PUT (avoids queue races).
  const patch = {
    _translateDone: true,
    _translateModel: msg._translateModel || null,
  };
  if (field === 'translatedContent') {
    patch.translatedContent = msg.translatedContent;
    patch._showingTranslation = true;
  } else if (field === 'content') {
    patch.content = msg.content;
  }
  if (typeof _patchMessageOnServer === 'function') {
    _patchMessageOnServer(convId, idx, patch);
  }

  _renderMsgInPlace(convId, idx, msg);
}

/**
 * Apply a 'running' status update (partial preview, retry status).
 * Surgically re-renders only when something changed, so we don't
 * thrash the DOM on every poll tick.
 */
function _applyTranslationStatus(convId, idx, msg, result) {
  let changed = false;
  if (result.statusMessage && result.statusMessage !== msg._translateStatus) {
    msg._translateStatus = result.statusMessage;
    msg._translateStatusKind = result.statusKind || '';
    changed = true;
  }
  if (result.partial && result.partial !== msg._translatePartial) {
    msg._translatePartial = result.partial;
    changed = true;
  }
  if (changed) _renderMsgInPlace(convId, idx, msg);
  return changed;
}

/**
 * Apply terminal-error state (no retry path was taken).
 */
function _applyTranslationError(convId, idx, msg, errMsg) {
  msg._translateDone = true;
  msg._translateError = errMsg || 'Translation failed';
  delete msg._translateTaskId;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
  if (typeof saveConversations === 'function') saveConversations(convId);
  if (typeof _patchMessageOnServer === 'function') {
    _patchMessageOnServer(convId, idx, {
      _translateDone: true,
      _translateError: msg._translateError,
      _translateTaskId: null,
    });
  }
  _renderMsgInPlace(convId, idx, msg);
}

/**
 * Try to recover an auto-committed translation from the server when a
 * task expired/errored.  Returns true if recovery succeeded.
 */
async function _tryRecoverFromServer(convId, idx, msg, field) {
  try {
    const resp = await fetch(apiUrl(`/api/conversations/${convId}`));
    if (!resp.ok) return false;
    const data = await resp.json();
    const dbMsg = data.messages?.[idx];
    if (!dbMsg) return false;
    if (field === 'translatedContent' && dbMsg.translatedContent) {
      _applyTranslationDone(convId, idx, msg,
        { translated: dbMsg.translatedContent, model: dbMsg._translateModel },
        'translatedContent');
      return true;
    }
    if (field === 'content' && dbMsg.content && msg.originalContent &&
        dbMsg.content !== msg.originalContent) {
      _applyTranslationDone(convId, idx, msg,
        { translated: dbMsg.content, model: dbMsg._translateModel },
        'content');
      return true;
    }
  } catch (e) {
    console.debug('[Translate] DB-recovery check failed:', e?.message);
  }
  return false;
}

/**
 * Shared poll loop — used by manual / auto / resume-retry paths.
 * Returns when the task reaches a terminal state (or attempt cap).
 */
async function _pollTranslationLoop(opts) {
  const { convId, idx, msg, taskId, field, mode } = opts;
  for (let attempt = 0; attempt <= _TRANSLATE_POLL_MAX_ATTEMPTS; attempt++) {
    if (attempt >= _TRANSLATE_POLL_MAX_ATTEMPTS) {
      // Polling timed out — server may still auto-commit, but stop polling here.
      // Treat as a terminal outcome consistent with mode.
      if (mode === 'auto') {
        return await _handleTerminalFailure(opts, 'poll timeout');
      }
      _applyTranslationError(convId, idx, msg, 'Translation timeout');
      return;
    }
    const delay = attempt < 5 ? _TRANSLATE_POLL_FAST_DELAY : _TRANSLATE_POLL_SLOW_DELAY;
    await new Promise(r => setTimeout(r, delay));

    const result = await _pollTranslateTask(taskId);

    if (result.status === 'done' && result.translated) {
      _applyTranslationDone(convId, idx, msg, result, field);
      return;
    }
    if (result.status === 'running') {
      _applyTranslationStatus(convId, idx, msg, result);
      continue;
    }
    // error | not_found — try DB-recovery, then optionally retry.
    if (result.status === 'error' || result.status === 'not_found') {
      const recovered = await _tryRecoverFromServer(convId, idx, msg, field);
      if (recovered) return;
      return await _handleTerminalFailure(opts, result.error || result.status);
    }
  }
}

/**
 * Terminal-failure handler — auto mode retries with a fresh task,
 * manual mode surfaces the error and stops.
 */
async function _handleTerminalFailure(opts, errMsg) {
  const { convId, idx, msg, mode, conv, sourceLang, targetLang, field } = opts;
  const convAuto = conv && conv.autoTranslate !== undefined
    ? !!conv.autoTranslate
    : (typeof autoTranslate !== 'undefined' ? !!autoTranslate : false);

  // Auto mode + autoTranslate ON → retry with a fresh task.
  // Bound the retry chain by counting prior attempts on the message.
  if (mode === 'auto' && convAuto && msg.content) {
    const tries = (msg._translateRetryCount || 0) + 1;
    if (tries <= 2) {
      msg._translateRetryCount = tries;
      console.log(`%c[Translate] 🔄 Auto-retry ${tries}/2 for msg ${idx} (prev err: ${errMsg})`,
        'color:#8b5cf6');
      return await _runTranslationPipeline(conv, idx, msg, {
        sourceLang, targetLang, field, mode: 'auto',
      });
    }
    console.warn(`[Translate] Auto-retry budget exhausted for msg ${idx}: ${errMsg}`);
  }
  _applyTranslationError(convId, idx, msg, errMsg);
}

/**
 * UNIFIED TRANSLATION RUNNER — single entry point for manual / auto / retry.
 *
 * @param {object} conv  Conversation object (from `conversations` global)
 * @param {number} idx   Message index within conv.messages
 * @param {object} msg   conv.messages[idx]
 * @param {object} opts
 *   @param {string}  opts.sourceLang     'English' | 'Chinese' | '' (auto)
 *   @param {string}  opts.targetLang     'Chinese' | 'English'
 *   @param {string}  opts.field          'translatedContent' | 'content'
 *   @param {string}  opts.mode           'manual' | 'auto'
 *   @param {string}  [opts.existingTaskId]  resume an already-started task
 *   @param {string}  [opts.text]         override text-to-translate (default msg.content)
 */
async function _runTranslationPipeline(conv, idx, msg, opts) {
  const convId = conv.id;
  const field = opts.field || 'translatedContent';
  const mode  = opts.mode  || 'auto';
  const text  = opts.text != null ? opts.text : msg.content;

  if (!text || !text.trim()) {
    console.debug(`[Translate] skip msg ${idx}: empty text (mode=${mode})`);
    return;
  }

  // Re-translate stale partial translations from prior mid-stream runs.
  if (_isStalePartialTranslation(msg)) {
    console.warn(`[Translate] 🔄 Stale partial translation on msg ${idx} ` +
      `(translated=${msg.translatedContent.length} vs content=${msg.content.length}) — re-translating`);
    _resetTranslationState(msg);
  }

  let taskId = opts.existingTaskId || null;
  if (!taskId) {
    try {
      taskId = await _startTranslateTask(
        text, opts.targetLang, opts.sourceLang || '',
        convId, idx, field, msg && msg._msgId
      );
    } catch (e) {
      console.error('[Translate] start failed:', e);
      _applyTranslationError(convId, idx, msg, e?.message || 'Failed to start translation');
      return;
    }
    if (!taskId) {
      _applyTranslationError(convId, idx, msg, 'Failed to start translation task');
      return;
    }
  }

  // Mark message as pending so renderMessage shows the "翻译中…" indicator.
  msg._translateTaskId = taskId;
  msg._translateField  = field;
  msg._translateDone   = false;
  delete msg._translateError;
  if (typeof saveConversations === 'function') saveConversations(convId);
  _renderMsgInPlace(convId, idx, msg);

  // Poll until terminal (success / recovered / final-error).
  await _pollTranslationLoop({
    convId, idx, msg, conv, taskId, field, mode,
    sourceLang: opts.sourceLang, targetLang: opts.targetLang,
  });
}

async function _callTranslateAPI(text, targetLang, sourceLang, timeoutMs) {
  // Scale timeout with text length: large texts need more time for LLM to complete
  if (!timeoutMs) timeoutMs = text.length > 6000 ? 120000 : text.length > 3000 ? 90000 : 60000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(apiUrl("/api/translate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, targetLang, sourceLang }),
      signal: ctrl.signal,
    });
  } catch (fetchErr) {
    clearTimeout(timer);
    if (fetchErr.name === 'AbortError') {
      throw new Error("Translation timed out — server may be overloaded");
    }
    throw fetchErr;
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = body.error || r.statusText || r.status;
    throw new Error(`Translation failed: ${detail}`);
  }
  const d = await r.json();
  if (!d.translated) throw new Error("Translation returned empty result");
  return d.translated;
}

// ═══════════════════════════════════════════════════════════
//  ★ Async Translation Tasks — fire-and-forget, survive page reload
// ═══════════════════════════════════════════════════════════

/**
 * Start a server-side async translation task.
 * Returns taskId immediately. The server will:
 * 1) Run the translation in a background thread
 * 2) Auto-commit the result into the DB conversation
 * So even if the user closes the tab, the translation completes.
 */
async function _startTranslateTask(text, targetLang, sourceLang, convId, msgIdx, field, msgId) {
  try {
    const body = { text, targetLang, sourceLang, convId, msgIdx, field };
    if (msgId) body.msgId = msgId;
    const r = await fetch(apiUrl("/api/translate/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      console.error("[TranslateTask] start failed:", r.status);
      return null;
    }
    const d = await r.json();
    console.log(`%c[TranslateTask] Started ${d.taskId} for conv=${convId?.slice(0,8)} msg=${msgIdx} msgId=${(msgId||'').slice(0,8)||'-'} field=${field}`, 'color:#8b5cf6');
    return d.taskId;
  } catch (e) {
    console.error("[TranslateTask] start error:", e);
    return null;
  }
}

/**
 * Poll a single translation task. Returns {status, translated?, error?}
 */
async function _pollTranslateTask(taskId) {
  try {
    const r = await fetch(apiUrl(`/api/translate/poll/${taskId}`));
    if (r.status === 404) return { status: 'not_found' };
    if (!r.ok) return { status: 'error', error: `HTTP ${r.status}` };
    return await r.json();
  } catch (e) {
    return { status: 'error', error: e.message };
  }
}

/**
 * Poll multiple translation tasks in one request.
 */
async function _pollTranslateTaskBatch(taskIds) {
  if (!taskIds.length) return [];
  try {
    const r = await fetch(apiUrl("/api/translate/poll_batch"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ taskIds }),
    });
    if (!r.ok) return taskIds.map(id => ({ taskId: id, status: 'error' }));
    return await r.json();
  } catch (e) {
    return taskIds.map(id => ({ taskId: id, status: 'error', error: e.message }));
  }
}

/**
 * Resume any pending translation tasks for a conversation.
 * Called on page load and when switching to a conversation.
 * Scans messages for _translateTaskId without completed translation,
 * polls the server, and applies results.
 */
async function _resumePendingTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv || !conv.messages) return;

  // ★ FIX: never auto-translate while streaming — content is still growing,
  //   translating partial content produces incomplete translations.
  //   finishStream() will trigger translation when the stream is done.
  if (activeStreams.has(convId)) {
    console.log(`%c[TranslateTask] ⏭ Skipping _resumePendingTranslations — stream active for conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
    return;
  }

  // ── Phase 0: detect the LAST assistant msg that should have been translated but wasn't ──
  // (e.g. page closed before finishStream could start the task)
  // Only check the last assistant message — don't retroactively translate old history
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : true;
  if (_convAutoTranslate && conv.messages.length > 0) {
    // Find the last assistant message (skip empty ghost messages)
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const msg = conv.messages[i];
      if (msg.role !== 'assistant') continue;
      if (!msg.content) continue;  // ★ skip ghost empty assistant msgs, don't break
      // ★ FIX: detect stale partial translations before skipping —
      // if translatedContent exists but is < 15% of content length, it was translated
      // from partial content (mid-stream) and needs re-translation.
      if (_isStalePartialTranslation(msg)) {
        console.warn(`[TranslateTask] 🔄 Stale partial translation in resume — ` +
          `translated=${msg.translatedContent.length} vs content=${msg.content.length} ` +
          `(${(msg.translatedContent.length/msg.content.length*100).toFixed(1)}%) — re-translating msg ${i}`);
        _resetTranslationState(msg);
        _runTranslationPipeline(conv, i, msg, {
          sourceLang: 'English', targetLang: 'Chinese',
          field: 'translatedContent', mode: 'auto',
        });
        break;
      }
      if (msg.translatedContent || msg._translateTaskId || msg._translateDone !== undefined) break;
      // ★ Skip image gen results — nothing meaningful to translate
      if (msg._igResult || msg._isImageGen || msg._igResults) break;
      // ★ FIX: When autoTranslate is ON, always translate — don't rely on the
      // language heuristic which fails for bilingual/mixed-language responses.
      console.log(`%c[TranslateTask] 🔄 Auto-starting missed translation for msg ${i} in conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
      _runTranslationPipeline(conv, i, msg, {
        sourceLang: 'English', targetLang: 'Chinese',
        field: 'translatedContent', mode: 'auto',
      });
      break; // only check the last assistant msg
    }
  }

  // ── Phase 1: collect messages with active translate tasks ──
  const pending = [];
  conv.messages.forEach((msg, idx) => {
    if (msg._translateTaskId && !msg._translateDone) {
      // Check if the translation already landed (e.g. server auto-committed and we reloaded)
      if (msg._translateField === 'translatedContent' && msg.translatedContent) {
        msg._translateDone = true;
        return;
      }
      if (msg._translateField === 'content' && msg.originalContent && msg.content !== msg.originalContent) {
        msg._translateDone = true;
        return;
      }
      pending.push({ taskId: msg._translateTaskId, idx, msg });
    }
  });

  if (!pending.length) return;
  console.log(`%c[TranslateTask] Resuming ${pending.length} pending translations for conv=${convId.slice(0,8)}`, 'color:#8b5cf6;font-weight:bold');

  // ── Phase 2: immediately re-render to show "翻译中…" indicators ──
  // ★ Use forceScroll=false to preserve scroll position (prevents jump-to-top flash)
  if (activeConvId === convId) {
    renderChat(conv, false);
  }

  // ── Phase 3: one-shot batch poll, then hand off to the unified pipeline ──
  const taskIds = pending.map(p => p.taskId);
  const results = await _pollTranslateTaskBatch(taskIds);

  for (const result of results) {
    const p = pending.find(x => x.taskId === result.taskId);
    if (!p) continue;
    const field = p.msg._translateField || 'translatedContent';

    if (result.status === 'done' && result.translated) {
      _applyTranslationDone(convId, p.idx, p.msg, result, field);
      console.log(`%c[TranslateTask] ✓ Applied ${result.taskId} to msg ${p.idx} (${field})`, 'color:#22c55e');
      continue;
    }

    if (result.status === 'running') {
      // Surface any retry-status / partial preview so the user sees progress,
      // then attach the unified poll loop so this in-flight task uses the
      // same cadence and apply/error path as fresh tasks.
      _applyTranslationStatus(convId, p.idx, p.msg, result);
      _runTranslationPipeline(conv, p.idx, p.msg, {
        sourceLang: '', targetLang: '',
        field, mode: 'auto', existingTaskId: p.taskId,
      });
      continue;
    }

    if (result.status === 'error' || result.status === 'not_found') {
      console.warn(`[TranslateTask] ${result.taskId} ${result.status} — checking DB for auto-committed translation...`);
      const recovered = await _tryRecoverFromServer(convId, p.idx, p.msg, field);
      if (recovered) continue;
      // No DB recovery available — auto-mode pipeline will retry, manual will surface error.
      if (_convAutoTranslate && field === 'translatedContent' && p.msg.content) {
        p.msg._translateTaskId = null;
        delete p.msg._translateError;
        _runTranslationPipeline(conv, p.idx, p.msg, {
          sourceLang: 'English', targetLang: 'Chinese',
          field: 'translatedContent', mode: 'auto',
        });
      } else {
        _applyTranslationError(convId, p.idx, p.msg, result.error || 'Task expired');
      }
    }
  }
}
