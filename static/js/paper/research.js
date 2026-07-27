/* ═════════════════════════════════════════════════
   paper/research.js — auto-research: a DIRECTION → scored ideas

   The Reading-Mode landing entry for the auto-research capability
   (harvest → survey → ideate; docs/AUTO_RESEARCH_SYSTEM_DESIGN.md).

   ── Why the landing screen and not a 7th paper tab ──
   Every `.paper-tab-panel` is PAPER-HASH scoped: `_switchPaperTab` dispatches
   `_loadOrGenerateReport` / `_initVideoTab`, and the video tab looks its job up
   with `Api.paper.videoLookup(_paperHash)`. A research DIRECTION is pre-paper —
   there is no open paper when you start one — so a tab would force the user to
   first open an unrelated paper. This module therefore reads NO paper state.

   ── Why it does not go through the LLM tool ──
   `produce_research` exists as a model tool, but it is (a) only invoked if the
   model elects to and (b) search-gated in `_build_produce`, so it vanishes when
   a user turns web search off. A UI control cannot be built on either. We call
   the capability-agnostic `POST /api/v1/tasks/start` instead.

   ── The three contracts this file owes the user ──
   1. START with no paper open.
   2. Progress that survives a refresh / tab switch — the elapsed clock is
      adopted from the SERVER's `createdAt`, never minted locally (a local
      stopwatch re-mints on every render and washes a long-running job into
      looking fresh).
   3. A degraded job must LOOK degraded. Research keeps `status='done'` when
      its structural gate wipes every idea (status is the LIFECYCLE axis);
      `artifact_quality` is the PRODUCT axis and is the only thing separating
      "good artifact" from "valid artifact out of a broken pipeline".
   ═════════════════════════════════════════════════ */

/** Live research job state — the single source the renderer paints from. */
var _researchStream = null;
var _researchPollTimer = null;

/** Phases the research recipe walks, in order (stepper vocabulary). */
var _RESEARCH_PHASES = ['harvest', 'survey', 'ideate'];

function _newResearchStream(direction) {
  return {
    direction: direction,
    taskId: null,
    status: 'pending',      // pending|running|done|error|aborted
    phase: '',              // harvest|survey|ideate
    startedAt: 0,           // SERVER createdAt (epoch ms) — never Date.now()
    lastEventAt: 0,         // SERVER updatedAt (epoch ms)
    degraded: false,
    degradedReason: '',
    gateReached: '',        // accepted|rubric|structural|none
    accepted: 0,
    rejected: 0,
    corpusSize: 0,
    folderId: '',           // library folder the harvested papers landed in
    error: '',
  };
}

/**
 * Adopt the server's clocks onto the local state.
 *
 * Min-guard (mirrors `_pmAdoptServerClocks` in the media tabs): the start
 * instant may only ever move EARLIER, and an implausible value is ignored —
 * so a re-attach after a refresh continues the real elapsed time and can never
 * jump backward.
 */
function _researchAdoptServerClocks(st, src) {
  if (!st || !src) return;
  var started = Number(src.createdAt);
  if (isFinite(started) && started > 1e12 && started <= Date.now() + 60000) {
    if (!st.startedAt || started < st.startedAt) st.startedAt = started;
  }
  var seen = Number(src.updatedAt);
  if (isFinite(seen) && seen > 1e12 && seen >= (st.lastEventAt || 0)) {
    st.lastEventAt = seen;
  }
}

/** Fold one task snapshot into the stream state. */
function _researchApplySnapshot(st, snap) {
  if (!st || !snap) return;
  _researchAdoptServerClocks(st, snap);
  if (snap.status) st.status = snap.status;

  // ★ The product-quality axis. A degraded job reports status='done' by
  // design, so reading `status` alone would render a 100% gate wipe as a
  // clean success — the exact bug artifact_quality exists to expose.
  var q = snap.artifact_quality;
  if (q && typeof q === 'object') {
    st.degraded = !!q.degraded;
    st.degradedReason = q.reason || '';
  }

  var res = snap.result;
  if (res && typeof res === 'object') {
    st.accepted = Array.isArray(res.accepted) ? res.accepted.length : 0;
    st.rejected = Array.isArray(res.rejected) ? res.rejected.length : 0;
    st.corpusSize = Number(res.corpus_size) || 0;
    st.gateReached = res.gate_reached || '';
    if (res.folder_id) st.folderId = res.folder_id;
  }
  var meta = snap.meta;
  if (meta && meta.direction && !st.direction) st.direction = meta.direction;
}

/** Entry point from the landing screen's research input. */
function _submitResearchDirection() {
  var input = document.getElementById('paperResearchInput');
  var q = input && input.value ? input.value.trim() : '';
  if (!q) { debugLog('Describe the research direction to explore', 'warning'); return; }
  _startResearchJob(q);
}

/**
 * Landing-screen bridge: start a research job from the SHARED describe box.
 *
 * The landing screen's free-text area is already direction-shaped ("describe
 * what you're looking for"), so research reuses it rather than adding a second
 * input the user has to choose between. Recommend and research are two verbs
 * over one direction — that is why neither needs an open paper.
 */
function _startResearchFromDescribe() {
  var input = document.getElementById('paperDescribeInput');
  var q = input && input.value ? input.value.trim() : '';
  if (!q) { debugLog('Describe the research direction to explore', 'warning'); return; }
  _startResearchJob(q);
}

/**
 * Start an auto-research job for `direction` and stream its progress.
 *
 * Reads NO paper state on purpose — see the file header.
 */
async function _startResearchJob(direction) {
  var s = _newResearchStream(direction);
  _researchStream = s;
  _paintResearch();

  try {
    var data = await Api.tasks.start('research', { direction: direction });
    if (!data || !data.ok || !data.taskId) {
      throw new Error((data && data.error) || 'research start failed');
    }
    if (_researchStream !== s) return;      // superseded by a newer submit
    s.taskId = data.taskId;
    s.status = 'running';

    // Live progress over the push channel the runtime already broadcasts on
    // (lib/research/runtime.py sets push_channel='research'); the poll below
    // is the reconnect/catch-up net, not the primary transport.
    try { pushSubscribe('research', s.taskId, function (ev) {
      if (_researchStream !== s) return;
      if (ev && ev.phase) s.phase = ev.phase;
      if (ev && ev.type === 'final') _pollResearchOnce(s);
      _paintResearch();
    }); } catch (e) { console.debug('[Research] push subscribe failed:', e); }

    await _pollResearchOnce(s);
    _scheduleResearchPoll(s);
  } catch (e) {
    console.error('[Research] start failed:', e);
    if (_researchStream === s) {
      s.status = 'error';
      s.error = e && e.message ? e.message : String(e);
      _paintResearch();
    }
  }
}

/** One snapshot poll. Also the re-attach path after a refresh. */
async function _pollResearchOnce(s) {
  if (!s || !s.taskId) return;
  var snap = await Api.tasks.get(s.taskId);
  if (_researchStream !== s) return;
  if (!snap || snap.ok === false) return;
  _researchApplySnapshot(s, snap);
  _paintResearch();
  // A terminal snapshot must stop the poll loop here, not merely fail to
  // schedule the NEXT tick somewhere else: a job that finishes between ticks
  // would otherwise keep a timer alive for the life of the page.
  if (!_researchIsRunning(s)) _stopResearchPoll();
}

/** True while the job can still change state. */
function _researchIsRunning(s) {
  return !!s && (s.status === 'pending' || s.status === 'running');
}

function _stopResearchPoll() {
  if (_researchPollTimer) { clearTimeout(_researchPollTimer); _researchPollTimer = null; }
}

function _scheduleResearchPoll(s) {
  _stopResearchPoll();
  if (!_researchIsRunning(s)) return;
  _researchPollTimer = setTimeout(async function () {
    if (_researchStream !== s) return;
    try { await _pollResearchOnce(s); } catch (e) {
      console.debug('[Research] poll failed:', e);
    }
    if (_researchIsRunning(s)) _scheduleResearchPoll(s);
  }, 2000);
}

/** Stop the running job (the generic abort verb). */
async function _abortResearchJob() {
  var s = _researchStream;
  if (!s || !s.taskId) return;
  try { await Api.tasks.abort(s.taskId); } catch (e) {
    console.debug('[Research] abort failed:', e);
  }
  s.status = 'aborted';
  _stopResearchPoll();
  try { pushUnsubscribe('research', s.taskId); } catch (e) {
    console.debug('[Research] push unsubscribe failed:', e);
  }
  _paintResearch();
}

/** Open the library folder the harvested corpus landed in. */
function _openResearchFolder() {
  var s = _researchStream;
  if (!s || !s.folderId) return;
  if (typeof _showPaperLanding === 'function') _showPaperLanding();
  if (typeof _renderPaperLibrary === 'function') _renderPaperLibrary();
}

/** Render the research console from `_researchStream`. */
function _paintResearch() {
  var s = _researchStream;
  if (!s) return;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  var running = (s.status === 'pending' || s.status === 'running');

  var steps = _RESEARCH_PHASES.map(function (p) {
    var done = _RESEARCH_PHASES.indexOf(p) < _RESEARCH_PHASES.indexOf(s.phase);
    var active = (p === s.phase);
    return '<div class="paper-step' + (active ? ' is-active' : '') +
           (done ? ' is-done' : '') + '">' + escapeHtml(_tt('paper.research.' + p)) +
           '</div>';
  }).join('');

  // The quality banner. Shown for a degraded job REGARDLESS of status='done'.
  var quality = '';
  if (s.degraded) {
    quality =
      '<div class="pm-quality is-degraded" role="alert">' +
        '<div class="pm-quality-title">' +
          escapeHtml(_tt('paper.research.degraded')) + '</div>' +
        '<div class="pm-quality-reason">' + escapeHtml(s.degradedReason) + '</div>' +
      '</div>';
  }

  var body;
  if (running) {
    body =
      '<div class="pm-console-head">' +
        '<div class="pm-console-title">' + escapeHtml(_tt('paper.research.running')) + '</div>' +
        '<button class="pm-console-abort" onclick="_abortResearchJob()">' +
          escapeHtml(_tt('paper.research.abort')) + '</button>' +
      '</div>' +
      '<div class="paper-stepper">' + steps + '</div>' +
      '<div class="paper-media-activity" data-research-elapsed data-started-at="' +
        String(s.startedAt || 0) + '"></div>';
  } else {
    body =
      '<div class="pm-console-head">' +
        '<div class="pm-console-title">' +
          escapeHtml(_tt('paper.research.finished')) + '</div>' +
      '</div>' + quality +
      '<div class="pm-research-tally">' +
        '<span>' + escapeHtml(String(s.accepted)) + ' accepted</span>' +
        '<span>' + escapeHtml(String(s.rejected)) + ' rejected</span>' +
        '<span>' + escapeHtml(String(s.corpusSize)) + ' papers</span>' +
      '</div>' +
      (s.folderId
        ? '<button class="paper-retry-btn" onclick="_openResearchFolder()">' +
            escapeHtml(_tt('paper.research.openFolder')) + '</button>'
        : '');
  }

  viewer.innerHTML =
    '<div class="pm-console" data-research-shell="1">' +
      '<div class="pm-studio-head">' +
        '<div class="pm-studio-head-text">' +
          '<div class="pm-studio-title">' + escapeHtml(s.direction) + '</div>' +
          '<div class="pm-studio-sub">' +
            escapeHtml(_tt('paper.research.subtitle')) + '</div>' +
        '</div>' +
      '</div>' + body +
    '</div>';
}
