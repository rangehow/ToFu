/* ═══════════════════════════════════════════════════════════════════
   core/request_inspector.js — Request Inspector drawer (P2)

   docs/DEBUG_PANEL_REDESIGN.md (owner-ratified form A): a Network-style
   per-task request list in a RIGHT-SIDE drawer (squeezes chatinner, never
   covers it). Two-level task axis: Task rows → Request rows → detail.

   SERVER-AUTHORITATIVE: task list and round rows fold from
   /api/v1/tasks/* (the persisted task_events log, 6h). The in-memory
   _debugRequests (debug_panel.js, P1) is ONLY a live accelerator —
   sse_poll_fallback never processes messages_snapshot, so the client log
   has gaps the server log never does.

   The detail pane REUSES showMessagesInDebug (debug_panel.js) — there is
   NO second message/JSON renderer. The legacy .debug-panel markup lives
   inside this drawer's right pane (index.html); CSS re-poses it static.

   This file is concatenated by lib/js_bundler.py AFTER core/debug_panel.js
   — symbols share window scope, no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

let _riOpen = false;
const _riSel = { taskId: null, fold: null };

function toggleRequestInspector() {
  if (_riOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riOpen = true;
  /* Keep the legacy debugVisible flag in sync — other readers (restore
   * paths, the _applyDebugModeVisibility helper) key off it. */
  if (typeof debugVisible !== 'undefined') debugVisible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riLoadTasks(typeof activeConvId !== 'undefined' ? activeConvId : null);
}

function closeRequestInspector() {
  _riOpen = false;
  if (typeof debugVisible !== 'undefined') debugVisible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riOpen) return;
  _riSel.taskId = null;
  _riSel.fold = null;
  _riLoadTasks(convId);
}

function _riEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(s == null ? '' : String(s)) : String(s == null ? '' : s);
}

function _riTimeLabel(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch (_) { return ''; }
}

function _riEl(id) { return document.getElementById(id); }

/* ── Level 1: task rows for the active conversation ── */
async function _riLoadTasks(convId) {
  const list = _riEl('riTaskList');
  const rounds = _riEl('riRoundList');
  if (!list) return;
  if (rounds) rounds.innerHTML = '';
  if (!convId) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(convId) : null;
  if (!_riOpen) return;  // drawer closed while fetching
  const tasks = (data && Array.isArray(data.tasks)) ? data.tasks : [];
  if (!tasks.length) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  list.innerHTML = '';
  for (const row of tasks) {
    const el = document.createElement('div');
    el.className = 'ri-task' + (_riSel.taskId === row.taskId ? ' ri-sel' : '');
    const liveBadge = row.live
      ? `<span class="ri-live-badge">${_riEsc(t('ri.live'))}</span>` : '';
    const reqN = row.requestCount || 0;
    const approx = (row.legacyCount || 0) > 0;
    const countLabel = approx
      ? `≈${reqN + row.legacyCount}`
      : `${reqN}`;
    const expired = !row.hasEvents && !row.live;
    el.innerHTML =
      `<div class="ri-task-top">` +
      `<span class="ri-task-id">${_riEsc(String(row.taskId).slice(0, 8))}</span>` +
      liveBadge +
      `<span class="ri-task-time">${_riEsc(_riTimeLabel(row.createdAt))}</span>` +
      `</div>` +
      `<div class="ri-task-sub">` +
      (expired
        ? `<span class="ri-expired">${_riEsc(t('ri.expired'))}</span>`
        : `<span title="${_riEsc(t('ri.requests'))}">${countLabel} ${_riEsc(t('ri.requests'))}</span>`) +
      ` · ${_riEsc(row.status || '')}` +
      `</div>`;
    el.onclick = () => _riSelectTask(row.taskId);
    list.appendChild(el);
  }
}

/* ── Level 2: request rows (metadata) for the selected task ── */
async function _riSelectTask(taskId) {
  _riSel.taskId = taskId;
  _riSel.fold = null;
  /* Re-mark the selected task row. */
  const list = _riEl('riTaskList');
  if (list) {
    list.querySelectorAll('.ri-task').forEach((el) => {
      el.classList.toggle('ri-sel',
        el.querySelector('.ri-task-id') &&
        taskId.startsWith(el.querySelector('.ri-task-id').textContent));
    });
  }
  const rounds = _riEl('riRoundList');
  if (!rounds) return;
  rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  const fold = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequests(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale response
  _riSel.fold = fold;
  rounds.innerHTML = '';
  if (!fold || !fold.eventsAvailable) {
    rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.expired'))}</div>`;
    return;
  }
  /* Coverage chip — honest disclosure for endpoint-driven tasks (Planner /
   * Critic calls are NOT captured; see design §7). */
  if (fold.coverage === 'partial') {
    const chip = document.createElement('div');
    chip.className = 'ri-coverage-chip';
    chip.innerHTML =
      (typeof Icon === 'function' ? Icon('alertTriangle', 12) : '') +
      ` <span>${_riEsc(t('ri.coveragePartial'))}</span>`;
    rounds.appendChild(chip);
  }
  const reqs = Array.isArray(fold.requests) ? fold.requests : [];
  if (!reqs.length) {
    const emp = document.createElement('div');
    emp.className = 'ri-empty';
    emp.textContent = t('ri.empty');
    rounds.appendChild(emp);
  }
  for (const row of reqs) {
    const el = document.createElement('div');
    el.className = 'ri-round';
    el.dataset.round = String(row.roundNum);
    const attempts = Array.isArray(row.attempts) ? row.attempts : [];
    const tok = row.approxTokens >= 1000
      ? (row.approxTokens / 1000).toFixed(1) + 'K' : String(row.approxTokens || 0);
    const attemptBits = attempts.map((a) => {
      const el2 = (a.streamElapsedMs / 1000).toFixed(1) + 's';
      const fb = /FALLBACK|REACTIVE|DISCARDED/.test(a.tag || '') ? ' ⚠' : '';
      return `<span class="ri-attempt" title="${_riEsc(a.traceId || '')}">` +
        `${_riEsc(a.tag || a.model)} ${a.tokensIn}→${a.tokensOut} · ${el2}${fb}</span>`;
    }).join('');
    el.innerHTML =
      `<div class="ri-round-top">` +
      `<span class="ri-round-n">R${_riEsc(row.roundNum)}</span>` +
      `<span class="ri-round-model">${_riEsc(row.model || '?')}</span>` +
      `<span class="ri-round-meta">${row.messageCount} msgs · ~${tok}tok` +
      (row.toolsCount ? ` · ${row.toolsCount} tools` : '') + `</span>` +
      `</div>` +
      (attemptBits ? `<div class="ri-round-attempts">${attemptBits}</div>` : '');
    el.onclick = () => _riSelectRound(taskId, row.roundNum, el);
    rounds.appendChild(el);
  }
  /* State mirrors (NOT requests) — collapsed at the bottom, clearly labeled. */
  const states = Array.isArray(fold.states) ? fold.states : [];
  if (states.length) {
    const head = document.createElement('div');
    head.className = 'ri-states-head';
    head.textContent = `${t('ri.states')} (${states.length}) — ${t('ri.stateNote')}`;
    rounds.appendChild(head);
    for (const s of states) {
      const el = document.createElement('div');
      el.className = 'ri-state-row';
      el.textContent = `${s.label || s.roundNum} · ${s.messageCount} msgs`;
      rounds.appendChild(el);
    }
  }
}

/* ── Level 3: detail — REUSES showMessagesInDebug (no second renderer) ── */
async function _riSelectRound(taskId, roundNum, el) {
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.toggle('ri-sel', r === el));
  }
  /* Live accelerator: the in-memory P1 log may already hold this round's
   * payload (in-flight task, SSE-fed). Metadata-only (stripped) entries
   * fall through to the server fetch. */
  const acc = (typeof _debugRequests !== 'undefined') &&
    _debugRequests[taskId] && _debugRequests[taskId].rounds[String(roundNum)];
  if (acc && acc.messages) {
    if (typeof showMessagesInDebug === 'function')
      showMessagesInDebug(acc.messages, acc.label, false,
        typeof activeConvId !== 'undefined' ? activeConvId : null,
        acc.tools || undefined, false);
    return;
  }
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequestPayload(taskId, roundNum) : null;
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale
  if (data && data.messages && typeof showMessagesInDebug === 'function') {
    showMessagesInDebug(data.messages, data.label || '', false,
      typeof activeConvId !== 'undefined' ? activeConvId : null,
      data.tools || undefined, false);
  }
}
