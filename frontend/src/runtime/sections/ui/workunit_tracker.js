/* ===== migrated source: ui/workunit_tracker.js ===== */
/* ui/workunit_tracker.js — WorkUnit live hydration for produce_* cards.
 *
 * docs/API_CONTRACT.md §2.18: the server projects every production task into
 * ONE workUnit block; this tracker is its only client-side consumer. A
 * produce_* tool round's ACCEPTANCE (badge queued|running + meta.workUnitRef)
 * is all the card knows at render time — the model's turn has ended by
 * design, so everything after acceptance (stage progress, retries, degraded
 * quality, the download link) arrives HERE, never via another LLM message.
 *
 * Rendering semantics: the card's HTML string carries only the re-attach
 * handle (data-wu-task / data-wu-kind) and an empty .ptool-wu-live region;
 * this tracker hydrates that region in place, mirroring the fingerprint-gate
 * in-place updates of _tickTimerCountdowns. A conversation re-render simply
 * re-arms the scan (the handle is durable in the persisted round meta), and
 * the next poll re-hydrates from the server-projected snapshot — the client
 * NEVER derives lifecycle state itself.
 *
 * Residency: demand-scoped. Cards call _wuScanSoon() while their HTML string
 * is being built; a debounced scan registers [data-wu-task] elements and
 * starts ONE shared poll loop (chained setTimeout, never setInterval): 2s
 * cadence backing off towards 5s while nothing changes, ticks skipped while
 * the tab is hidden, loop stops when the registry empties. No boot listener,
 * no SSE channel — terminal truth is the task snapshot, never an event.
 */

const _WU_TERMINAL = { done: 1, degraded: 1, failed: 1, aborted: 1 };
const _WU_DELAY_MIN = 2000;
const _WU_DELAY_MAX = 5000;

/* taskId → { kind, fp, seen } — `fp` fingerprints the last applied snapshot
 * so unchanged polls skip DOM writes and drive the backoff. */
const _wuRegistry = new Map();
let _wuScanTimer = null;
let _wuPollTimer = null;
let _wuDelay = _WU_DELAY_MIN;

function _wuT(k, d) {
  return (typeof t === 'function') ? t(k, d) : (d || k);
}

/* Debounced scan entry — called by _renderMotionVideoBlock while the card
 * string is being built (before it reaches the DOM). */
/* Registry probes — the registry itself stays a module-local const (eval
 * scopes it away from tests); these function declarations are the readable
 * surface for the jsdom harness and console debugging. */
function _wuTrackedCount() { return _wuRegistry.size; }
function _wuTracked(taskId) { return _wuRegistry.has(taskId); }

function _wuScanSoon() {
  if (_wuScanTimer) return;
  _wuScanTimer = setTimeout(function () {
    _wuScanTimer = null;
    _wuScanNow();
  }, 50);
}

function _wuScanNow() {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  const els = document.querySelectorAll('[data-wu-task]');
  for (const el of els) {
    const id = el.getAttribute('data-wu-task');
    if (!id || _wuTracked(id)) continue;
    _wuRegistry.set(id, {
      kind: el.getAttribute('data-wu-kind') || '',
      fp: '',
    });
  }
  if (_wuTrackedCount()) _wuEnsureLoop();
}

function _wuTasksApi() {
  return (typeof Api !== 'undefined' && Api && Api.tasks &&
    typeof Api.tasks.get === 'function') ? Api.tasks : null;
}

function _wuEnsureLoop() {
  if (_wuPollTimer) return;
  _wuPollTimer = setTimeout(_wuTick, _wuDelay);
}

/* One tick polls every registered (non-terminal) unit, applies changed
 * snapshots, and reschedules. The loop self-terminates when the registry
 * empties (all units terminal or their cards gone). */
function _wuTick() {
  _wuPollTimer = null;
  if (!_wuTrackedCount()) return;
  if (typeof document !== 'undefined' && document.hidden) {
    _wuEnsureLoop();
    return;
  }
  const api = _wuTasksApi();
  if (!api) { _wuEnsureLoop(); return; }
  /* Cards whose element left the DOM (conversation deleted, fingerprint
   * re-render in flight) are NOT dropped here — a re-render re-arms the scan
   * and the same taskId re-registers; a truly gone card's unit still reaches
   * terminal state and is then forgotten. */
  const ids = Array.from(_wuRegistry.keys());
  let changed = false;
  let pending = ids.length;
  const settle = function () {
    pending -= 1;
    if (pending > 0) return;
    _wuDelay = changed ? _WU_DELAY_MIN
      : Math.min(_WU_DELAY_MAX, _wuDelay + 1500);
    if (_wuTrackedCount()) _wuEnsureLoop();
  };
  for (const id of ids) {
    Promise.resolve(api.get(id)).then(function (snap) {
      if (snap && snap.workUnit) {
        if (_wuApply(id, snap.workUnit)) changed = true;
      }
      /* null (onError:'null' / 404 after TTL sweep): keep the registration;
       * a restarted server re-serves the unit by id via the durable path. */
    }).catch(function () { /* transient — next tick retries */ })
      .then(settle);
  }
}

/* Apply one server-projected workUnit to every card carrying the handle.
 * Returns true when the snapshot differed from the last applied one. */
function _wuApply(taskId, unit) {
  const entry = _wuRegistry.get(taskId);
  if (!entry || !unit || typeof unit !== 'object') return false;
  const stages = Array.isArray(unit.stages) ? unit.stages : [];
  const fp = JSON.stringify([
    unit.status, unit.stageTotal || 0,
    stages.map(function (s) {
      return [s.name, s.state, s.attempt, s.elapsedS,
        (s.lastErrors || []).length];
    }),
    unit.quality || null, unit.failure || null,
    (Array.isArray(unit.artifacts) ? unit.artifacts : []).length,
  ]);
  if (fp === entry.fp) return false;
  entry.fp = fp;
  const html = _wuLiveHtml(unit);
  const cards = document.querySelectorAll('[data-wu-task="' + taskId + '"]');
  for (const card of cards) {
    const live = card.querySelector('.ptool-wu-live');
    if (live) live.innerHTML = html;
    const badge = card.querySelector('.ptool-convmeta-header .ptool-badge');
    if (badge) {
      badge.textContent = _wuStatusLabel(unit.status);
      badge.className = 'ptool-badge ptool-wu-badge-' +
        (_WU_TERMINAL[unit.status] || unit.status === 'running' ||
         unit.status === 'queued' || unit.status === 'retrying'
          ? unit.status : 'failed');
    }
  }
  if (_WU_TERMINAL[unit.status]) {
    _wuRegistry.delete(taskId);
    _wuAnnounce(unit);
  }
  return true;
}

function _wuStatusLabel(status) {
  return _wuT('wu.status.' + status, status || '');
}

function _wuEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(String(s == null ? '' : s)) : String(s == null ? '' : s);
}

function _wuIcon(name, size) {
  return (typeof Icon === 'function') ? Icon(name, size || 12) : '';
}

/* The .ptool-wu-live region: a stage stepper (observed stages + the
 * not-yet-started remainder implied by stageTotal) plus, for terminal units,
 * ONE honest outcome line — download links for done/degraded, the failure
 * envelope (message + hint) for failed, muted for aborted. */
function _wuLiveHtml(unit) {
  const stages = Array.isArray(unit.stages) ? unit.stages : [];
  const total = Number(unit.stageTotal || 0);
  let html = '<div class="ptool-wu-stages">';
  for (const s of stages) {
    if (!s || typeof s !== 'object') continue;
    const state = (s.state === 'done' || s.state === 'active' ||
      s.state === 'skipped' || s.state === 'failed') ? s.state : 'pending';
    const icon = state === 'done' ? _wuIcon('check')
      : state === 'active' ? '<span class="ptool-spinner"></span>'
      : state === 'skipped' ? _wuIcon('chevronDown')
      : state === 'failed' ? _wuIcon('x')
      : _wuIcon('clock');
    const attempt = (Number(s.attempt || 0) > 1)
      ? '<span class="ptool-wu-attempt">' + _wuEsc(
        _wuT('wu.attempt', 'attempt {n}').replace('{n}', Number(s.attempt))) +
        '</span>'
      : '';
    const elapsed = (s.elapsedS != null && state !== 'pending')
      ? '<span class="ptool-wu-elapsed">' + _wuEsc(Number(s.elapsedS)) + 's</span>'
      : '';
    const lastErr = (Array.isArray(s.lastErrors) && s.lastErrors.length &&
      (state === 'failed' || Number(s.attempt || 0) > 1))
      ? '<span class="ptool-wu-stage-err">' + _wuEsc(
        String(s.lastErrors[s.lastErrors.length - 1]).slice(0, 120)) + '</span>'
      : '';
    html += '<div class="ptool-wu-stage ptool-wu-stage-' + state + '">' +
      '<span class="ptool-wu-stage-icon">' + icon + '</span>' +
      '<span class="ptool-wu-stage-name">' + _wuEsc(s.name || '') + '</span>' +
      attempt + elapsed + lastErr + '</div>';
  }
  for (let i = stages.length; i < total; i++) {
    html += '<div class="ptool-wu-stage ptool-wu-stage-pending">' +
      '<span class="ptool-wu-stage-icon">' + _wuIcon('clock') + '</span>' +
      '<span class="ptool-wu-stage-name">' + _wuEsc(
        _wuT('wu.stagePending', 'stage {n}').replace('{n}', i + 1)) +
      '</span></div>';
  }
  html += '</div>';

  const artifacts = Array.isArray(unit.artifacts) ? unit.artifacts : [];
  const links = artifacts.map(function (a) {
    if (!a || !a.href) return '';
    const bits = [a.label || 'file'];
    if (a.pages != null) bits.push(_wuT('wu.pages', '{n} pages')
      .replace('{n}', a.pages));
    if (a.bytes) bits.push(_wuFmtBytes(a.bytes));
    return '<a class="ptool-wu-download" href="' + _wuEsc(a.href) +
      '" download>' + _wuIcon('save', 12) + '<span>' + _wuEsc(
        _wuT('wu.download', 'Download {label}')
          .replace('{label}', bits.join(' · '))) + '</span></a>';
  }).join('');

  if (unit.status === 'done' || unit.status === 'degraded') {
    const degraded = unit.status === 'degraded';
    const reason = degraded && unit.quality && unit.quality.reason
      ? '<span class="ptool-wu-line-note">' + _wuEsc(unit.quality.reason) +
        '</span>' : '';
    html += '<div class="ptool-wu-line ' +
      (degraded ? 'ptool-wu-line-warn' : 'ptool-wu-line-ok') + '">' +
      _wuIcon(degraded ? 'alertTriangle' : 'check', 13) +
      '<span>' + _wuEsc(degraded
        ? _wuT('wu.degradedLine', 'Finished with reduced quality')
        : _wuT('wu.doneLine', 'Finished')) + '</span>' + reason +
      links + '</div>';
  } else if (unit.status === 'failed') {
    const f = unit.failure || {};
    const msg = f.message ? '<span class="ptool-wu-line-note">' +
      _wuEsc(String(f.message).slice(0, 200)) + '</span>' : '';
    const hint = f.hint ? '<div class="ptool-wu-hint">' +
      _wuEsc(String(f.hint).slice(0, 300)) + '</div>' : '';
    html += '<div class="ptool-wu-line ptool-wu-line-err">' +
      _wuIcon('x', 13) +
      '<span>' + _wuEsc(_wuT('wu.failedLine', 'Job failed')) + '</span>' +
      msg + '</div>' + hint;
  } else if (unit.status === 'aborted') {
    html += '<div class="ptool-wu-line ptool-wu-line-muted">' +
      _wuIcon('ban', 13) +
      '<span>' + _wuEsc(_wuT('wu.abortedLine', 'Stopped')) + '</span></div>';
  }
  return html;
}

function _wuFmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB';
  if (v >= 1024) return Math.round(v / 1024) + ' KB';
  return v + ' B';
}

/* Ephemeral completion notice — the durable record is the card itself; the
 * toast only catches a user reading further down the transcript. */
function _wuAnnounce(unit) {
  if (typeof showToast !== 'function') return;
  const title = _wuStatusLabel(unit.status);
  const body = unit.title || '';
  try { showToast('', title, body, 2600); } catch (e) { /* cosmetic only */ }
}
